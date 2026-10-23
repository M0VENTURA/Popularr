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
import re
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from sqlalchemy import text

from db.engine import db_session
from helpers.config_helpers import _SLSKD_MIN_ACCEPT_SCORE, get_length_tolerance_seconds
from helpers.normalization_service import normalize_string, queue_duration_seconds

logger = structlog.get_logger(__name__)

_MUSIC_ROOT = os.environ.get("MUSIC_ROOT", "/music")


def _length_tolerance_seconds() -> float:
    try:
        return float(get_length_tolerance_seconds())
    except Exception:
        return 5.0


def _log_queue_event(event_type: str, message: str, queue_id: int | None) -> None:
    try:
        from services.queue.queue_diagnostics_service import log_queue_event
        log_queue_event(event_type, message, queue_id=queue_id)
    except Exception:
        pass


_STALE_DOWNLOADING_MINUTES = 60
_SLSKD_ACTIVE_STATE_TIMEOUT_MINUTES = 60
_SLSKD_REMOTELY_QUEUED_TIMEOUT_MINUTES = 60
_SLSKD_SUCCEEDED_NOT_FOUND_TIMEOUT_MINUTES = 15
_SLSKD_SUCCEEDED_NOT_FOUND_MAX_CYCLES = 3
_LANDED_NOT_IMPORTED_MAX_CYCLES = 3


def _refresh_downloading_item(queue_id: int) -> dict[str, Any] | None:
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
    if not value:
        return None
    norm = str(value).replace("\\", "/").strip()
    return norm.lower() or None


def _db_now_naive() -> datetime:
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


def _record_rejected_for_row(
    queue_id: Any,
    username: Any,
    filename: Any,
    reason: str = "",
) -> None:
    """Remember this candidate as rejected FOR THIS QUEUE ROW.

    The peer block below is global and lives in the pipeline; this record is
    per row and survives restarts, so the next search for the row skips the
    exact file that already failed. The log shows why it is needed: queue row
    6667 downloaded, rejected and deleted the same file from the same peer
    about 20 times while the block was logged every time.
    """
    if not queue_id or not (username or filename):
        return
    try:
        from db.repositories.queue import record_rejected_candidate

        record_rejected_candidate(
            int(queue_id),
            str(username or ""),
            str(filename or ""),
            reason=reason,
        )
    except Exception as exc:
        logger.debug("Rejected-candidate record failed", queue_id=queue_id, error=str(exc))


def _remember_failed_peer(
    transfer: dict[str, Any],
    queue_id: Any = None,
    reason: str = "",
) -> None:
    """Persist peer block into the DB/pipeline blocklist so it isn't retried.

    With a ``queue_id`` the exact candidate is also recorded on the row, so a
    peer that rejected or stalled THIS file is not chosen for it again.
    """
    try:
        from services.downloads.download_pipeline_service import _block_peer
        _block_peer(transfer.get("username"), transfer.get("filename"))
    except Exception:
        pass
    if queue_id:
        _record_rejected_for_row(
            queue_id, transfer.get("username"), transfer.get("filename"), reason
        )


def _monitored_downloads_dir() -> str:
    try:
        from services.downloads.download_scan_service import resolve_downloads_dir
        return resolve_downloads_dir()
    except Exception:
        return "?"


#: ⚠️ ONE look, no sleeping. This used to poll 3 × 10 s for EVERY transfer
#: slskd reported as succeeded but whose file was not visible yet — inside the
#: queue cycle lock, so a handful of slow landings froze the whole queue for
#: minutes. The completion check runs again next cycle anyway, and
#: ``_SLSKD_SUCCEEDED_NOT_FOUND_MAX_CYCLES`` already gives the file its grace.
_FILE_ARRIVAL_POLLS = 1
_FILE_ARRIVAL_POLL_SECONDS = 0


def _wait_for_transfer_file(found_filename: str, local_file_path: str) -> Optional[str]:
    import time as _time

    monitored = _monitored_downloads_dir()
    candidates = []
    _found = str(found_filename or "").replace("\\", "/").strip()
    base = os.path.basename(_found)
    
    if monitored and monitored != "?":
        if base:
            candidates.append(os.path.join(monitored, base))
        if _found and not _found.startswith("/"):
            candidates.append(os.path.join(monitored, _found))
    if local_file_path:
        candidates.append(str(local_file_path).replace("\\", "/"))
        
    try:
        from services.infrastructure.filesystem_service import resolve_original_archive_dir
        _archive_root = resolve_original_archive_dir()
        if _archive_root and os.path.isdir(_archive_root) and base:
            for _aroot, _adirs, _afiles in os.walk(_archive_root):
                if base in _afiles:
                    candidates.append(os.path.join(_aroot, base))
                    break
    except Exception:
        pass

    import unicodedata as _ud

    def _norm_key(name: str) -> str:
        n = _ud.normalize("NFC", name)
        stripped = _ud.normalize("NFKD", n)
        stripped = "".join(c for c in stripped if not _ud.combining(c))
        return stripped.lower()

    _base_key = _norm_key(base)
    _base_punct = _punctuation_stripped_key(base)
    _base_pfx = _track_prefix_stripped_key(base)
    search_roots: set[str] = set()
    if monitored and os.path.isdir(monitored) and monitored != "?":
        search_roots.add(monitored)
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
        _DEPTH_LIMIT = 8
        for _root_dir in search_roots:
            try:
                _depth_limit = _DEPTH_LIMIT
                for _root, _dirs, _files in os.walk(_root_dir):
                    _rel_depth = _root.count(os.sep) - _root_dir.count(os.sep)
                    if _rel_depth >= _depth_limit:
                        _dirs[:] = []
                        continue
                    _dirs[:] = [
                        d for d in _dirs
                        if not d.startswith(".") and not d.startswith("__")
                        and d.lower() != "original"
                    ]
                    for _fname in _files:
                        if (
                            _norm_key(_fname) == _base_key
                            or (_base_punct and _punctuation_stripped_key(_fname) == _base_punct)
                            or (_base_pfx and _track_prefix_stripped_key(_fname) == _base_pfx)
                        ):
                            candidates.append(os.path.join(_root, _fname))
                            break
            except Exception:
                pass

    if not candidates:
        return None

    for _attempt in range(max(1, _FILE_ARRIVAL_POLLS)):
        for path in candidates:
            try:
                if path and os.path.isfile(path):
                    return path
            except Exception:
                pass
        if _FILE_ARRIVAL_POLL_SECONDS > 0 and _attempt < _FILE_ARRIVAL_POLLS - 1:
            _time.sleep(_FILE_ARRIVAL_POLL_SECONDS)
    return None


