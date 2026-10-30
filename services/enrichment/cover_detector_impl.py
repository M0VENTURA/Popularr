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
- Originals covered by later artists (e.g. AFI's "Totalimmortal" covered by
  The Offspring, or "The Boy Who Destroyed the World" covered by Punk Goes
  8-Bit) no longer get flipped into covers of their own cover bands:
  - ``_detect_via_work_id`` checks if the performer (or a band member / alias)
    wrote the work, seeds ``performer_key`` with the track's known release year,
    and treats same-or-earlier performer year as a conclusive negative.
  - ``_original_by_writer`` tracks when the performer is a writer-linked
    candidate and aborts if the performer recorded it first.
  - ``_find_original_recording`` and ``_earliest_work_recording`` track the
    performer's own earliest recording year and exclude candidates flagged
    with cover work relations.
  - ``_get_band_members`` no longer caches empty lists on transient failures.
  - ``_clear_unconfirmed_verdicts`` also strips stale ``(X Cover)`` suffixes
    from both the DB title and the audio file's TITLE tag when clearing a
    false cover verdict, and deletes the file's ``IS_COVER`` /
    ``ORIGINAL_COVER_ARTIST`` tags so a later Navidrome import cannot restore
    the flag.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import OrderedDict
from datetime import datetime, timezone
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
    is_track_artist_placeholder,
    names_match,
    normalize_unicode_punctuation,
    normalize_writer_credits,
)

logger = structlog.get_logger(__name__)
# NOTE: the module used to force itself to DEBUG here regardless of the
# configured level, flooding the file logs on every scan. Verbosity now
# follows the global logging config like every other module.

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

#: Per-detector-instance cap for the cached work -> recordings browse.
_WORK_RECORDING_CACHE_MAX = 64

#: Sentinel so a cached EMPTY browse (inconclusive) is not re-fetched.
_MISS = object()

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


def _extract_track_year(track: dict[str, Any]) -> int | None:
    """Extract a valid 4-digit release year from a track dict, if present."""
    for field in ("original_year", "year", "release_year", "date", "release_date"):
        raw = track.get(field)
        if not raw:
            continue
        s = str(raw).strip()
        if len(s) >= 4 and s[:4].isdigit():
            yr = int(s[:4])
            if 1900 <= yr <= 2100:
                return yr
    return None


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


_REL_LIST_KEY = {
    "recording": "recording-relation-list",
    "work": "work-relation-list",
    "artist": "artist-relation-list",
    "release": "release-relation-list",
    "release_group": "release_group-relation-list",
}


def _ngs_relations(entity: Any, _depth: int = 0) -> Any:
    """Give a MusicBrainz JSON entity the relation keys this module reads.

    ⚠️⚠️ THE REASON COVER DETECTION FOUND NOTHING. This module was written
    against the musicbrainzngs XML shape (``recording-relation-list``,
    ``work-relation-list``, ``artist-relation-list``, ``attribute-list``), but
    the shared client returns the JSON web service, which puts EVERY relation
    in one ``relations`` list tagged with ``target-type`` (see
    ``musicbrainz_service``, which reads ``recording.get("relations")``). So
    every relation lookup here read an empty list: no "cover of" recording
    relation, no work relation, no writers — the reported "0/20 settled" after
    every step, even for Feuerschwanz's "Gangnam Style", which MusicBrainz
    states is a "cover recording of 강남스타일".

    Builds the ngs-style lists from ``relations`` (keeping ``relations``
    intact) and recurses into the nested work/recording/artist, so both shapes
    and both helper styles work. Entities already in ngs shape pass through.
    Returns a COPY; the input is never mutated (it may be a cached object).
    """
    if not isinstance(entity, dict) or _depth > 3:
        return entity
    out = dict(entity)
    relations = out.get("relations")
    if not isinstance(relations, list):
        return out
    buckets: dict[str, list[dict[str, Any]]] = {}
    normalised: list[dict[str, Any]] = []
    for rel in relations:
        if not isinstance(rel, dict):
            continue
        rel = dict(rel)
        attrs = rel.get("attributes")
        if isinstance(attrs, list) and "attribute-list" not in rel:
            rel["attribute-list"] = list(attrs)
        target_type = str(rel.get("target-type") or "").strip().replace("-", "_")
        if not target_type:
            for candidate in ("recording", "work", "artist", "release"):
                if isinstance(rel.get(candidate), dict):
                    target_type = candidate
                    break
        nested = rel.get(target_type.replace("_", "-")) if target_type else None
        if isinstance(nested, dict):
            rel[target_type.replace("_", "-")] = _ngs_relations(nested, _depth + 1)
        key = _REL_LIST_KEY.get(target_type)
        if key:
            buckets.setdefault(key, []).append(rel)
        normalised.append(rel)
    out["relations"] = normalised
    for key, rels in buckets.items():
        if not out.get(key):
            out[key] = rels
    return out


def _rel_attributes(rel: dict[str, Any]) -> set[str]:
    raw = rel.get("attribute-list") or rel.get("attributes") or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(a).strip().casefold() for a in raw if str(a).strip()}


def _cover_work_ids_local(recording: dict[str, Any]) -> set[str]:
    """Works this recording is a COVER performance of ("cover recording of").

    Read directly from the relation attributes, in either shape, so the
    verdict never depends on which shape ``extract_cover_work_ids`` expects.
    """
    found: set[str] = set()
    rec = _ngs_relations(recording)
    for rel in (rec or {}).get("work-relation-list") or []:
        if not isinstance(rel, dict):
            continue
        if str(rel.get("type") or "").strip().casefold() not in {"performance", "recording of"}:
            continue
        if "cover" not in _rel_attributes(rel):
            continue
        work_id = str((rel.get("work") or {}).get("id") or "").strip()
        if work_id:
            found.add(work_id)
    return found


