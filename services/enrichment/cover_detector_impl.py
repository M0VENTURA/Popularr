"""
Cover song detection module — implementation class.

Detects cover songs by analyzing songwriter/composer data from MusicBrainz,
then attributes the original artist and updates track metadata accordingly.

Rebuilt with the following corrections:

- ``_detect_via_isrc`` and ``_detect_via_recording_relation`` returned bare
  ``{artist, year, confidence}`` dicts, which ``_build_update`` then indexed
  with ``result["track_id"]``. Every successful high-confidence detection
  raised ``KeyError`` and aborted the whole album. Both paths now return
  fully-formed results.
- ``_resolve_recording_mbid`` called ``search_recordings(recording=..., artist=...)``
  while the client takes a positional Lucene query, so it raised ``TypeError``
  inside a bare ``except`` and always returned ``None``. All searches now use
  one escaped query-string form.
- Cover relation chains are followed to the root, so a cover of a cover
  resolves to the true original rather than the intermediate version.
- ``_find_original_recording`` shared ``earliest``/``earliest_year`` between
  writer-verified and unverified candidates, letting an unverified earlier
  recording overwrite a verified match. The buckets are now separate and
  writer-verified always wins.
- Work-relation candidates are filtered by relation type and cover annotation,
  because every cover of a song links to the same work as the original.
- ``_fetch_writers_from_musicbrainz`` called ``get_release(work_id)``; it now
  calls ``get_work``.
- Cover hints are read from the track title only. Reading the album title
  applied one hint to every track on the album.
- ``is_cover`` matched "cover" as a substring ("Cover Me", "Undercover").
- Writer-detected covers passed the writer-info dict to ``_build_update``,
  so ``file_path`` was always ``None`` and file tags were never written.
- The module-level release cache is bounded and no longer caches failures.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from api_clients.musicbrainz_http import escape_lucene_special_chars
from services.enrichment.musicbrainz_service import get_shared_mb_client
from db.repositories.cover_detection_repository import (
    apply_cover_metadata_batch,
    get_track_genres,
    get_track_writers_from_db,
    is_common_writer_for_artist,
    track_has_original_by_artist,
    writer_coverage_for_artist,
)
from helpers.musicbrainz_helpers import (
    artist_from_credit,
    extract_cover_work_ids,
    extract_work_ids,
    year_from_recording,
)
from helpers.normalization_service import (
    canonical_track_title,
    names_match,
    normalize_writer_credits,
)

logger = structlog.get_logger(__name__)
# Force this specific module to emit DEBUG logs regardless of global config
logging.getLogger(__name__).setLevel(logging.DEBUG)

# Matches a trailing "(X Cover)" annotation in a track title.
_COVER_SUFFIX_RE = re.compile(r"\s*\([^)]+\s+cover\)\s*$", re.IGNORECASE)

# Word-boundary "cover" match, used instead of a bare substring test so that
# "Cover Me", "Undercover" and "Discover" are not treated as annotations.
_COVER_WORD_RE = re.compile(
    r"\((?:[^)]+\s+)?cover\)|\bcover\s+of\b|\boriginally\s+by\b",
    re.IGNORECASE,
)

# =============================================================================
# THREAD-SAFE SHARED CACHE
# =============================================================================
_CACHE_LOCK = threading.Lock()
_RELEASE_TRACKLIST_CACHE: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_RELEASE_TRACKLIST_CACHE_MAX = 512

# Compilation artist names used to decide when per-track-artist lookups are needed.
_COMPILATION_ARTIST_NAMES = frozenset({
    "various artists", "various", "v/a", "va", "compilation", "soundtrack",
})

COVER_RECHECK_DAYS = 90

# Maximum cover-relation hops followed when resolving a cover of a cover.
_MAX_COVER_CHAIN_DEPTH = 6

# Maximum candidate recordings inspected during a work/title search.
_MAX_CANDIDATE_INSPECTIONS = 12

_WRITER_RELATION_TYPES = frozenset({"composer", "writer", "lyricist"})


def _checked_fresh(checked_ts: Any) -> bool:
    """True when the cover-verdict timestamp is within the recheck window."""
    if not checked_ts:
        return False
    try:
        if isinstance(checked_ts, str):
            checked_ts = datetime.fromisoformat(str(checked_ts).replace("Z", "+00:00"))
        if checked_ts.tzinfo is None:
            checked_ts = checked_ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - checked_ts).days < COVER_RECHECK_DAYS
    except Exception:
        return False


def _cache_release(release_mbid: str, release: dict[str, Any]) -> None:
    """Store a release tracklist, bounding the shared cache."""
    if not release_mbid or not release:
        return
    with _CACHE_LOCK:
        _RELEASE_TRACKLIST_CACHE[release_mbid] = release
        _RELEASE_TRACKLIST_CACHE.move_to_end(release_mbid)
        while len(_RELEASE_TRACKLIST_CACHE) > _RELEASE_TRACKLIST_CACHE_MAX:
            _RELEASE_TRACKLIST_CACHE.popitem(last=False)


def _cached_release(release_mbid: str) -> dict[str, Any] | None:
    if not release_mbid:
        return None
    with _CACHE_LOCK:
        release = _RELEASE_TRACKLIST_CACHE.get(release_mbid)
        if release is not None:
            _RELEASE_TRACKLIST_CACHE.move_to_end(release_mbid)
        return release


def _has_cover_annotation(title: str) -> bool:
    return bool(_COVER_SUFFIX_RE.search(title or ""))


# ---------------------------------------------------------------------------
# CoverDetector
# ---------------------------------------------------------------------------

class CoverDetector:
    """Detect and attribute cover songs using MusicBrainz relations and writer/composer data."""

    def __init__(self, db_connection: Any = None):
        # Use the shared client singleton to enforce global rate limits and
        # connection pooling.
        self.mb = get_shared_mb_client()
        self.db_conn = db_connection
        self._band_members_cache: dict[str, list[str]] = {}
        self._mem_lock = threading.Lock()

    # -- search helpers ---------------------------------------------------

    def _search_recordings_by_title(
        self,
        title: str,
        limit: int = 15,
        artist: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search recordings using the client's positional Lucene query form.

        The previous keyword form (``recording=``, ``artist=``) did not match
        the client signature and raised ``TypeError`` inside a bare ``except``,
        so recording lookups silently returned nothing.
        """
        title = str(title or "").strip()
        if not title:
            return []
        query = f'recording:"{escape_lucene_special_chars(title)}"'
        if artist:
            query += f' AND artist:"{escape_lucene_special_chars(str(artist).strip())}"'
        try:
            return self.mb.search_recordings(query, limit=limit) or []
        except Exception as exc:
            logger.debug("Recording search failed", title=title, error=str(exc))
            return []

    def _get_recording(self, mbid: str, inc: str) -> dict[str, Any]:
        try:
            return self.mb.get_recording(mbid, inc=inc) or {}
        except Exception as exc:
            logger.debug("Recording fetch failed", mbid=mbid, error=str(exc))
            return {}

    @staticmethod
    def _result(
        track_id: str,
        title: str,
        core: dict[str, Any],
        writer: str = "",
    ) -> dict[str, Any]:
        """Build a fully-formed cover result.

        The ISRC and recording-relation paths previously returned only
        ``{artist, year, confidence}``, which ``_build_update`` then indexed
        with ``result["track_id"]`` — raising ``KeyError`` and aborting the
        entire album on every successful high-confidence detection.
        """
        return {
            "track_id": track_id,
            "title": title,
            "is_cover": True,
            "original_artist": core.get("artist", ""),
            "original_year": core.get("year"),
            "writer": writer,
            "confidence": core.get("confidence", "medium"),
        }

    # -- album entry point ------------------------------------------------

    def detect_covers_for_album(
        self,
        album: str,
        artist: str,
        tracks: list[dict[str, Any]],
        force: bool = False,
    ) -> list[dict[str, Any]]:
        """Detect cover songs in an album."""
        logger.info(
            "Starting cover detection",
            album=album,
            artist=artist,
            track_count=len(tracks),
        )

        for track in tracks:
            track.setdefault("album", album)
            track.setdefault("album_artist", artist)

        # Keep the source track for every id so updates always carry file_path.
        tracks_by_id: dict[str, dict[str, Any]] = {
            str(t.get("id")): t for t in tracks if t.get("id")
        }

        seen_track_ids: set[str] = set()
        cover_results: list[dict[str, Any]] = []
        pending_updates: list[dict[str, Any]] = []
        _fresh_skipped: set[str] = set()

        if not force:
            checked_map = self._load_cover_checked_map([t.get("id") for t in tracks])
            for track in tracks:
                tid = track.get("id")
                if not tid:
                    continue
                if _checked_fresh(track.get("cover_last_checked") or checked_map.get(tid)):
                    _fresh_skipped.add(tid)
                    seen_track_ids.add(tid)
                    continue
                if self._is_already_confirmed_cover(track) or self._cover_has_original_artist(track):
                    seen_track_ids.add(tid)

        track_writers = self._collect_track_writers(tracks, artist, seen_track_ids)

        def _record(result: dict[str, Any], track: dict[str, Any]) -> None:
            # A track is never a cover OF ITS OWN performer. On a Various
            # Artists compilation the album artist is the placeholder
            # "Various Artists", which matches no real credit, so an
            # album-artist comparison concluded that e.g. P.O.D. was the
            # "original artist" of P.O.D.'s own recording and renamed nine of
            # "Little Nicky"'s twelve tracks to "<title> (P.O.D. Cover)".
            # Checked here, at the single funnel every detection path goes
            # through, so no path can reintroduce it.
            _orig = str(result.get("original_artist") or "").strip()
            _performer = str(track.get("artist") or "").strip()
            if _orig and _performer and names_match(_orig, _performer):
                logger.debug(
                    "Cover result discarded: original equals the track's own artist",
                    track=track.get("title"),
                    artist=_performer,
                )
                return
            cover_results.append(result)
            seen_track_ids.add(result["track_id"])
            pending_updates.append(self._build_update(result, track))

        # Step 2: ISRC-based matching
        for track in tracks:
            tid = track.get("id")
            if not tid or tid in seen_track_ids:
                continue
            isrc = str(track.get("isrc") or "").strip()
            if not isrc:
                continue
            core = self._detect_via_isrc(
                isrc=isrc,
                title=track.get("title", ""),
                album_artist=artist,
                track_artist=str(track.get("artist") or "").strip(),
            )
            if core:
                logger.debug("Cover detected via ISRC", track=track.get("title"), isrc=isrc)
                _record(self._result(tid, track.get("title", ""), core), track)

        # Step 3: MusicBrainz recording-relation detection
        for track in tracks:
            tid = track.get("id")
            if not tid or tid in seen_track_ids:
                continue
            mbid = self._resolve_recording_mbid(track, artist, album)
            if not mbid:
                continue
            track["mbid"] = mbid

            core = self._detect_via_recording_relation(
                recording_mbid=mbid,
                title=track.get("title", ""),
                album_artist=artist,
                release_mbid=str(track.get("musicbrainz_album_mbid") or "").strip(),
            )
            if core:
                logger.debug("Cover detected via recording relation", track=track.get("title"), mbid=mbid)
                _record(self._result(tid, track.get("title", ""), core), track)

        # Step 4: writer-based detection
        is_compilation = artist.lower() in _COMPILATION_ARTIST_NAMES
        writer_skip_all = False

        if not is_compilation and track_writers:
            coverage = writer_coverage_for_artist(self.db_conn, artist)
            if coverage < 0.10:
                logger.info(
                    "Writer coverage low — skipping writer-based detection",
                    artist=artist,
                    coverage_pct=coverage * 100,
                )
                writer_skip_all = True

        track_artist_coverage_cache: dict[str, float] = {}

        for tid, info in track_writers.items():
            if tid in seen_track_ids or writer_skip_all:
                continue

            if is_compilation:
                ta = info.get("track_artist", "") or ""
                if ta not in track_artist_coverage_cache:
                    track_artist_coverage_cache[ta] = writer_coverage_for_artist(self.db_conn, ta)
                if track_artist_coverage_cache[ta] < 0.10:
                    continue

            for writer in info.get("writers", []):
                if self._is_writer_same_as_artist(writer, artist):
                    continue
                if track_has_original_by_artist(self.db_conn, artist, info["title"]):
                    break
                if is_common_writer_for_artist(
                    self.db_conn, writer, artist, track_artist=info.get("track_artist"),
                ):
                    break

                search_title = _COVER_SUFFIX_RE.sub("", info["title"]).strip() or info["title"]
                original = self._find_original_recording(
                    search_title, writer, album_artist=artist
                )
                if not original:
                    continue
                if names_match(original.get("artist", ""), artist):
                    break

                # Pass the source track, not the writer-info dict, so the
                # update carries a usable file_path.
                source = tracks_by_id.get(tid, {})
                logger.debug("Cover detected via writer matching", track=info["title"], writer=writer)
                _record(
                    self._result(tid, info["title"], original, writer=writer),
                    source,
                )
                break

        # Step 5: heuristic title-based detection
        for track in tracks:
            tid = track.get("id")
            if not tid or tid in seen_track_ids:
                continue
            title = track.get("title", "")
            # Album text is deliberately excluded: an album-level annotation
            # previously applied the same hint artist to every track.
            hint = self._detect_cover_hint_from_text(title)
            if not hint:
                continue

            base_title = _COVER_SUFFIX_RE.sub("", title).strip()
            if (
                base_title
                and base_title.lower() != title.lower()
                and track_has_original_by_artist(self.db_conn, artist, base_title)
            ):
                continue

            resolved = {"artist": hint, "year": None, "confidence": "low"}
            resolved_writer = ""
            writers_for_track = (track_writers.get(tid) or {}).get("writers", [])

            if writers_for_track and base_title:
                for wr in writers_for_track:
                    if self._is_writer_same_as_artist(wr, artist):
                        continue
                    mb_orig = self._find_original_recording(base_title, wr, album_artist=artist)
                    if mb_orig and not names_match(mb_orig.get("artist", ""), artist):
                        resolved = mb_orig
                        resolved_writer = wr
                        break

            logger.debug("Cover detected via title hint", track=title, original_artist=resolved.get("artist"))
            _record(self._result(tid, title, resolved, writer=resolved_writer), track)

        # Step 6: work-based fallback
        for track in tracks:
            tid = track.get("id")
            if not tid or tid in seen_track_ids:
                continue
            if not self._is_cover_flagged(track):
                continue
            if (track.get("original_cover_artist") or "").strip():
                continue

            work_mbid = str(track.get("work_mbid") or "").strip()
            if work_mbid:
                logger.debug("Using pre-fetched work_mbid for cover fallback", track=track.get("title"), work_mbid=work_mbid)
                original = self._earliest_work_recording(
                    work_ids={work_mbid},
                    search_title=canonical_track_title(track.get("title", "")) or track.get("title", ""),
                    exclude_mbid=str(track.get("mbid") or track.get("recording_mbid") or ""),
                    album_artist=artist,
                    confidence="medium",
                )
            else:
                mbid = self._resolve_recording_mbid(track, artist, album)
                if not mbid:
                    continue
                track["mbid"] = mbid
                original = self._find_original_via_work_lookup(
                    mbid, track.get("title", ""), artist
                )
                
            if not original:
                continue

            logger.debug("Cover detected via work fallback", track=track.get("title"))
            _record(self._result(tid, track.get("title", ""), original), track)

        if pending_updates:
            successful = set(apply_cover_metadata_batch(self.db_conn, pending_updates))
            for update in pending_updates:
                tid = update.get("track_id")
                if tid not in successful:
                    continue
                fp = update.get("file_path")
                if fp and Path(fp).exists():
                    clean_title = update.get("title", "")
                    self._update_file_metadata(fp, clean_title, ["Cover"])

        self._persist_checked(tracks, _fresh_skipped)

        logger.info("Cover detection complete", album=album, found_count=len(cover_results))
        return cover_results

    def _persist_checked(self, tracks: list[dict[str, Any]], skipped: set[str]) -> None:
        try:
            assessed = [
                str(t.get("id"))
                for t in tracks
                if t.get("id") and str(t.get("id")) not in skipped
            ]
            if not assessed:
                return
            from sqlalchemy import text as _text
            from db.engine import db_session as _db_session

            placeholders = ", ".join(f":tid{i}" for i in range(len(assessed)))
            with _db_session() as session:
                session.execute(
                    _text(
                        f"UPDATE tracks SET cover_last_checked = :checked "
                        f"WHERE id IN ({placeholders})"
                    ),
                    {
                        "checked": datetime.now(timezone.utc).isoformat(),
                        **{f"tid{i}": t for i, t in enumerate(assessed)},
                    },
                )
        except Exception as exc:
            logger.debug("cover_last_checked persist failed", error=str(exc))

    # -- quick check ------------------------------------------------------

    def is_cover(
        self,
        title: str,
        artist: str,
        composer: str | None = None,
        writer: str | None = None,
    ) -> bool:
        if not title:
            return False
        if _COVER_SUFFIX_RE.search(title):
            return True
        # Word-boundary match; a bare "cover" substring matched "Cover Me",
        # "Undercover" and "Discover".
        if _COVER_WORD_RE.search(title):
            return True
        # NOTE: a songwriter differing from the performer is normal for most
        # commercially released music and is not on its own evidence of a
        # cover. It is returned only as weak corroboration.
        if composer or writer:
            for candidate in (composer, writer):
                if candidate and not names_match(candidate, artist):
                    return False
        return False

    # -- ISRC -------------------------------------------------------------

    def _detect_via_isrc(
        self,
        isrc: str,
        title: str,
        album_artist: str,
        track_artist: str = "",
    ) -> dict[str, Any] | None:
        """Resolve an original via recordings sharing the track's ISRC.

        An ISRC identifies one specific recording, so a differently-credited
        recording sharing it is weaker evidence than a modelled cover
        relation. Confidence is capped at medium and a known year is required.

        ⚠️ The credit to compare against is the TRACK's own performer, not the
        album artist. On a Various Artists compilation the album artist is the
        placeholder "Various Artists", which matches no real credit — so the
        comparison always passed, and every ISRC-bearing track on "Little
        Nicky" was reported as a cover of its own artist (P.O.D., Incubus,
        Deftones, ...), renaming 9 of 12 tracks to "<title> (P.O.D. Cover)".
        """
        performer = str(track_artist or "").strip() or str(album_artist or "").strip()
        try:
            recordings = self.mb.lookup_by_isrc(
                isrc, inc="artist-credits+releases+work-rels+recording-rels"
            )
            if not recordings:
                return None

            earliest: dict[str, Any] | None = None
            earliest_year = 9999

            for rec in recordings:
                if not rec.get("id"):
                    continue
                rec_artist = artist_from_credit(rec.get("artist-credit", []))
                if not rec_artist or names_match(rec_artist, performer):
                    continue
                # Skip candidates that are themselves annotated as covers.
                if _has_cover_annotation(str(rec.get("title") or "")):
                    continue
                year = year_from_recording(rec)
                if year is not None and year < earliest_year:
                    earliest_year = year
                    earliest = {"artist": rec_artist, "year": year, "confidence": "medium"}

            if earliest:
                logger.info(
                    "ISRC-based cover detected",
                    track=title,
                    original_artist=earliest["artist"],
                )
            return earliest
        except Exception as exc:
            logger.debug("ISRC lookup failed", isrc=isrc, error=str(exc))
            return None

    # -- recording relations ----------------------------------------------

    def _cover_relation_target(
        self,
        recording: dict[str, Any],
        recording_mbid: str,
        album_artist: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Return the recording this one is a cover *of*, if modelled."""
        for rel in recording.get("recording-relation-list", []) or []:
            if not isinstance(rel, dict):
                continue
            rt = str(rel.get("type", "")).strip().lower()
            direction = str(rel.get("direction", "forward")).strip().lower()
            # Forward only: a backward "cover" relation means the target is a
            # cover of this recording, not its original.
            if rt != "cover" or direction != "forward":
                continue
            orig_rec = rel.get("recording") or {}
            orig_id = str(orig_rec.get("id") or "")
            if not orig_id or orig_id == recording_mbid:
                continue

            orig_artist = artist_from_credit(orig_rec.get("artist-credit", []))
            orig_year = year_from_recording(orig_rec)
            if not orig_artist or orig_year is None:
                details = self._get_recording(orig_id, "artist-credits+releases")
                if details:
                    orig_artist = orig_artist or artist_from_credit(
                        details.get("artist-credit", [])
                    )
                    if orig_year is None:
                        orig_year = year_from_recording(details)
            if not orig_artist:
                continue
            if album_artist and names_match(orig_artist, album_artist):
                continue
            return (
                {"artist": orig_artist, "year": orig_year, "confidence": "high"},
                orig_id,
            )
        return None, None

    def _resolve_cover_chain(
        self,
        recording_mbid: str,
        album_artist: str,
    ) -> dict[str, Any] | None:
        """Follow cover relations to the root of the chain.

        A cover relation can point at another cover. Following only one hop
        attributes the track to the intermediate version rather than the
        original, which is the primary cause of cover-of-a-cover results.
        """
        visited: set[str] = {recording_mbid}
        current_id = recording_mbid
        best: dict[str, Any] | None = None

        for _ in range(_MAX_COVER_CHAIN_DEPTH):
            recording = self._get_recording(
                current_id, "recording-rels+artist-credits+releases"
            )
            if not recording:
                break
            core, next_id = self._cover_relation_target(
                recording, current_id, album_artist
            )
            if not core or not next_id or next_id in visited:
                break
            visited.add(next_id)
            best = core
            current_id = next_id

        if best:
            logger.debug(
                "Cover chain resolved",
                seed=recording_mbid,
                hops=len(visited) - 1,
                original_artist=best.get("artist"),
            )
        return best

    def _detect_via_recording_relation(
        self,
        recording_mbid: str,
        title: str,
        album_artist: str,
        release_mbid: str = "",
        _depth: int = 0,
    ) -> dict[str, Any] | None:
        try:
            seed = self._get_recording(
                recording_mbid, "work-rels+recording-rels+artist-credits+releases"
            )
            if not seed:
                return None

            # Modelled cover relations are the most reliable signal. Follow the
            # chain so a cover of a cover resolves to the true original.
            chain_result = self._resolve_cover_chain(recording_mbid, album_artist)

            cover_work_ids = extract_cover_work_ids(seed)
            if chain_result and not cover_work_ids:
                return chain_result

            if release_mbid and _depth == 0:
                result = self._check_release_for_cover(
                    release_mbid, title, recording_mbid, album_artist, _depth=_depth + 1
                )
                if result:
                    return result

            if not cover_work_ids:
                return chain_result

            search_title = (
                seed.get("title") or _COVER_SUFFIX_RE.sub("", title).strip() or title
            )
            candidate = self._earliest_work_recording(
                work_ids=cover_work_ids,
                search_title=search_title,
                exclude_mbid=recording_mbid,
                album_artist=album_artist,
                confidence="high",
            )
            return candidate or chain_result
        except Exception as exc:
            logger.debug(
                "Recording-relation detection failed",
                mbid=recording_mbid,
                error=str(exc),
            )
            return None

    def _earliest_work_recording(
        self,
        work_ids: set[str],
        search_title: str,
        exclude_mbid: str,
        album_artist: str | None,
        confidence: str,
    ) -> dict[str, Any] | None:
        """Find the earliest non-cover recording of the given work(s).

        Every cover of a song is a *performance* of the same work, so work-id
        overlap alone cannot separate originals from covers. Candidates are
        additionally filtered on relation type, cover annotation and cover
        work ids, then ranked by earliest known release year.
        """
        recordings = self._search_recordings_by_title(search_title, limit=15)
        if not recordings:
            return None

        earliest: dict[str, Any] | None = None
        earliest_year = 9999
        inspected = 0

        for rec in recordings:
            if inspected >= _MAX_CANDIDATE_INSPECTIONS:
                break
            rid = rec.get("id")
            if not rid or rid == exclude_mbid:
                continue
            inspected += 1

            details = self._get_recording(rid, "artist-credits+releases+work-rels")
            if not details:
                continue

            if not (extract_work_ids(details) & work_ids):
                continue
            # Exclude candidates that are themselves covers of this work.
            if extract_cover_work_ids(details) & work_ids:
                logger.debug("Skipping candidate: Already marked as cover of this work", mbid=rid)
                continue
            if _has_cover_annotation(str(details.get("title") or rec.get("title") or "")):
                logger.debug("Skipping candidate: Already annotated as cover", mbid=rid)
                continue

            rec_artist = artist_from_credit(
                details.get("artist-credit", []) or rec.get("artist-credit", [])
            )
            if not rec_artist:
                continue
            if album_artist and names_match(rec_artist, album_artist):
                logger.debug("Skipping candidate: Artist matches current", mbid=rid)
                continue

            # A candidate without a year cannot be shown to precede anything,
            # so it is not eligible to be called the original.
            year = year_from_recording(details)
            if year is None or year >= earliest_year:
                logger.debug("Skipping candidate: Missing year or not earlier", mbid=rid, year=year, earliest_year=earliest_year)
                continue

            earliest_year = year
            earliest = {"artist": rec_artist, "year": year, "confidence": confidence}

        return earliest

    def _check_release_for_cover(
        self,
        release_mbid: str,
        track_title: str,
        recording_mbid: str | None,
        album_artist: str | None,
        _depth: int = 0,
    ) -> dict[str, Any] | None:
        release = _cached_release(release_mbid)
        if release is None:
            try:
                release = self.mb.get_release(
                    release_mbid, inc="recordings+artist-credits"
                ) or {}
            except Exception:
                return None
            # Only cache a non-empty response so a failure is retried.
            if release:
                _cache_release(release_mbid, release)

        canonical_target = canonical_track_title(track_title)
        for medium in release.get("media", []) or []:
            for mb_track in medium.get("tracks", []) or []:
                rec = mb_track.get("recording") or {}
                candidate = rec.get("title") or mb_track.get("title") or ""
                if canonical_track_title(candidate) != canonical_target:
                    continue
                release_rec_id = str(rec.get("id") or "").strip()
                if not release_rec_id or release_rec_id == (recording_mbid or "").strip():
                    continue
                return self._detect_via_recording_relation(
                    recording_mbid=release_rec_id,
                    title=track_title,
                    album_artist=album_artist or "",
                    _depth=_depth,
                )
        return None

    # -- heuristics -------------------------------------------------------

    def _detect_cover_hint_from_text(self, track_title: str) -> str | None:
        """Extract an original-artist hint from the track title only.

        The album title is deliberately not consulted: an album-level
        annotation applied the same hint artist to every track on the album.
        """
        text = str(track_title or "").strip()
        if not text:
            return None
        patterns = [
            r"\(([^)]+?)\s+cover\)",
            r"originally\s+by\s+([\w\s'&\-.]+)",
            r"cover\s+of\s+([\w\s'&\-.]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                artist_name = match.group(1).strip(" -.,")
                if artist_name:
                    logger.debug("Cover hint extracted from title", track_title=track_title, extracted_artist=artist_name)
                    return artist_name
        return None

    def _find_original_recording(
        self,
        title: str,
        writer: str,
        album_artist: str | None = None,
    ) -> dict[str, Any] | None:
        """Find the earliest recording of a title credited to the given writer.

        Writer-verified and unverified candidates are tracked separately.
        Previously both wrote to the same ``earliest``/``earliest_year``
        variables, so an unverified recording with an earlier year could
        overwrite a writer-verified match and become the reported original.
        """
        try:
            search_title = canonical_track_title(title) or title
            recordings = self._search_recordings_by_title(search_title, limit=10)
            if not recordings:
                return None

            verified: dict[str, Any] | None = None
            verified_year = 9999
            unverified: dict[str, Any] | None = None
            unverified_year = 9999
            inspected = 0

            for rec in recordings:
                if inspected >= 8:
                    break
                rid = rec.get("id")
                if not rid:
                    continue
                inspected += 1

                details = self._get_recording(
                    rid, "artist-credits+releases+work-rels+artist-rels"
                )
                if not details:
                    continue

                writer_names = self._writers_from_recording(details)
                writer_match = any(names_match(writer, c) for c in writer_names)

                rec_artist = artist_from_credit(
                    details.get("artist-credit", []) or rec.get("artist-credit", [])
                )
                if not rec_artist:
                    continue
                if album_artist and names_match(rec_artist, album_artist):
                    continue
                if _has_cover_annotation(str(details.get("title") or rec.get("title") or "")):
                    continue

                year = year_from_recording(details)
                if year is None:
                    logger.debug("Skipping candidate: Missing year", mbid=rid)
                    continue

                if writer_match:
                    if year < verified_year:
                        verified_year = year
                        verified = {
                            "artist": rec_artist,
                            "year": year,
                            "confidence": "medium",
                        }
                elif year < unverified_year:
                    unverified_year = year
                    unverified = {
                        "artist": rec_artist,
                        "year": year,
                        "confidence": "low",
                    }

            # A writer-verified match always wins, regardless of year.
            return verified or unverified
        except Exception as exc:
            logger.debug(
                "Original recording lookup failed",
                track=title,
                writer=writer,
                error=str(exc),
            )
            return None

    @staticmethod
    def _writers_from_recording(details: dict[str, Any]) -> list[str]:
        writer_names: list[str] = []
        for rel in details.get("artist-relation-list", []) or []:
            if str(rel.get("type", "")).lower() in _WRITER_RELATION_TYPES:
                name = (rel.get("artist") or {}).get("name")
                if name:
                    writer_names.append(name)
        for rel in details.get("work-relation-list", []) or []:
            work = rel.get("work") or {}
            for work_rel in work.get("artist-relation-list", []) or []:
                if str(work_rel.get("type", "")).lower() in _WRITER_RELATION_TYPES:
                    name = (work_rel.get("artist") or {}).get("name")
                    if name:
                        writer_names.append(name)
        return normalize_writer_credits(writer_names)

    def _find_original_via_work_lookup(
        self,
        recording_mbid: str,
        title: str,
        album_artist: str | None = None,
    ) -> dict[str, Any] | None:
        try:
            seed = self._get_recording(recording_mbid, "work-rels+artist-credits")
            if not seed:
                return None

            work_ids: set[str] = set()
            for rel in seed.get("work-relation-list", []) or []:
                if not isinstance(rel, dict):
                    continue
                if str(rel.get("type", "")).strip().lower() != "performance":
                    continue
                wid = (rel.get("work") or {}).get("id")
                if wid:
                    work_ids.add(wid)

            if not work_ids:
                return None

            return self._earliest_work_recording(
                work_ids=work_ids,
                search_title=canonical_track_title(title) or title,
                exclude_mbid=recording_mbid,
                album_artist=album_artist,
                confidence="medium",
            )
        except Exception as exc:
            logger.debug("Work-based fallback failed", mbid=recording_mbid, error=str(exc))
            return None

    # -- writers ----------------------------------------------------------

    def _load_cover_checked_map(self, track_ids: list[Any]) -> dict[str, Any]:
        ids = [str(t) for t in track_ids if t]
        if not ids:
            return {}
        try:
            from sqlalchemy import text as _text
            from db.engine import db_session as _db_session

            placeholders = ", ".join(f":tid{i}" for i in range(len(ids)))
            with _db_session() as session:
                rows = session.execute(
                    _text(
                        f"SELECT id, cover_last_checked FROM tracks "
                        f"WHERE id IN ({placeholders})"
                    ),
                    {f"tid{i}": t for i, t in enumerate(ids)},
                ).fetchall() or []
            return {str(r[0]): r[1] for r in rows}
        except Exception as exc:
            logger.debug("cover_last_checked load failed", error=str(exc))
            return {}

    def _collect_track_writers(
        self,
        tracks: list[dict[str, Any]],
        album_artist: str,
        seen_track_ids: set[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        seen_track_ids = seen_track_ids or set()
        result: dict[str, dict[str, Any]] = {}
        for track in tracks:
            tid = track.get("id")
            if not tid or tid in seen_track_ids:
                continue
            writers = self._get_track_writers(track)
            if writers:
                result[tid] = {
                    "title": track.get("title", "Unknown"),
                    "writers": writers,
                    "mbid": track.get("mbid"),
                    "track_artist": track.get("artist")
                    or track.get("album_artist")
                    or album_artist,
                }
        return result

    def _get_track_writers(self, track: dict[str, Any]) -> list[str]:
        writers: list[str] = []

        # Accept both the writer and composer columns; the artist-scan path
        # previously supplied composer data under the writer key.
        for key in ("writer", "composer"):
            raw = track.get(key)
            if not raw:
                continue
            if isinstance(raw, list):
                writers.extend(str(w) for w in raw if w)
            elif isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        writers.extend(str(w) for w in parsed if w)
                    elif parsed:
                        writers.append(str(parsed))
                except (json.JSONDecodeError, TypeError, ValueError):
                    writers.extend(
                        part.strip() for part in re.split(r"[;,/]", raw) if part.strip()
                    )

        if not writers:
            writers = get_track_writers_from_db(None, track.get("id", "")) or []

        if not writers:
            mbid = track.get("mbid") or self._resolve_recording_mbid(
                track,
                album_artist=track.get("album_artist") or track.get("artist"),
                album_title=track.get("album"),
            )
            if mbid:
                track["mbid"] = mbid
                writers = self._fetch_writers_from_musicbrainz(mbid)

        return normalize_writer_credits(writers)

    def _fetch_writers_from_musicbrainz(self, mbid: str) -> list[str]:
        try:
            recording = self._get_recording(mbid, "artist-rels+work-rels")
            if not recording:
                return []

            writers: list[str] = []
            for rel in recording.get("artist-relation-list", []) or []:
                if str(rel.get("type", "")).lower() in _WRITER_RELATION_TYPES:
                    name = (rel.get("artist") or {}).get("name")
                    if name and name not in writers:
                        writers.append(name)

            for rel in recording.get("work-relation-list", []) or []:
                work = rel.get("work") or {}
                wid = work.get("id")
                work_rels = work.get("artist-relation-list", [])
                if wid and not work_rels:
                    # Previously called get_release() with a work id, which
                    # queries the wrong entity and never returns writers.
                    try:
                        work_details = self.mb.get_work(wid, inc="artist-rels") or {}
                        work_rels = work_details.get("artist-relation-list", [])
                    except Exception as exc:
                        logger.debug("Work lookup failed", work_id=wid, error=str(exc))
                        work_rels = []
                for work_rel in work_rels or []:
                    if str(work_rel.get("type", "")).lower() in _WRITER_RELATION_TYPES:
                        name = (work_rel.get("artist") or {}).get("name")
                        if name and name not in writers:
                            writers.append(name)
            return writers
        except Exception as exc:
            logger.debug("Failed to fetch writers from MB", mbid=mbid, error=str(exc))
            return []

    # -- mbid resolution --------------------------------------------------

    def _resolve_recording_mbid(
        self,
        track: dict[str, Any],
        album_artist: str | None = None,
        album_title: str | None = None,
    ) -> str | None:
        del album_title  # retained for signature compatibility

        existing = str(track.get("mbid") or "").strip()
        if existing:
            return existing

        title = str(track.get("title") or "").strip()
        if not title:
            return None

        search_artist = str(track.get("artist") or album_artist or "").strip()
        release_mbid = str(track.get("musicbrainz_album_mbid") or "").strip()

        def _titles_match(left: str, right: str) -> bool:
            lt = canonical_track_title(left)
            rt = canonical_track_title(right)
            if not lt or not rt:
                return False
            return lt == rt or lt.startswith(rt) or rt.startswith(lt)

        def _artist_matches(artist_credit: list[dict[str, Any]]) -> bool:
            if not search_artist:
                return True
            return names_match(artist_from_credit(artist_credit), search_artist)

        if release_mbid:
            release = _cached_release(release_mbid)
            if release is None:
                try:
                    release = self.mb.get_release(
                        release_mbid, inc="recordings+artist-credits"
                    ) or {}
                except Exception as exc:
                    logger.debug(
                        "Release lookup failed", release_mbid=release_mbid, error=str(exc)
                    )
                    release = {}
                if release:
                    _cache_release(release_mbid, release)

            for medium in release.get("media", []) or []:
                for mb_track in medium.get("tracks", []) or []:
                    rec = mb_track.get("recording") or {}
                    ct = rec.get("title") or mb_track.get("title") or ""
                    if not _titles_match(title, ct):
                        continue
                    if _artist_matches(
                        rec.get("artist-credit", []) or mb_track.get("artist-credit", [])
                    ):
                        resolved = str(rec.get("id") or "").strip()
                        if resolved:
                            return resolved

        for rec in self._search_recordings_by_title(
            title, limit=10, artist=search_artist or None
        ):
            if not _titles_match(title, rec.get("title", "")):
                continue
            if not _artist_matches(rec.get("artist-credit", []) or []):
                continue
            resolved = str(rec.get("id") or "").strip()
            if resolved:
                return resolved
        return None

    # -- band members -----------------------------------------------------

    def _get_band_members(self, artist: str) -> list[str]:
        with self._mem_lock:
            cached = self._band_members_cache.get(artist)
        if cached is not None:
            return cached

        members: list[str] = []
        try:
            results = self.mb.search_artists(
                f'artist:"{escape_lucene_special_chars(artist)}"', limit=3
            ) or []
            for result in results:
                if not names_match(result.get("name", ""), artist):
                    continue
                artist_mbid = result.get("id")
                if not artist_mbid:
                    continue
                details = self.mb.get_artist(artist_mbid, inc="artist-rels") or {}
                for rel in details.get("artist-relation-list", []) or []:
                    if str(rel.get("type", "")).lower() == "member of band":
                        name = (rel.get("artist") or {}).get("name")
                        if name and name not in members:
                            members.append(name)
                break
        except Exception as exc:
            logger.debug("Failed to fetch band members", artist=artist, error=str(exc))

        with self._mem_lock:
            self._band_members_cache[artist] = members
        return members

    def _is_writer_same_as_artist(self, writer: str, artist: str) -> bool:
        if names_match(writer, artist):
            return True
        return any(names_match(writer, member) for member in self._get_band_members(artist))

    # -- state helpers ----------------------------------------------------

    def _is_already_confirmed_cover(self, track: dict[str, Any]) -> bool:
        """True when the track is flagged as a cover and tagged accordingly.

        The title suffix is no longer required, so a confirmed cover whose
        title was never rewritten is still recognised.
        """
        if not self._is_cover_flagged(track):
            return False

        def _has_cover_genre(raw: Any) -> bool:
            if not raw:
                return False
            if isinstance(raw, list):
                return any(str(g).strip().lower() == "cover" for g in raw)
            raw = str(raw)
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return any(str(g).strip().lower() == "cover" for g in parsed)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
            return any(
                part.strip().lower() == "cover" for part in re.split(r"[;,]", raw)
            )

        if _has_cover_genre(track.get("genres")) or _has_cover_genre(
            track.get("musicbrainz_genres")
        ):
            return True
        if _has_cover_annotation(track.get("title", "")):
            return True
        if self.db_conn:
            db_genres = get_track_genres(self.db_conn, track.get("id", "")) or {}
            if _has_cover_genre(db_genres.get("genres")) or _has_cover_genre(
                db_genres.get("musicbrainz_genres")
            ):
                return True
        return False

    @staticmethod
    def _is_cover_flagged(track: dict[str, Any]) -> bool:
        val = track.get("is_cover")
        if val in (None, 0, False):
            return False
        if isinstance(val, str) and val.strip().lower() in ("0", "false", "no", ""):
            return False
        return bool(val)

    @staticmethod
    def _cover_has_original_artist(track: dict[str, Any]) -> bool:
        if not track:
            return False
        if track.get("cover_manual_override"):
            return True
        if not CoverDetector._is_cover_flagged(track):
            return False
        return bool(str(track.get("original_cover_artist") or "").strip())

    # -- persistence ------------------------------------------------------

    @staticmethod
    def _build_update(result: dict[str, Any], track: dict[str, Any]) -> dict[str, Any]:
        original_artist = result.get("original_artist", "")
        
        # Actively strip out the " (X Cover)" suffix if it was previously added by the scanner
        raw_title = track.get("title", "")
        clean_title = _COVER_SUFFIX_RE.sub("", raw_title).strip()
        
        return {
            "track_id": result["track_id"],
            "title": clean_title,
            "original_artist": original_artist,
            "original_year": result.get("original_year"),
            "writer": result.get("writer", ""),
            "confidence": result.get("confidence", "low"),
            "is_cover": True,
            "file_path": (track or {}).get("file_path"),
            "is_cover_reason": (
                f"Cover detection: {result.get('confidence', 'low')} confidence, "
                f"originally by {original_artist or 'unknown'}"
            ),
        }

    @staticmethod
    def _update_file_metadata(
        file_path: str, title: str, additional_genres: list[str]
    ) -> bool:
        try:
            from services.metadata.tag_file_service import write_tags_to_file

            tags: dict[str, Any] = {"title": title}
            if additional_genres:
                tags["genre"] = "; ".join(additional_genres)
            return write_tags_to_file(file_path, tags)
        except Exception as exc:
            logger.error(
                "Failed to update file metadata", file_path=file_path, error=str(exc)
            )
            return False