def _accent_stripped_key(name: str) -> str:
    try:
        import unicodedata as _ud
        nfc = _ud.normalize("NFC", str(name or ""))
        stripped = "".join(c for c in _ud.normalize("NFKD", nfc) if not _ud.combining(c))
        return stripped.lower().strip()
    except Exception:
        return str(name or "").lower().strip()


def _punctuation_stripped_key(name: str) -> str:
    try:
        import unicodedata as _ud
        nfc = _ud.normalize("NFC", str(name or ""))
        stripped = "".join(c for c in _ud.normalize("NFKD", nfc) if not _ud.combining(c))
        return "".join(c for c in stripped.lower() if c.isalnum())
    except Exception:
        return "".join(c for c in str(name or "").lower() if c.isalnum())


_TRACK_NUM_PREFIX_RE = re.compile(r"^\d{1,4}(?:[\s._-]*\d{1,4})*[\s._-]*")


def _basename_without_track_prefix(name: str) -> str:
    try:
        base = str(name or "").strip()
    except Exception:
        return str(name or "")
    base = _TRACK_NUM_PREFIX_RE.sub("", base)
    return base


def _track_prefix_stripped_key(name: str) -> str:
    try:
        import unicodedata as _ud
        stripped = _basename_without_track_prefix(name)
        nfc = _ud.normalize("NFC", stripped)
        stripped = "".join(c for c in _ud.normalize("NFKD", nfc) if not _ud.combining(c))
        return "".join(c for c in stripped.lower() if c.isalnum())
    except Exception:
        return _punctuation_stripped_key(name)


_SUCCEEDED_NOT_FOUND_CYCLES: dict[int, int] = {}
_LANDED_NOT_IMPORTED_CYCLES: dict[int, int] = {}


def _bump_cycle_counter(counter: dict[int, int], queue_id: int) -> int:
    counter[int(queue_id)] = counter.get(int(queue_id), 0) + 1
    return counter[int(queue_id)]


def _clear_cycle_counter(counter: dict[int, int], queue_id: int) -> None:
    try:
        counter.pop(int(queue_id), None)
    except Exception:
        pass


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
        _stripped = _accent_stripped_key(base)
        if _stripped and _stripped != base.lower():
            by_basename.setdefault(_stripped, []).append(f)
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
    detail: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    try:
        from helpers.config_helpers import get_search_quality_config
        _allowed = set(get_search_quality_config().get("allowed_formats") or set())
    except Exception:
        _allowed = set()
    if _allowed:
        _ext = os.path.splitext(str(file_path or ""))[1].lower().lstrip(".")
        if _ext not in _allowed:
            if detail is not None:
                detail.clear()
                detail["reason"] = "unsupported_format"
                detail["found_format"] = _ext
                detail["allowed_formats"] = sorted(_allowed)
            return False, "unsupported_format"

    meta_state = None
    try:
        from services.queue.queue_metadata_matcher import _metadata_matches_queue_item
        meta_state = _metadata_matches_queue_item(file_path, queue_item, detail=detail)
    except Exception as exc:
        logger.debug("Metadata check failed", file_path=file_path, error=str(exc))

    if meta_state is True:
        return True, "metadata"
    if meta_state is False:
        return False, "metadata"

    if detail is not None:
        detail.clear()

    try:
        from services.downloads.match_engine import filename_matches_queue_item
        is_match = filename_matches_queue_item(file_path, queue_item)
    except Exception as exc:
        logger.debug("Filename match failed", file_path=file_path, error=str(exc))
        is_match = False
    if not is_match and detail is not None:
        detail["reason"] = "filename_mismatch"
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

    if all(qa in _GENERIC_COMPILATION_ARTISTS for qa in queue_artists):
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


def _queue_reject_failure_reason(match_source: str) -> str:
    if match_source == "unsupported_format":
        return "Downloaded file type is not an allowed format; deleted and rescheduled"
    return "Downloaded file did not match queue item; deleted and rescheduled"


DISCARD_REASON_LABELS: dict[str, str] = {
    "wrong_artist": "Wrong artist",
    "wrong_length": "Wrong length",
    "title_mismatch": "Different song",
    "edition_mismatch": "Different edition",
    "filename_mismatch": "Filename does not match",
    "unsupported_format": "File type not allowed",
}


def _discard_reason(detail: dict[str, Any], match_source: str) -> str:
    reason = str((detail or {}).get("reason") or "").strip()
    if reason:
        return reason
    return "unsupported_format" if match_source == "unsupported_format" else "filename_mismatch"