def _work_ids_local(recording: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    rec = _ngs_relations(recording)
    for rel in (rec or {}).get("work-relation-list") or []:
        if not isinstance(rel, dict):
            continue
        work_id = str((rel.get("work") or {}).get("id") or "").strip()
        if work_id:
            found.add(work_id)
    return found


def _all_cover_work_ids(recording: dict[str, Any]) -> set[str]:
    ids = _cover_work_ids_local(recording)
    try:
        ids |= set(extract_cover_work_ids(recording) or ())
    except Exception:
        pass
    return ids


def _all_work_ids(recording: dict[str, Any]) -> set[str]:
    ids = _work_ids_local(recording)
    try:
        ids |= set(extract_work_ids(recording) or ())
    except Exception:
        pass
    return ids


def _release_date_key(rec: dict[str, Any]) -> str:
    """Earliest known release date as a sortable "YYYY-MM-DD" string.

    Comparing YEARS alone tied two 1997 recordings of "My Heart Will Go On"
    and two 2013 recordings of "Wake Me Up", and each tie went to the wrong
    artist. A missing month/day sorts last within its year; no date at all
    sorts after everything ("9999-99-99").
    """
    dates: list[str] = []
    for value in (rec.get("first-release-date"), rec.get("date")):
        if value:
            dates.append(str(value))
    for rel in rec.get("releases") or []:
        if isinstance(rel, dict) and rel.get("date"):
            dates.append(str(rel["date"]))
    keys: list[str] = []
    for d in dates:
        d = d.strip()
        if len(d) >= 4 and d[:4].isdigit():
            parts = (d.split("-") + ["99", "99"])[:3]
            parts = [p.zfill(2) if i else p for i, p in enumerate(parts)]
            keys.append("-".join(parts))
    return min(keys) if keys else "9999-99-99"


def _undated(key: str) -> bool:
    return key.startswith("9999")


def log_unified_safe(message: str) -> None:
    """Write to the scan log the operator watches; never raises."""
    try:
        from helpers.logging_config import log_unified

        log_unified(message)
    except Exception:
        logger.info(message)


def _has_cover_annotation(title: str) -> bool:
    return bool(_COVER_SUFFIX_RE.search(title or ""))


def _same_artist(left: str, right: str) -> bool:
    """True when two credits name the same artist.

    ``names_match`` alone is not enough for the self-credit guard: it is
    punctuation- and accent-SENSITIVE, so a diacritic difference in the SAME
    name ("Ünloco" in the library vs "Unloco" on MusicBrainz) compared as two
    different artists and the track was recorded as a cover of itself. The
    Unicode-punctuation fold plus a diacritic fold (``normalize_string``)
    closes that without merging genuinely different names.
    """
    if not left or not right:
        return False
    if names_match(left, right):
        return True
    try:
        from helpers.normalization_service import normalize_string

        _l = normalize_string(normalize_unicode_punctuation(left))
        _r = normalize_string(normalize_unicode_punctuation(right))
        return bool(_l) and _l == _r
    except Exception:
        return False


#: Album-level artist values that name a COMPILATION rather than a performer.
#: NOT redefined here — ``is_track_artist_placeholder`` owns that list, and this
#: module already had a second copy (``_COMPILATION_ARTIST_NAMES``) that had
#: drifted from it. One list, one source of truth.


def comparison_artist(track: dict[str, Any], album_artist: str) -> str:
    """The artist this row's cover verdict must be measured AGAINST.

    ⭐⭐ THE BUG THIS EXISTS FOR. A cover verdict is always a statement about a
    PERFORMER: "this recording is not by whoever wrote/released it first". On a
    Various Artists compilation the ALBUM artist is the placeholder
    ``"Various Artists"``, which matches no real credit — so every comparison
    made against it is vacuous:

    * ``_same_artist(candidate, "Various Artists")`` is never true, so the
      performer's OWN recording is never excluded from the candidate set and the
      "the performer released it first" conclusive negative can never fire;
    * ``_performer_wrote_work(work, "Various Artists")`` returns False by its
      own placeholder guard, so a track artist who WROTE the song is never
      recognised as the originator.

    Measured on "The Best 80's Car Songs…": Crowded House's own "Don't Dream
    It's Over" — which they wrote and released in 1986 — was attributed to a
    later cover by "(Denise Jannah Cover)", because the comparison artist was
    the album's "Various Artists" and nothing about Crowded House was ever
    consulted.

    The TRACK artist is the performer. The album artist is only a fallback for
    a single-artist release (where the two are the same anyway). Returns ``""``
    when neither names a real performer, which callers treat as "no performer to
    compare against" — never as a match.
    """
    performer = str(track.get("artist") or "").strip()
    if performer and not is_track_artist_placeholder(performer):
        return performer
    fallback = str(album_artist or "").strip()
    if fallback and not is_track_artist_placeholder(fallback):
        return fallback
    return ""


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
        self._artist_band_cache: dict[str, list[dict[str, str]]] = {}
        self._work_recording_cache: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
        self._mem_lock = threading.Lock()

    @staticmethod
    def _track_has_logged_identity(track: dict[str, Any]) -> bool:
        """True when the track row carries ANY MusicBrainz identity the scan logged.

        The metadata scan writes ``recording_mbid``/``mbid``/``musicbrainz_trackid``
        and (since the persist fix) ``musicbrainz_workid``; the Navidrome import
        writes ``recording_mbid``/``musicbrainz_trackid`` from the file tags. A
        track with NONE of these has NOT been resolved by MusicBrainz — no
        recording, no work — so every cover-detection step that would need to
        Search for it would just duplicate a lookup the metadata pass already
        tried and failed. The deep pass skips such tracks instead of re-searching.
        """
        return bool(
            str(
                track.get("mbid")
                or track.get("recording_mbid")
                or track.get("musicbrainz_trackid")
                or track.get("work_mbid")
                or track.get("musicbrainz_workid")
                or track.get("musicbrainz_album_mbid")
                or ""
            ).strip()
        )

    @staticmethod
    def _va_compilation_without_release(
        album_artist: str, tracks: list[dict[str, Any]]
    ) -> bool:
        """True for a Various-Artists album that is not an official release.

        ⭐ Reported: *"Various Artists compilations are struggling to match the
        cover detection when it wasn't an official album release. It should
        match these using artist name and track name."*

        A VA compilation assembled from a playlist (or any folder that never
        matched a MusicBrainz release) has no release MBID, so the metadata
        pass had nothing to resolve its tracks against and every row lacks a
        logged recording/work identity. Applying the "the metadata pass already
        tried" skip would then silence the WHOLE album; the track ARTIST and
        TITLE are enough to find the recording, so the deep pass must fall back
        to them.
        """
        if not (
            is_track_artist_placeholder(album_artist)
            or str(album_artist or "").strip().casefold() in _COMPILATION_ARTIST_NAMES
        ):
            return False
        for track in tracks or []:
            if not isinstance(track, dict):
                continue
            if str(
                track.get("musicbrainz_album_mbid")
                or track.get("musicbrainz_albumid")
                or track.get("musicbrainz_releasegroupid")
                or ""
            ).strip():
                # An official release IS known, so the metadata pass had
                # something to resolve against — keep the duplicate-search skip.
                return False
        return True

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
            return _ngs_relations(self.mb.get_recording(mbid, inc=inc) or {})
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

    # -- work-first ------------------------------------------------------

    def _work_recordings(self, work_mbid: str, limit: int = 100) -> list[dict[str, Any]]:
        """Browse a work's recordings, cached per detector instance.

        ⚠️ 100 (the MusicBrainz browse maximum), not 25. A famous song has
        hundreds of recordings and the browse is NOT ordered by date, so a
        25-row page routinely missed the original altogether — "Gangnam
        Style" has well over 25 covers on MusicBrainz. It is still ONE request.
        """
        with self._mem_lock:
            cached = self._work_recording_cache.get(work_mbid, _MISS)
        if cached is not _MISS:
            return cached
        try:
            recordings = self.mb.browse_work_recordings(
                work_mbid, inc="artist-credits+releases+work-rels", limit=limit
            ) or []
        except Exception:
            try:
                recordings = self.mb.browse_work_recordings(
                    work_mbid, inc="artist-credits+releases", limit=limit
                ) or []
            except Exception as exc:
                logger.debug("Work recording browse failed", work_mbid=work_mbid, error=str(exc))
                return []
        recordings = [_ngs_relations(r) for r in recordings if isinstance(r, dict)]
        # An EMPTY page is never cached: it is far more often a failed or
        # throttled request than a song with no recordings, and caching it hid
        # every recording of that work for the rest of the scan.
        if recordings:
            with self._mem_lock:
                self._work_recording_cache[work_mbid] = recordings
                while len(self._work_recording_cache) > _WORK_RECORDING_CACHE_MAX:
                    self._work_recording_cache.popitem(last=False)
        return recordings

    def _performer_wrote_work(self, work_mbid: str, performer: str) -> bool:
        """True when the performer (or a band member / alias of the performer) wrote the work."""
        if not work_mbid or not performer or is_track_artist_placeholder(performer):
            return False
        _work_title, writers = self._work_writers(work_mbid)
        if not writers:
            return False
        for w in writers:
            w_name = w.get("name", "")
            if w_name and self._is_writer_same_as_artist(w_name, performer):
                return True
        ranked = self._ranked_writer_candidates(writers)
        return any(_same_artist(name, performer) for _aid, name in ranked if name)

    def _detect_via_work_id(
        self,
        work_mbid: str,
        performer: str,
        exclude_mbid: str = "",
        track_year: int | None = None,
        allow_writer_check: bool = True,
    ) -> dict[str, Any] | None:
        """Answer the cover question from the work the metadata scan logged.

        A cover and its original are BOTH performances of the SAME work, so the
        work's own recording list is the complete candidate set — no title
        search over the MusicBrainz index is needed. One ``browse`` call
        replaces the deep pipeline's per-track fan-out (ISRC lookup + recording
        fetches + up to ~9 requests per writer).

        Return contract (the caller treats these differently):

        * ``{"artist", "year", "confidence"}`` — the earliest recording of the
          work credited to an artist OTHER than *performer*: a cover.
        * ``{}`` — conclusive negative: the performer wrote the work or
          originated the earliest recording of the song. The caller skips the
          track's deep fan-out.
        * ``None`` — inconclusive (no usable work data); the caller falls
          through to the full deep pipeline.
        """
        recordings = self._work_recordings(work_mbid)
        if not recordings:
            return None

        # Check whether the track's own recording on the work page is explicitly
        # flagged by MusicBrainz as a cover performance of this work.
        own_recording_flagged_cover = False
        if exclude_mbid:
            for rec in recordings:
                if isinstance(rec, dict) and str(rec.get("id") or "").strip() == exclude_mbid:
                    if _all_cover_work_ids(rec) & {work_mbid}:
                        own_recording_flagged_cover = True
                    break

        # If the performer (or a band member of the performer) wrote the work
        # and MusicBrainz does NOT explicitly mark this recording as a cover,
        # the performer is the originator (prevents AFI originals covered later
        # by The Offspring or Punk Goes 8-Bit from flipping backwards).
        if allow_writer_check and not own_recording_flagged_cover:
            if self._performer_wrote_work(work_mbid, performer):
                return {}

        earliest: dict[str, Any] | None = None
        earliest_key = "9999-99-99"
        performer_key = (
            f"{int(track_year):04d}-99-99"
            if isinstance(track_year, int) and 1900 <= track_year <= 2100
            else "9999-99-99"
        )
        credited = 0
        for rec in recordings:
            if not isinstance(rec, dict) or not rec.get("id"):
                continue
            rec_id = str(rec.get("id") or "").strip()
            if rec_id and rec_id == exclude_mbid:
                key = _release_date_key(rec)
                if key < performer_key:
                    performer_key = key
                continue
            if _has_cover_annotation(str(rec.get("title") or "")):
                continue
            # Never treat a candidate recording that MusicBrainz itself marks
            # as a cover of this work as the original recording!
            if _all_cover_work_ids(rec) & {work_mbid}:
                continue
            rec_artist = str(
                artist_from_credit(rec.get("artist-credit", []) or []) or ""
            ).strip()
            if not rec_artist:
                continue
            credited += 1
            key = _release_date_key(rec)
            if _same_artist(rec_artist, performer):
                if key < performer_key:
                    performer_key = key
                continue
            if _undated(key) or key >= earliest_key:
                continue
            earliest_key = key
            earliest = {
                "artist": rec_artist,
                "year": year_from_recording(rec),
                "confidence": "medium",
            }

        # The performer recorded it FIRST (or in the same year when month/day
        # is unknown): they originated the song.
        if earliest and not _undated(performer_key):
            if performer_key <= earliest_key or performer_key[:4] < earliest_key[:4]:
                return {}
            if performer_key[:4] == earliest_key[:4] and performer_key.endswith("-99-99"):
                return {}

        if earliest:
            by_writer = self._original_by_writer(
                {work_mbid}, performer=performer, exclude_mbid=exclude_mbid,
            )
            if by_writer == {}:
                # _original_by_writer determined the performer is the writer/originator
                return {}
            if by_writer:
                return {**by_writer, "confidence": "medium"}
            return earliest
        # The work HAS recordings bearing a credit and none of them differs
        # from the performer — the performer originated the song.
        return {} if credited else None

    # -- album entry point ------------------------------------------------

    def detect_covers_for_album(
        self,
        album: str,
        artist: str,
        tracks: list[dict[str, Any]],
        force: bool = False,
    ) -> list[dict[str, Any]]:
        """Detect cover songs in an album.

        ⚠️ THIS PASS EMITS ITS OWN PROGRESS, deliberately.

        It was reported as a static wait: *"add more output for the Cover
        Detection in the logs so we can see what's being checked rather than a
        static wait while it's happening."* The pass is the longest single
        operation in an album — six detection techniques, each capable of a
        MusicBrainz round trip, per track — and it emitted NOTHING to
        ``unified_scan.log`` (the Scanner tab's file) until it finished. Its
        structlog lines went to ``info.log``/``debug.log``, which the scan view
        does not tail, so a healthy run and a hung one looked identical for
        minutes.

        Three levels, so progress is visible without flooding the log:

        * ``log_unified`` — the START line (with the split between tracks that
          need checking and tracks whose verdict is already settled, which is
          what makes the duration predictable), one line per CONFIRMED cover
          (rare and actionable), a heartbeat while work is outstanding, and the
          SUMMARY;
        * ``log_scan_detail`` — per-track detail at debug level, following the
          same convention ``track_stage`` records (one line per track per stage
          flooded a 13-track album with output before any result was visible);
        * structlog, unchanged, for the existing file logs.
        """
        from helpers.logging_config import log_scan_detail, log_unified

        logger.info(
            "Starting cover detection",
            album=album,
            artist=artist,
            track_count=len(tracks),
        )

        # ⚠️ WORK ON COPIES WITH STRING IDS. Every set below
        # (``seen_track_ids``, ``_confirmed_skipped``, ``_fresh_skipped``) is
        # compared against ``str(id)`` in some places and the RAW id in others.
        # With integer ids those never matched, so ``_clear_unconfirmed_verdicts``
        # (which compares strings) saw confirmed covers as "assessed and found
        # nothing" and CLEARED them. Copies also stop this pass mutating the
        # caller's rows (it writes ``mbid`` onto them).
        tracks = [dict(t) for t in (tracks or []) if isinstance(t, dict)]
        for track in tracks:
            if track.get("id") is not None and str(track.get("id")).strip():
                track["id"] = str(track["id"]).strip()
            else:
                track["id"] = ""

        for track in tracks:
            track.setdefault("album", album)
            track.setdefault("album_artist", artist)
            if not str(track.get("work_mbid") or "").strip():
                _stored_work = str(track.get("musicbrainz_workid") or "").strip()
                if _stored_work:
                    # The metadata scan persists the work MBID under the REAL
                    # column name (``musicbrainz_workid``); this module reads
                    # ``work_mbid``. Normalise so the work-first step can see it.
                    track["work_mbid"] = _stored_work

        # Keep the source track for every id so updates always carry file_path.
        tracks_by_id: dict[str, dict[str, Any]] = {
            str(t.get("id")): t for t in tracks if t.get("id")
        }

        seen_track_ids: set[str] = set()
        cover_results: list[dict[str, Any]] = []
        pending_updates: list[dict[str, Any]] = []
        _fresh_skipped: set[str] = set()
        #: Tracks whose stored verdict stands, so they are neither re-detected
        #: nor cleared. Kept separate from ``seen_track_ids`` because that set
        #: also holds DETECTED covers — and the two need opposite treatment when
        #: clearing unconfirmed flags at the end of this function.
        _confirmed_skipped: set[str] = set()
        #: Confirmed covers whose "(X Cover)" title suffix had been lost — see
        #: the repair below. Counted separately so the summary does not report
        #: a repair as a new detection.
        _repaired_titles: set[str] = set()
        #: Tracks whose stored cover verdict was RE-CONFIRMED by this pass with
        #: the SAME original artist. They are confirmed for the clear step, but
        #: they are not written again — a verdict that has not changed must not
        #: churn its DB row or audio-file tags on every scan.
        _verified_existing_ids: set[str] = set()

        # ── Overlay the CURRENT stored verdict ────────────────────────────
        # ``tracks`` is the caller's load-time snapshot. The track stage runs
        # before this pass and may have just written a cover verdict for this
        # album, so judging the snapshot made a flag set earlier in the SAME
        # scan invisible — and ``_clear_unconfirmed_verdicts`` then saw
        # "nothing stored" and left it set. One query also supplies the
        # freshness timestamps used below.
        _stored_state = self._load_cover_checked_map([t.get("id") for t in tracks])
        for track in tracks:
            _state = _stored_state.get(str(track.get("id") or "")) or {}
            if not _state:
                continue
            for _column in ("is_cover", "original_cover_artist", "cover_manual_override"):
                if _column in _state:
                    track[_column] = _state[_column]
            if _state.get("cover_last_checked") is not None:
                track["cover_last_checked"] = _state["cover_last_checked"]

        if not force:
            for track in tracks:
                tid = track.get("id")
                if not tid:
                    continue
                # A user-locked verdict outranks every heuristic: never
                # re-detect it and never clear it.
                if track.get("cover_manual_override"):
                    _confirmed_skipped.add(tid)
                    seen_track_ids.add(tid)
                    continue
                # ``_cover_recheck`` is set by the scan runner when this pass
                # stripped "(X Cover)" wording from the title: the stored
                # timestamp says "checked recently", but the evidence the
                # title-hint step needs only exists on THIS pass.
                #
                # ⚠️ THE FRESHNESS WINDOW ONLY PROTECTS NEGATIVE VERDICTS.
                # A track already flagged as a cover is ALWAYS re-verified,
                # however recently it was checked. Previously a stored verdict
                # was trusted FOREVER (and re-stamped on every scan), so a
                # wrong flag — e.g. one written by the compilation bug — could
                # never be corrected: the deep pass skipped it, and the Great
                # Covers playlist kept picking it up. Positive verdicts are a
                # small fraction of a library, so re-checking them is cheap;
                # leaving a false one is not.
                if (
                    not track.get("_cover_recheck")
                    and not self._is_cover_flagged(track)
                    and _checked_fresh(
                        track.get("cover_last_checked")
                        or (_stored_state.get(tid) or {}).get("cover_last_checked")
                    )
                ):
                    _fresh_skipped.add(tid)
                    seen_track_ids.add(tid)
                    continue
                if self._stored_verdict_is_self_referential(track):
                    # A recording is never a cover OF ITS OWN PERFORMER, so a
                    # stored verdict saying exactly that is provably wrong. Do
                    # NOT let it suppress detection: fall through and assess the
                    # track properly, so the end of this function can clear it
                    # when nothing confirms it.
                    logger.debug(
                        "Stored cover verdict ignored — original artist is the "
                        "track's own performer",
                        track=track.get("title"),
                        original_artist=track.get("original_cover_artist"),
                    )

        # ⭐ The START line, emitted AFTER the skip scan above so the number of
        # tracks that actually need work is known. That number — not the album's
        # track count — is what makes the wait predictable: a re-scanned album
        # usually settles every verdict immediately and finishes in seconds,
        # while a first pass pays MusicBrainz round trips per track. Without this
        # split the two look identical from the outside.
        _to_check = [
            str(t.get("id")) for t in tracks
            if t.get("id") and str(t.get("id")) not in seen_track_ids
        ]
        _settled = len(seen_track_ids)
        if _to_check:
            log_unified(
                f"[COVER_DETECT] Checking {len(_to_check)} track(s) for "
                f"'{artist} - {album}' ({_settled} already settled)"
            )
        else:
            log_unified(
                f"[COVER_DETECT] All {len(tracks)} track(s) already settled for "
                f"'{artist} - {album}' — nothing to check"
            )

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
            _performer_usable = bool(_performer) and not is_track_artist_placeholder(_performer)
            _orig_usable = bool(_orig) and not is_track_artist_placeholder(_orig)
            if _orig_usable and _performer_usable and _same_artist(_orig, _performer):
                logger.debug(
                    "Cover result discarded: original equals the track's own artist",
                    track=track.get("title"),
                    artist=_performer,
                )
                return
            if _orig and not _performer_usable:
                logger.info(
                    "Cover result discarded: track artist is not a usable performer",
                    track=track.get("title"),
                    track_artist=_performer or None,
                    original_artist=_orig,
                )
                return
            _tid = str(result["track_id"])
            _stored_orig = str(track.get("original_cover_artist") or "").strip()
            if (
                self._is_cover_flagged(track)
                and _stored_orig
                and _orig
                and _same_artist(_stored_orig, _orig)
            ):
                # Same verdict the row already carries: it STANDS (so the
                # clear step must not touch it), but there is nothing to write.
                # Skipping the pending update keeps an unchanged cover from
                # rewriting its DB row and audio-file tags on every scan.
                _verified_existing_ids.add(_tid)
                seen_track_ids.add(_tid)
                # The verdict is genuine, so restore its "(X Cover)" suffix
                # if a previous pass stripped it. This repair runs only AFTER
                # detection has re-confirmed the verdict — doing it first (as
                # the old code did) stamped a bogus suffix onto a FALSE stored
                # verdict and then shielded it from re-assessment.
                _repair = self._suffix_repair_update(track)
                if _repair is not None:
                    pending_updates.append(_repair)
                    _repaired_titles.add(_tid)
                log_scan_detail(
                    f"[COVER_DETECT] \u2713 '{track.get('title')}' still a cover "
                    f"(original={_orig})"
                )
                return
            cover_results.append(result)
            seen_track_ids.add(_tid)
            pending_updates.append(self._build_update(result, track))

            # ⭐ The ACTIONABLE event, so it goes to the scan log the operator
            # watches rather than only to ``info.log``. Covers are rare, so one
            # line per finding is cheap — and it is the line that explains why a
            # title is about to gain a "(X Cover)" suffix and a "Cover" genre.
            _conf = str(result.get("confidence") or "low")
            log_unified(
                f"[COVER_DETECT] cover: '{track.get('title')}' "
                f"-> originally by {_orig or 'unknown'} ({_conf})"
            )
            # Per-track detail at debug level, the same convention track_stage
            # records: one line per track per stage flooded the log before any
            # result was visible.
            log_scan_detail(
                f"[COVER_DETECT] \u2713 '{track.get('title')}' is a cover "
                f"(original={_orig or 'unknown'}, confidence={_conf})"
            )

        def _step_done(step: str, index: int) -> None:
            """Report movement after one detection technique has finished."""
            if not _to_check:
                # Every verdict was already settled, so the six techniques all
                # returned immediately and there is no wait to narrate.
                return
            _remaining = sum(
                1 for t in tracks
                if t.get("id") and str(t.get("id")) not in seen_track_ids
            )
            log_unified(
                f"[COVER_DETECT] step {index}/{_TOTAL_STEPS} ({step}) done — "
                f"{len(seen_track_ids)}/{len(tracks)} settled, {_remaining} queued"
            )

        #: Number of detection techniques below, so the progress line can say
        #: "step 3/6". Kept next to the calls it counts — see ``_step_done``.
        _TOTAL_STEPS = 6

        # Step 1: work-first detection — the metadata scan ALREADY resolved
        # each track's work (persisted as ``musicbrainz_workid`` / published
        # in-memory as ``work_mbid``). A cover and its original are BOTH
        # performances of the SAME work, so browsing the work's recordings —
        # ONE MusicBrainz request — is the complete candidate set: no title
        # search, no per-candidate recording fetches. Tracks the work answers
        # conclusively are SKIPPED from the expensive ISRC / recording-
        # relation / writer fan-out below (up to ~9 MB requests per writer),
        # which previously re-derived over the network what the metadata scan
        # had already logged.
        for track in tracks:
            tid = track.get("id")
            if not tid or tid in seen_track_ids:
                continue
            work_mbid = str(track.get("work_mbid") or "").strip()
            if not work_mbid:
                continue
            # ⭐ ``comparison_artist`` — the TRACK artist, never a compilation's
            # album placeholder. Returns "" when neither names a performer.
            performer = comparison_artist(track, artist)
            if not performer:
                continue
            original = self._detect_via_work_id(
                work_mbid=work_mbid,
                performer=performer,
                exclude_mbid=str(
                    track.get("mbid") or track.get("recording_mbid") or ""
                ).strip(),
                track_year=_extract_track_year(track),
            )
            if original is None:
                # No usable work data — fall through to the deep pipeline.
                continue
            if not original:
                logger.debug(
                    "Work-first cover check: conclusive negative — performer "
                    "originated or wrote the work",
                    track=track.get("title"),
                    work_mbid=work_mbid,
                )
                seen_track_ids.add(tid)
                log_scan_detail(
                    f"[COVER_DETECT] \u2717 '{track.get('title')}' not a cover "
                    "(performer originated or wrote the work)"
                )
                continue
            logger.debug(
                "Cover detected via the logged work",
                track=track.get("title"),
                work_mbid=work_mbid,
                original_artist=original.get("artist"),
            )
            _record(self._result(tid, track.get("title", ""), original), track)

        _step_done("work lookup", 1)

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
                # ⭐ The track artist, resolved once. ``album_artist`` is passed
                # empty rather than the compilation placeholder it would
                # otherwise fall back to.
                album_artist="",
                track_artist=comparison_artist(track, artist),
                track_year=_extract_track_year(track),
            )
            if core:
                logger.debug("Cover detected via ISRC", track=track.get("title"), isrc=isrc)
                _record(self._result(tid, track.get("title", ""), core), track)

        _step_done("ISRC lookup", 2)

        # Step 3: MusicBrainz recording-relation detection
        # Computed once for the album — see the identity gate below.
        _va_without_release = self._va_compilation_without_release(artist, tracks)
        for track in tracks:
            tid = track.get("id")
            if not tid or tid in seen_track_ids:
                continue
            # Skip tracks without any logged MusicBrainz identity: resolving
            # the recording here would search MusicBrainz for a track the
            # metadata pass already failed to resolve — the exact duplicate
            # network work this pass is meant to avoid.
            #
            # ⚠️ EXCEPT on a VA compilation that is NOT an official release
            # (a playlist-derived folder, for example): there was no release
            # for the metadata pass to resolve against, so it never logged an
            # identity and there is no duplicate search to avoid. The track's
            # own ARTIST + TITLE are enough to find the recording, which is the
            # only route cover detection has for these albums.
            if not self._track_has_logged_identity(track):
                if not _va_without_release:
                    continue
                if not comparison_artist(track, artist):
                    # No real performer to search with or compare against.
                    continue
            mbid = self._resolve_recording_mbid(track, artist, album)
            if not mbid:
                continue
            track["mbid"] = mbid

            # ⭐⭐ THE TRACK'S OWN ARTIST, not the album's. A cover verdict is
            # about a PERFORMER, and on a compilation the album artist is the
            # placeholder "Various Artists" — comparing against it disabled the
            # writer check AND the "performer released it first" negative, which
            # is how Crowded House's own "Don't Dream It's Over" was attributed
            # to a later cover. See ``comparison_artist``.
            _performer = comparison_artist(track, artist)
            core = self._detect_via_recording_relation(
                recording_mbid=mbid,
                title=track.get("title", ""),
                performer=_performer,
                release_mbid=str(track.get("musicbrainz_album_mbid") or "").strip(),
                track_year=_extract_track_year(track),
            )
            if core:
                logger.debug("Cover detected via recording relation", track=track.get("title"), mbid=mbid)
                _record(self._result(tid, track.get("title", ""), core), track)

        _step_done("recording relations", 3)

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

            source = tracks_by_id.get(tid, {})
            # ⭐ The TRACK's artist. Falling back to the album artist is the bug
            # this whole pass is being fixed for: on a compilation that fallback
            # is "Various Artists", and every writer comparison below then
            # answers "no" without looking at the performer at all.
            performer = str(info.get("track_artist") or "").strip()
            if not performer or is_track_artist_placeholder(performer):
                performer = comparison_artist(source, artist)
            if not performer:
                continue

            if is_compilation:
                ta = info.get("track_artist", "") or ""
                if ta not in track_artist_coverage_cache:
                    track_artist_coverage_cache[ta] = writer_coverage_for_artist(self.db_conn, ta)
                if track_artist_coverage_cache[ta] < 0.10:
                    continue

            # If ANY credited writer on the track is the performer (or a band
            # member of the performer), they wrote the song themselves — do not
            # search other writers to turn a co-written original into a cover.
            if any(self._is_writer_same_as_artist(w, performer) for w in info.get("writers", [])):
                continue

            for writer in info.get("writers", []):
                if self._is_writer_same_as_artist(writer, performer):
                    continue
                if track_has_original_by_artist(self.db_conn, performer, info["title"]):
                    break
                if is_common_writer_for_artist(
                    self.db_conn, writer, artist, track_artist=info.get("track_artist"),
                ):
                    break

                search_title = _COVER_SUFFIX_RE.sub("", info["title"]).strip() or info["title"]
                original = self._find_original_recording(
                    search_title,
                    writer,
                    album_artist=performer,
                    track_year=_extract_track_year(source),
                )
                if not original:
                    continue
                if _same_artist(original.get("artist", ""), performer):
                    break

                # Pass the source track, not the writer-info dict, so the
                # update carries a usable file_path.
                logger.debug("Cover detected via writer matching", track=info["title"], writer=writer)
                _record(
                    self._result(tid, info["title"], original, writer=writer),
                    source,
                )
                break

        _step_done("writer matching", 4)

        # Step 5: heuristic title-based detection
        for track in tracks:
            tid = track.get("id")
            if not tid or tid in seen_track_ids:
                continue
            # Do not trust a title "(X Cover)" suffix that was previously
            # injected by our own scanner when ``force=True`` is re-evaluating
            #whether the track is actually a cover!
            if force and self._is_cover_flagged(track) and not track.get("cover_manual_override"):
                continue
            title = track.get("title", "")
            # Album text is deliberately excluded: an album-level annotation
            # previously applied the same hint artist to every track.
            hint = self._detect_cover_hint_from_text(title)
            if not hint:
                continue

            # ⭐ THE TRACK'S OWN ARTIST. Every test below asks "is this the
            # performer's own song?", and on a compilation the album artist is
            # "Various Artists" — which made all three vacuous.
            _performer = comparison_artist(track, artist)

            base_title = _COVER_SUFFIX_RE.sub("", title).strip()
            if (
                base_title
                and base_title.lower() != title.lower()
                and _performer
                and track_has_original_by_artist(self.db_conn, _performer, base_title)
            ):
                continue

            resolved = {"artist": hint, "year": None, "confidence": "low"}
            resolved_writer = ""
            writers_for_track = (track_writers.get(tid) or {}).get("writers", [])

            if writers_for_track and base_title:
                for wr in writers_for_track:
                    if _performer and self._is_writer_same_as_artist(wr, _performer):
                        continue
                    mb_orig = self._find_original_recording(
                        base_title,
                        wr,
                        album_artist=_performer,
                        track_year=_extract_track_year(track),
                    )
                    if mb_orig and not _same_artist(mb_orig.get("artist", ""), _performer):
                        resolved = mb_orig
                        resolved_writer = wr
                        break

            logger.debug("Cover detected via title hint", track=title, original_artist=resolved.get("artist"))
            _record(self._result(tid, title, resolved, writer=resolved_writer), track)

        _step_done("title hints", 5)

        # Step 6: work-based fallback
        for track in tracks:
            tid = track.get("id")
            if not tid or tid in seen_track_ids:
                continue
            if not self._is_cover_flagged(track):
                continue
            if (track.get("original_cover_artist") or "").strip() and not force:
                continue

            work_mbid = str(track.get("work_mbid") or track.get("musicbrainz_workid") or "").strip()
            # ⭐ THE TRACK'S OWN ARTIST — see ``comparison_artist``. Both arms
            # below phrase their answer relative to the performer.
            _performer = comparison_artist(track, artist)
            if work_mbid:
                logger.debug("Using pre-fetched work_mbid for cover fallback", track=track.get("title"), work_mbid=work_mbid)
                original = self._earliest_work_recording(
                    work_ids={work_mbid},
                    search_title=canonical_track_title(track.get("title", "")) or track.get("title", ""),
                    exclude_mbid=str(track.get("mbid") or track.get("recording_mbid") or ""),
                    album_artist=_performer,
                    confidence="medium",
                    track_year=_extract_track_year(track),
                )
            else:
                # Same skip rule as Step 3: without any logged identity the
                # recording would need an online search the metadata pass has
                # already tried — skip instead of re-searching.
                if not self._track_has_logged_identity(track):
                    continue
                mbid = self._resolve_recording_mbid(track, artist, album)
                if not mbid:
                    continue
                track["mbid"] = mbid
                original = self._find_original_via_work_lookup(
                    mbid,
                    track.get("title", ""),
                    _performer,
                    track_year=_extract_track_year(track),
                )

            if not original:
                continue

            logger.debug("Cover detected via work fallback", track=track.get("title"))
            _record(self._result(tid, track.get("title", ""), original), track)

        _step_done("work fallback", 6)

        # ⭐ Announced BEFORE the database write and the per-file tag rewrite
        # below, because those are the remaining slow part (one mutagen write per
        # confirmed cover) and the operator would otherwise see the detection
        # finish and then wait again with no explanation.
        if pending_updates:
            log_unified(
                f"[COVER_DETECT] writing tags for {len(pending_updates)} cover(s)…"
            )

        if pending_updates:
            from db.repositories.cover_detection_repository import build_cover_title

            successful = {
                str(t) for t in apply_cover_metadata_batch(self.db_conn, pending_updates)
            }
            for update in pending_updates:
                tid = str(update.get("track_id") or "")
                if tid not in successful:
                    continue
                fp = str(update.get("file_path") or "").strip()
                if not fp:
                    continue
                _full_title = build_cover_title(
                    update.get("title", ""), update.get("original_artist", "")
                )
                if _full_title:
                    self._update_file_metadata(fp, _full_title, [])

        # ── Clear verdicts that this pass could NOT confirm ────────────────
        self._clear_unconfirmed_verdicts(
            tracks,
            confirmed_ids=(
                {str(r.get("track_id")) for r in cover_results}
                | _repaired_titles
                | _verified_existing_ids
            ),
            fresh_skipped=_fresh_skipped,
            confirmed_skipped=_confirmed_skipped,
            force=force,
        )

        self._persist_checked(tracks, _fresh_skipped)

        _checked = len(_to_check)
        # ⭐ Report the covers the ALBUM HAS, not only the ones this pass wrote.
        # A re-scanned album re-confirms every stored verdict through the same
        # six techniques and then skips the write, so counting writes alone made
        # an 11-cover album report "0 cover(s)" — the operator read that as a
        # failed detection even though the breakdown said already-confirmed=11.
        # ``new`` keeps the distinction visible; the leading number is the total.
        _already_confirmed = len(_verified_existing_ids) + sum(
            1 for tid in _confirmed_skipped
            if self._is_cover_flagged(tracks_by_id.get(tid) or {})
        )
        _cover_total = len(cover_results) + _already_confirmed
        log_unified(
            f"[COVER_DETECT] {artist} - {album}: {_cover_total} cover(s) "
            f"(new={len(cover_results)}) | "
            f"checked={_checked}, settled-in-window={len(_fresh_skipped)}, "
            f"already-confirmed={_already_confirmed}"
            + (f", titles-repaired={len(_repaired_titles)}" if _repaired_titles else "")
        )

        logger.info(
            "Cover detection complete",
            album=album,
            found_count=_cover_total,
            new_count=len(cover_results),
        )
        _repair_results = [
            {
                "track_id": u["track_id"],
                "title": u.get("title", ""),
                "is_cover": True,
                "original_artist": u.get("original_artist", ""),
                "original_year": None,
                "writer": "",
                "confidence": "stored",
                "repaired": True,
            }
            for u in pending_updates
            if str(u.get("track_id")) in _repaired_titles
        ]
        return cover_results + _repair_results

    def _suffix_repair_update(self, track: dict[str, Any]) -> dict[str, Any] | None:
        """An update restoring a confirmed cover's "(X Cover)" title, or None.

        Only for a track whose STORED verdict names an original artist, whose
        title lacks the suffix, that is not user-locked, and whose original is
        not its own performer (a self-referential verdict is wrong and must not
        be written into the title).
        """
        if track.get("cover_manual_override"):
            return None
        if not self._is_cover_flagged(track):
            return None
        original = str(track.get("original_cover_artist") or "").strip()
        title = str(track.get("title") or "").strip()
        if not original or not title or _has_cover_annotation(title):
            return None
        if is_track_artist_placeholder(original):
            return None
        performer = str(track.get("artist") or "").strip()
        if performer and _same_artist(original, performer):
            return None
        update = self._build_update(
            {
                "track_id": str(track.get("id")),
                "original_artist": original,
                "confidence": "stored",
            },
            track,
        )
        # Keep the reason the original detection recorded.
        update["is_cover_reason"] = (
            str(track.get("is_cover_reason") or "").strip() or update["is_cover_reason"]
        )
        return update

    def _clear_unconfirmed_verdicts(
        self,
        tracks: list[dict[str, Any]],
        confirmed_ids: set[str],
        fresh_skipped: set[str],
        confirmed_skipped: set[str],
        force: bool,
    ) -> None:
        """Clear stored cover verdicts this pass did not confirm.

        Also strips any stale ``(X Cover)`` suffix from the track's title in
        both the database and the underlying audio file so previously poisoned
        titles (e.g. ``Totalimmortal (The Offspring Cover)``) are restored to
        their clean original title.
        """
        to_clear: list[dict[str, Any]] = []
        for track in tracks:
            tid = str(track.get("id") or "")
            if not tid:
                continue
            if track.get("cover_manual_override"):
                continue
            if tid in confirmed_ids or tid in confirmed_skipped or tid in fresh_skipped:
                continue
            if not self._is_cover_flagged(track) and not self._cover_has_original_artist(track):
                continue  # nothing stored — nothing to clear
            to_clear.append(track)

        if not to_clear:
            return

        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session

        cleared = 0
        for track in to_clear:
            tid = str(track.get("id") or "")
            raw_title = str(track.get("title") or "").strip()
            clean_title = _COVER_SUFFIX_RE.sub("", raw_title).strip() or raw_title
            title_changed = bool(clean_title and clean_title != raw_title)

            try:
                with _db_session() as session:
                    if title_changed:
                        result = session.execute(
                            _text(
                                "UPDATE tracks SET is_cover = 0, "
                                "original_cover_artist = NULL, "
                                "title = :title, "
                                "is_cover_reason = :reason "
                                "WHERE CAST(id AS TEXT) = :id "
                                "AND cover_manual_override IS NOT TRUE"
                            ),
                            {
                                "title": clean_title,
                                "reason": (
                                    "Deep cover detection found no evidence: "
                                    "MusicBrainz work relations, ISRC, recording "
                                    "relations and writer coverage all negative"
                                ),
                                "id": tid,
                            },
                        )
                    else:
                        result = session.execute(
                            _text(
                                "UPDATE tracks SET is_cover = 0, "
                                "original_cover_artist = NULL, "
                                "is_cover_reason = :reason "
                                "WHERE CAST(id AS TEXT) = :id "
                                "AND cover_manual_override IS NOT TRUE"
                            ),
                            {
                                "reason": (
                                    "Deep cover detection found no evidence: "
                                    "MusicBrainz work relations, ISRC, recording "
                                    "relations and writer coverage all negative"
                                ),
                                "id": tid,
                            },
                        )
                    if (result.rowcount or 0) > 0:
                        cleared += 1
                        fp = str(track.get("file_path") or "").strip()
                        if fp:
                            # ⚠️ THE FILE TAG MUST GO TOO. Clearing only the
                            # DB row leaves ``IS_COVER`` on disk; the next
                            # Navidrome import reads it back (OpenSubsonic's
                            # ``tags``) and re-flags the track, so the false
                            # verdict survives every scan and keeps showing up
                            # in the Great Covers playlist.
                            self._clear_cover_file_tags(
                                fp, clean_title if title_changed else None
                            )
            except Exception as exc:
                logger.debug("Cover verdict clear failed", track_id=tid, error=str(exc))

        if cleared:
            logger.info(
                "Cleared unconfirmed cover verdicts",
                count=cleared,
                album_tracks=len(tracks),
            )

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
                        f"WHERE CAST(id AS TEXT) IN ({placeholders})"
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
        track_year: int | None = None,
    ) -> dict[str, Any] | None:
        """Resolve an original via recordings sharing the track's ISRC.

        An ISRC identifies one specific recording, so a differently-credited
        recording sharing it is weak evidence, not proof. Confidence is capped
        at medium and a known year is required.
        """
        performer = str(track_artist or "").strip() or str(album_artist or "").strip()
        if not performer or is_track_artist_placeholder(performer):
            logger.debug(
                "ISRC cover check skipped: no usable track performer",
                track=title,
                performer=performer or None,
            )
            return None
        try:
            recordings = self.mb.lookup_by_isrc(
                isrc, inc="artist-credits+releases+work-rels+recording-rels"
            )
            if not recordings:
                return None
            recordings = [_ngs_relations(r) for r in recordings if isinstance(r, dict)]

            earliest: dict[str, Any] | None = None
            earliest_year = 9999
            performer_earliest_year = (
                int(track_year)
                if isinstance(track_year, int) and 1900 <= track_year <= 2100
                else 9999
            )

            for rec in recordings:
                if not rec.get("id"):
                    continue
                rec_artist = artist_from_credit(rec.get("artist-credit", []))
                if not rec_artist:
                    continue
                year = year_from_recording(rec)
                if _same_artist(rec_artist, performer):
                    if year is not None and year < performer_earliest_year:
                        performer_earliest_year = year
                    continue
                # Skip candidates that are themselves annotated or modelled as covers.
                if _has_cover_annotation(str(rec.get("title") or "")):
                    continue
                if _all_cover_work_ids(rec):
                    continue
                if year is not None and year < earliest_year:
                    earliest_year = year
                    earliest = {"artist": rec_artist, "year": year, "confidence": "medium"}

            if earliest and performer_earliest_year <= earliest_year:
                return None

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
        performer: str,
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
            # A relation pointing back at the performer is not a cover — it is
            # the performer's own recording, mis-modelled upstream.
            if performer and _same_artist(orig_artist, performer):
                continue
            return (
                {"artist": orig_artist, "year": orig_year, "confidence": "high"},
                orig_id,
            )
        return None, None

    def _resolve_cover_chain(
        self,
        recording_mbid: str,
        performer: str,
        seed_recording: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Follow cover relations to the root of the chain."""
        visited: set[str] = {recording_mbid}
        current_id = recording_mbid
        best: dict[str, Any] | None = None
        _first = True

        for _ in range(_MAX_COVER_CHAIN_DEPTH):
            if _first and seed_recording:
                recording = seed_recording
            else:
                recording = self._get_recording(
                    current_id, "recording-rels+artist-credits+releases"
                )
            _first = False
            if not recording:
                break
            core, next_id = self._cover_relation_target(
                recording, current_id, performer
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

    def _original_from_cover_work(
        self,
        work_ids: set[str],
        *,
        performer: str,
        exclude_mbid: str = "",
        track_year: int | None = None,
    ) -> dict[str, Any] | None:
        """Original artist of a cover, from the covered WORK's own recordings.

        Called when MusicBrainz explicitly states the recording is a cover of
        ``work_ids`` (so ``allow_writer_check=False`` is passed to avoid
        short-circuiting if a writer covers their own earlier band's work).
        """
        for work_id in sorted(work_ids or ()):
            try:
                original = self._detect_via_work_id(
                    work_mbid=work_id,
                    performer=performer,
                    exclude_mbid=exclude_mbid,
                    track_year=track_year,
                    allow_writer_check=False,
                )
            except Exception as exc:
                logger.debug("Cover-work original lookup failed", work_mbid=work_id, error=str(exc))
                continue
            if original:
                return {**original, "confidence": "high"}
        return None

    def _artist_bands(self, artist_id: str) -> list[dict[str, str]]:
        """Performing identities linked to a writer: ``[{"id", "name", "kind"}]``."""
        if not artist_id:
            return []
        with self._mem_lock:
            cached = self._artist_band_cache.get(artist_id)
        if cached is not None:
            return cached
        bands: list[dict[str, str]] = []
        try:
            details = _ngs_relations(self.mb.get_artist(artist_id, inc="artist-rels") or {})
            for rel in details.get("artist-relation-list", []) or []:
                rtype = str(rel.get("type", "")).strip().lower()
                if rtype == "is person":
                    kind = "alias"
                elif rtype in {"member of band", "member"}:
                    kind = "band"
                else:
                    continue
                band = rel.get("artist") or {}
                bid = str(band.get("id") or "").strip()
                if bid and bid != artist_id and all(b["id"] != bid for b in bands):
                    bands.append({
                        "id": bid,
                        "name": str(band.get("name") or "").strip(),
                        "kind": kind,
                    })
        except Exception as exc:
            logger.debug("Band lookup failed", artist_id=artist_id, error=str(exc))
        if bands:
            with self._mem_lock:
                self._artist_band_cache[artist_id] = bands
        return bands

    def _work_writers(self, work_id: str) -> tuple[str, list[dict[str, str]]]:
        """``(work title, [{"id", "name"}] writers)`` for a work."""
        try:
            work = _ngs_relations(self.mb.get_work(work_id, inc="artist-rels") or {})
        except Exception as exc:
            logger.debug("Work writer lookup failed", work_mbid=work_id, error=str(exc))
            return "", []
        writers: list[dict[str, str]] = []
        for rel in work.get("artist-relation-list", []) or []:
            if str(rel.get("type", "")).lower() not in _WRITER_RELATION_TYPES:
                continue
            artist = rel.get("artist") or {}
            wid = str(artist.get("id") or "").strip()
            if wid and all(w["id"] != wid for w in writers):
                writers.append({"id": wid, "name": str(artist.get("name") or "").strip()})
        return str(work.get("title") or "").strip(), writers

    def _ranked_writer_candidates(
        self, writers: list[dict[str, str]]
    ) -> list[tuple[str, str]]:
        """Writer-linked performers, MOST LIKELY ORIGINAL FIRST."""
        aliases: list[tuple[str, str]] = []
        band_lists: list[list[tuple[str, str]]] = []
        band_count: dict[str, int] = {}
        band_name: dict[str, str] = {}
        for writer in writers[:4]:
            own: list[tuple[str, str]] = []
            for linked in self._artist_bands(writer["id"]):
                if linked.get("kind") == "alias":
                    aliases.append((linked["id"], linked["name"]))
                else:
                    own.append((linked["id"], linked["name"]))
                    band_count[linked["id"]] = band_count.get(linked["id"], 0) + 1
                    band_name[linked["id"]] = linked["name"]
            band_lists.append(own)

        ordered: list[tuple[str, str]] = []
        seen: set[str] = set()

        def _add(aid: str, name: str) -> None:
            if aid and aid not in seen:
                seen.add(aid)
                ordered.append((aid, name))

        for aid, name in aliases:
            _add(aid, name)
        for aid in sorted(
            (b for b, c in band_count.items() if c >= 2),
            key=lambda b: (-band_count[b], band_name[b]),
        ):
            _add(aid, band_name[aid])
        for writer in writers[:4]:
            _add(writer["id"], writer["name"])
        depth = max((len(lst) for lst in band_lists), default=0)
        for i in range(depth):
            for lst in band_lists:
                if i < len(lst):
                    _add(*lst[i])
        return ordered

    def _original_by_writer(
        self,
        work_ids: set[str],
        *,
        performer: str,
        exclude_mbid: str = "",
        title_hint: str = "",
        max_candidates: int = 8,
    ) -> dict[str, Any] | None:
        """The EARLIEST recording of the work by a writer-linked performer.

        Returns ``{}`` (conclusive negative) if the performer is themselves a
        writer-linked candidate and no other writer-linked candidate recorded
        it earlier.
        """
        for work_id in sorted(work_ids or ()):
            work_title, writers = self._work_writers(work_id)
            if not writers:
                log_unified_safe(
                    f"[COVER_DETECT] original lookup: work {work_id} has no writer credits"
                )
                continue
            ranked = self._ranked_writer_candidates(writers)
            candidates: dict[str, str] = dict(ranked)

            performer_is_writer_candidate = any(
                performer and _same_artist(name, performer)
                for _aid, name in ranked
                if name
            )

            best: dict[str, Any] | None = None
            best_key = "9999-99-99"
            performer_best_key = "9999-99-99"

            def _consider(rec: dict[str, Any]) -> None:
                nonlocal best, best_key, performer_best_key
                if not isinstance(rec, dict):
                    return
                if exclude_mbid and str(rec.get("id") or "") == exclude_mbid:
                    return
                if _has_cover_annotation(str(rec.get("title") or "")):
                    return
                if _all_cover_work_ids(rec) & {work_id}:
                    return
                for credit in rec.get("artist-credit") or []:
                    if not isinstance(credit, dict):
                        continue
                    artist = credit.get("artist") or {}
                    aid = str(artist.get("id") or "")
                    if aid not in candidates:
                        continue
                    name = str(artist.get("name") or credit.get("name") or candidates[aid]).strip()
                    if not name:
                        return
                    key = _release_date_key(rec)
                    if performer and _same_artist(name, performer):
                        if key < performer_best_key:
                            performer_best_key = key
                        return
                    if best is None or key < best_key:
                        best_key = key
                        best = {
                            "artist": name,
                            "year": year_from_recording(rec),
                            "confidence": "medium",
                        }
                    return

            page = self._work_recordings(work_id)
            for rec in page:
                _consider(rec)

            # If the performer IS one of the writer-linked bands/artists and
            # recorded it first (or is the only writer-linked artist on the
            # work), then the performer originated the song!
            if performer_is_writer_candidate:
                if best is None:
                    return {}
                if not _undated(performer_best_key) and performer_best_key <= best_key:
                    return {}

            titles = [t for t in dict.fromkeys([
                work_title,
                _COVER_SUFFIX_RE.sub("", str(title_hint or "")).strip(),
            ]) if t]
            searches = 0
            if titles:
                targets = {canonical_track_title(t) for t in titles}

                def _search(query: str) -> int:
                    nonlocal searches
                    searches += 1
                    try:
                        found = self.mb.search_recordings(query, limit=10) or []
                    except Exception as exc:
                        logger.debug("Writer-performer search failed", query=query, error=str(exc))
                        return 0
                    hits = 0
                    for rec in found:
                        if canonical_track_title(str(rec.get("title") or "")) in targets:
                            _consider(rec)
                            hits += 1
                    return hits

                total_hits = 0
                if best is None or not page:
                    for aid, _name in ranked[:max_candidates]:
                        for ttl in titles:
                            hits = _search(
                                f'arid:{aid} AND recording:"{escape_lucene_special_chars(ttl)}"'
                            )
                            total_hits += hits
                            if hits:
                                break
                    if total_hits == 0:
                        for _aid, aname in ranked[:max_candidates]:
                            if not aname:
                                continue
                            _search(
                                f'artist:"{escape_lucene_special_chars(aname)}" AND '
                                f'recording:"{escape_lucene_special_chars(titles[0])}"'
                            )
                            if best is not None:
                                break

            if performer_is_writer_candidate:
                if best is None:
                    return {}
                if not _undated(performer_best_key) and performer_best_key <= best_key:
                    return {}

            log_unified_safe(
                "[COVER_DETECT] original lookup: writers="
                + ", ".join(w["name"] for w in writers[:4])
                + " | candidates=" + (", ".join(n for _a, n in ranked[:max_candidates]) or "none")
                + f" | page={len(page)} | searches={searches}"
                + " | result=" + (
                    f"{best['artist']} ({best_key[:10]})" if best else "none"
                )
            )
            if best:
                return best
        return None

    def _earliest_across_pages(
        self,
        work_ids: set[str],
        *,
        performer: str,
        exclude_mbid: str = "",
        max_pages: int = 4,
    ) -> dict[str, Any] | None:
        """Earliest non-performer recording of the work across several pages."""
        for work_id in sorted(work_ids or ()):
            best: dict[str, Any] | None = None
            best_key = "9999-99-99"
            per_artist: dict[str, int] = {}
            seen = performer_n = covers = undated = 0
            pages_read = 0
            for page in range(max_pages):
                try:
                    if page == 0:
                        recordings = self._work_recordings(work_id)
                    else:
                        recordings = self.mb.browse_work_recordings(
                            work_id,
                            inc="artist-credits+releases",
                            limit=100,
                            offset=page * 100,
                        ) or []
                except TypeError:
                    break  # HTTP client predates ``offset``
                except Exception as exc:
                    logger.debug("Work page browse failed", work_mbid=work_id, page=page, error=str(exc))
                    break
                pages_read += 1
                for rec in recordings:
                    if not isinstance(rec, dict) or not rec.get("id"):
                        continue
                    seen += 1
                    if exclude_mbid and str(rec.get("id")) == exclude_mbid:
                        continue
                    if _has_cover_annotation(str(rec.get("title") or "")):
                        covers += 1
                        continue
                    if _all_cover_work_ids(rec) & {work_id}:
                        covers += 1
                        continue
                    name = str(artist_from_credit(rec.get("artist-credit", []) or []) or "").strip()
                    if not name or is_track_artist_placeholder(name):
                        continue
                    if performer and _same_artist(name, performer):
                        performer_n += 1
                        continue
                    per_artist[name] = per_artist.get(name, 0) + 1
                    key = _release_date_key(rec)
                    if _undated(key):
                        undated += 1
                        continue
                    if best is None or key < best_key:
                        best_key = key
                        best = {
                            "artist": name,
                            "year": year_from_recording(rec),
                            "confidence": "medium",
                        }
                if len(recordings) < 100:
                    break

            how = "earliest"
            if best is None and per_artist:
                top_name, top_n = sorted(per_artist.items(), key=lambda kv: (-kv[1], kv[0]))[0]
                if top_n >= 2:
                    best = {"artist": top_name, "year": None, "confidence": "low"}
                    how = f"most recordings ({top_n})"

            log_unified_safe(
                f"[COVER_DETECT] earliest-recording lookup: work {work_id} | "
                f"pages={pages_read} seen={seen} performer={performer_n} "
                f"covers={covers} undated={undated} | result="
                + (f"{best['artist']} via {how}" if best else "none")
            )
            if best:
                return best
        return None

    def _writer_from_cover_work(
        self,
        work_ids: set[str],
        *,
        performer: str,
    ) -> dict[str, Any] | None:
        """Last resort for a STATED cover whose original recording is unknown."""
        for work_id in sorted(work_ids or ()):
            try:
                work = _ngs_relations(self.mb.get_work(work_id, inc="artist-rels") or {})
            except Exception as exc:
                logger.debug("Cover-work writer lookup failed", work_mbid=work_id, error=str(exc))
                continue
            for rel in work.get("artist-relation-list", []) or []:
                if str(rel.get("type", "")).lower() not in _WRITER_RELATION_TYPES:
                    continue
                artist = rel.get("artist") or {}
                name = str(
                    artist.get("name") or rel.get("target-credit") or ""
                ).strip()
                if name and not (performer and _same_artist(name, performer)):
                    return {"artist": name, "year": None, "confidence": "medium"}
        return None

    def _detect_via_recording_relation(
        self,
        recording_mbid: str,
        title: str,
        performer: str,
        release_mbid: str = "",
        track_year: int | None = None,
        _depth: int = 0,
    ) -> dict[str, Any] | None:
        """Cover verdict for one recording, from its MusicBrainz relations.

        ⚠️ ``performer`` is the TRACK's artist, NOT the album's. Every decision
        below is phrased relative to it ("is the candidate the performer?",
        "did the performer write the work?"), so handing it a compilation's
        album artist ("Various Artists") makes all of them vacuously false —
        which is exactly the reported "cover detection is not based on the track
        artist". Callers must resolve it through :func:`comparison_artist`.
        """
        try:
            seed = self._get_recording(
                recording_mbid, "work-rels+recording-rels+artist-credits+releases"
            )
            if not seed:
                return None

            # Modelled cover relations are the most reliable signal. Follow the
            # chain so a cover of a cover resolves to the true original.
            chain_result = self._resolve_cover_chain(
                recording_mbid, performer, seed_recording=seed
            )

            cover_work_ids = _all_cover_work_ids(seed)
            if chain_result and not cover_work_ids:
                return chain_result

            if release_mbid and _depth == 0:
                result = self._check_release_for_cover(
                    release_mbid,
                    title,
                    recording_mbid,
                    performer,
                    track_year=track_year,
                    _depth=_depth + 1,
                )
                if result:
                    return result

            if not cover_work_ids:
                return chain_result

            from_work = self._original_from_cover_work(
                cover_work_ids,
                performer=performer,
                exclude_mbid=recording_mbid,
                track_year=track_year,
            )
            if from_work:
                return from_work
            by_writer = self._original_by_writer(
                cover_work_ids,
                performer=performer,
                exclude_mbid=recording_mbid,
                title_hint=title,
            )
            if by_writer and by_writer != {}:
                return by_writer
            search_title = (
                seed.get("title") or _COVER_SUFFIX_RE.sub("", title).strip() or title
            )
            candidate = self._earliest_work_recording(
                work_ids=cover_work_ids,
                search_title=search_title,
                exclude_mbid=recording_mbid,
                album_artist=performer,
                confidence="medium",
                track_year=track_year,
            )
            if candidate:
                return candidate
            if chain_result:
                return chain_result
            deep = self._earliest_across_pages(
                cover_work_ids,
                performer=performer,
                exclude_mbid=recording_mbid,
            )
            if deep:
                return deep
            fallback = self._writer_from_cover_work(cover_work_ids, performer=performer)

            if fallback:
                fallback = {**fallback, "confidence": "low"}
            return fallback
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
        track_year: int | None = None,
    ) -> dict[str, Any] | None:
        """Find the earliest non-cover recording of the given work(s)."""
        recordings = self._search_recordings_by_title(search_title, limit=15)
        if not recordings:
            return None

        earliest: dict[str, Any] | None = None
        earliest_key = "9999-99-99"
        performer_earliest_key = (
            f"{int(track_year):04d}-99-99"
            if isinstance(track_year, int) and 1900 <= track_year <= 2100
            else "9999-99-99"
        )
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

            if not (_all_work_ids(details) & work_ids):
                continue

            rec_artist = artist_from_credit(
                details.get("artist-credit", []) or rec.get("artist-credit", [])
            )
            if not rec_artist:
                continue

            key = _release_date_key(details)
            if album_artist and _same_artist(rec_artist, album_artist):
                if key < performer_earliest_key:
                    performer_earliest_key = key
                logger.debug("Skipping candidate: Artist matches current", mbid=rid)
                continue

            # Exclude candidates that are themselves covers of this work or any work.
            if _all_cover_work_ids(details):
                logger.debug("Skipping candidate: Already marked as cover of a work", mbid=rid)
                continue
            if _has_cover_annotation(str(details.get("title") or rec.get("title") or "")):
                logger.debug("Skipping candidate: Already annotated as cover", mbid=rid)
                continue

            if _undated(key) or key >= earliest_key:
                logger.debug("Skipping candidate: Missing date or not earlier", mbid=rid, date=key, earliest=earliest_key)
                continue
            earliest_key = key
            earliest = {
                "artist": rec_artist,
                "year": year_from_recording(details),
                "confidence": confidence,
            }

        if earliest and not _undated(performer_earliest_key):
            if performer_earliest_key <= earliest_key or performer_earliest_key[:4] <= earliest_key[:4]:
                return None

        return earliest

    def _check_release_for_cover(
        self,
        release_mbid: str,
        track_title: str,
        recording_mbid: str | None,
        performer: str | None,
        track_year: int | None = None,
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
                    performer=performer or "",
                    track_year=track_year,
                    _depth=_depth,
                )
        return None

    # -- heuristics -------------------------------------------------------

    def _detect_cover_hint_from_text(self, track_title: str) -> str | None:
        """Extract an original-artist hint from the track title only."""
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
        track_year: int | None = None,
    ) -> dict[str, Any] | None:
        """Find the earliest recording of a title credited to the given writer.

        Writer-verified and unverified candidates are tracked separately,
        recordings marked as covers on MusicBrainz are skipped, and the
        performer's own earliest recording year is tracked so later covers of
        the performer's own song (e.g. The Offspring covering AFI's
        "Totalimmortal") never beat the performer's original.
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
            performer_earliest_year = (
                int(track_year)
                if isinstance(track_year, int) and 1900 <= track_year <= 2100
                else 9999
            )
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

                rec_artist = artist_from_credit(
                    details.get("artist-credit", []) or rec.get("artist-credit", [])
                )
                if not rec_artist:
                    continue

                year = year_from_recording(details)

                # Track the performer's own earliest recording year instead of
                # discarding it blindly.
                if album_artist and _same_artist(rec_artist, album_artist):
                    if year is not None and year < performer_earliest_year:
                        performer_earliest_year = year
                    continue

                # Skip candidates that MusicBrainz explicitly marks as covers
                # or that carry a "(X Cover)" title suffix.
                if _all_cover_work_ids(details):
                    logger.debug("Skipping candidate: Marked as cover work relation", mbid=rid)
                    continue
                if _has_cover_annotation(str(details.get("title") or rec.get("title") or "")):
                    continue

                if year is None:
                    logger.debug("Skipping candidate: Missing year", mbid=rid)
                    continue

                writer_names = self._writers_from_recording(details)
                writer_match = any(names_match(writer, c) for c in writer_names)

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

            # A writer-verified match always wins over unverified.
            best = verified or unverified
            if best and best.get("year") and performer_earliest_year <= best["year"]:
                return None
            return best
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
        track_year: int | None = None,
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
                track_year=track_year,
            )
        except Exception as exc:
            logger.debug("Work-based fallback failed", mbid=recording_mbid, error=str(exc))
            return None

    # -- writers ----------------------------------------------------------

    def _load_cover_checked_map(self, track_ids: list[Any]) -> dict[str, dict[str, Any]]:
        """Return the CURRENT stored cover state for *track_ids*.

        ⭐ The rows this pass is handed were read at the START of the scan; the
        track stage runs before this pass and may have just written a new
        ``is_cover`` / ``original_cover_artist`` verdict for the album. Judging
        the stale snapshot meant a flag set earlier in the SAME scan was
        invisible here: the deep pass could not confirm it, yet
        ``_clear_unconfirmed_verdicts`` saw "nothing stored" and left it set.

        Keyed by string id, each value carries ``cover_last_checked``, so this
        one query serves both the freshness window and the verdict overlay.
        Returns ``{}`` on any failure — the caller then falls back to the rows
        it was given.
        """
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
                        f"SELECT CAST(id AS TEXT), cover_last_checked, is_cover, "
                        f"original_cover_artist, cover_manual_override FROM tracks "
                        f"WHERE CAST(id AS TEXT) IN ({placeholders})"
                    ),
                    {f"tid{i}": t for i, t in enumerate(ids)},
                ).fetchall() or []
            return {
                str(r[0]): {
                    "cover_last_checked": r[1],
                    "is_cover": r[2],
                    "original_cover_artist": r[3],
                    "cover_manual_override": r[4],
                }
                for r in rows
            }
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
                    try:
                        work_details = _ngs_relations(self.mb.get_work(wid, inc="artist-rels") or {})
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

        existing = str(
            track.get("mbid")
            or track.get("recording_mbid")
            or track.get("musicbrainz_trackid")
            or ""
        ).strip()
        if existing:
            return existing

        title = str(track.get("title") or "").strip()
        if not title:
            return None

        # ⭐ The PERFORMER, never the album placeholder. A search constrained
        # to ``artist:"Various Artists"`` returns nothing (every compilation
        # track searched that way came back empty); with no usable performer the
        # title alone still finds candidates, which the title/artist check below
        # filters.
        search_artist = comparison_artist(track, album_artist or "")
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
            return _same_artist(artist_from_credit(artist_credit), search_artist)

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
        if not artist:
            return []
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
                if not _same_artist(result.get("name", ""), artist):
                    continue
                artist_mbid = result.get("id")
                if not artist_mbid:
                    continue
                details = _ngs_relations(self.mb.get_artist(artist_mbid, inc="artist-rels") or {})
                for rel in details.get("artist-relation-list", []) or []:
                    if str(rel.get("type", "")).lower() in {"member of band", "member", "is person"}:
                        rel_artist = rel.get("artist") or {}
                        for candidate in (
                            rel_artist.get("name"),
                            rel.get("target-credit"),
                            rel.get("source-credit"),
                        ):
                            if candidate and candidate not in members:
                                members.append(str(candidate).strip())
                break
        except Exception as exc:
            logger.debug("Failed to fetch band members", artist=artist, error=str(exc))

        # Only cache non-empty lists so a rate-limited request doesn't poison
        # writer-vs-band checks for the rest of the scan.
        if members:
            with self._mem_lock:
                self._band_members_cache[artist] = members
        return members

    def _is_writer_same_as_artist(self, writer: str, artist: str) -> bool:
        if _same_artist(writer, artist):
            return True
        return any(_same_artist(writer, member) for member in self._get_band_members(artist))

    # -- state helpers ----------------------------------------------------

    def _is_already_confirmed_cover(self, track: dict[str, Any]) -> bool:
        """True when the track is flagged as a cover and tagged accordingly."""
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

    @staticmethod
    def _stored_verdict_is_self_referential(track: dict[str, Any]) -> bool:
        """True when a stored cover verdict names the track's own performer."""
        if not CoverDetector._is_cover_flagged(track):
            return False
        if track.get("cover_manual_override"):
            return False

        original = str(track.get("original_cover_artist") or "").strip()
        if not original:
            return False

        performer = str(track.get("artist") or "").strip()
        if not performer or is_track_artist_placeholder(performer):
            return False
        if is_track_artist_placeholder(original):
            return False

        return _same_artist(original, performer)

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

    @staticmethod
    def _clear_cover_file_tags(file_path: str, title: str | None = None) -> bool:
        """Remove the cover verdict from the AUDIO FILE's tags.

        ``write_tags_to_file``'s contract is that ``""`` clears a frame and an
        absent key leaves it alone, so empty strings — not omission — are what
        delete ``IS_COVER`` / ``ORIGINAL_COVER_ARTIST``. Without this the next
        Navidrome import reads the stale tag back (OpenSubsonic exposes the
        file's tags) and restores the DB flag that was just cleared.
        """
        tags: dict[str, Any] = {
            "is_cover": "",
            "original_cover_artist": "",
        }
        if title:
            tags["title"] = title
        try:
            from services.metadata.tag_file_service import write_tags_to_file

            return write_tags_to_file(file_path, tags)
        except Exception as exc:
            logger.debug(
                "Failed to clear cover file tags", file_path=file_path, error=str(exc)
            )
            return False
