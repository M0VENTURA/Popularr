"""Download completion reconciliation service.

When the Soulseek pipeline requests a download it marks the queue item
``downloading`` and stores the remote ``found_filename``.  This service is
the missing final stage: it watches for the file actually landing on disk and
transfers it into the music library.

Matching precedence (highest first):
1. slskd's completed-transfers API — each entry carries a ``localFilePath``
   that maps the remote filename to the exact on-disk location (no walk).
2. Exact filename match — ``found_filename`` (or its basename) against the
   downloads folder walk.
3. Basename-anywhere match — the queue's ``found_filename`` BASENAME is
   matched against every audio file under the downloads tree (and torrents /
   sibling roots), IGNORING the folder path entirely.  The comparison strips
   accents AND punctuation, so the Aephanemer "Utopie" loop's root cause is
   handled: the peer's remote ``Par‐delà le mur des siècles (instrumental)``
   (U+2010 hyphen + accents + parens) matches the on-disk
   ``par-dela le mur des siecles instrumental`` regardless of which folder or
   peer it came from.
4. Fuzzy match — ``_score_soulseek_candidate`` + metadata verification so
   wrong-version / false-positive files are rejected instead of imported.

After a match is found the file is verified against the queue item's expected
metadata/duration, moved to /music via ``move_track_to_library``, and the row
is promoted to the terminal ``imported`` state (removed from the active queue).

Items whose slskd transfer reached a terminal failed state, or that went stale
while marked ``downloading`` with no file present, are marked failed so the
automatic retry scheduler can re-download them instead of leaving them stuck.

Callers:
    - services/downloads/download_scan_service.check_completed_downloads
      (registered as a queue_orchestrator maintenance hook)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from sqlalchemy import text

from db.engine import db_session
from helpers.config_helpers import _SLSKD_MIN_ACCEPT_SCORE
from helpers.normalization_service import queue_duration_seconds

logger = structlog.get_logger(__name__)

_MUSIC_ROOT = os.environ.get("MUSIC_ROOT", "/music")


def _log_queue_event(event_type: str, message: str, queue_id: int | None) -> None:
    """Record a queue event to the in-memory store and ``queue.log``."""
    try:
        from services.queue.queue_diagnostics_service import log_queue_event
        log_queue_event(event_type, message, queue_id=queue_id)
    except Exception:
        pass


_STALE_DOWNLOADING_MINUTES = 60
_SLSKD_ACTIVE_STATE_TIMEOUT_MINUTES = 60
_SLSKD_REMOTELY_QUEUED_TIMEOUT_MINUTES = 60
#: A transfer that slskd reports as succeeded but whose file never appeared
#: on disk.  The file may still be flushing/renaming, so the item is kept
#: ``downloading`` and polled for this long BEFORE it is failed for retry —
#: failing immediately caused the infinite re-download loop (download a file
#: that "succeeded" but was not found → requeue → search picks a DIFFERENT
#: file → repeat).
_SLSKD_SUCCEEDED_NOT_FOUND_TIMEOUT_MINUTES = 15


def _refresh_downloading_item(queue_id: int) -> dict[str, Any] | None:
    """Re-fetch a downloading item FRESH; ``None`` when it is no longer
    ``downloading`` (a retry already requeued + re-downloaded it).

    ``check_completed_downloads`` snapshots the downloading rows at cycle
    start.  While the cycle runs, the search pipeline can requeue an item and
    download a NEW file for it (the old transfer "succeeded but was not
    found" → mark_failed → requeue → re-download).  Processing the STALE row
    then double-fails the item with the OLD ``found_filename`` — the reported
    infinite "slskd transfer succeeded but local file not found — retrying"
    loop.  Re-fetching per item keeps the completion cycle in sync.
    """
    try:
        with db_session() as session:
            row = session.execute(
                text("SELECT * FROM download_queue WHERE id = :qid AND status = 'downloading'"),
                {"qid": queue_id},
            ).fetchone()
            return dict(row._mapping) if row else None
    except Exception:
        return None


# =============================================================================
# TRANSFER KEY NORMALISATION
# =============================================================================

def _normalize_transfer_key(value: str) -> Optional[str]:
    """Normalise a transfer/filename for dict lookups (lower, forward slashes)."""
    if not value:
        return None
    norm = str(value).replace("\\", "/").strip()
    return norm.lower() or None


def _db_now_naive() -> datetime:
    """Return the DB server's current time as a *naive* datetime."""
    try:
        with db_session() as session:
            value = session.execute(text("SELECT CURRENT_TIMESTAMP")).scalar()
        if value is not None:
            if isinstance(value, str):
                try:
                    value = datetime.fromisoformat(value)
                except ValueError:
                    return value
            if getattr(value, "tzinfo", None) is not None:
                value = value.replace(tzinfo=None)
            return value
    except Exception:
        pass
    return datetime.utcnow()


def _remember_failed_peer(transfer: dict[str, Any]) -> None:
    """Tell the download pipeline to avoid this peer for the failed file."""
    try:
        from services.downloads.download_pipeline_service import _block_peer
        _block_peer(transfer.get("username"), transfer.get("filename"))
    except Exception:
        pass


def _monitored_downloads_dir() -> str:
    try:
        from services.downloads.download_scan_service import resolve_downloads_dir
        return resolve_downloads_dir(prefer_music_subfolder=False)
    except Exception:
        return "?"


_FILE_ARRIVAL_POLLS = 3
_FILE_ARRIVAL_POLL_SECONDS = 10


def _wait_for_transfer_file(found_filename: str, local_file_path: str) -> Optional[str]:
    """Poll up to ~30s for a just-completed transfer to appear on disk.

    slskd preserves the REMOTE directory structure when saving, so a remote
    ``music/Artist/Album/01 - Track.flac`` lands at
    ``<downloads>/music/Artist/Album/01 - Track.flac`` (nested), NOT flattened
    to ``<downloads>/01 - Track.flac``.  Previously only the flat basename and
    the (often empty) ``localFilePath`` were checked, so a genuinely-completed
    transfer was reported "no local file found" and re-downloaded forever.
    """
    import time as _time

    monitored = _monitored_downloads_dir()
    candidates = []
    _found = str(found_filename or "").replace("\\", "/").strip()
    base = os.path.basename(_found)
    
    if monitored and monitored != "?":
        if base:
            candidates.append(os.path.join(monitored, base))
        # The full remote-relative path under the downloads root.
        if _found and not _found.startswith("/"):
            candidates.append(os.path.join(monitored, _found))
    if local_file_path:
        candidates.append(str(local_file_path).replace("\\", "/"))
        
    try:
        from services.infrastructure.filesystem_service import (
            _original_archive_subfolder_name,
            resolve_original_archive_dir,
        )
        _archive_root = resolve_original_archive_dir()
        if _archive_root and os.path.isdir(_archive_root) and base:
            for _aroot, _adirs, _afiles in os.walk(_archive_root):
                if base in _afiles:
                    candidates.append(os.path.join(_aroot, base))
                    break
    except Exception:
        pass

    # Recursive fallback: walk the downloads root (deep) looking for the
    # basename anywhere under it — covers slskd's nested-directory saves and
    # any path-mapping drift between slskd and the app.  The match is
    # Unicode-tolerant: files with accented names ("Le cimetière marin.flac")
    # may land with different normalization (NFC/NFD) or stripped accents on
    # disk, so compare the NFC-normalised AND accent-stripped basenames, PLUS
    # the punctuation-stripped key ("Par‐delà le mur des siècles" vs
    # "par-dela le mur des siecles") so hyphen/paren drift never blocks it.
    import unicodedata as _ud

    def _norm_key(name: str) -> str:
        n = _ud.normalize("NFC", name)
        stripped = _ud.normalize("NFKD", n)
        stripped = "".join(c for c in stripped if not _ud.combining(c))
        return stripped.lower()

    _base_key = _norm_key(base)
    _base_punct = _punctuation_stripped_key(base)
    search_roots: set[str] = set()
    if monitored and os.path.isdir(monitored) and monitored != "?":
        search_roots.add(monitored)
        # slskd may save under a sibling ``torrents`` root or the configured
        # torrents subfolder when its complete-directory differs from the
        # app's downloads dir — search those too.
        try:
            from services.downloads.download_scan_service import resolve_torrents_dir
            search_roots.add(resolve_torrents_dir())
        except Exception:
            pass
        _parent = os.path.dirname(os.path.normpath(monitored))
        for _cand in ("torrents", "Torrents", "TORRENTS"):
            _sibling = os.path.join(_parent, _cand)
            if os.path.isdir(_sibling):
                search_roots.add(_sibling)

    if search_roots and base:
        # Deep-enough to reach ``music/Artist/Album/file.flac`` (depth 4 from
        # the downloads root) and ``music/music/...`` double-nested layouts
        # without scanning the whole library.
        _DEPTH_LIMIT = 8
        for _root_dir in search_roots:
            try:
                _depth_limit = _DEPTH_LIMIT
                for _root, _dirs, _files in os.walk(_root_dir):
                    _rel_depth = _root.count(os.sep) - _root_dir.count(os.sep)
                    if _rel_depth >= _depth_limit:
                        _dirs[:] = []
                        continue
                    # Prune hidden/dunder dirs and the FLAC conversion archive.
                    _dirs[:] = [
                        d for d in _dirs
                        if not d.startswith(".") and not d.startswith("__")
                        and d.lower() != "original"
                    ]
                    for _fname in _files:
                        if (
                            _norm_key(_fname) == _base_key
                            or (_base_punct and _punctuation_stripped_key(_fname) == _base_punct)
                        ):
                            candidates.append(os.path.join(_root, _fname))
                            break
            except Exception:
                pass

    if not candidates:
        return None

    for _ in range(_FILE_ARRIVAL_POLLS):
        for path in candidates:
            try:
                if path and os.path.isfile(path):
                    return path
            except Exception:
                pass
        _time.sleep(_FILE_ARRIVAL_POLL_SECONDS)
    return None


def _accent_stripped_key(name: str) -> str:
    """Lowercased, accent-stripped key for tolerant filename matching.

    Accented filenames ("Le cimetière marin.flac") can land with different
    Unicode normalization (NFC vs NFD) or with accents stripped ("Le cimetiere
    marin.flac") depending on the peer/OS.  Comparing this key tolerates all
    three.
    """
    try:
        import unicodedata as _ud
        nfc = _ud.normalize("NFC", str(name or ""))
        stripped = "".join(c for c in _ud.normalize("NFKD", nfc) if not _ud.combining(c))
        return stripped.lower().strip()
    except Exception:
        return str(name or "").lower().strip()


def _punctuation_stripped_key(name: str) -> str:
    """Lowercased, accent- AND punctuation-stripped key for basename matching.

    The Aephanemer "Utopie" loop: the queue's ``found_filename`` uses the
    PEER's remote path (``Par‐delà le mur des siècles`` — U+2010 hyphen +
    accents), while the file lands on disk with a DIFFERENT peer's spelling
    (``Par-dela le mur des siecles`` — ASCII hyphen, no accents, no parens).
    Exact / lowercase / accent-stripped keys all fail because the *hyphen
    characters and parentheses differ*.

    This key strips EVERYTHING that is not a letter or digit, so
    ``Par‐delà le mur des siècles (instrumental)`` and
    ``par-dela le mur des siecles instrumental`` collapse to the same
    ``pardelalemurdessieclesinstrumental`` and the file is found no matter
    which folder or peer it came from.
    """
    try:
        import unicodedata as _ud
        nfc = _ud.normalize("NFC", str(name or ""))
        stripped = "".join(c for c in _ud.normalize("NFKD", nfc) if not _ud.combining(c))
        return "".join(c for c in stripped.lower() if c.isalnum())
    except Exception:
        return "".join(c for c in str(name or "").lower() if c.isalnum())


def _is_stale_queue_item(
    item: dict[str, Any],
    stale_minutes: int = 10,
    now: datetime | None = None,
) -> bool:
    updated_at = item.get("updated_at")
    if not updated_at:
        return False
    try:
        updated_text = str(updated_at).replace("Z", "+00:00")
        updated_dt = datetime.fromisoformat(updated_text)
        if updated_dt.tzinfo is not None:
            updated_dt = updated_dt.astimezone(timezone.utc).replace(tzinfo=None)
        if now is None:
            now = _db_now_naive()
        return (now - updated_dt).total_seconds() >= (stale_minutes * 60)
    except Exception:
        return False


# =============================================================================
# FILE MATCHING
# =============================================================================

def _build_download_index(fs_files: list[dict[str, Any]]) -> dict[str, Any]:
    by_rel_norm: dict[str, dict[str, Any]] = {}
    by_basename: dict[str, list[dict[str, Any]]] = {}
    by_punct: dict[str, list[dict[str, Any]]] = {}
    by_token: dict[str, list[dict[str, Any]]] = {}

    try:
        from services.queue.queue_scoring import _tokenize_meaningful
    except Exception:
        _tokenize_meaningful = None

    for f in fs_files:
        rel = f["rel_path"].replace("\\", "/")
        norm = _normalize_transfer_key(rel)
        if norm:
            by_rel_norm.setdefault(norm, f)
        base = os.path.basename(rel)
        by_basename.setdefault(base.lower(), []).append(f)
        # Accent-stripped basename key so accented files ("Le cimetière
        # marin.flac") are found even when the queue's found_filename uses a
        # different accent normalization (NFC/NFD) or stripped accents.
        _stripped = _accent_stripped_key(base)
        if _stripped and _stripped != base.lower():
            by_basename.setdefault(_stripped, []).append(f)
        # Punctuation-stripped key ("Par‐delà le mur des siècles (instrumental)"
        # == "par-dela le mur des siecles instrumental") — the Aephanemer
        # loop's real cause: peers spell the SAME track differently, so a
        # basename-only match must ignore hyphens/parens/accents entirely.
        _punct = _punctuation_stripped_key(base)
        if _punct:
            by_punct.setdefault(_punct, []).append(f)
        if _tokenize_meaningful is not None:
            for token in set(_tokenize_meaningful(base)):
                by_token.setdefault(token, []).append(f)

    return {
        "by_rel_norm": by_rel_norm,
        "by_basename": by_basename,
        "by_punct": by_punct,
        "by_token": by_token,
    }


def _fuzzy_candidates(
    fs_index: dict[str, Any],
    item: dict[str, Any],
    fs_files: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    try:
        from helpers.normalization_service import normalize_artist
        from services.queue.queue_scoring import (
            _tokenize_meaningful,
            normalize_core_title,
        )
    except Exception:
        return []

    tokens: set[str] = set(_tokenize_meaningful(normalize_core_title(item.get("title") or "")))
    tokens |= set(_tokenize_meaningful(normalize_artist(item.get("artist") or "") or ""))

    if not tokens:
        return []

    token_index = fs_index.get("by_token") or {}
    seen: dict[str, dict[str, Any]] = {}
    for token in tokens:
        for f in token_index.get(token, []):
            seen.setdefault(f["full_path"], f)

    # Punctuation-stripped basename fallback: a file whose basename collapses
    # to the SAME key as the queue title ("Par‐delà le mur des siècles
    # (instrumental)" vs on-disk "par-dela le mur des siecles instrumental")
    # is a basename-anywhere candidate — the folder path is ignored entirely.
    punct_index = fs_index.get("by_punct") or {}
    title_key = _punctuation_stripped_key(normalize_core_title(item.get("title") or ""))
    if title_key:
        for f in punct_index.get(title_key, []):
            seen.setdefault(f["full_path"], f)

    if seen:
        return list(seen.values())
    return []


def _file_matches_queue_item(
    file_path: str,
    queue_item: dict[str, Any],
    relative_name: str | None = None,
) -> tuple[bool, str]:
    from services.downloads.match_engine import filename_matches_queue_item
    from services.queue.queue_metadata_matcher import _metadata_matches_queue_item

    meta_state = None
    try:
        meta_state = _metadata_matches_queue_item(file_path, queue_item)
    except Exception as exc:
        logger.debug("Metadata check failed", file_path=file_path, error=str(exc))

    if meta_state is True:
        return True, "metadata"
    if meta_state is False:
        return False, "metadata"

    try:
        is_match = filename_matches_queue_item(file_path, queue_item)
    except Exception as exc:
        logger.debug("Filename match failed", file_path=file_path, error=str(exc))
        is_match = False
    return bool(is_match), "filename"


def _matching_file_exists_unconfirmed(
    item: dict[str, Any],
    fs_files: list[dict[str, Any]],
    downloads_dir: str,
) -> Optional[str]:
    try:
        from helpers.normalization_service import normalize_artist
        from services.queue.queue_scoring import (
            _tokenize_meaningful,
            normalize_core_title,
        )
    except Exception:
        return None

    item_title = normalize_core_title(item.get("title") or "")
    item_artist = normalize_artist(item.get("artist") or "")
    if not item_title:
        return None
        
    title_tokens = set(_tokenize_meaningful(item_title))
    if not title_tokens:
        return None

    for f in (fs_files or []):
        rel = str(f.get("rel_path") or "").replace("\\", "/")
        base = os.path.basename(rel).lower()
        hit = sum(1 for tok in title_tokens if tok in base)
        if title_tokens and hit >= max(1, (len(title_tokens) + 1) // 2):
            return rel
    return None


def _file_artist_matches_queue_item(file_path: str, queue_item: dict[str, Any]) -> Optional[bool]:
    try:
        from helpers.config_helpers import _GENERIC_COMPILATION_ARTISTS
        from helpers.metadata_reader import read_mp3_metadata
        from helpers.normalization_service import normalize_artist
    except Exception:
        return None

    try:
        meta = read_mp3_metadata(file_path) or {}
    except Exception:
        return None

    file_artist = str(meta.get("artist") or "").strip()
    file_album_artist = str(meta.get("album_artist") or "").strip()
    file_artist_norm = normalize_artist(file_artist)
    file_album_artist_norm = normalize_artist(file_album_artist)

    queue_artist = str(queue_item.get("artist") or "").strip()
    queue_album_artist = str(queue_item.get("album_artist") or "").strip()
    queue_artist_norm = normalize_artist(queue_artist)
    queue_album_artist_norm = normalize_artist(queue_album_artist)

    if not file_artist_norm and not file_album_artist_norm:
        return None

    file_artists = [a for a in (file_artist_norm, file_album_artist_norm) if a]
    if file_artists and all(a in _GENERIC_COMPILATION_ARTISTS for a in file_artists):
        return None

    queue_artists = [a for a in (queue_artist_norm, queue_album_artist_norm) if a]
    if not queue_artists:
        return None

    for fa in file_artists:
        for qa in queue_artists:
            if fa == qa or (fa and qa and (fa in qa or qa in fa)):
                return True

    return False


def _claim_file(file_path: str, claimed_files: set[str], downloads_dir: str) -> None:
    try:
        rel = (
            os.path.relpath(file_path, downloads_dir)
            if downloads_dir and os.path.isabs(file_path)
            else file_path
        )
        norm = _normalize_transfer_key(rel)
        if norm:
            claimed_files.add(norm)
            claimed_files.add(os.path.basename(norm))
    except Exception:
        pass


def _is_file_claimed(file_path: str, claimed_files: set[str], downloads_dir: str) -> bool:
    if not claimed_files:
        return False
    try:
        rel = (
            os.path.relpath(file_path, downloads_dir)
            if downloads_dir and os.path.isabs(file_path)
            else file_path
        )
        norm = _normalize_transfer_key(rel)
        return bool(norm and (norm in claimed_files or os.path.basename(norm) in claimed_files))
    except Exception:
        return False


def _queue_row_references_file(local_path: str, downloads_dir: str) -> bool:
    try:
        variants: set[str] = set()
        for p in (local_path, os.path.normpath(local_path), os.path.realpath(local_path)):
            variants.add(str(p))
            variants.add(str(p).replace("\\", "/"))
            norm = _normalize_transfer_key(str(p))
            if norm:
                variants.add(norm)
                
        base = os.path.basename(str(local_path))
        if downloads_dir:
            try:
                rel = os.path.relpath(str(local_path), downloads_dir)
                variants.add(rel)
                variants.add(rel.replace("\\", "/"))
                rel_norm = _normalize_transfer_key(rel)
                if rel_norm:
                    variants.add(rel_norm)
            except Exception:
                pass
                
        variants = {v for v in variants if v}
        if not variants:
            return False
            
        with db_session() as session:
            for v in variants:
                if session.execute(
                    text("SELECT 1 FROM download_queue WHERE file_path = :v OR found_filename = :v LIMIT 1"),
                    {"v": v},
                ).fetchone():
                    return True
            if session.execute(
                text("SELECT 1 FROM download_queue WHERE LOWER(found_filename) = :b OR LOWER(file_path) = :b LIMIT 1"),
                {"b": base.lower()},
            ).fetchone():
                return True
    except Exception:
        return True
    return False


def _delete_mismatched_download(file_path: str, queue_id: int, reason: str) -> None:
    try:
        if os.path.isfile(file_path):
            os.remove(file_path)
            logger.warning("Deleted mismatched download", queue_id=queue_id, path=file_path, reason=reason)
    except Exception as exc:
        logger.warning("Could not delete mismatched file", queue_id=queue_id, path=file_path, error=str(exc))


def _block_peer_for_queue_item(queue_id: int, found_filename: str | None = None) -> None:
    """Block the Soulseek peer that provided the wrong file for a queue item.

    When a downloaded file fails the completion match, the retry would find
    the SAME wrong result on Soulseek and download it again — the pipeline's
    in-memory peer blocklist is only populated on search/download failures in
    ``download_pipeline_service``, never on post-download mismatch.  Look up
    the offending transfer (by queue id or remote filename) and block that
    peer+filename so the next search drops it.
    """
    try:
        from api_clients.slskd_http import get_slskd_client
        from services.downloads.slskd_service import SlskdService

        client = get_slskd_client()
        if client is None:
            return
        slskd = SlskdService(http_client=client)

        _remote = str(found_filename or "").replace("\\", "/").strip()
        _remote_norm = _normalize_transfer_key(_remote) if _remote else None
        _remote_base = os.path.basename(_remote_norm or "") if _remote_norm else None

        for transfer in slskd.get_completed_transfers():
            username = str(transfer.get("username") or "")
            remote = str(transfer.get("filename") or "").replace("\\", "/").strip()
            remote_norm = _normalize_transfer_key(remote) if remote else None
            remote_base = os.path.basename(remote_norm or "") if remote_norm else None
            if not username:
                continue
            # Match the transfer to this queue item: same remote filename
            # (or basename), or same local file path.
            matches = (
                (_remote_norm and remote_norm == _remote_norm)
                or (_remote_base and remote_base == _remote_base)
                or (_remote_base and remote_norm and remote_norm.endswith(f"/{_remote_base}"))
            )
            if not matches:
                continue
            try:
                from services.downloads.download_pipeline_service import _block_peer
                _block_peer(username, remote)
                logger.warning("Blocked peer after mismatched download", queue_id=queue_id, username=username, filename=remote[:120])
            except Exception as _be:
                logger.debug("Peer block skipped", queue_id=queue_id, error=str(_be))
            return
    except Exception as exc:
        logger.debug("Peer-block lookup failed", queue_id=queue_id, error=str(exc))


def _extract_duration_seconds(file_path: str) -> Optional[int]:
    try:
        from helpers.metadata_reader import read_mp3_metadata
        meta = read_mp3_metadata(file_path) or {}
        dur = meta.get("duration_ms")
        if not dur:
            return None
        seconds = queue_duration_seconds(dur)
        return int(seconds) if seconds else None
    except Exception:
        return None


# =============================================================================
# MOVE / IMPORT
# =============================================================================

#: Album/RELEASE-scoped fields that an album-page lookup applies to every track.
#: The download-import path must apply the same set so a downloaded album
#: matches the MusicBrainz release it was queued against.
_ALBUM_LEVEL_COLUMNS: tuple[str, ...] = (
    "musicbrainz_albumtype",
    "musicbrainz_albumstatus",
    "musicbrainz_albumartistid",
    "musicbrainz_releasegroupid",
    "releasecountry",
    "originalyear",
    "releasedate",
    "recordlabel",
    "catalognumber",
    "barcode",
    "media",
)


def _album_level_mb_fields(release: dict[str, Any]) -> dict[str, Any]:
    """Map a flattened MusicBrainz release to tracks-column names.

    These are the ALBUM-scoped fields ``routes/ui_routes.py`` writes to every
    track when a release is matched from the album page.  A downloaded album
    must end up with the same set, or it will not match the release it was
    queued against.

    ``year``/``release_year`` are deliberately NOT included: the import path
    owns those through the queue row, and the tag writer has dedicated
    DATE-vs-ORIGINALYEAR handling for them.
    """
    out: dict[str, Any] = {}
    mapping = {
        "musicbrainz_albumtype": release.get("album_type"),
        "musicbrainz_albumstatus": release.get("status"),
        "musicbrainz_albumartistid": release.get("album_artist_mbid"),
        "musicbrainz_releasegroupid": release.get("release_group_mbid"),
        "releasecountry": release.get("releasecountry"),
        "originalyear": release.get("original_year"),
        "releasedate": release.get("releasedate"),
        "recordlabel": release.get("recordlabel"),
        "catalognumber": release.get("catalognumber"),
        "barcode": release.get("barcode"),
        "media": release.get("media"),
    }
    for column, value in mapping.items():
        if value is not None and str(value).strip() != "":
            out[column] = value
    return out


def _resolve_album_level_metadata(item: dict[str, Any]) -> dict[str, Any]:
    """Resolve album-level MusicBrainz metadata for a completing download.

    Two sources, in order:

    1. What was STORED when the release was queued — the queue row's own
       columns, then the ``metadata`` JSON that ``add_release_tracks_to_queue``
       writes (this is the "persist at queue time" half).
    2. A REFRESH from MusicBrainz for anything still missing (the "fetch on
       import" half), using the release MBID the row was queued against.

    A failed fetch is not an error: the stored values are still applied, and a
    release the user never matched simply resolves to no album fields.
    """
    resolved: dict[str, Any] = {}

    # ── 1. stored: queue-row columns ─────────────────────────────────────
    for column in _ALBUM_LEVEL_COLUMNS:
        value = item.get(column)
        if value is not None and str(value).strip() != "":
            resolved[column] = value

    # ── 1b. stored: the queue row's metadata JSON ────────────────────────
    stored = item.get("metadata")
    if isinstance(stored, str):
        try:
            stored = json.loads(stored or "{}") or {}
        except Exception:
            stored = {}
    if isinstance(stored, dict):
        album_payload = stored.get("album_metadata")
        if isinstance(album_payload, dict):
            for column, value in album_payload.items():
                if (
                    column not in resolved
                    and value is not None
                    and str(value).strip() != ""
                ):
                    resolved[column] = value

    # ── 2. refresh from MusicBrainz for anything still missing ───────────
    release_mbid = str(
        item.get("release_mbid") or item.get("release_id") or ""
    ).strip()
    missing = [c for c in _ALBUM_LEVEL_COLUMNS if not resolved.get(c)]
    if release_mbid and missing:
        try:
            from services.enrichment.musicbrainz_service import (
                fetch_musicbrainz_release_metadata,
            )

            release = fetch_musicbrainz_release_metadata(release_mbid)
            if isinstance(release, dict):
                for column, value in _album_level_mb_fields(release).items():
                    if column not in resolved and str(value).strip() != "":
                        resolved[column] = value
        except Exception as exc:
            logger.debug(
                "Album-level MB refresh failed — using stored values only",
                release_mbid=release_mbid,
                error=str(exc),
            )

    return resolved


def _apply_stored_metadata(item: dict[str, Any], file_path: str) -> None:
    meta: dict[str, Any] = {
        "title": item.get("title"),
        "artist": item.get("artist"),
        "album": item.get("album"),
        "album_artist": item.get("album_artist") or item.get("artist"),
        "year": item.get("year"),
        "track_number": item.get("track_number"),
    }
    _disc_raw = item.get("disc_number")
    try:
        _disc_num = int(str(_disc_raw).split("/")[0])
    except (TypeError, ValueError):
        _disc_num = 0
        
    meta["disc_number"] = str(_disc_raw) if _disc_num >= 2 else ""
    meta = {k: v for k, v in meta.items() if v not in (None, "")}
    if _disc_num < 2:
        meta["disc_number"] = ""
        
    recording_mbid = item.get("recording_mbid")
    if recording_mbid:
        meta["recording_mbid"] = recording_mbid
    release_mbid = item.get("release_mbid") or item.get("release_id")
    if release_mbid:
        meta["release_mbid"] = release_mbid
    if item.get("musicbrainz_artistid"):
        meta["musicbrainz_artistid"] = item.get("musicbrainz_artistid")
    if item.get("musicbrainz_albumartistid"):
        meta["musicbrainz_albumartistid"] = item.get("musicbrainz_albumartistid")
    if item.get("musicbrainz_releasegroupid"):
        meta["musicbrainz_releasegroupid"] = item.get("musicbrainz_releasegroupid")

    # ── Album-level MusicBrainz metadata ─────────────────────────────────
    # Resolved from what was stored at queue time and, for anything still
    # missing, refreshed from MusicBrainz via the release MBID.  Applied to
    # BOTH the file tags and the tracks row so an imported album carries the
    # same release metadata an album-page lookup would have written.
    _album_level = _resolve_album_level_metadata(item)
    for _col, _val in _album_level.items():
        meta.setdefault(_col, _val)

    # ── Stored MusicBrainz enrichment (writer / cover / genres) ──────────
    # ``add_release_tracks_to_queue`` persisted the per-recording work-rels
    # (writer + cover attribution) and MB genres in the queue row's
    # ``metadata`` JSONB.  Apply them to the file tags so the imported track
    # carries them, AND to the tracks table so the DB row immediately has the
    # enrichment (the reported bug: imported tracks lost the MB metadata they
    # were matched with because the tracks-table write only happened during a
    # later Navidrome scan, and the extractor never read cover/genres back).
    _stored: dict[str, Any] = {}
    try:
        _stored = item.get("metadata") or {}
        if isinstance(_stored, str):
            try:
                _stored = json.loads(_stored) or {}
            except Exception:
                _stored = {}
        if isinstance(_stored, dict):
            if _stored.get("writer"):
                meta["writer"] = str(_stored["writer"])
            if _stored.get("is_cover"):
                meta["is_cover"] = 1
                if _stored.get("original_cover_artist"):
                    meta["original_cover_artist"] = str(_stored["original_cover_artist"])
            if _stored.get("musicbrainz_genres"):
                meta["musicbrainz_genres"] = str(_stored["musicbrainz_genres"])
            if _stored.get("work_mbid"):
                meta["musicbrainz_workid"] = str(_stored["work_mbid"])
    except Exception as _stored_exc:
        logger.debug("Stored MB metadata parse failed", queue_id=item.get("id"), error=str(_stored_exc))

    if not meta:
        return

    try:
        from services.metadata.tag_file_service import update_file_metadata
        update_file_metadata(file_path, meta)
    except Exception as exc:
        logger.debug("Could not apply stored metadata to file", queue_id=item.get("id"), path=file_path, error=str(exc))

    # ── Persist the MB enrichment to the tracks table ────────────────────
    # The just-moved file may not be re-scanned immediately; write the stored
    # MB metadata straight to the matching tracks row so the library shows it
    # (writer / cover / genres / work MBID / album MBID) without waiting for
    # a Navidrome import.
    try:
        _db_payload: dict[str, Any] = {}
        _mb_cols = {
            "writer", "is_cover", "original_cover_artist", "musicbrainz_genres",
            "musicbrainz_workid", "recording_mbid", "release_mbid",
            "musicbrainz_artistid", "musicbrainz_albumartistid",
            "musicbrainz_releasegroupid",
            # Album-level fields — see ``_ALBUM_LEVEL_COLUMNS``.  Without these
            # the row kept the track-level enrichment but not the release
            # identity, so the library showed an album that did not match the
            # MBID release it was downloaded for.
            "musicbrainz_albumtype", "musicbrainz_albumstatus",
            "releasecountry", "originalyear", "releasedate",
            "recordlabel", "catalognumber", "barcode", "media",
            # ``release_mbid`` is a download_queue column, not a tracks column;
            # the tracks-table equivalent is ``musicbrainz_album_mbid``.
            "musicbrainz_album_mbid",
        }
        for _col in _mb_cols:
            if meta.get(_col) not in (None, ""):
                _db_payload[_col] = meta[_col]

        if release_mbid and not _db_payload.get("musicbrainz_album_mbid"):
            _db_payload["musicbrainz_album_mbid"] = release_mbid

        if recording_mbid or release_mbid:
            # Find the track row by MBID first (most reliable), then by the
            # just-moved file path.
            _track_id = _resolve_track_id_for_import(
                file_path=file_path,
                recording_mbid=recording_mbid,
                release_mbid=release_mbid,
                artist=meta.get("artist"),
                title=meta.get("title"),
            )
            if _track_id:
                _db_payload["id"] = _track_id
                from db.repositories.tracks import insert_or_update_track
                insert_or_update_track(_track_id, _db_payload)
                logger.info(
                    "Applied stored MusicBrainz metadata to track row",
                    queue_id=item.get("id"),
                    track_id=_track_id,
                    fields=sorted(_db_payload),
                )
    except Exception as exc:
        logger.debug("Could not persist MB metadata to tracks table", queue_id=item.get("id"), error=str(exc))


def _resolve_track_id_for_import(
    *,
    file_path: str,
    recording_mbid: str | None,
    release_mbid: str | None,
    artist: str | None,
    title: str | None,
) -> str | None:
    """Find the tracks-table id for a just-imported file.

    Resolution order:
    1. Exact file_path match (the file was moved into the library).
    2. recording_mbid match.
    3. (artist, title) match.
    Returns None when no row exists yet (the next Navidrome scan creates it).
    """
    try:
        with db_session() as session:
            if file_path:
                row = session.execute(
                    text("SELECT id FROM tracks WHERE file_path = :p LIMIT 1"),
                    {"p": file_path},
                ).fetchone()
                if row:
                    return str(row[0])
            if recording_mbid:
                row = session.execute(
                    text("SELECT id FROM tracks WHERE recording_mbid = :m LIMIT 1"),
                    {"m": recording_mbid},
                ).fetchone()
                if row:
                    return str(row[0])
            if artist and title:
                row = session.execute(
                    text("""
                        SELECT id FROM tracks
                        WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:a)
                          AND LOWER(title) = LOWER(:t)
                        LIMIT 1
                    """),
                    {"a": artist, "t": title},
                ).fetchone()
                if row:
                    return str(row[0])
    except Exception:
        pass
    return None


def _move_and_import(item: dict[str, Any], abs_path: str, match_source: str) -> dict[str, Any]:
    queue_id = item.get("id")

    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE download_queue
                    SET status = 'moving', updated_at = CURRENT_TIMESTAMP
                    WHERE id = :qid AND status = 'downloading'
                """),
                {"qid": queue_id},
            )
            if result.rowcount == 0:
                logger.info("Move already claimed elsewhere — skipping", queue_id=queue_id)
                return {"success": False, "error": "already_claimed"}
    except Exception as exc:
        logger.error("Could not claim move", queue_id=queue_id, error=str(exc))
        return {"success": False, "error": str(exc)}

    def _reset_to_downloading(reason: str) -> None:
        try:
            from db.repositories.queue import update_queue_item
            update_queue_item(queue_id, status="downloading")
        except Exception:
            pass
        logger.warning("Reset item to downloading", queue_id=queue_id, reason=reason)

    try:
        from db.repositories.queue import update_queue_item
        from services.downloads.download_organize_helpers import move_track_to_library
        from services.downloads.download_verification_service import (
            mark_queue_item_moved,
            verify_file_in_music,
        )

        if not abs_path or not os.path.isfile(abs_path) or os.path.getsize(abs_path) == 0:
            logger.warning("Source file missing before copy — resetting to downloading", queue_id=queue_id, path=abs_path)
            _reset_to_downloading(f"source file missing before copy: {abs_path}")
            return {"success": False, "error": "source_file_missing"}

        _apply_stored_metadata(item, abs_path)

        track = {
            "file_path": abs_path,
            "artist": item.get("artist"),
            "title": item.get("title"),
            "track_number": item.get("track_number"),
            "disc_number": item.get("disc_number"),
        }
        release_metadata = {
            "album_artist": item.get("album_artist") or item.get("artist"),
            "album": item.get("album"),
            "year": item.get("year"),
        }

        move_result = move_track_to_library(track, release_metadata, _MUSIC_ROOT)
        if not move_result.get("success"):
            logger.warning("Move failed — resetting to downloading", queue_id=queue_id, error=move_result.get("error"))
            _reset_to_downloading(f"move failed: {move_result.get('error')}")
            return move_result

        target_path = move_result.get("target_path")
        if not target_path:
            _reset_to_downloading("move returned no target path")
            return {"success": False, "error": "missing target path"}

        if os.path.isfile(target_path):
            _apply_stored_metadata(item, target_path)

        if not item.get("duration"):
            duration = _extract_duration_seconds(target_path) or _extract_duration_seconds(abs_path)
            if duration:
                try:
                    update_queue_item(queue_id, duration=duration)
                except Exception:
                    pass

        verify_result = verify_file_in_music(queue_id, target_path)
        file_exists = (
            bool(target_path)
            and os.path.isfile(target_path)
            and os.path.getsize(target_path) > 0
        )

        if verify_result.get("success") or file_exists:
            # Sweep the target folder for duplicates of this track (e.g.
            # repeated downloads leaving "02 - Lay Your Head to Rest.flac"
            # plus "02 - Lay Your Head to Rest_12345.flac").  Keeps the
            # just-moved file (highest quality wins otherwise).
            try:
                from services.downloads.download_organize_helpers import dedupe_library_folder
                dedupe_library_folder(os.path.dirname(target_path), keep_path=target_path)
            except Exception as exc:
                logger.debug("Library folder dedup sweep failed", folder=os.path.dirname(target_path), error=str(exc))

            try:
                mark_queue_item_moved(queue_id, target_path)
                update_queue_item(
                    queue_id,
                    status="imported",
                    music_file_path=target_path,
                    file_path=target_path,
                    found_filename=os.path.basename(target_path),
                    copied_individually=1,
                    copied_individually_at=datetime.now().isoformat(),
                )
            except Exception as exc:
                logger.error("Status update failed — resetting to downloading", queue_id=queue_id, error=str(exc))
                _reset_to_downloading(f"status update failed: {exc}")
                return {"success": False, "error": str(exc)}
                
            logger.info("Verified and imported track to library", queue_id=queue_id, target=target_path, source=match_source)
            return {"success": True, "target_path": target_path}

        logger.error("Verification failed for moved file", queue_id=queue_id, error=verify_result.get("error"))
        try:
            from db.repositories.queue import mark_failed
            mark_failed(queue_id, f"Move verification failed: {verify_result.get('error')}")
        except Exception:
            pass
        return {"success": False, "error": verify_result.get("error")}

    except Exception as exc:
        logger.error("Unhandled error during move — resetting to downloading", queue_id=queue_id, error=str(exc), exc_info=True)
        _reset_to_downloading(f"unhandled error: {exc}")
        return {"success": False, "error": str(exc)}


def _sync_download_progress(active: list[dict[str, Any]], downloading: list[dict[str, Any]]) -> None:
    if not active or not downloading:
        return

    transfer_by_key: dict[str, dict[str, Any]] = {}
    for t in active:
        filename = _normalize_transfer_key(t.get("filename") or "")
        if filename:
            transfer_by_key[filename] = t
            transfer_by_key[os.path.basename(filename)] = t

    for item in downloading:
        found_fn = (item.get("found_filename") or "").strip()
        found_norm = _normalize_transfer_key(found_fn)
        if not found_norm:
            continue
        transfer = (
            transfer_by_key.get(found_norm)
            or transfer_by_key.get(os.path.basename(found_norm))
        )
        if not transfer:
            continue
        try:
            from db.repositories.queue import update_queue_item
            update_queue_item(
                item.get("id"),
                progress=transfer.get("progress"),
                speed=transfer.get("averageSpeed"),
            )
        except Exception:
            pass


# =============================================================================
# TRANSFER STATE RECONCILIATION
# =============================================================================

def _reconcile_stale_moving(stale_minutes: int = 10) -> dict[str, int]:
    stats = {"imported": 0, "reset": 0, "skipped": 0}

    try:
        from db.repositories.queue import update_queue_item
        from services.downloads.download_organize_helpers import _build_target_path
    except Exception:
        return stats

    try:
        with db_session() as session:
            rows = session.execute(text("""
                SELECT * FROM download_queue
                WHERE status = 'moving'
                  AND updated_at < CURRENT_TIMESTAMP - make_interval(mins => :stale_minutes)
            """), {"stale_minutes": stale_minutes}).fetchall() or []
            items = [dict(r._mapping) for r in rows]
    except Exception as exc:
        logger.error("Could not fetch stale 'moving' items", error=str(exc))
        return stats

    for item in items:
        queue_id = item.get("id")
        try:
            target = item.get("music_file_path")
            if not (target and os.path.isfile(str(target))):
                try:
                    target = _build_target_path(
                        _MUSIC_ROOT,
                        item.get("album_artist") or item.get("artist"),
                        item.get("year"),
                        item.get("album"),
                        item.get("artist"),
                        item.get("title"),
                        item.get("track_number"),
                        item.get("file_path") or "track.mp3",
                        disc_number=item.get("disc_number"),
                    )
                except Exception:
                    target = None

            if not (target and os.path.isfile(str(target))) and target:
                try:
                    mp3_variant = f"{os.path.splitext(str(target))[0]}.mp3"
                    if os.path.isfile(mp3_variant):
                        target = mp3_variant
                except Exception:
                    pass

            if target and os.path.isfile(str(target)):
                update_queue_item(
                    queue_id,
                    status="imported",
                    music_file_path=str(target),
                    file_path=str(target),
                    found_filename=os.path.basename(str(target)),
                    copied_individually=1,
                    copied_individually_at=datetime.now().isoformat(),
                )
                stats["imported"] += 1
                logger.info("Recovered stale 'moving' item — file found in library", queue_id=queue_id, target=target)
            else:
                update_queue_item(queue_id, status="downloading")
                stats["reset"] += 1
                logger.info("Recovered stale 'moving' item — reset to downloading", queue_id=queue_id)
        except Exception as exc:
            logger.warning("Stale 'moving' recovery failed", queue_id=queue_id, error=str(exc))
            stats["skipped"] += 1

    return stats


def _reconcile_transfer_state(
    item: dict[str, Any],
    slskd: Any,
    active: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> bool:
    queue_id = item.get("id")
    found_fn = (item.get("found_filename") or "").strip()

    if active is None:
        try:
            active = slskd.get_active_downloads()
        except Exception as exc:
            logger.debug("Could not fetch active transfers", error=str(exc))
            active = []

    if not active:
        if _is_stale_queue_item(item, stale_minutes=10, now=now):
            logger.warning("Transfer missing from slskd API while downloading and stale", queue_id=queue_id)
            from db.repositories.queue import mark_failed
            mark_failed(queue_id, "Transfer missing from slskd API while marked downloading")
            _log_queue_event("failed", "Missing from slskd transfers and stale — scheduling retry", queue_id)
            return True
        return False

    transfer = None
    found_norm = _normalize_transfer_key(found_fn)
    for t in active:
        filename = _normalize_transfer_key(t.get("filename") or "")
        if found_norm and filename and (
            filename == found_norm or os.path.basename(filename) == os.path.basename(found_norm)
        ):
            transfer = t
            break

    if transfer is None:
        if _is_stale_queue_item(item, stale_minutes=10, now=now):
            logger.warning("Transfer not found and item stale", queue_id=queue_id)
            from db.repositories.queue import mark_failed
            mark_failed(queue_id, "Transfer missing from slskd API while marked downloading")
            _log_queue_event("failed", "Transfer not found and item stale — scheduling retry", queue_id)
            return True
        return False

    state = slskd.state_text(transfer.get("state"))
    failed_states = slskd.FAILED_STATES

    if state in failed_states:
        logger.warning("slskd reports failed state", queue_id=queue_id, state=state)
        _remember_failed_peer(transfer)
        from db.repositories.queue import mark_failed
        mark_failed(queue_id, f"slskd transfer failed: {state}")
        _log_queue_event("failed", f"slskd transfer failed: {state} — scheduling retry", queue_id)
        return True

    if slskd.is_success_state(state):
        local = str(transfer.get("localFilePath") or "")
        landed = _wait_for_transfer_file(found_fn, local)
        if landed:
            # Return the LANDED PATH (not just False) so the caller can
            # immediately claim + import it — previously it returned False,
            # the item stayed 'downloading', and the next cycle's
            # slskd_completed lookup failed to map the path, so the file was
            # found-and-re-found every cycle ("Transfer file appeared within
            # the grace window" repeating forever) without ever being moved.
            logger.info("Transfer file appeared within the grace window", queue_id=queue_id, path=landed)
            return landed

        logger.warning(
            "slskd succeeded but no local file found (will keep polling)",
            queue_id=queue_id,
            local_file_path=local or "(empty)",
            found_filename=found_fn,
            monitored_dir=_monitored_downloads_dir(),
        )
        # A succeeded transfer whose file has NOT appeared is usually a
        # slskd flush/rename delay — the file may land moments later.  Do
        # NOT cancel the transfer + mark_failed immediately: that was the
        # infinite re-download loop (each retry's search picked a DIFFERENT
        # file, which also "succeeded" without appearing → requeue → …).
        # Keep the item 'downloading' so a later cycle claims the file the
        # moment it lands; only after the grace period is it failed for a
        # (much slower) retry.
        if _is_stale_queue_item(item, stale_minutes=_SLSKD_SUCCEEDED_NOT_FOUND_TIMEOUT_MINUTES, now=now):
            logger.warning("Succeeded transfer never produced a local file — failing for retry", queue_id=queue_id)
            _remember_failed_peer(transfer)
            try:
                transfer_id = str(transfer.get("id") or "")
                username = str(transfer.get("username") or "")
                if transfer_id and username:
                    slskd.cancel_download(username, transfer_id, remove=True)
            except Exception as exc:
                logger.debug("Could not remove stale transfer", queue_id=queue_id, error=str(exc))

            from db.repositories.queue import mark_failed
            mark_failed(queue_id, "slskd transfer succeeded but local file not found")
            _log_queue_event("failed", "slskd transfer succeeded but local file not found — retrying", queue_id)
            return True
        return False

    if state == slskd.STATE_QUEUED_REMOTELY:
        if _is_stale_queue_item(item, stale_minutes=_SLSKD_REMOTELY_QUEUED_TIMEOUT_MINUTES, now=now):
            logger.warning("Remotely queued too long — cancelling", queue_id=queue_id)
            try:
                transfer_id = str(transfer.get("id") or "")
                username = str(transfer.get("username") or "")
                if transfer_id and username:
                    slskd.cancel_download(username, transfer_id, remove=True)
            except Exception:
                pass
            _remember_failed_peer(transfer)
            from db.repositories.queue import mark_failed
            mark_failed(queue_id, "Remotely queued too long")
            _log_queue_event("failed", "Remotely queued too long — cancelling and retrying", queue_id)
            return True
        return False

    if state in slskd.ACTIVE_STATES:
        if _is_stale_queue_item(item, stale_minutes=_SLSKD_ACTIVE_STATE_TIMEOUT_MINUTES, now=now):
            logger.warning("Download timed out — cancelling", queue_id=queue_id, state=state)
            try:
                transfer_id = str(transfer.get("id") or "")
                username = str(transfer.get("username") or "")
                if transfer_id and username:
                    slskd.cancel_download(username, transfer_id, remove=True)
            except Exception:
                pass
            _remember_failed_peer(transfer)
            from db.repositories.queue import mark_failed
            mark_failed(queue_id, f"slskd download timed out ({state})")
            _log_queue_event("failed", f"slskd download timed out ({state}) — cancelling and retrying", queue_id)
            return True

    return False


# =============================================================================
# MAIN ENTRYPOINT
# =============================================================================

def check_completed_downloads() -> dict[str, Any]:
    """Reconcile 'downloading' queue items against completed downloads."""
    stats: dict[str, Any] = {
        "success": True,
        "downloading_items": 0,
        "imported": 0,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
    }

    try:
        moving_stats = _reconcile_stale_moving()
        stats["imported"] += moving_stats.get("imported", 0)
        stats["moving_reset"] = moving_stats.get("reset", 0)
        stats["moving_skipped"] = moving_stats.get("skipped", 0)
    except Exception as exc:
        logger.warning("Stale 'moving' reconciliation failed", error=str(exc))

    db_now = _db_now_naive()

    try:
        from db.repositories.queue import get_queue_status_counts
        downloading_count = get_queue_status_counts().get("downloading", 0)
        if not downloading_count:
            return stats
        stats["downloading_items"] = downloading_count
    except Exception:
        pass

    try:
        from api_clients.slskd_http import get_slskd_client
        from db.repositories.queue import mark_failed, update_queue_item
        from helpers.logging_config import log_unified
        from services.downloads.download_scan_service import resolve_downloads_dir
        from services.downloads.slskd_service import SlskdService

        slskd_client = get_slskd_client()
        slskd = SlskdService(http_client=slskd_client) if slskd_client is not None else None
        downloads_dir = resolve_downloads_dir(prefer_music_subfolder=False)

        log_unified(f"[QUEUE] Checking {downloading_count} completed download(s) — downloads dir: {downloads_dir}")

        slskd_completed: dict[str, str] = {}
        if slskd is not None:
            try:
                for transfer in slskd.get_completed_transfers():
                    local = str(transfer.get("localFilePath") or "").replace("\\", "/").strip()
                    remote = str(transfer.get("filename") or "").replace("\\", "/").strip()
                    remote_norm = _normalize_transfer_key(remote)
                    # Resolve the on-disk location.  slskd sometimes reports an
                    # empty ``localFilePath`` even though the file landed — it
                    # preserves the REMOTE directory structure under the
                    # downloads root (``music/Artist/Album/01 - Track.flac``).
                    # Fall back to that remote-relative path so the transfer is
                    # not dropped from the completed map.
                    resolved_local = local
                    if (not resolved_local or not os.path.isfile(resolved_local)) and downloads_dir and remote_norm:
                        _candidate = os.path.join(downloads_dir, remote)
                        if os.path.isfile(_candidate):
                            resolved_local = _candidate
                        else:
                            _cand_base = os.path.basename(remote_norm)
                            if _cand_base:
                                import unicodedata as _ud
                                _base_key = _ud.normalize("NFC", _cand_base).lower()
                                _base_stripped = "".join(
                                    c for c in _ud.normalize("NFKD", _base_key) if not _ud.combining(c)
                                ).lower()
                                _base_punct = _punctuation_stripped_key(_cand_base)
                                # Search the downloads root PLUS the torrents
                                # dir / sibling ``torrents`` roots — slskd's
                                # complete-directory can differ from the app's
                                # downloads dir.
                                _search_roots = {downloads_dir}
                                try:
                                    from services.downloads.download_scan_service import resolve_torrents_dir
                                    _search_roots.add(resolve_torrents_dir())
                                except Exception:
                                    pass
                                _parent = os.path.dirname(os.path.normpath(downloads_dir))
                                for _cand_dir in ("torrents", "Torrents", "TORRENTS"):
                                    _sib = os.path.join(_parent, _cand_dir)
                                    if os.path.isdir(_sib):
                                        _search_roots.add(_sib)
                                for _sr in _search_roots:
                                    if not os.path.isdir(_sr):
                                        continue
                                    for _root, _dirs, _files in os.walk(_sr):
                                        # Prune hidden/dunder + archive dirs.
                                        _dirs[:] = [
                                            d for d in _dirs
                                            if not d.startswith(".") and not d.startswith("__")
                                            and d.lower() != "original"
                                        ]
                                        for _fname in _files:
                                            _fn_key = _ud.normalize("NFC", _fname).lower()
                                            _fn_stripped = "".join(
                                                c for c in _ud.normalize("NFKD", _fn_key) if not _ud.combining(c)
                                            ).lower()
                                            if (
                                                _fn_key == _base_key
                                                or _fn_stripped == _base_stripped
                                                or (_base_punct and _punctuation_stripped_key(_fname) == _base_punct)
                                            ):
                                                resolved_local = os.path.join(_root, _fname)
                                                break
                                        if resolved_local and os.path.isfile(resolved_local):
                                            break
                    if resolved_local and os.path.isfile(resolved_local):
                        if remote_norm:
                            slskd_completed[remote_norm] = resolved_local
                            slskd_completed[os.path.basename(remote_norm)] = resolved_local
                        slskd_completed[os.path.basename(resolved_local).lower()] = resolved_local
                logger.debug("Queried slskd completed transfers", count=len(slskd_completed))
            except Exception as exc:
                logger.debug("Could not query slskd completed transfers", error=str(exc))

        fs_files: list[Any] = []
        if os.path.isdir(downloads_dir):
            try:
                from services.infrastructure.filesystem_service import (
                    _original_archive_subfolder_name,
                    resolve_original_archive_dir,
                )
                _archive_dir = resolve_original_archive_dir()
                _archive_name = _original_archive_subfolder_name()

                # Walk the downloads root PLUS the torrents dir / sibling
                # ``torrents`` roots — slskd's complete-directory can differ
                # from the app's downloads dir, and the basename-anywhere
                # matcher must find files wherever they landed.
                _walk_roots: set[str] = {downloads_dir}
                try:
                    from services.downloads.download_scan_service import resolve_torrents_dir
                    _walk_roots.add(resolve_torrents_dir())
                except Exception:
                    pass
                _parent = os.path.dirname(os.path.normpath(downloads_dir))
                for _cand_dir in ("torrents", "Torrents", "TORRENTS"):
                    _sib = os.path.join(_parent, _cand_dir)
                    if os.path.isdir(_sib):
                        _walk_roots.add(_sib)

                seen_paths: set[str] = set()
                for _walk_root in _walk_roots:
                    if not os.path.isdir(_walk_root):
                        continue
                    for root, dirs, files in os.walk(_walk_root):
                        dirs[:] = [
                            d for d in dirs
                            if d != _archive_name
                            and os.path.normpath(os.path.join(root, d)) != _archive_dir
                            and not d.startswith(".")
                            and not d.startswith("__")
                            and d.lower() != "original"
                        ]
                        for filename in sorted(files):
                            ext = os.path.splitext(filename)[1].lower()
                            if ext not in (".mp3", ".flac", ".m4a", ".ogg", ".wav", ".aac", ".wma", ".opus"):
                                continue
                            full_path = os.path.join(root, filename)
                            if full_path in seen_paths:
                                continue
                            seen_paths.add(full_path)
                            fs_files.append({
                                "full_path": full_path,
                                "rel_path": os.path.relpath(full_path, downloads_dir),
                            })
                logger.debug("Filesystem walk completed", count=len(fs_files))
            except Exception as exc:
                logger.debug("Filesystem walk failed", error=str(exc))

        fs_index = _build_download_index(fs_files) if fs_files else {
            "by_rel_norm": {},
            "by_basename": {},
            "by_punct": {},
            "by_token": {},
        }

        claimed_files: set[str] = set()
        try:
            with db_session() as session:
                rows = session.execute(text("""
                    SELECT found_filename FROM download_queue
                    WHERE found_filename IS NOT NULL
                      AND found_filename <> ''
                      AND status NOT IN ('downloading', 'queued', 'failed', 'searching')
                """)).fetchall() or []
            for row in rows:
                fn = (row[0] or "") if not hasattr(row, "_mapping") else (row._mapping.get("found_filename") or "")
                if fn:
                    norm = _normalize_transfer_key(fn)
                    if norm:
                        claimed_files.add(norm)
                        claimed_files.add(os.path.basename(norm))
        except Exception as exc:
            logger.debug("Could not pre-load claimed files", error=str(exc))

        downloading: list[dict[str, Any]] = []
        try:
            with db_session() as session:
                rows = session.execute(text("SELECT * FROM download_queue WHERE status = 'downloading' ORDER BY id")).fetchall() or []
                downloading = [dict(r._mapping) for r in rows]
        except Exception as exc:
            logger.error("Could not fetch downloading items", error=str(exc))
            stats["errors"] += 1
            return stats

        active_transfers: list[dict[str, Any]] = []
        if slskd is not None and downloading:
            try:
                active_transfers = slskd.get_active_downloads()
            except Exception as exc:
                logger.debug("Could not fetch active transfers", error=str(exc))
            if active_transfers:
                _sync_download_progress(active_transfers, downloading)

        for item in downloading:
            try:
                queue_id = item.get("id")

                # Re-fetch the row: the cycle snapshot may be stale (a retry
                # already requeued + re-downloaded this item), and processing
                # the stale row double-fails it → infinite re-download loop.
                fresh = _refresh_downloading_item(queue_id)
                if fresh is None:
                    stats["skipped"] += 1
                    continue
                item = fresh

                _item_source = str(item.get("source") or "").lower()
                if _item_source in ("local", "discovered"):
                    stats["skipped"] += 1
                    continue

                existing_music = item.get("music_file_path")
                if existing_music and os.path.isfile(existing_music):
                    logger.info("Item already in music library — promoting to imported", queue_id=queue_id)
                    update_queue_item(
                        queue_id,
                        status="imported",
                        copied_individually=1,
                        copied_individually_at=datetime.now().isoformat(),
                    )
                    stats["imported"] += 1
                    log_unified(f"[QUEUE] {item.get('artist') or ''} - {item.get('title') or ''} → already in library, promoted to imported")
                    _log_queue_event("imported", f"{item.get('artist') or ''} - {item.get('title') or ''} → already in library, promoted to imported", queue_id)
                    continue

                match_found: str | None = None
                match_source = ""
                abs_path: str | None = None

                found_fn = (item.get("found_filename") or "").replace("\\", "/").strip()

                if found_fn:
                    found_norm = _normalize_transfer_key(found_fn)
                    if found_norm:
                        abs_path = (
                            slskd_completed.get(found_norm)
                            or slskd_completed.get(os.path.basename(found_norm))
                        )
                    if abs_path:
                        abs_path = str(abs_path).replace("\\", "/")
                    if abs_path and os.path.isfile(abs_path):
                        if _is_file_claimed(abs_path, claimed_files, downloads_dir):
                            abs_path = None
                        else:
                            try:
                                _artist_ok = _file_artist_matches_queue_item(abs_path, item)
                            except Exception:
                                _artist_ok = None
                                
                            if _artist_ok is False:
                                abs_path = None
                            else:
                                is_match, match_source = _file_matches_queue_item(abs_path, item)
                                if is_match:
                                    match_found = os.path.relpath(abs_path, downloads_dir)
                                    _claim_file(abs_path, claimed_files, downloads_dir)
                                else:
                                    _delete_mismatched_download(abs_path, queue_id, f"metadata mismatch ({match_source})")
                                    _block_peer_for_queue_item(queue_id, found_fn)
                                    mark_failed(queue_id, "Downloaded file did not match queue item; deleted and rescheduled")
                                    stats["failed"] += 1
                                    log_unified(f"[QUEUE] {item.get('artist') or ''} - {item.get('title') or ''} → failed: downloaded file did not match queue item (deleted + rescheduled)")
                                    _log_queue_event("failed", f"{item.get('artist') or ''} - {item.get('title') or ''} → failed: downloaded file did not match queue item (deleted + rescheduled)", queue_id)
                                    continue

                if match_found is None and found_fn:
                    found_norm = _normalize_transfer_key(found_fn)
                    found_basename = os.path.basename(found_norm) if found_norm else None
                    exact_candidates: list[dict[str, Any]] = []
                    if found_norm:
                        hit = fs_index["by_rel_norm"].get(found_norm)
                        if hit:
                            exact_candidates.append(hit)
                    if found_basename:
                        exact_candidates.extend(fs_index["by_basename"].get(found_basename.lower(), []))
                        # Accent-stripped key so accented filenames match even
                        # when the found_filename normalizes accents differently.
                        _stripped = _accent_stripped_key(found_basename)
                        if _stripped and _stripped != found_basename.lower():
                            exact_candidates.extend(fs_index["by_basename"].get(_stripped, []))
                        # Punctuation-stripped basename ("Par‐delà le mur des
                        # siècles (instrumental)" == "par-dela le mur des siecles
                        # instrumental") — finds the file ANYWHERE under the
                        # downloads tree by basename alone, ignoring the folder
                        # path entirely (the reported Aephanemer loop).
                        _punct = _punctuation_stripped_key(found_basename)
                        if _punct:
                            exact_candidates.extend(fs_index["by_punct"].get(_punct, []))

                    seen_paths: set[str] = set()
                    for f in exact_candidates:
                        candidate = f["full_path"]
                        if candidate in seen_paths:
                            continue
                        seen_paths.add(candidate)
                        rel = f["rel_path"].replace("\\", "/")
                        if _is_file_claimed(candidate, claimed_files, downloads_dir):
                            continue
                        try:
                            _artist_ok = _file_artist_matches_queue_item(candidate, item)
                        except Exception:
                            _artist_ok = None
                        if _artist_ok is False:
                            continue
                        is_match, match_source = _file_matches_queue_item(candidate, item, rel)
                        if not is_match:
                            _delete_mismatched_download(candidate, queue_id, f"metadata mismatch for exact filename match ({match_source})")
                            _block_peer_for_queue_item(queue_id, found_fn)
                            mark_failed(queue_id, "Downloaded file did not match queue item; deleted and rescheduled")
                            stats["failed"] += 1
                            match_found = "__rejected__"
                            break
                        match_found = f["rel_path"]
                        abs_path = candidate
                        _claim_file(candidate, claimed_files, downloads_dir)
                        break

                if match_found == "__rejected__":
                    match_found = None

                if match_found is None:
                    # The remote name recorded when this download was enqueued
                    # tells us EXACTLY which on-disk file belongs to this queue
                    # item.  A fuzzy candidate whose basename matches it was
                    # downloaded for THIS item — a mismatch means it is the
                    # wrong file and must be removed, not left on disk to be
                    # re-picked by the watcher or a later retry.
                    _expected_bases = set()
                    if found_fn:
                        _fb = os.path.basename(found_fn)
                        if _fb:
                            _expected_bases.add(_fb.lower())
                            _expected_bases.add(_fb)

                    for f in _fuzzy_candidates(fs_index, item, fs_files):
                        rel = f["rel_path"].replace("\\", "/")
                        fn_key = _normalize_transfer_key(rel)
                        if fn_key in claimed_files or os.path.basename(fn_key) in claimed_files:
                            continue
                        candidate = f["full_path"]
                        _cand_base = os.path.basename(candidate)

                        try:
                            from services.queue.queue_metadata_matcher import _metadata_matches_queue_item
                            meta_state = _metadata_matches_queue_item(candidate, item)
                        except Exception:
                            meta_state = None

                        if meta_state is False:
                            # A file that genuinely fails the metadata match is
                            # WRONG for this queue item — never leave it on
                            # disk to be re-picked by the watcher or a retry.
                            if _cand_base.lower() in _expected_bases or _cand_base in _expected_bases:
                                _delete_mismatched_download(candidate, queue_id, "metadata mismatch")
                                _block_peer_for_queue_item(queue_id, found_fn)
                                mark_failed(queue_id, "Downloaded file did not match queue item; deleted and rescheduled")
                                stats["failed"] += 1
                                match_found = "__rejected__"
                                log_unified(f"[QUEUE] {item.get('artist') or ''} - {item.get('title') or ''} → failed: downloaded file did not match queue item (deleted + rescheduled)")
                                _log_queue_event("failed", f"{item.get('artist') or ''} - {item.get('title') or ''} → failed: downloaded file did not match queue item (deleted + rescheduled)", queue_id)
                                break
                            continue
                        if meta_state is None:
                            try:
                                from services.queue.queue_scoring import _score_soulseek_candidate
                                score = _score_soulseek_candidate(rel, item, candidate_duration=_extract_duration_seconds(candidate))
                            except Exception:
                                score = 0.0
                            if score < _SLSKD_MIN_ACCEPT_SCORE:
                                # Sub-threshold fuzzy candidate: if this is the
                                # file slskd downloaded for THIS item, remove it
                                # so it cannot be re-picked as a "fresh"
                                # download that then mismatches again.  Other
                                # files (torrents, manual drops) are left alone.
                                if _cand_base.lower() in _expected_bases or _cand_base in _expected_bases:
                                    _delete_mismatched_download(candidate, queue_id, f"below accept threshold (score={score:.2f})")
                                    _block_peer_for_queue_item(queue_id, found_fn)
                                    mark_failed(queue_id, "Downloaded file below acceptance threshold; deleted and rescheduled")
                                    stats["failed"] += 1
                                    match_found = "__rejected__"
                                    log_unified(f"[QUEUE] {item.get('artist') or ''} - {item.get('title') or ''} → failed: downloaded file below acceptance threshold (deleted + rescheduled)")
                                    _log_queue_event("failed", f"{item.get('artist') or ''} - {item.get('title') or ''} → failed: below acceptance threshold (deleted + rescheduled)", queue_id)
                                    break
                                continue

                        try:
                            _artist_ok = _file_artist_matches_queue_item(candidate, item)
                        except Exception:
                            _artist_ok = None
                        if _artist_ok is False:
                            if _cand_base.lower() in _expected_bases or _cand_base in _expected_bases:
                                _delete_mismatched_download(candidate, queue_id, "artist mismatch")
                                _block_peer_for_queue_item(queue_id, found_fn)
                                mark_failed(queue_id, "Downloaded file artist did not match queue item; deleted and rescheduled")
                                stats["failed"] += 1
                                match_found = "__rejected__"
                                log_unified(f"[QUEUE] {item.get('artist') or ''} - {item.get('title') or ''} → failed: downloaded file artist did not match queue item (deleted + rescheduled)")
                                _log_queue_event("failed", f"{item.get('artist') or ''} - {item.get('title') or ''} → failed: artist mismatch (deleted + rescheduled)", queue_id)
                                break
                            continue

                        if meta_state is None and _artist_ok is not True:
                            continue

                        match_found = rel
                        abs_path = candidate
                        match_source = "metadata" if meta_state is True else "filename"
                        claimed_files.add(fn_key)
                        claimed_files.add(os.path.basename(fn_key))
                        break

                if match_found == "__rejected__":
                    # The mismatched download was deleted and the queue item
                    # requeued (mark_failed → 'queued') — do NOT fall through
                    # to the stale/reconcile block, which would re-process an
                    # already-handled item.
                    continue

                if match_found is None or abs_path is None:
                    if slskd is not None:
                        _reconciled = _reconcile_transfer_state(item, slskd, active=active_transfers, now=db_now)
                        if _reconciled is True:
                            stats["failed"] += 1
                            continue
                        if isinstance(_reconciled, str) and _reconciled:
                            # The transfer completed and the file just landed —
                            # claim it now and fall through to match/import
                            # instead of waiting for a later cycle.
                            _landed_path = _reconciled
                            if os.path.isfile(_landed_path) and not _is_file_claimed(_landed_path, claimed_files, downloads_dir):
                                try:
                                    _artist_ok = _file_artist_matches_queue_item(_landed_path, item)
                                except Exception:
                                    _artist_ok = None
                                if _artist_ok is not False:
                                    is_match, match_source = _file_matches_queue_item(_landed_path, item, None)
                                    if is_match:
                                        abs_path = _landed_path
                                        match_found = os.path.relpath(_landed_path, downloads_dir) if downloads_dir else _landed_path
                                        match_source = match_source
                                        _claim_file(_landed_path, claimed_files, downloads_dir)
                                    else:
                                        # The landed file does NOT match this
                                        # queue item — it is the WRONG download
                                        # (e.g. a different track from the same
                                        # single).  Delete it + block the peer +
                                        # requeue so it cannot loop forever being
                                        # re-found every cycle.
                                        _delete_mismatched_download(_landed_path, queue_id, f"landed file mismatch ({match_source})")
                                        _block_peer_for_queue_item(queue_id, found_fn)
                                        mark_failed(queue_id, "Downloaded file did not match queue item; deleted and rescheduled")
                                        stats["failed"] += 1
                                        continue
                                else:
                                    # Artist mismatch on the landed file.
                                    _delete_mismatched_download(_landed_path, queue_id, "landed file artist mismatch")
                                    _block_peer_for_queue_item(queue_id, found_fn)
                                    mark_failed(queue_id, "Downloaded file artist did not match queue item; deleted and rescheduled")
                                    stats["failed"] += 1
                                    continue
                    # Only treat as stale/no-file when we STILL have nothing —
                    # the landed-path claim above sets abs_path and must fall
                    # through to the import logic below.
                    if abs_path is not None and match_found is not None:
                        pass
                    elif _is_stale_queue_item(item, stale_minutes=_STALE_DOWNLOADING_MINUTES, now=db_now):
                        _unconfirmed = _matching_file_exists_unconfirmed(item, fs_files, downloads_dir)
                        if _unconfirmed:
                            logger.warning("Stale in downloading but matching file exists — marking for manual review", queue_id=queue_id)
                            log_unified(f"[QUEUE] {item.get('artist') or ''} - {item.get('title') or ''} → needs manual review: file exists but metadata/artist unconfirmed ({_unconfirmed})")
                            _log_queue_event("manual_review", f"File exists but metadata/artist unconfirmed: {_unconfirmed}", queue_id)
                            stats["skipped"] += 1
                            continue
                            
                        logger.warning("No file found and stale in downloading — scheduling retry", queue_id=queue_id)
                        mark_failed(queue_id, "No file found while marked downloading")
                        stats["failed"] += 1
                        log_unified(f"[QUEUE] {item.get('artist') or ''} - {item.get('title') or ''} → failed: no file found while marked downloading (stale)")
                        _log_queue_event("failed", "No file found while marked downloading (stale)", queue_id)
                        continue
                    elif abs_path is None:
                        stats["skipped"] += 1
                    continue

                expected_dur = item.get("duration")
                if expected_dur:
                    expected_dur = queue_duration_seconds(expected_dur)
                if expected_dur:
                    actual_dur = _extract_duration_seconds(abs_path)
                    if actual_dur and abs(expected_dur - actual_dur) > 20:
                        logger.warning("Duration mismatch — deleting and retrying", queue_id=queue_id, expected=expected_dur, actual=actual_dur)
                        _delete_mismatched_download(abs_path, queue_id, f"duration mismatch: expected {expected_dur}s, got {actual_dur}s")
                        _block_peer_for_queue_item(queue_id, found_fn)
                        mark_failed(queue_id, f"Pre-copy duration mismatch: expected {expected_dur}s, got {actual_dur}s")
                        stats["failed"] += 1
                        log_unified(f"[QUEUE] {item.get('artist') or ''} - {item.get('title') or ''} → failed: duration mismatch")
                        _log_queue_event("failed", f"Duration mismatch: expected {expected_dur}s, got {actual_dur}s", queue_id)
                        continue

                try:
                    update_queue_item(queue_id, found_filename=match_found, file_path=abs_path)
                except Exception:
                    pass

                move_result = _move_and_import(item, abs_path, match_source)
                if move_result.get("success"):
                    stats["imported"] += 1
                    log_unified(f"[QUEUE] {item.get('artist') or ''} - {item.get('title') or ''} → imported to library (match={match_source})")
                    _log_queue_event("imported", f"Imported to library (match={match_source})", queue_id)
                else:
                    stats["errors"] += 1
                    log_unified(f"[QUEUE] {item.get('artist') or ''} - {item.get('title') or ''} → import failed: {move_result.get('error')}")
                    _log_queue_event("failed", f"Import failed: {move_result.get('error')}", queue_id)

            except Exception as exc:
                logger.error("Unhandled error processing queue item", queue_id=item.get("id"), error=str(exc), exc_info=True)
                stats["errors"] += 1

        try:
            seen_orphans: set[str] = set()
            for _local in slskd_completed.values():
                try:
                    if not _local or not os.path.isfile(_local):
                        continue
                    if os.path.basename(str(_local)).lower() in claimed_files:
                        continue
                    real = os.path.realpath(_local)
                    if real in seen_orphans:
                        continue
                    seen_orphans.add(real)
                    
                    if _queue_row_references_file(str(_local), downloads_dir):
                        continue
                        
                    os.remove(_local)
                    stats["orphans_deleted"] = stats.get("orphans_deleted", 0) + 1
                    logger.warning("Deleted orphaned slskd download", path=_local)
                    _log_queue_event("failed", f"Deleted orphaned slskd download: {os.path.basename(str(_local))}", None)
                except Exception as exc:
                    logger.warning("Orphan sweep error", path=_local, error=str(exc))
        except Exception as exc:
            logger.warning("Orphan sweep failed", error=str(exc))

        return stats

    except Exception as exc:
        logger.error("check_completed_downloads failed", error=str(exc), exc_info=True)
        return {"success": False, "error": str(exc), **stats}