def _discard_detail_sentence(detail: dict[str, Any], match_source: str) -> str:
    reason = _discard_reason(detail, match_source)
    found_artist = str(detail.get("found_artist") or "").strip()
    found_len = detail.get("found_length_seconds")
    want_len = detail.get("expected_length_seconds")

    def _mmss(seconds: Any) -> str:
        try:
            total = int(round(float(seconds)))
        except (TypeError, ValueError):
            return ""
        return f"{total // 60}:{total % 60:02d}"

    if reason == "wrong_artist":
        if found_artist:
            return f"Wrong artist — the file is credited to '{found_artist}'"
        return "Wrong artist — the file has no usable artist tag"

    if reason == "wrong_length":
        got, want = _mmss(found_len), _mmss(want_len)
        if got and want:
            return f"Wrong length — the file is {got} but the queue item is {want}"
        if got:
            return f"Wrong length — the file is {got}"
        return "Wrong length — did not match the queue item"

    if reason == "title_mismatch":
        found_title = str(detail.get("found_title") or "").strip()
        if found_title:
            return f"Different song — the file is titled '{found_title}'"
        return "Different song — the file's title does not match"

    if reason == "edition_mismatch":
        return "Different edition — the file's edition annotation does not match"

    if reason == "unsupported_format":
        ext = str(detail.get("found_format") or "").lstrip(".")
        allowed = ", ".join(detail.get("allowed_formats") or [])
        if ext:
            return f"File type not allowed — '.{ext}'" + (f" (allowed: {allowed})" if allowed else "")
        return "File type not allowed"

    if reason == "filename_mismatch":
        return "Filename does not match the queue item"

    return "Downloaded file did not match queue item"


def _record_discarded_download(
    *,
    item: dict[str, Any],
    queue_id: int | None,
    detail: dict[str, Any] | None,
    match_source: str,
    filename: str = "",
) -> None:
    try:
        from db.repositories.queue_rejections import record_rejection

        _detail = dict(detail or {})
        reason = _discard_reason(_detail, match_source)
        sentence = _discard_detail_sentence(_detail, match_source)

        record_rejection(
            reason=reason,
            queue_id=queue_id,
            artist=str(item.get("artist") or ""),
            title=str(item.get("title") or ""),
            album=str(item.get("album") or ""),
            detail=sentence,
            found_artist=str(_detail.get("found_artist") or ""),
            found_title=str(_detail.get("found_title") or ""),
            found_length_seconds=_detail.get("found_length_seconds"),
            expected_length_seconds=_detail.get("expected_length_seconds"),
            filename=filename,
        )
    except Exception as exc:
        logger.debug("Could not record discarded download", queue_id=queue_id, error=str(exc))


def _block_peer_for_queue_item(
    queue_id: int,
    found_filename: str | None = None,
    reason: str = "rejected download",
) -> None:
    """Block the peer that served a rejected file, and record the candidate on the row.

    The peer is looked up in slskd's completed transfers. The candidate is
    recorded on the queue row EVEN WHEN that lookup fails (slskd unreachable,
    or the transfer already removed): the old version returned early in those
    cases, so nothing at all stopped the next search choosing the same file.
    Without a username the record matches that filename from ANY peer.
    """
    _remote = str(found_filename or "").replace("\\", "/").strip()
    _blocked_user = ""
    try:
        from api_clients.slskd_http import get_slskd_client
        from services.downloads.slskd_service import SlskdService

        client = get_slskd_client()
        if client is not None:
            slskd = SlskdService(http_client=client)

            _remote_norm = _normalize_transfer_key(_remote) if _remote else None
            _remote_base = os.path.basename(_remote_norm or "") if _remote_norm else None

            for transfer in slskd.get_completed_transfers():
                username = str(transfer.get("username") or "")
                remote = str(transfer.get("filename") or "").replace("\\", "/").strip()
                remote_norm = _normalize_transfer_key(remote) if remote else None
                remote_base = os.path.basename(remote_norm or "") if remote_norm else None
                if not username:
                    continue
                matches = (
                    (_remote_norm and remote_norm == _remote_norm)
                    or (_remote_base and remote_base == _remote_base)
                    or (_remote_base and remote_norm and remote_norm.endswith(f"/{_remote_base}"))
                )
                if not matches:
                    continue
                _blocked_user = username
                if remote:
                    _remote = remote
                try:
                    from services.downloads.download_pipeline_service import _block_peer
                    _block_peer(username, remote)
                    logger.warning("Blocked peer after mismatched download", queue_id=queue_id, username=username, filename=remote[:120])
                except Exception as _be:
                    logger.debug("Peer block skipped", queue_id=queue_id, error=str(_be))
                break
    except Exception as exc:
        logger.debug("Peer-block lookup failed", queue_id=queue_id, error=str(exc))

    _record_rejected_for_row(queue_id, _blocked_user, _remote, reason)


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

#: The album-scoped release columns a download import carries and applies.
#: Every name here must be a real ``tracks`` COLUMN — the list builds the
#: sibling-inheritance ``SELECT``, so a non-column breaks that query.
#: ``release_title`` (the edition, "Release Name") and ``release_year`` (this
#: edition's year) were the two missing ones: the release payload has carried
#: both since ``_flatten_release``, but the download path dropped them, so a
#: downloaded album arrived with no edition name/year while the album page's
#: Lookup MBID proposed both.
_ALBUM_LEVEL_COLUMNS: tuple[str, ...] = (
    "musicbrainz_albumtype",
    "musicbrainz_albumstatus",
    "musicbrainz_albumartistid",
    "musicbrainz_releasegroupid",
    "releasecountry",
    "originalyear",
    "originaldate",
    "releasedate",
    "release_title",
    "release_year",
    "disctotal",
    "recordlabel",
    "catalognumber",
    "barcode",
    "media",
)

_ALBUM_FIELD_SOURCES: tuple[str, ...] = ("stored", "sibling", "MusicBrainz", "missing")
_SOURCE_MAP_LOGGED_KEY = "_album_source_map_logged"


def _album_level_mb_fields(release: dict[str, Any]) -> dict[str, Any]:
    """Album-scoped release fields, read off a flattened MB release.

    One mapping for every path that imports a release (queue time,
    post-download, folder match, heal), so a field missing here is missing
    everywhere.
    """
    out: dict[str, Any] = {}
    mapping = {
        "musicbrainz_albumtype": release.get("album_type"),
        "musicbrainz_albumstatus": release.get("status"),
        "musicbrainz_albumartistid": release.get("album_artist_mbid"),
        "musicbrainz_releasegroupid": release.get("release_group_mbid"),
        "releasecountry": release.get("releasecountry"),
        "originalyear": release.get("original_year"),
        "originaldate": release.get("original_date"),
        "releasedate": release.get("releasedate"),
        # The SPECIFIC edition name/year. The release GROUP's name is the
        # album's own identity (``album``), so these say WHICH pressing is held.
        "release_title": (
            release.get("specific_release_title")
            or release.get("release_title")
        ),
        "release_year": (
            release.get("version_release_year")
            if release.get("version_release_year") not in (None, "")
            else release.get("release_year")
        ),
        "disctotal": release.get("disctotal"),
        "recordlabel": release.get("recordlabel"),
        "catalognumber": release.get("catalognumber"),
        "barcode": release.get("barcode"),
        "media": release.get("media"),
    }
    for column, value in mapping.items():
        if value is not None and str(value).strip() != "":
            out[column] = value

    # The ALBUM's MusicBrainz genres travel alongside, NOT as a ``tracks``
    # column — ``_ALBUM_LEVEL_COLUMNS`` is used to build a SELECT, so a
    # non-column there would break that query.
    _genres = _album_level_genres(release)
    if _genres:
        out["musicbrainz_album_genres"] = _genres
    return out


def _album_level_genres(release: dict[str, Any]) -> str:
    """The ALBUM's MusicBrainz genres: release GROUP then the RELEASE.

    Not the per-recording list — that travels per track and was already
    imported. This is a FILL for tracks with no genre evidence of their own,
    so the per-track affiliation markers are stripped: a "Cover" here would be
    stamped on every track of the album.
    """
    from services.enrichment.genre_aggregation_service import (
        strip_track_affiliation_tags,
    )

    names: list[str] = []
    seen: set[str] = set()
    for raw in (
        release.get("release_group_genres"),
        release.get("release_genres"),
    ):
        for genre in strip_track_affiliation_tags(raw):
            key = genre.casefold()
            if key and key not in seen:
                seen.add(key)
                names.append(genre)
    return ", ".join(names)


def _describe_album_source_map(sources: dict[str, str]) -> str:
    return " ".join(
        f"{name}=[{' '.join(c for c in _ALBUM_LEVEL_COLUMNS if sources.get(c) == name) or '-'}]"
        for name in _ALBUM_FIELD_SOURCES
    )


def _resolve_album_level_metadata(item: dict[str, Any]) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    sources: dict[str, str] = {c: "missing" for c in _ALBUM_LEVEL_COLUMNS}

    for column in _ALBUM_LEVEL_COLUMNS:
        value = item.get(column)
        if value is not None and str(value).strip() != "":
            resolved[column] = value
            sources[column] = "stored"

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
                    if column in sources:
                        sources[column] = "stored"

    _inherit_release = str(
        item.get("release_mbid") or item.get("release_id") or ""
    ).strip()
    _inherit_group = str(item.get("musicbrainz_releasegroupid") or "").strip()
    _missing_album_cols = [c for c in _ALBUM_LEVEL_COLUMNS if not resolved.get(c)]
    if _missing_album_cols:
        try:
            with db_session() as session:
                sibling = session.execute(
                    text(
                        f"""
                        SELECT {', '.join(_ALBUM_LEVEL_COLUMNS)}
                        FROM tracks
                        WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                          AND LOWER(COALESCE(album, '')) = LOWER(:album)
                          AND COALESCE(file_path, '') NOT LIKE '__queued_for_download__%%'
                          AND (
                                (:inherit_release = '' AND :inherit_group = '')
                             OR LOWER(COALESCE(musicbrainz_album_mbid, '')) = LOWER(:inherit_release)
                             OR (
                                    :inherit_group <> ''
                                AND LOWER(COALESCE(musicbrainz_releasegroupid, '')) = LOWER(:inherit_group)
                                )
                          )
                        ORDER BY CASE
                            WHEN :inherit_release <> ''
                             AND LOWER(COALESCE(musicbrainz_album_mbid, '')) = LOWER(:inherit_release)
                                THEN 0
                            WHEN COALESCE(musicbrainz_releasegroupid, '') <> '' THEN 1
                            ELSE 2
                        END, id
                        LIMIT 1
                        """
                    ),
                    {
                        "artist": item.get("album_artist") or item.get("artist"),
                        "album": item.get("album"),
                        "inherit_release": _inherit_release,
                        "inherit_group": _inherit_group,
                    },
                ).fetchone()
            if sibling is not None:
                mapping = getattr(sibling, "_mapping", None) or dict(sibling)
                for column in _ALBUM_LEVEL_COLUMNS:
                    if column in resolved:
                        continue
                    value = mapping.get(column)
                    if value is not None and str(value).strip() != "":
                        resolved[column] = value
                        sources[column] = "sibling"
        except Exception as exc:
            logger.debug(
                "Album MBID inheritance from library rows failed",
                album=item.get("album"), error=str(exc),
            )

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
                        if column in sources:
                            sources[column] = "MusicBrainz"
        except Exception as exc:
            logger.debug(
                "Album-level MB refresh failed — using stored values only",
                release_mbid=release_mbid,
                error=str(exc),
            )

    if not item.get(_SOURCE_MAP_LOGGED_KEY):
        item[_SOURCE_MAP_LOGGED_KEY] = True
        try:
            from helpers.logging_config import log_unified

            log_unified(
                f"[QUEUE] album metadata sources {_describe_album_source_map(sources)}",
                queue_id=item.get("id"),
                album=item.get("album"),
            )
        except Exception as _diag_exc:
            logger.debug(
                "Album metadata source map not written",
                error=str(_diag_exc),
            )

    return resolved


def _match_release_track(
    release: dict[str, Any], item: dict[str, Any]
) -> dict[str, Any] | None:
    tracks = release.get("tracks") or []
    if not isinstance(tracks, list) or not tracks:
        return None

    recording_mbid = str(item.get("recording_mbid") or "").strip()
    if recording_mbid:
        for track in tracks:
            if str(track.get("recording_mbid") or "").strip() == recording_mbid:
                return track

    title = normalize_string(item.get("title") or "")
    if title:
        for track in tracks:
            if normalize_string(str(track.get("title") or "")) == title:
                return track
    return None


def _resolve_missing_identity(
    item: dict[str, Any], file_path: str | None = None
) -> dict[str, Any]:
    from services.downloads.download_organize_helpers import _extract_year_for_path

    fills: dict[str, Any] = {}

    def _missing(key: str) -> bool:
        return item.get(key) in (None, "")

    def _fill_year(*values: Any) -> bool:
        for value in values:
            year = _extract_year_for_path(value)
            if year != "Unknown":
                fills["year"] = year
                return True
        return False

    if _missing("year"):
        _fill_year(item.get("release_year"), item.get("release_date"))

    if _missing("year") and "year" not in fills:
        stored = item.get("metadata")
        if isinstance(stored, str):
            try:
                stored = json.loads(stored or "{}")
            except Exception:
                stored = {}
        album_payload = stored.get("album_metadata") if isinstance(stored, dict) else None
        if isinstance(album_payload, dict):
            _fill_year(album_payload.get("releasedate"), album_payload.get("originalyear"))

    need_year = "year" not in fills and _missing("year")
    need_track = _missing("track_number")

    release_mbid = str(item.get("release_mbid") or item.get("release_id") or "").strip()
    if release_mbid and (need_year or need_track):
        try:
            from services.enrichment.musicbrainz_service import (
                fetch_musicbrainz_release_metadata,
            )

            release = fetch_musicbrainz_release_metadata(release_mbid)
        except Exception as exc:
            logger.debug(
                "Identity refresh failed — using stored values only",
                queue_id=item.get("id"),
                release_mbid=release_mbid,
                error=str(exc),
            )
            release = None
        if isinstance(release, dict):
            if need_year:
                _fill_year(
                    release.get("release_year"),
                    release.get("releasedate"),
                    release.get("original_year"),
                )
            if _missing("track_number"):
                match = _match_release_track(release, item)
                if match:
                    if match.get("track_number") not in (None, ""):
                        fills["track_number"] = match["track_number"]
                    if _missing("disc_number") and match.get("disc_number") not in (None, ""):
                        fills["disc_number"] = match["disc_number"]

    need_year = "year" not in fills and _missing("year")
    if (need_year or _missing("track_number")) and file_path and os.path.isfile(file_path):
        try:
            from services.downloads.download_scan_service import (
                _extract_discovered_metadata,
            )

            discovered = _extract_discovered_metadata(
                file_path, os.path.basename(file_path)
            ) or {}
        except Exception as exc:
            logger.debug(
                "Source-file identity read failed",
                queue_id=item.get("id"),
                error=str(exc),
            )
            discovered = {}
        if need_year and discovered.get("year"):
            _fill_year(discovered["year"])
        if _missing("track_number") and discovered.get("track_number") not in (None, ""):
            fills["track_number"] = discovered["track_number"]

    return fills


def _sniff_image_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _fetch_import_art(item: dict[str, Any]) -> Optional[tuple[bytes, str]]:
    url = str(item.get("cover_art_url") or "").strip()
    if url:
        try:
            import httpx

            resp = httpx.get(url, timeout=10)
            if resp.status_code == 200 and resp.content:
                return resp.content, _sniff_image_mime(resp.content)
        except Exception as exc:
            logger.debug(
                "Queue cover URL fetch failed",
                queue_id=item.get("id"), url=url, error=str(exc),
            )

    artist = str(item.get("album_artist") or item.get("artist") or "").strip()
    album = str(item.get("album") or "").strip()
    if artist and album:
        try:
            from services.enrichment.album_art_service import get_or_fetch_album_art

            data, mime = get_or_fetch_album_art(artist, album)
            if data:
                return data, (mime or _sniff_image_mime(data))
        except Exception as exc:
            logger.debug(
                "Album art lookup failed",
                queue_id=item.get("id"), artist=artist, album=album, error=str(exc),
            )

    release_mbid = str(item.get("release_mbid") or item.get("release_id") or "").strip()
    if release_mbid:
        try:
            from api_clients.coverartarchive import get_release_front_image_bytes

            data = get_release_front_image_bytes(release_mbid)
            if data:
                return data, _sniff_image_mime(data)
        except Exception as exc:
            logger.debug(
                "CAA art lookup failed",
                queue_id=item.get("id"), release_mbid=release_mbid, error=str(exc),
            )
    return None


def _apply_stored_metadata(item: dict[str, Any], file_path: str) -> bool:
    _album_level = _resolve_album_level_metadata(item)

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

    try:
        _disctotal = int(str(_album_level.get("disctotal") or "0").split("/")[0])
    except (TypeError, ValueError):
        _disctotal = 0

    if _disc_num >= 2:
        _disc_value: str | None = str(_disc_raw)
    elif _disc_num == 1:
        _disc_value = "1" if _disctotal >= 2 else ""
    else:
        _disc_value = "" if _disctotal < 2 else None

    meta = {k: v for k, v in meta.items() if v not in (None, "")}
    if _disc_value is not None:
        meta["disc_number"] = _disc_value
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

    for _col, _val in _album_level.items():
        meta.setdefault(_col, _val)

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
            if _stored.get("isrc"):
                meta["isrc"] = str(_stored["isrc"])
            if _stored.get("tracktotal"):
                meta["tracktotal"] = str(_stored["tracktotal"])
            if _stored.get("musicbrainz_artistid"):
                meta["musicbrainz_artistid"] = str(_stored["musicbrainz_artistid"])
    except Exception as _stored_exc:
        logger.debug("Stored MB metadata parse failed", queue_id=item.get("id"), error=str(_stored_exc))

    # ── Album-level MusicBrainz genres: a FILL for this track's ``genres`` ──
    # The release GROUP's and RELEASE's curated genres. Applied ONLY when the
    # track has NO genre evidence of its own — the album list is an aggregate
    # of exactly those sources, so writing it back would flatten the track to
    # the album blend.
    #
    # ⚠️ DATABASE ONLY, like the album page's own genre fill. The file's genre
    # tag belongs to the popularity/finalise scan's DB → file sync; writing it
    # here would make the import a second genre writer.
    _album_genre_fill = ""
    if isinstance(_stored, dict) and not str(
        _stored.get("musicbrainz_genres") or ""
    ).strip():
        _album_genre_fill = str(
            (_stored.get("album_metadata") or {}).get("musicbrainz_album_genres") or ""
        ).strip()

    _track_missing = [
        _k for _k in ("isrc", "tracktotal", "musicbrainz_artistid")
        if not meta.get(_k)
    ]
    if _track_missing and release_mbid:
        try:
            from services.enrichment.musicbrainz_service import (
                fetch_musicbrainz_release_metadata,
            )

            _release = fetch_musicbrainz_release_metadata(release_mbid)
            _match = (
                _match_release_track(_release, item)
                if isinstance(_release, dict)
                else None
            )
            if _match:
                _fills = {
                    "isrc": _match.get("isrc"),
                    "tracktotal": _match.get("tracktotal"),
                    "musicbrainz_artistid": _match.get("artist_mbid"),
                }
                for _k in _track_missing:
                    if _fills.get(_k) not in (None, ""):
                        meta[_k] = str(_fills[_k])
        except Exception as _track_exc:
            logger.debug(
                "Track-level MB enrichment refresh failed — stored values only",
                queue_id=item.get("id"),
                release_mbid=release_mbid,
                error=str(_track_exc),
            )

    if not meta:
        return False

    _tags_written = False
    try:
        from services.metadata.tag_file_service import update_file_metadata
        _tags_written = bool(update_file_metadata(file_path, meta))
    except Exception as exc:
        logger.warning(
            "Could not apply stored metadata to file",
            queue_id=item.get("id"), path=file_path, error=str(exc),
        )
    if not _tags_written:
        logger.warning(
            "Imported file keeps its downloaded tags — tag write failed or was skipped",
            queue_id=item.get("id"), path=file_path,
        )

    try:
        _art = _fetch_import_art(item) if os.path.isfile(file_path) else None
        if _art:
            from services.metadata.tag_file_service import embed_album_art
            _art_bytes, _art_mime = _art
            embed_album_art(file_path, _art_bytes, _art_mime)
    except Exception as _art_exc:
        logger.warning("Album art embed skipped", queue_id=item.get("id"), error=str(_art_exc))

    try:
        _db_payload: dict[str, Any] = {}
        _mb_cols = {
            "writer", "is_cover", "original_cover_artist", "musicbrainz_genres",
            "musicbrainz_workid", "recording_mbid", "release_mbid",
            "musicbrainz_artistid", "musicbrainz_albumartistid",
            "musicbrainz_releasegroupid", "musicbrainz_albumtype", "musicbrainz_albumstatus",
            "releasecountry", "originalyear", "originaldate", "releasedate",
            "release_title", "release_year",
            "disctotal", "recordlabel", "catalognumber", "barcode", "media",
            "isrc", "tracktotal", "musicbrainz_album_mbid",
        }
        for _col in _mb_cols:
            if meta.get(_col) not in (None, ""):
                _db_payload[_col] = meta[_col]

        if _album_genre_fill and not _db_payload.get("genres"):
            _db_payload["genres"] = _album_genre_fill

        if release_mbid and not _db_payload.get("musicbrainz_album_mbid"):
            _db_payload["musicbrainz_album_mbid"] = release_mbid

        if recording_mbid or release_mbid:
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
    except Exception as exc:
        logger.debug("Could not persist MB metadata to tracks table", queue_id=item.get("id"), error=str(exc))

    return _tags_written


def _resolve_track_id_for_import(
    *,
    file_path: str,
    recording_mbid: str | None,
    release_mbid: str | None,
    artist: str | None,
    title: str | None,
) -> str | None:
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
                return {"success": False, "error": "already_claimed"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    def _reset_to_downloading(reason: str) -> None:
        try:
            from db.repositories.queue import update_queue_item
            update_queue_item(queue_id, status="downloading")
        except Exception:
            pass

    try:
        from db.repositories.queue import update_queue_item
        from services.downloads.download_organize_helpers import move_track_to_library
        from services.downloads.download_verification_service import (
            mark_queue_item_moved,
            verify_file_in_music,
        )

        if not abs_path or not os.path.isfile(abs_path) or os.path.getsize(abs_path) == 0:
            _reset_to_downloading(f"source file missing before copy: {abs_path}")
            return {"success": False, "error": "source_file_missing"}

        try:
            _identity = _resolve_missing_identity(item, abs_path)
        except Exception:
            _identity = {}
        if _identity:
            item.update(_identity)
            try:
                update_queue_item(queue_id, **_identity)
            except Exception:
                pass

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
            try:
                from services.downloads.download_organize_helpers import dedupe_library_folder
                dedupe_library_folder(os.path.dirname(target_path), keep_path=target_path)
            except Exception:
                pass

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
                _reset_to_downloading(f"status update failed: {exc}")
                return {"success": False, "error": str(exc)}
                
            return {"success": True, "target_path": target_path}

        try:
            from db.repositories.queue import mark_failed
            mark_failed(queue_id, f"Move verification failed: {verify_result.get('error')}")
        except Exception:
            pass
        return {"success": False, "error": verify_result.get("error")}

    except Exception as exc:
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
            else:
                update_queue_item(queue_id, status="downloading")
                stats["reset"] += 1
        except Exception:
            stats["skipped"] += 1

    return stats


def _reconcile_transfer_state(
    item: dict[str, Any],
    slskd: Any,
    active: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> bool | str:
    queue_id = item.get("id")
    found_fn = (item.get("found_filename") or "").strip()

    if active is None:
        try:
            active = slskd.get_active_downloads()
        except Exception:
            active = []

    if not active:
        if _is_stale_queue_item(item, stale_minutes=10, now=now):
            from db.repositories.queue import mark_failed
            mark_failed(queue_id, "Transfer missing from slskd API while marked downloading")
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
            from db.repositories.queue import mark_failed
            mark_failed(queue_id, "Transfer missing from slskd API while marked downloading")
            return True
        return False

    state = slskd.state_text(transfer.get("state"))
    failed_states = slskd.FAILED_STATES

    if state in failed_states:
        _remember_failed_peer(transfer, queue_id, reason=str(state))
        from db.repositories.queue import mark_failed
        mark_failed(queue_id, f"slskd transfer failed: {state}")
        return True

    if slskd.is_success_state(state):
        local = str(transfer.get("localFilePath") or "")
        landed = _wait_for_transfer_file(found_fn, local)
        if landed:
            _clear_cycle_counter(_SUCCEEDED_NOT_FOUND_CYCLES, queue_id)
            return landed

        _succeeded_not_found_cycles = _bump_cycle_counter(
            _SUCCEEDED_NOT_FOUND_CYCLES, queue_id
        )
        _grace_exhausted = (
            _succeeded_not_found_cycles >= _SLSKD_SUCCEEDED_NOT_FOUND_MAX_CYCLES
            or _is_stale_queue_item(item, stale_minutes=_SLSKD_SUCCEEDED_NOT_FOUND_TIMEOUT_MINUTES, now=now)
        )
        if _grace_exhausted:
            _clear_cycle_counter(_SUCCEEDED_NOT_FOUND_CYCLES, queue_id)
            _remember_failed_peer(transfer, queue_id, reason=str(state))
            try:
                transfer_id = str(transfer.get("id") or "")
                username = str(transfer.get("username") or "")
                if transfer_id and username:
                    slskd.cancel_download(username, transfer_id, remove=True)
            except Exception:
                pass

            from db.repositories.queue import mark_failed
            mark_failed(queue_id, "slskd transfer succeeded but local file not found")
            return True
        return False

    if state == slskd.STATE_QUEUED_REMOTELY:
        if _is_stale_queue_item(item, stale_minutes=_SLSKD_REMOTELY_QUEUED_TIMEOUT_MINUTES, now=now):
            try:
                transfer_id = str(transfer.get("id") or "")
                username = str(transfer.get("username") or "")
                if transfer_id and username:
                    slskd.cancel_download(username, transfer_id, remove=True)
            except Exception:
                pass
            _remember_failed_peer(transfer, queue_id, reason=str(state))
            from db.repositories.queue import mark_failed
            mark_failed(queue_id, "Remotely queued too long")
            return True
        return False

    if state in slskd.ACTIVE_STATES:
        if _is_stale_queue_item(item, stale_minutes=_SLSKD_ACTIVE_STATE_TIMEOUT_MINUTES, now=now):
            try:
                transfer_id = str(transfer.get("id") or "")
                username = str(transfer.get("username") or "")
                if transfer_id and username:
                    slskd.cancel_download(username, transfer_id, remove=True)
            except Exception:
                pass
            _remember_failed_peer(transfer, queue_id, reason=str(state))
            from db.repositories.queue import mark_failed
            mark_failed(queue_id, f"slskd download timed out ({state})")
            return True

    return False


# =============================================================================
# MAIN ENTRYPOINT
# =============================================================================

def check_completed_downloads() -> dict[str, Any]:
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
    except Exception:
        pass

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
        downloads_dir = resolve_downloads_dir()

        slskd_completed: dict[str, str] = {}
        if slskd is not None:
            try:
                for transfer in slskd.get_completed_transfers():
                    local = str(transfer.get("localFilePath") or "").replace("\\", "/").strip()
                    remote = str(transfer.get("filename") or "").replace("\\", "/").strip()
                    remote_norm = _normalize_transfer_key(remote)
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
                                _base_punct = _punctuation_stripped_key(_cand_base)
                                _base_pfx = _track_prefix_stripped_key(_cand_base)
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
                                        _dirs[:] = [
                                            d for d in _dirs
                                            if not d.startswith(".") and not d.startswith("__")
                                            and d.lower() != "original"
                                        ]
                                        for _fname in _files:
                                            _fn_key = _ud.normalize("NFC", _fname).lower()
                                            if (
                                                _fn_key == _base_key
                                                or (_base_punct and _punctuation_stripped_key(_fname) == _base_punct)
                                                or (_base_pfx and _track_prefix_stripped_key(_fname) == _base_pfx)
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
            except Exception:
                pass

        fs_files: list[Any] = []
        if os.path.isdir(downloads_dir):
            try:
                from services.infrastructure.filesystem_service import (
                    _original_archive_subfolder_name,
                    resolve_original_archive_dir,
                )
                _archive_dir = resolve_original_archive_dir()
                _archive_name = _original_archive_subfolder_name()

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
            except Exception:
                pass

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
        except Exception:
            pass

        downloading: list[dict[str, Any]] = []
        try:
            with db_session() as session:
                rows = session.execute(text("SELECT * FROM download_queue WHERE status = 'downloading' ORDER BY id")).fetchall() or []
                downloading = [dict(r._mapping) for r in rows]
        except Exception:
            stats["errors"] += 1
            return stats

        active_transfers: list[dict[str, Any]] = []
        if slskd is not None and downloading:
            try:
                active_transfers = slskd.get_active_downloads()
            except Exception:
                pass
            if active_transfers:
                _sync_download_progress(active_transfers, downloading)

        for item in downloading:
            try:
                queue_id = item.get("id")

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
                    update_queue_item(
                        queue_id,
                        status="imported",
                        copied_individually=1,
                        copied_individually_at=datetime.now().isoformat(),
                    )
                    stats["imported"] += 1
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
                                _discard_detail: dict[str, Any] = {}
                                is_match, match_source = _file_matches_queue_item(
                                    abs_path, item, detail=_discard_detail
                                )
                                if is_match:
                                    match_found = os.path.relpath(abs_path, downloads_dir)
                                    _claim_file(abs_path, claimed_files, downloads_dir)
                                else:
                                    _delete_mismatched_download(abs_path, queue_id, f"rejected ({match_source})")
                                    _block_peer_for_queue_item(queue_id, found_fn)
                                    _record_discarded_download(
                                        item=item,
                                        queue_id=queue_id,
                                        detail=_discard_detail,
                                        match_source=match_source,
                                        filename=found_fn,
                                    )
                                    mark_failed(queue_id, _queue_reject_failure_reason(match_source))
                                    stats["failed"] += 1
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
                        _stripped = _accent_stripped_key(found_basename)
                        if _stripped and _stripped != found_basename.lower():
                            exact_candidates.extend(fs_index["by_basename"].get(_stripped, []))
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
                        _discard_detail2: dict[str, Any] = {}
                        is_match, match_source = _file_matches_queue_item(
                            candidate, item, rel, detail=_discard_detail2
                        )
                        if not is_match:
                            _delete_mismatched_download(candidate, queue_id, f"rejected for exact filename match ({match_source})")
                            _block_peer_for_queue_item(queue_id, found_fn)
                            _record_discarded_download(
                                item=item,
                                queue_id=queue_id,
                                detail=_discard_detail2,
                                match_source=match_source,
                                filename=rel,
                            )
                            mark_failed(queue_id, _queue_reject_failure_reason(match_source))
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
                            if _cand_base.lower() in _expected_bases or _cand_base in _expected_bases:
                                _delete_mismatched_download(candidate, queue_id, "metadata mismatch")
                                _block_peer_for_queue_item(queue_id, found_fn)
                                mark_failed(queue_id, "Downloaded file did not match queue item; deleted and rescheduled")
                                stats["failed"] += 1
                                match_found = "__rejected__"
                                break
                            continue
                        if meta_state is None:
                            try:
                                from services.queue.queue_scoring import _score_soulseek_candidate
                                score = _score_soulseek_candidate(rel, item, candidate_duration=_extract_duration_seconds(candidate))
                            except Exception:
                                score = 0.0
                            if score < _SLSKD_MIN_ACCEPT_SCORE:
                                if _cand_base.lower() in _expected_bases or _cand_base in _expected_bases:
                                    _delete_mismatched_download(candidate, queue_id, f"below accept threshold (score={score:.2f})")
                                    _block_peer_for_queue_item(queue_id, found_fn)
                                    mark_failed(queue_id, "Downloaded file below acceptance threshold; deleted and rescheduled")
                                    stats["failed"] += 1
                                    match_found = "__rejected__"
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
                    continue

                if match_found is None or abs_path is None:
                    if slskd is not None:
                        _reconciled = _reconcile_transfer_state(item, slskd, active=active_transfers, now=db_now)
                        if _reconciled is True:
                            stats["failed"] += 1
                            continue
                        if isinstance(_reconciled, str) and _reconciled:
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
                                        _claim_file(_landed_path, claimed_files, downloads_dir)
                                    else:
                                        _delete_mismatched_download(_landed_path, queue_id, f"landed file mismatch ({match_source})")
                                        _block_peer_for_queue_item(queue_id, found_fn)
                                        mark_failed(queue_id, "Downloaded file did not match queue item; deleted and rescheduled")
                                        stats["failed"] += 1
                                        continue
                                else:
                                    _delete_mismatched_download(_landed_path, queue_id, "landed file artist mismatch")
                                    _block_peer_for_queue_item(queue_id, found_fn)
                                    mark_failed(queue_id, "Downloaded file artist did not match queue item; deleted and rescheduled")
                                    stats["failed"] += 1
                                    continue
                            else:
                                _landed_cycles = _bump_cycle_counter(
                                    _LANDED_NOT_IMPORTED_CYCLES, queue_id
                                )
                                if _landed_cycles >= _LANDED_NOT_IMPORTED_MAX_CYCLES:
                                    _clear_cycle_counter(_LANDED_NOT_IMPORTED_CYCLES, queue_id)
                                    mark_failed(queue_id, "Slskd file landed but could not be claimed/imported")
                                    stats["failed"] += 1
                                    continue

                    if abs_path is not None and match_found is not None:
                        pass
                    elif _is_stale_queue_item(item, stale_minutes=_STALE_DOWNLOADING_MINUTES, now=db_now):
                        _unconfirmed = _matching_file_exists_unconfirmed(item, fs_files, downloads_dir)
                        if _unconfirmed:
                            stats["skipped"] += 1
                            continue
                            
                        mark_failed(queue_id, "No file found while marked downloading")
                        stats["failed"] += 1
                        continue
                    elif abs_path is None:
                        stats["skipped"] += 1
                    continue

                expected_dur = item.get("duration")
                if expected_dur:
                    expected_dur = queue_duration_seconds(expected_dur)
                if expected_dur:
                    actual_dur = _extract_duration_seconds(abs_path)
                    if actual_dur and abs(expected_dur - actual_dur) > _length_tolerance_seconds():
                        _delete_mismatched_download(abs_path, queue_id, f"duration mismatch: expected {expected_dur}s, got {actual_dur}s")
                        _block_peer_for_queue_item(queue_id, found_fn)
                        mark_failed(queue_id, f"Pre-copy duration mismatch: expected {expected_dur}s, got {actual_dur}s")
                        stats["failed"] += 1
                        continue

                try:
                    update_queue_item(queue_id, found_filename=match_found, file_path=abs_path)
                except Exception:
                    pass

                move_result = _move_and_import(item, abs_path, match_source)
                if move_result.get("success"):
                    stats["imported"] += 1
                else:
                    stats["errors"] += 1

            except Exception:
                stats["errors"] += 1

        return stats

    except Exception as exc:
        logger.error("check_completed_downloads failed", error=str(exc))
        return {"success": False, "error": str(exc), **stats}
