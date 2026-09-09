"""MusicBrainz enrichment and lookup service.

Owns MusicBrainz interpretation and matching rules. This module performs no
track mutation. Database access is limited to read-only library comparison
helpers.

Album identity rules:
- The album name supplied by the caller (the library album) is authoritative.
  A recording's specific release title never replaces it.
- The album year comes from the matched release group's ``first-release-date``
  (the original release year), not from the edition/version held in the
  collection and not from each recording's first linked release.

Operational behaviour:
- Re-entrant singleton lock so shared-service creation can request the shared
  HTTP client without deadlocking.
- Structured start, completion, skip, and failure logs for every operation.
- A single shared heartbeat monitor thread warns about in-flight calls that
  have not returned, instead of one thread per call.
- Atomic, bounded, timestamped MBID cache writes, flushed on a dirty flag
  rather than on every match.
- Plain Cover Art Archive URLs.

Request-volume controls:
- Per-artist release-group results are memoised, so the artist-wide single
  detection fallback fires once per artist instead of once per track.
- Best-release browses are memoised per release group, so release-group
  scoring does not re-browse the same group for each candidate.
- Availability of the HTTP client is checked before firing broad fallback
  queries, so a MusicBrainz outage does not amplify into heavier requests.

Web-service correctness notes:
- ``recording-level-rels`` is a RELEASE-level subquery ("include relationships
  for the recordings on this release"). It is not a valid ``inc`` value on the
  recording resource and makes MusicBrainz answer 400, so recording
  relationship lookups must not request it.
- An artist's ``begin-area`` is frequently a city, so it cannot be used as a
  country without checking the area type.

Public exports required by other modules:
    get_shared_mb_client, get_shared_mb_service, lookup_recording_metadata,
    merge_metadata, fetch_musicbrainz_release_metadata, fetch_release_metadata,
    resolve_release_id, lookup_musicbrainz_album, get_release_group_releases,
    get_musicbrainz_best_release, compare_musicbrainz_release
"""
from __future__ import annotations

import atexit
import difflib
import itertools
import json
import os
import re
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from typing import Any, Callable, Iterator, TypeVar

import structlog

try:
    from rapidfuzz import fuzz as _rapidfuzz_fuzz  # type: ignore[import-untyped]
    _HAVE_RAPIDFUZZ = True
except ImportError:
    _rapidfuzz_fuzz = None
    _HAVE_RAPIDFUZZ = False

from api_clients.musicbrainz_http import (
    MusicBrainzHttpClient,
    escape_lucene_special_chars,
)
from helpers.normalization_service import (
    edition_annotations_compatible,
    normalize_string,
    normalize_title_for_lookup,
    normalize_title_for_lucene_query,
    normalize_title_for_mbid_match,
    strip_featured_artist,
    strip_search_keywords,
    strip_single_release_suffix,
)

logger = structlog.get_logger(__name__)

T = TypeVar("T")

__all__ = [
    "MusicBrainzService",
    "build_artist_credit_string",
    "calculate_match_score",
    "compare_musicbrainz_release",
    "fetch_musicbrainz_release_metadata",
    "fetch_release_metadata",
    "get_musicbrainz_best_release",
    "get_release_group_releases",
    "get_shared_mb_client",
    "get_shared_mb_service",
    "lookup_musicbrainz_album",
    "lookup_recording_metadata",
    "merge_metadata",
    "primary_album_artist",
    "resolve_release_id",
]

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _default_cache_file() -> str:
    """Choose a cache path that survives a container restart where possible.

    ``/tmp`` is deliberately the last resort: a worker restart wipes it and
    every MBID has to be looked up again, which is exactly the request storm
    this cache exists to prevent.
    """
    for candidate in ("/data", "/config", "/var/lib/popularr"):
        if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
            return os.path.join(candidate, "mbid_cache.json")
    return "mbid_cache.json"


CACHE_FILE = os.getenv("MUSICBRAINZ_CACHE_FILE", "") or _default_cache_file()

_HEARTBEAT_SECONDS = max(5.0, float(os.getenv("MUSICBRAINZ_HEARTBEAT_SECONDS", "30")))
_MONITOR_TICK_SECONDS = 2.0

# Flush the MBID cache to disk at most this often instead of on every write.
_CACHE_FLUSH_SECONDS = max(
    5.0, float(os.getenv("MUSICBRAINZ_CACHE_FLUSH_SECONDS", "30"))
)

_CACHE_IO_LOCK = threading.Lock()
# Must be re-entrant. get_shared_mb_service() holds this lock and then calls
# get_shared_mb_client(), which acquires it again on the same thread.
_INIT_LOCK = threading.RLock()

_MB_BATCH_CHUNK = 20
_MB_BATCH_SIMILARITY_FLOOR = 0.6
_MBID_CACHE_SIMILARITY_FLOOR = 0.6
_MBID_CACHE_TTL_SECONDS = 30 * 24 * 3600
_MBID_CACHE_MAX_SIZE = 5000
_RELEASE_GROUP_MATCH_FLOOR = 0.6

# How many release-group candidates get the (expensive) track-count refinement.
_TRACK_COUNT_REFINE_LIMIT = 3

# Valid `inc` values for a RECORDING lookup. `recording-level-rels` is a
# release-level subquery and makes MusicBrainz reject the request with 400.
_RECORDING_RELATIONSHIP_INC = "artist-rels+work-rels+work-level-rels"

_COMPARE_LIBRARY_TRACKS_SQL = """
    SELECT id, title, track_number, disc_number, artist, year,
           mbid, file_path, duration, mb_ignored_fields
    FROM tracks
    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
      AND LOWER(COALESCE(album, '')) = LOWER(:album)
    ORDER BY COALESCE(disc_number, '1'), COALESCE(track_number, '999')
"""


# ---------------------------------------------------------------------------
# Diagnostics helpers
# ---------------------------------------------------------------------------

def _error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _year_of(value: Any) -> int | None:
    """Return the four-digit year from a MusicBrainz date value."""
    text = str(value or "").strip()
    return int(text[:4]) if len(text) >= 4 and text[:4].isdigit() else None


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce a MusicBrainz numeric field that may arrive as a string."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


@contextmanager
def _logged_section(section: str, **context: Any) -> Iterator[None]:
    started = time.monotonic()
    logger.info("[MB] section started", section=section, **context)
    try:
        yield
    except Exception as exc:
        logger.exception(
            "[MB] section failed",
            section=section,
            elapsed_s=round(time.monotonic() - started, 3),
            error=_error(exc),
            **context,
        )
        raise
    else:
        logger.info(
            "[MB] section completed",
            section=section,
            elapsed_s=round(time.monotonic() - started, 3),
            **context,
        )


# ---------------------------------------------------------------------------
# Shared heartbeat monitor
#
# One background thread watches every in-flight call, rather than spawning a
# thread per call. At a few hundred MusicBrainz calls per scan the old
# per-call threads were pure overhead.
# ---------------------------------------------------------------------------

_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: "OrderedDict[int, dict[str, Any]]" = OrderedDict()
_INFLIGHT_IDS = itertools.count()
_MONITOR_THREAD: threading.Thread | None = None


def _monitor_loop() -> None:
    while True:
        time.sleep(_MONITOR_TICK_SECONDS)
        now = time.monotonic()
        due: list[dict[str, Any]] = []
        with _INFLIGHT_LOCK:
            for entry in _INFLIGHT.values():
                if now >= entry["next_warn"]:
                    entry["next_warn"] = now + _HEARTBEAT_SECONDS
                    due.append(
                        {
                            "section": entry["section"],
                            "elapsed_s": round(now - entry["started"], 1),
                            "context": entry["context"],
                        }
                    )
        for item in due:
            logger.warning(
                "[MB] call still running",
                section=item["section"],
                elapsed_s=item["elapsed_s"],
                **item["context"],
            )


def _ensure_monitor() -> None:
    global _MONITOR_THREAD
    if _MONITOR_THREAD is not None and _MONITOR_THREAD.is_alive():
        return
    with _INIT_LOCK:
        if _MONITOR_THREAD is None or not _MONITOR_THREAD.is_alive():
            _MONITOR_THREAD = threading.Thread(
                target=_monitor_loop,
                name="mb-heartbeat-monitor",
                daemon=True,
            )
            _MONITOR_THREAD.start()


def _call_with_heartbeat(
    section: str,
    func: Callable[..., T],
    *args: Any,
    log_context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> T:
    """Run a call synchronously, warning periodically while it is still running.

    This does not cancel a blocked request. Connection and read timeouts belong
    in MusicBrainzHttpClient. This makes a stalled call visible in the logs.

    Note that the "started" log is emitted before the call enters the HTTP
    client's rate-limit turnstile, so overlapping start/complete pairs in the
    log indicate queued callers, not concurrent HTTP requests.
    """
    context = dict(log_context or {})
    started = time.monotonic()
    _ensure_monitor()
    call_id = next(_INFLIGHT_IDS)
    with _INFLIGHT_LOCK:
        _INFLIGHT[call_id] = {
            "section": section,
            "started": started,
            "context": context,
            "next_warn": started + _HEARTBEAT_SECONDS,
        }

    logger.info("[MB] call started", section=section, **context)
    try:
        result = func(*args, **kwargs)
    except Exception as exc:
        logger.exception(
            "[MB] call failed",
            section=section,
            elapsed_s=round(time.monotonic() - started, 3),
            error=_error(exc),
            **context,
        )
        raise
    else:
        logger.info(
            "[MB] call completed",
            section=section,
            elapsed_s=round(time.monotonic() - started, 3),
            **context,
        )
        return result
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(call_id, None)


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

def _similarity(a: str, b: str) -> float:
    from services.popularity.popularity_math import fuzzy_match_score
    return fuzzy_match_score(a, b)


def _mbid_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if _HAVE_RAPIDFUZZ and _rapidfuzz_fuzz is not None:
        return _rapidfuzz_fuzz.token_sort_ratio(a, b) / 100.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def build_artist_credit_string(artist_credit: list[Any]) -> str:
    parts: list[str] = []
    for credit in artist_credit or []:
        if isinstance(credit, dict):
            parts.append(str(credit.get("name") or ""))
            parts.append(str(credit.get("joinphrase") or ""))
        else:
            parts.append(str(credit))
    return "".join(parts).strip()


def primary_album_artist(artist_credit: list[Any] | str) -> str:
    if isinstance(artist_credit, list) and artist_credit:
        first = artist_credit[0]
        if isinstance(first, dict):
            return str(first.get("name") or "").strip()
        return str(first or "").strip()
    if isinstance(artist_credit, str):
        return artist_credit.strip()
    return ""


def calculate_match_score(
    mb_title: str,
    mb_artist_credit: list[Any] | str,
    local_album: str,
    local_artist: str,
) -> float:
    title_score = _similarity(normalize_string(local_album), normalize_string(mb_title))
    artist_score = _similarity(
        normalize_string(local_artist),
        normalize_string(primary_album_artist(mb_artist_credit)),
    )
    return (title_score * 0.6) + (artist_score * 0.4)


def _parse_secondary_types(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(part).strip() for part in raw if str(part).strip()]
    return []


def _compose_album_type(primary_type: str, secondary_types: list[str]) -> str:
    primary = str(primary_type or "album").strip().casefold() or "album"
    meaningful = {
        "compilation", "live", "remix", "soundtrack", "spokenword",
        "demo", "dj-mix", "mixtape", "interview", "audiobook", "ep",
    }
    secondary = next(
        (
            str(value).casefold()
            for value in secondary_types or []
            if str(value).casefold() in meaningful
        ),
        "",
    )
    return f"{primary}+{secondary}" if secondary else primary


def _release_group_primary_type(group: Any) -> str:
    """Read a release group's primary type across MB response shapes."""
    if not isinstance(group, dict):
        return ""
    return str(
        group.get("primary-type")
        or group.get("primary_type")
        or group.get("type")
        or ""
    ).casefold()


def _country_area_name(area: Any) -> str:
    """Return an area's name only when that area really is a country.

    MusicBrainz areas are hierarchical: an artist's ``area`` is usually the
    country, but ``begin-area`` is very often a city or a subdivision. Reading
    either one blindly stored values like "Paris" in the artist country field.

    An untyped area is accepted for backwards compatibility, because search
    results do not always include the ``type`` attribute and ``area`` is the
    country in the overwhelming majority of those responses.
    """
    if not isinstance(area, dict):
        return ""
    name = str(area.get("name") or "").strip()
    if not name:
        return ""
    area_type = str(area.get("type") or "").strip().casefold()
    if area_type and area_type != "country":
        return ""
    return name


def _artist_country_name(data: Any) -> str:
    """Extract a country name from a MusicBrainz artist document."""
    if not isinstance(data, dict):
        return ""
    for key in ("area", "begin-area", "beginArea"):
        name = _country_area_name(data.get(key))
        if name:
            return name
    return ""


def _artist_lookup_candidates(artist: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for candidate in (artist or "", strip_featured_artist(artist or "")):
        key = str(candidate or "").casefold().strip()
        if key and key not in seen:
            result.append(str(candidate).strip())
            seen.add(key)
    return result


def _normalise_artist_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _mb_artist_credit_name(artist_credit: list[Any] | str) -> str:
    return primary_album_artist(artist_credit)


def _cover_art_url(release_group_id: str = "", release_id: str = "") -> str:
    if release_group_id:
        return f"https://coverartarchive.org/release-group/{release_group_id}/front-250"
    if release_id:
        return f"https://coverartarchive.org/release/{release_id}/front-250"
    return ""


def _first_isrc(recording: dict[str, Any]) -> str | None:
    from helpers.normalization_service import normalize_isrc
    for raw in recording.get("isrcs") or recording.get("isrc-list") or []:
        value = normalize_isrc(raw)
        if value:
            return value
    return normalize_isrc(recording.get("isrc")) or None


def _recording_matches_album(recording: dict[str, Any], album: str) -> bool:
    album = str(album or "").strip().casefold()
    if not album or not recording:
        return False
    for release in recording.get("releases") or []:
        if not isinstance(release, dict):
            continue
        release_group = release.get("release-group") or {}
        candidates = (
            release.get("title") or "",
            release_group.get("title") if isinstance(release_group, dict) else "",
        )
        if any(value and _similarity(str(value), album) >= 0.6 for value in candidates):
            return True
    return False


def _release_track_count(release: Any) -> int:
    """Total track count for a release, tolerating string counts."""
    if not isinstance(release, dict):
        return 0
    direct = _as_int(release.get("track-count"), 0)
    if direct > 0:
        return direct
    return sum(
        _as_int(medium.get("track-count"), 0)
        for medium in release.get("media") or []
        if isinstance(medium, dict)
    )


def _client_available(client: Any) -> bool:
    """Return False when the HTTP client reports MusicBrainz as unavailable."""
    checker = getattr(client, "is_available", None)
    if not callable(checker):
        return True
    try:
        return bool(checker())
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

_SHARED_MB_CLIENT: MusicBrainzHttpClient | None = None
_service: "MusicBrainzService | None" = None
_shared_mb_service: "MusicBrainzService | None" = None


def get_shared_mb_client() -> MusicBrainzHttpClient:
    """Return the process-wide shared MusicBrainz HTTP client."""
    global _SHARED_MB_CLIENT

    if _SHARED_MB_CLIENT is not None:
        return _SHARED_MB_CLIENT

    started = time.monotonic()
    logger.info("[MB] shared HTTP client initialization requested")

    with _INIT_LOCK:
        if _SHARED_MB_CLIENT is None:
            creation_started = time.monotonic()
            try:
                _SHARED_MB_CLIENT = MusicBrainzHttpClient(enabled=True)
            except Exception as exc:
                logger.exception(
                    "[MB] shared HTTP client creation failed",
                    elapsed_s=round(time.monotonic() - creation_started, 3),
                    error=_error(exc),
                )
                raise
            logger.info(
                "[MB] shared HTTP client creation completed",
                client_type=type(_SHARED_MB_CLIENT).__name__,
                elapsed_s=round(time.monotonic() - creation_started, 3),
            )
        else:
            logger.debug("[MB] shared HTTP client was initialized by another caller")

    logger.info(
        "[MB] shared HTTP client ready",
        total_s=round(time.monotonic() - started, 3),
    )
    return _SHARED_MB_CLIENT


# ---------------------------------------------------------------------------
# Best-release memoisation
#
# Release-group scoring refines its top candidates by track count, and the
# comparison path resolves the same group again. Without memoisation a single
# album could trigger six or more identical browse requests.
# ---------------------------------------------------------------------------

_BEST_RELEASE_LOCK = threading.Lock()
_BEST_RELEASE_CACHE: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_BEST_RELEASE_CACHE_MAX = 500


def _best_release_cache_get(key: str) -> dict[str, Any] | None:
    with _BEST_RELEASE_LOCK:
        if key not in _BEST_RELEASE_CACHE:
            return None
        _BEST_RELEASE_CACHE.move_to_end(key)
        return _BEST_RELEASE_CACHE[key]


def _best_release_cache_set(key: str, value: dict[str, Any]) -> None:
    with _BEST_RELEASE_LOCK:
        _BEST_RELEASE_CACHE[key] = value
        _BEST_RELEASE_CACHE.move_to_end(key)
        while len(_BEST_RELEASE_CACHE) > _BEST_RELEASE_CACHE_MAX:
            _BEST_RELEASE_CACHE.popitem(last=False)


def clear_release_caches() -> None:
    """Drop memoised best-release results between major pipeline runs."""
    with _BEST_RELEASE_LOCK:
        _BEST_RELEASE_CACHE.clear()


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class MusicBrainzService:
    """MusicBrainz lookup, matching, and interpretation."""

    def __init__(
        self,
        http_client: MusicBrainzHttpClient | None = None,
        enabled: bool = True,
    ) -> None:
        started = time.monotonic()
        logger.info(
            "[MB] service construction started",
            enabled=enabled,
            supplied_client=http_client is not None,
        )
        self.enabled = enabled
        self.http = http_client or MusicBrainzHttpClient(enabled=enabled)
        # Memoised artist-wide release-group results, keyed by casefolded artist.
        self._artist_singles_cache: dict[str, list[dict[str, Any]]] = {}
        self._album_year_cache: dict[str, int | None] = {}
        self._single_result_cache: dict[str, bool] = {}
        self._mem_lock = threading.Lock()
        self._cache_dirty = False
        self._cache_last_save = time.monotonic()
        self._mbid_cache: "OrderedDict[str, Any]" = self._load_cache()
        self._missing_release_group_warned = False
        atexit.register(self.flush_cache)
        logger.info(
            "[MB] service construction completed",
            enabled=enabled,
            cache_file=CACHE_FILE,
            cache_entries=len(self._mbid_cache),
            elapsed_s=round(time.monotonic() - started, 3),
        )

    # -- cache ------------------------------------------------------------

    def _load_cache(self) -> "OrderedDict[str, Any]":
        started = time.monotonic()
        logger.info("[MB] cache load started", cache_file=CACHE_FILE)
        with _CACHE_IO_LOCK:
            try:
                if not os.path.exists(CACHE_FILE):
                    logger.info(
                        "[MB] cache load skipped",
                        reason="cache file not found",
                        cache_file=CACHE_FILE,
                    )
                    return OrderedDict()
                with open(CACHE_FILE, "r", encoding="utf-8") as handle:
                    raw = json.load(handle)
                if not isinstance(raw, dict):
                    logger.warning(
                        "[MB] cache load discarded",
                        reason="cache root was not an object",
                        cache_file=CACHE_FILE,
                    )
                    return OrderedDict()
                output: "OrderedDict[str, Any]" = OrderedDict(
                    (key, value)
                    for key, value in raw.items()
                    if isinstance(value, (list, tuple))
                    and len(value) >= 2
                    and str(value[0] or "").strip()
                )
                logger.info(
                    "[MB] cache load completed",
                    cache_file=CACHE_FILE,
                    entries=len(output),
                    discarded=len(raw) - len(output),
                    elapsed_s=round(time.monotonic() - started, 3),
                )
                return output
            except Exception as exc:
                logger.exception(
                    "[MB] cache load failed",
                    cache_file=CACHE_FILE,
                    error=_error(exc),
                )
                return OrderedDict()

    def _record_mbid(self, key: str, mbid: str, confidence: float) -> None:
        """Insert an MBID result and mark the cache for a later flush."""
        with self._mem_lock:
            self._mbid_cache[key] = (mbid, confidence, time.time())
            self._mbid_cache.move_to_end(key)
            # True LRU eviction: the least recently *used* entry goes first.
            while len(self._mbid_cache) > _MBID_CACHE_MAX_SIZE:
                try:
                    self._mbid_cache.popitem(last=False)
                except KeyError:
                    break
            self._cache_dirty = True

    def _maybe_flush_cache(self, force: bool = False) -> None:
        """Write the cache only when dirty and not written too recently."""
        with self._mem_lock:
            if not self._cache_dirty:
                return
            now = time.monotonic()
            if not force and (now - self._cache_last_save) < _CACHE_FLUSH_SECONDS:
                return
            data = dict(self._mbid_cache)
            self._cache_dirty = False
            self._cache_last_save = now

        started = time.monotonic()
        with _CACHE_IO_LOCK:
            try:
                directory = os.path.dirname(CACHE_FILE)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                tmp_path = f"{CACHE_FILE}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, CACHE_FILE)
                logger.debug(
                    "[MB] cache save completed",
                    cache_file=CACHE_FILE,
                    entries=len(data),
                    elapsed_s=round(time.monotonic() - started, 3),
                )
            except Exception as exc:
                with self._mem_lock:
                    self._cache_dirty = True
                logger.exception(
                    "[MB] cache save failed",
                    cache_file=CACHE_FILE,
                    error=_error(exc),
                )

    def flush_cache(self) -> None:
        """Force any pending MBID cache changes to disk."""
        self._maybe_flush_cache(force=True)

    def clear_transient_caches(self) -> None:
        """Drop per-run memoisation between major multi-artist pipeline runs."""
        with self._mem_lock:
            self._artist_singles_cache.clear()
            self._album_year_cache.clear()
            self._single_result_cache.clear()
        clear_release_caches()
        clearer = getattr(self.http, "clear_caches", None)
        if callable(clearer):
            clearer()

    @staticmethod
    def _cache_key(title: str, artist: str) -> str:
        return f"{artist.casefold().strip()}::{title.casefold().strip()}"

    # -- recording lookups -------------------------------------------------

    def get_suggested_mbid(
        self,
        title: str,
        artist: str,
        limit: int = 5,
    ) -> tuple[str, float]:
        context = {"artist": artist, "track": title, "limit": limit}
        if not self.enabled or not title or not artist:
            logger.info(
                "[MB] recording suggestion skipped",
                reason="service disabled or incomplete input",
                **context,
            )
            return "", 0.0

        cache_key = self._cache_key(title, artist)
        now = time.time()
        with self._mem_lock:
            cached = self._mbid_cache.get(cache_key)
            if isinstance(cached, (list, tuple)) and len(cached) >= 2:
                mbid = str(cached[0] or "")
                score = float(cached[1] or 0)
                cached_at = float(cached[2]) if len(cached) >= 3 else None
                if mbid and (
                    cached_at is None or now - cached_at < _MBID_CACHE_TTL_SECONDS
                ):
                    self._mbid_cache.move_to_end(cache_key)
                    logger.info(
                        "[MB] recording suggestion cache hit",
                        mbid=mbid,
                        score=round(score, 3),
                        **context,
                    )
                    return mbid, round(score, 3)

        query_title = normalize_title_for_lucene_query(strip_search_keywords(title))
        query = (
            f'recording:"{escape_lucene_special_chars(query_title)}" '
            f'AND artist:"{escape_lucene_special_chars(artist)}"'
        )
        try:
            recordings = _call_with_heartbeat(
                "recording.search",
                self.http.search_recordings,
                query,
                limit=limit,
                log_context=context,
            ) or []
            best_mbid, best_score = "", 0.0
            normalized_title = normalize_title_for_mbid_match(title)
            for recording in recordings:
                if not isinstance(recording, dict):
                    continue
                candidate_title = str(recording.get("title") or "")
                if not edition_annotations_compatible(title, candidate_title):
                    continue
                score = _mbid_similarity(
                    normalized_title,
                    normalize_title_for_mbid_match(candidate_title),
                )
                if score > best_score:
                    best_mbid = str(recording.get("id") or "")
                    best_score = score

            if best_mbid and best_score >= _MBID_CACHE_SIMILARITY_FLOOR:
                self._record_mbid(cache_key, best_mbid, round(best_score, 3))
                self._maybe_flush_cache()

            logger.info(
                "[MB] recording suggestion completed",
                mbid=best_mbid or None,
                score=round(best_score, 3),
                candidate_count=len(recordings),
                **context,
            )
            return best_mbid, round(best_score, 3)
        except Exception as exc:
            logger.exception(
                "[MB] recording suggestion failed",
                error=_error(exc),
                **context,
            )
            return "", 0.0

    def lookup_recording_metadata(self, title: str, artist: str) -> dict[str, Any]:
        context = {"title": title, "artist": artist}
        if not title or not artist:
            logger.info(
                "[MB] recording metadata skipped",
                reason="incomplete input",
                **context,
            )
            return {}
        try:
            mbid, confidence = self.get_suggested_mbid(title, artist)
            if not mbid:
                return {}
            recording = _call_with_heartbeat(
                "recording.get",
                self.http.get_recording,
                mbid,
                inc="artist-credits+releases+release-groups+work-rels+genres",
                log_context={**context, "mbid": mbid},
            )
            if not recording:
                logger.info("[MB] recording metadata empty", mbid=mbid, **context)
                return {}
            # Single-track lookup: no album authority is available here, so the
            # recording's own release information is used.
            return self._recording_to_metadata(recording, mbid, confidence)
        except Exception as exc:
            logger.exception(
                "[MB] recording metadata lookup failed",
                error=_error(exc),
                **context,
            )
            return {}

    def lookup_recordings_by_mbid_bulk(
        self,
        mbids: list[str],
        *,
        album_name: str | None = None,
        original_release_year: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Fetch recordings, applying album-level identity when supplied."""
        if not self.enabled or not mbids:
            logger.info(
                "[MB] bulk recording lookup skipped",
                reason="service disabled" if not self.enabled else "no MBIDs supplied",
                authoritative_album_name=album_name,
                original_release_year=original_release_year,
            )
            return {}

        context = {
            "mbid_count": len(mbids),
            "authoritative_album_name": album_name,
            "original_release_year": original_release_year,
        }
        try:
            payload = _call_with_heartbeat(
                "recording.bulk_get",
                self.http.get_recordings_bulk,
                mbids,
                inc="artist-credits+releases+release-groups+work-rels+genres",
                log_context=context,
            ) or {}
            results: dict[str, dict[str, Any]] = {}
            for recording in payload.get("recordings", []) or []:
                if not isinstance(recording, dict):
                    continue
                mbid = str(recording.get("id") or "").strip()
                if not mbid:
                    continue
                results[mbid] = self._recording_to_metadata(
                    recording,
                    mbid,
                    1.0,
                    album_name=album_name,
                    original_release_year=original_release_year,
                )
            logger.info(
                "[MB] bulk recording lookup completed",
                returned=len(results),
                requested=len(mbids),
                **context,
            )
            return results
        except Exception as exc:
            logger.exception(
                "[MB] bulk recording lookup failed",
                error=_error(exc),
                **context,
            )
            return {}

    def _recording_to_metadata(
        self,
        recording: dict[str, Any],
        mbid: str,
        confidence: float,
        *,
        album_name: str | None = None,
        original_release_year: int | None = None,
    ) -> dict[str, Any]:
        """Convert a recording without adopting a specific release identity.

        ``album_name`` and ``original_release_year`` are authoritative when
        supplied. The specific release title and its date are retained only as
        diagnostic fields.
        """
        credits = recording.get("artist-credit") or []
        first = credits[0] if credits else {}
        if isinstance(first, dict):
            artist = str(first.get("name") or "").strip()
            artist_data = first.get("artist") or {}
            artist_mbid = (
                str(artist_data.get("id") or "").strip()
                if isinstance(artist_data, dict)
                else ""
            )
        else:
            artist = str(first or "").strip()
            artist_mbid = ""

        releases = recording.get("releases") or []
        specific_release = (
            releases[0] if releases and isinstance(releases[0], dict) else {}
        )
        specific_title = str(specific_release.get("title") or "").strip()
        version_release_year = _year_of(specific_release.get("date"))

        release_group = specific_release.get("release-group") or {}
        release_group_title = str(release_group.get("title") or "").strip()

        authoritative_album = str(album_name or "").strip()
        effective_album = authoritative_album or release_group_title or specific_title
        effective_year = (
            original_release_year
            if original_release_year is not None
            else version_release_year
        )

        if (
            authoritative_album
            and specific_title
            and authoritative_album.casefold() != specific_title.casefold()
        ):
            logger.debug(
                "[MB] specific release title ignored in favour of album name",
                recording_mbid=mbid,
                authoritative_album_name=authoritative_album,
                ignored_release_title=specific_title,
            )
        if (
            original_release_year is not None
            and version_release_year is not None
            and version_release_year != original_release_year
        ):
            logger.debug(
                "[MB] version release year ignored in favour of original year",
                recording_mbid=mbid,
                ignored_version_year=version_release_year,
                original_release_year=original_release_year,
            )

        writers: list[str] = []
        work_mbid = ""
        try:
            for relation in recording.get("relations") or []:
                if not isinstance(relation, dict):
                    continue
                if str(relation.get("type") or "").casefold() not in {
                    "performance",
                    "recording of",
                }:
                    continue
                work = relation.get("work") or {}
                if not isinstance(work, dict):
                    continue
                if work.get("id"):
                    work_mbid = str(work.get("id"))
                for work_relation in work.get("relations") or []:
                    if not isinstance(work_relation, dict):
                        continue
                    if str(work_relation.get("type") or "").casefold() not in {
                        "composer",
                        "writer",
                        "lyricist",
                    }:
                        continue
                    target = work_relation.get("artist") or {}
                    if isinstance(target, dict) and target.get("name"):
                        writers.append(str(target["name"]))
        except Exception as exc:
            logger.warning(
                "[MB] recording relationship parsing failed",
                recording_mbid=mbid,
                error=_error(exc),
            )

        genres = [
            str(item.get("name") or "").strip()
            for item in recording.get("genres") or []
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]

        return {
            "title": recording.get("title"),
            "artist": artist,
            "artist_mbid": artist_mbid or None,
            # Library album name wins over the specific release title.
            "album": effective_album,
            "album_artist": primary_album_artist(
                specific_release.get("artist-credit") or []
            ),
            "isrc": _first_isrc(recording),
            # Original release-group year wins over the version's year.
            "year": effective_year,
            "original_release_year": original_release_year,
            "version_release_year": version_release_year,
            "musicbrainz_release_title": specific_title,
            "recording_mbid": mbid,
            "confidence": confidence,
            "writer": ", ".join(dict.fromkeys(writers)),
            "work_mbid": work_mbid,
            "genres": list(dict.fromkeys(genres)),
        }

    # -- album-level authority --------------------------------------------

    def lookup_original_album_year(self, artist: str, album: str) -> int | None:
        """Return the matched release group's original first-release year.

        The release group's ``first-release-date`` represents the album's
        original release, not the date of the edition held in the collection.
        """
        context = {"artist": artist, "album": album}
        if not self.enabled or not artist or not album:
            logger.info(
                "[MB] original album year lookup skipped",
                reason="service disabled or incomplete input",
                **context,
            )
            return None

        cache_key = f"{artist.casefold().strip()}::{album.casefold().strip()}"
        with self._mem_lock:
            if cache_key in self._album_year_cache:
                cached_year = self._album_year_cache[cache_key]
                logger.info(
                    "[MB] original album year cache hit",
                    original_release_year=cached_year,
                    **context,
                )
                return cached_year

        clean_album = strip_search_keywords(album)
        query = (
            f'artist:"{escape_lucene_special_chars(artist)}" '
            f'AND releasegroup:"{escape_lucene_special_chars(clean_album)}"'
        )
        started = time.monotonic()
        logger.info(
            "[MB] original album year lookup started",
            clean_album=clean_album,
            query=query,
            **context,
        )
        try:
            groups = _call_with_heartbeat(
                "album.original_year_search",
                self.http.search_release_groups,
                query,
                limit=5,
                log_context={**context, "query": query},
            ) or []

            # Only widen the search when the client is healthy. An empty result
            # caused by a 503 must not escalate into a second query.
            if not groups and clean_album and _client_available(self.http):
                terms = normalize_title_for_lucene_query(clean_album)
                if terms:
                    fallback_query = (
                        f'artist:"{escape_lucene_special_chars(artist)}" '
                        f"AND releasegroup:{terms}"
                    )
                    groups = _call_with_heartbeat(
                        "album.original_year_fallback_search",
                        self.http.search_release_groups,
                        fallback_query,
                        limit=5,
                        log_context={**context, "query": fallback_query},
                    ) or []

            ranked: list[tuple[float, int, dict[str, Any]]] = []
            for group in groups:
                if not isinstance(group, dict):
                    continue
                year = _year_of(group.get("first-release-date"))
                if year is None:
                    logger.debug(
                        "[MB] release-group candidate has no first-release-date",
                        release_group_mbid=group.get("id"),
                        candidate_title=group.get("title"),
                        **context,
                    )
                    continue
                score = calculate_match_score(
                    str(group.get("title") or ""),
                    group.get("artist-credit") or [],
                    album,
                    artist,
                )
                ranked.append((score, year, group))

            ranked.sort(key=lambda item: item[0], reverse=True)

            if not ranked:
                logger.warning(
                    "[MB] original album year unavailable",
                    reason="no dated release-group candidates",
                    candidate_count=len(groups),
                    elapsed_s=round(time.monotonic() - started, 3),
                    **context,
                )
                with self._mem_lock:
                    self._album_year_cache[cache_key] = None
                return None

            score, year, group = ranked[0]
            if score < _RELEASE_GROUP_MATCH_FLOOR:
                logger.warning(
                    "[MB] original album year rejected",
                    reason="best release-group match below threshold",
                    candidate_year=year,
                    candidate_title=group.get("title"),
                    release_group_mbid=group.get("id"),
                    match_score=round(score, 3),
                    elapsed_s=round(time.monotonic() - started, 3),
                    **context,
                )
                with self._mem_lock:
                    self._album_year_cache[cache_key] = None
                return None

            logger.info(
                "[MB] original album year selected",
                original_release_year=year,
                release_group_mbid=group.get("id"),
                matched_release_group_title=group.get("title"),
                first_release_date=group.get("first-release-date"),
                match_score=round(score, 3),
                candidate_count=len(groups),
                elapsed_s=round(time.monotonic() - started, 3),
                **context,
            )
            with self._mem_lock:
                self._album_year_cache[cache_key] = year
            return year
        except Exception as exc:
            logger.exception(
                "[MB] original album year lookup failed",
                error=_error(exc),
                elapsed_s=round(time.monotonic() - started, 3),
                **context,
            )
            return None

    def lookup_album_metadata(
        self,
        entries: list[tuple[str, str]],
        candidates_per_entry: int = 5,
        album: str = "",
        original_release_year: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Look up an album's recordings with strict track-count alignment penalties."""
        if not self.enabled:
            logger.info(
                "[MB] album recording batch skipped",
                reason="service disabled",
                album=album,
            )
            return {}

        album = str(album or "").strip()
        unique = sorted(
            {
                (str(title or "").strip(), str(artist or "").strip())
                for title, artist in entries or []
                if title and artist
            }
        )
        if not unique:
            logger.info(
                "[MB] album recording batch skipped",
                reason="no valid track entries",
                album=album,
            )
            return {}

        album_artist = unique[0][1]
        effective_year = original_release_year
        if effective_year is None and album and album_artist:
            effective_year = self.lookup_original_album_year(album_artist, album)

        # Count how many tracks the local album has so we can penalize MB matches
        # that belong to 88-track Box Sets or 1-track Singles instead of the canonical album.
        local_track_count = len(unique)

        logger.info(
            "[MB] album metadata authority selected",
            authoritative_album_name=album,
            album_artist=album_artist,
            original_release_year=effective_year,
            local_track_count=local_track_count,
            year_source=(
                "caller"
                if original_release_year is not None
                else (
                    "musicbrainz_release_group"
                    if effective_year is not None
                    else "unavailable"
                )
            ),
            entry_count=len(unique),
        )

        results: dict[str, dict[str, Any]] = {}
        for chunk_start in range(0, len(unique), _MB_BATCH_CHUNK):
            chunk = unique[chunk_start:chunk_start + _MB_BATCH_CHUNK]
            query_groups = [
                f'(recording:"'
                f"{escape_lucene_special_chars(normalize_title_for_lucene_query(title))}"
                f'" AND artist:"{escape_lucene_special_chars(artist)}")'
                for title, artist in chunk
            ]
            chunk_context = {
                "chunk_start": chunk_start,
                "chunk_size": len(chunk),
                "authoritative_album_name": album,
                "original_release_year": effective_year,
            }

            try:
                recordings = _call_with_heartbeat(
                    "recording.batch_search",
                    self.http.search_recordings,
                    " OR ".join(query_groups),
                    limit=min(100, len(chunk) * candidates_per_entry),
                    inc="releases+release-groups+work-rels+genres",
                    log_context=chunk_context,
                ) or []
            except Exception as exc:
                logger.exception(
                    "[MB] album recording chunk search failed",
                    error=_error(exc),
                    **chunk_context,
                )
                continue

            batch: list[tuple[str, str, float]] = []
            for title, artist in chunk:
                normalized = normalize_title_for_mbid_match(title)

                candidates_ranked = []
                for recording in recordings:
                    if not isinstance(recording, dict):
                        continue
                    candidate_title = str(recording.get("title") or "")
                    if not edition_annotations_compatible(title, candidate_title):
                        continue

                    # 1. Base Text Similarity
                    base_score = _mbid_similarity(
                        normalized,
                        normalize_title_for_mbid_match(candidate_title),
                    )

                    if base_score < _MB_BATCH_SIMILARITY_FLOOR:
                        continue

                    anchor = _recording_matches_album(recording, album)

                    # 2. Track Count Penalty (Defends against 88-track Box Sets)
                    penalty = 0.0
                    if local_track_count > 0:
                        best_diff = 999
                        for rel in recording.get("releases") or []:
                            # Track counts can arrive as strings; coerce safely
                            # so one odd release cannot abort the whole chunk.
                            rel_track_count = _release_track_count(rel)
                            if rel_track_count > 0:
                                diff = abs(local_track_count - rel_track_count)
                                if diff < best_diff:
                                    best_diff = diff

                        if best_diff != 999:
                            # 5% penalty per missing/extra track on the release
                            # E.g. An 8-track album matching an 88-track Box Set = 4.0 penalty (instantly rejected)
                            penalty = best_diff * 0.05

                    final_score = base_score - penalty
                    candidates_ranked.append((final_score, base_score, anchor, recording))

                if not candidates_ranked:
                    logger.debug(
                        "[MB] album recording match rejected (no valid candidates)",
                        title=title,
                        artist=artist,
                        **chunk_context,
                    )
                    continue

                # Rank: Highest penalized score -> Is Album Anchor -> Highest raw text similarity
                candidates_ranked.sort(key=lambda x: (x[0], x[2], x[1]), reverse=True)

                best_final_score, best_base_score, best_anchor, best_recording = candidates_ranked[0]
                mbid = str(best_recording.get("id") or "").strip()

                if not mbid:
                    continue

                key = self._cache_key(title, artist)
                confidence = round(best_base_score, 3)
                batch.append((key, mbid, confidence))
                self._record_mbid(key, mbid, confidence)

            if batch:
                metadata = self.lookup_recordings_by_mbid_bulk(
                    [item[1] for item in batch],
                    album_name=album,
                    original_release_year=effective_year,
                )
                for key, mbid, confidence in batch:
                    if mbid not in metadata:
                        continue
                    track_metadata = {**metadata[mbid], "confidence": confidence}
                    # Final guard so album identity cannot be lost.
                    if album:
                        track_metadata["album"] = album
                    if effective_year is not None:
                        track_metadata["year"] = effective_year
                        track_metadata["original_release_year"] = effective_year
                    results[key] = track_metadata
                self._maybe_flush_cache()

            logger.info(
                "[MB] album recording chunk completed",
                candidate_count=len(recordings),
                matched_count=len(batch),
                **chunk_context,
            )

        logger.info(
            "[MB] album recording batch completed",
            authoritative_album_name=album,
            original_release_year=effective_year,
            entry_count=len(unique),
            matched_count=len(results),
        )
        return results

    # -- single detection --------------------------------------------------

    def is_single(
        self,
        title: str,
        artist: str,
        album_track_count: int | None = None,
    ) -> bool:
        del album_track_count
        if not self.enabled or not title or not artist:
            return False

        result_key = self._cache_key(title, artist)
        with self._mem_lock:
            if result_key in self._single_result_cache:
                return self._single_result_cache[result_key]

        try:
            outcome = False
            for candidate in _artist_lookup_candidates(artist):
                if self._recording_search_has_single_release(title, candidate):
                    outcome = True
                    break
                mbid, _ = self.get_suggested_mbid(title, candidate)
                if mbid and self._recording_has_single_release(mbid, title):
                    outcome = True
                    break
                if self._release_group_has_single_release(title, candidate):
                    outcome = True
                    break
            with self._mem_lock:
                self._single_result_cache[result_key] = outcome
            return outcome
        except Exception as exc:
            logger.exception(
                "[MB] single detection failed",
                artist=artist,
                track=title,
                error=_error(exc),
            )
            return False

    def _artist_release_groups(self, artist: str) -> list[dict[str, Any]]:
        """Return every release group credited to *artist*, memoised.

        This query carries no track-specific terms, so it is identical for
        every track by the same artist. Without memoisation a 15-track album
        fired it 15 times; it now costs one request per artist per run.
        """
        key = str(artist or "").casefold().strip()
        if not key:
            return []
        with self._mem_lock:
            cached = self._artist_singles_cache.get(key)
        if cached is not None:
            logger.debug(
                "[MB] artist release-group cache hit",
                artist=artist,
                group_count=len(cached),
            )
            return cached

        if not _client_available(self.http):
            logger.info(
                "[MB] artist release-group fallback skipped",
                reason="MusicBrainz reported unavailable",
                artist=artist,
            )
            return []

        groups = _call_with_heartbeat(
            "single.release_group_artist_fallback",
            self.http.search_release_groups,
            f'artist:"{escape_lucene_special_chars(artist)}"',
            limit=100,
            log_context={"artist": artist},
        ) or []
        groups = [group for group in groups if isinstance(group, dict)]
        with self._mem_lock:
            self._artist_singles_cache[key] = groups
        logger.info(
            "[MB] artist release-group fallback cached",
            artist=artist,
            group_count=len(groups),
        )
        return groups

    def _release_group_has_single_release(self, title: str, artist: str) -> bool:
        query_title = normalize_title_for_lucene_query(strip_search_keywords(title))
        query = (
            f'releasegroup:"{escape_lucene_special_chars(query_title)}" '
            f'AND artist:"{escape_lucene_special_chars(artist)}"'
        )
        context = {"artist": artist, "title": title}
        groups = _call_with_heartbeat(
            "single.release_group_search",
            self.http.search_release_groups,
            query,
            limit=10,
            log_context=context,
        ) or []
        if not groups:
            groups = self._artist_release_groups(artist)
        normalized = normalize_title_for_lookup(title)
        for group in groups:
            if not isinstance(group, dict):
                continue
            if _release_group_primary_type(group) not in {"single", "ep"}:
                continue
            group_title = str(group.get("title") or "")
            if not edition_annotations_compatible(title, group_title):
                continue
            if _similarity(normalized, normalize_title_for_lookup(group_title)) >= 0.7:
                return True
        return False

    def _recording_search_has_single_release(self, title: str, artist: str) -> bool:
        query_title = normalize_title_for_lucene_query(strip_search_keywords(title))
        query = (
            f'recording:"{escape_lucene_special_chars(query_title)}" '
            f'AND artist:"{escape_lucene_special_chars(artist)}"'
        )
        recordings = _call_with_heartbeat(
            "single.recording_search",
            self.http.search_recordings,
            query,
            limit=10,
            log_context={"artist": artist, "title": title},
        ) or []
        for recording in recordings:
            if not isinstance(recording, dict):
                continue
            for release in recording.get("releases") or []:
                if not isinstance(release, dict):
                    continue
                group = release.get("release-group") or {}
                if _release_group_primary_type(group) in {
                    "single",
                    "ep",
                } and self._rg_title_matches(title, str(group.get("title") or "")):
                    return True
        return False

    @staticmethod
    def _rg_title_matches(title: str, release_group_title: str) -> bool:
        if not title or not release_group_title:
            return False
        if not edition_annotations_compatible(title, release_group_title):
            return False
        left = normalize_title_for_lookup(strip_single_release_suffix(title) or title)
        right = normalize_title_for_lookup(
            strip_single_release_suffix(release_group_title) or release_group_title
        )
        return left == right or _similarity(left, right) >= 0.85

    def _recording_has_single_release(self, mbid: str, title: str = "") -> bool:
        recording = _call_with_heartbeat(
            "single.recording_get",
            self.http.get_recording,
            mbid,
            inc="releases+release-groups",
            log_context={"mbid": mbid, "title": title},
        )
        releases = (recording or {}).get("releases") or []

        # This check is only meaningful when the response actually carries
        # release-group sub-documents. If the HTTP client drops the requested
        # `inc`, every release looks typeless and the answer is a false
        # negative, so say so loudly once rather than silently returning False.
        if releases and not any(
            isinstance(release, dict) and release.get("release-group")
            for release in releases
        ):
            if not self._missing_release_group_warned:
                self._missing_release_group_warned = True
                logger.warning(
                    "[MB] recording lookup returned no release-group data",
                    reason=(
                        "the requested inc=release-groups was not honoured; "
                        "single detection via recording lookup cannot succeed"
                    ),
                    mbid=mbid,
                    title=title,
                )
            return False

        for release in releases:
            if not isinstance(release, dict):
                continue
            group = release.get("release-group") or {}
            if _release_group_primary_type(group) not in {"single", "ep"}:
                continue
            if not title or self._rg_title_matches(
                title, str(group.get("title") or "")
            ):
                return True
        return False

    # -- simple lookups ----------------------------------------------------

    def get_artist_country(self, artist: str) -> str:
        """Return the artist's country name, never a city or subdivision.

        An artist's ``begin-area`` is usually the town they formed in, so
        reading it without checking the area type stored values like "Paris"
        in the country field.
        """
        if not self.enabled or not artist:
            return ""
        try:
            result = _call_with_heartbeat(
                "artist.country_search",
                self.http.search_artists,
                f'artist:"{escape_lucene_special_chars(artist)}"',
                limit=1,
                inc="area",
                log_context={"artist": artist},
            ) or []
            data = result[0] if result and isinstance(result[0], dict) else {}
            country = _artist_country_name(data)
            if not country and data:
                logger.debug(
                    "[MB] artist country unresolved",
                    reason="no area of type country on the matched artist",
                    artist=artist,
                    area=(data.get("area") or {}).get("name"),
                    begin_area=(data.get("begin-area") or {}).get("name"),
                )
            return country
        except Exception as exc:
            logger.exception(
                "[MB] artist country lookup failed",
                artist=artist,
                error=_error(exc),
            )
            return ""

    def get_genres(self, title: str, artist: str) -> list[str]:
        if not self.enabled:
            return []
        try:
            mbid, _ = self.get_suggested_mbid(title, artist)
            if not mbid:
                return []
            recording = _call_with_heartbeat(
                "recording.genres_get",
                self.http.get_recording,
                mbid,
                inc="genres",
                log_context={"artist": artist, "title": title, "mbid": mbid},
            )
            return [
                str(item["name"])
                for item in (recording or {}).get("genres") or []
                if isinstance(item, dict) and item.get("name")
            ]
        except Exception as exc:
            logger.exception(
                "[MB] genre lookup failed",
                artist=artist,
                title=title,
                error=_error(exc),
            )
            return []

    # -- release-group matching -------------------------------------------

    def search_releasegroup_matches(
        self,
        artist_name: str,
        album_name: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        started = time.monotonic()
        context = {"artist": artist_name, "album": album_name, "limit": limit}
        logger.info("[MB] release-group matching started", enabled=self.enabled, **context)

        if not self.enabled or not artist_name or not album_name:
            logger.info(
                "[MB] release-group matching skipped",
                reason="service disabled or incomplete input",
                **context,
            )
            return []

        # 1. Determine local track count for penalty calculation
        local_count, max_track = _get_local_track_stats(artist_name, album_name)
        expected_count = max(local_count, max_track) if local_count > 0 else None

        clean_album = strip_search_keywords(album_name)
        escaped_artist = escape_lucene_special_chars(artist_name)
        exact_query = (
            f'artist:"{escaped_artist}" '
            f'AND releasegroup:"{escape_lucene_special_chars(clean_album)}"'
        )
        try:
            groups = _call_with_heartbeat(
                "release_group.exact_search",
                self.http.search_release_groups,
                exact_query,
                limit=limit,
                log_context={**context, "query": exact_query},
            ) or []
        except Exception:
            groups = []

        # Do not widen the query while MusicBrainz is failing: an empty result
        # from a 503 is indistinguishable from a genuine miss.
        if not groups and clean_album and _client_available(self.http):
            terms = normalize_title_for_lucene_query(clean_album)
            if terms:
                fallback_query = f'artist:"{escaped_artist}" AND releasegroup:{terms}'
                try:
                    groups = _call_with_heartbeat(
                        "release_group.fallback_search",
                        self.http.search_release_groups,
                        fallback_query,
                        limit=limit,
                        log_context={**context, "query": fallback_query},
                    ) or []
                except Exception:
                    groups = []
            else:
                logger.info(
                    "[MB] release-group fallback skipped",
                    reason="normalised fallback terms empty",
                    **context,
                )

        matches: list[dict[str, Any]] = []

        # Pass 1: Base Text and Type Scoring
        with _logged_section(
            "release_group.scoring", candidate_count=len(groups), **context
        ):
            for index, group in enumerate(groups):
                if not isinstance(group, dict):
                    continue
                try:
                    secondary_types = _parse_secondary_types(group.get("secondary-types"))
                    score = calculate_match_score(
                        str(group.get("title") or ""),
                        group.get("artist-credit") or [],
                        album_name,
                        artist_name,
                    )

                    # Apply structural penalties
                    sec_lower = {t.casefold() for t in secondary_types}
                    if "live" in sec_lower and "live" not in album_name.casefold():
                        score *= 0.7
                    if "compilation" in sec_lower and "compilation" not in album_name.casefold():
                        score *= 0.8
                    if not secondary_types and str(group.get("primary-type") or "").casefold() == "album":
                        score *= 1.15

                    match = {
                        "id": group.get("id"),
                        "title": group.get("title"),
                        "primary_type": group.get("primary-type"),
                        "match_score": score,
                        "secondary_types": secondary_types,
                        "first_release_date": group.get("first-release-date") or "",
                    }
                    matches.append(match)
                    logger.debug(
                        "[MB] release-group candidate scored",
                        candidate_index=index,
                        candidate_id=match["id"],
                        candidate_title=match["title"],
                        match_score=round(match["match_score"], 3),
                        **context,
                    )
                except Exception as exc:
                    logger.exception(
                        "[MB] release-group candidate scoring failed",
                        candidate_index=index,
                        error=_error(exc),
                        **context,
                    )

        # Sort to surface the best text/type matches before hitting the API for track counts
        matches.sort(key=lambda item: item.get("match_score", 0.0), reverse=True)

        # Pass 2: Track Count Penalty for the top candidates. Each browse is
        # memoised per release group, so repeated scoring of the same album no
        # longer re-issues the same request.
        if expected_count and matches:
            refined = 0
            for match in matches[:_TRACK_COUNT_REFINE_LIMIT]:
                group_id = str(match.get("id") or "")
                if not group_id:
                    continue
                best_release_data = get_musicbrainz_best_release(
                    artist_name,
                    album_name,
                    group_id,
                )

                best_rel = (best_release_data or {}).get("best_release")
                if best_rel and best_rel.get("track_count"):
                    diff = abs(expected_count - _as_int(best_rel["track_count"], 0))
                    match["match_score"] -= diff * 0.05
                    refined += 1
            logger.debug(
                "[MB] release-group track-count refinement completed",
                refined_candidates=refined,
                expected_track_count=expected_count,
                **context,
            )

        # Round and final sort
        for match in matches:
            match["match_score"] = round(match["match_score"], 3)

        matches.sort(key=lambda item: item.get("match_score", 0.0), reverse=True)

        best = matches[0] if matches else {}
        logger.info(
            "[MB] release-group matching completed",
            raw_candidate_count=len(groups),
            match_count=len(matches),
            best_match_id=best.get("id"),
            best_match_title=best.get("title"),
            best_match_score=best.get("match_score"),
            best_first_release_date=best.get("first_release_date"),
            total_s=round(time.monotonic() - started, 3),
            **context,
        )
        return matches

    # -- merge -------------------------------------------------------------

    @staticmethod
    def merge_metadata(
        base: dict[str, Any],
        mb: dict[str, Any],
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Merge metadata without replacing the collection's album identity.

        The album name keeps the existing library value. The year prefers the
        MusicBrainz original release-group year over the collection's version.
        """
        overrides = overrides or {}

        def pick(*values: Any) -> Any:
            return next(
                (value for value in values if value not in (None, "")),
                None,
            )

        return {
            "title": pick(overrides.get("title"), mb.get("title"), base.get("title")),
            "artist": pick(overrides.get("artist"), mb.get("artist"), base.get("artist")),
            # Library album name is authoritative.
            "album": pick(overrides.get("album"), base.get("album"), mb.get("album")),
            "album_artist": pick(
                overrides.get("album_artist"),
                base.get("album_artist"),
                mb.get("album_artist"),
            ),
            # Original release year is authoritative over the held version.
            "year": pick(
                overrides.get("year"),
                mb.get("original_release_year"),
                mb.get("year"),
                base.get("year"),
            ),
        }

    # -- relationships -----------------------------------------------------

    def get_artist_relationships(
        self,
        artist_mbid: str,
        relation_type: str = "artist",
    ) -> list[dict[str, Any]]:
        if not self.enabled or not artist_mbid:
            return []
        inc = {
            "artist": "artist-rels",
            "recording": "recording-rels",
            "work": "work-rels",
        }.get(relation_type, "artist-rels")
        try:
            data = _call_with_heartbeat(
                "artist.relationships_get",
                self.http.get_artist,
                artist_mbid,
                inc=inc,
                log_context={
                    "artist_mbid": artist_mbid,
                    "relation_type": relation_type,
                },
            ) or {}
            return data.get("relations", []) or []
        except Exception as exc:
            logger.exception(
                "[MB] artist relationships failed",
                artist_mbid=artist_mbid,
                error=_error(exc),
            )
            return []

    def get_recording_relationships(self, recording_mbid: str) -> list[dict[str, Any]]:
        """Fetch a recording's relationships, including work-level ones.

        ``recording-level-rels`` is deliberately NOT requested here: it is a
        release-level subquery, and asking for it on the recording resource
        made MusicBrainz answer 400 for every call, so composer and writer
        data was always empty.
        """
        if not self.enabled or not recording_mbid:
            return []
        try:
            data = _call_with_heartbeat(
                "recording.relationships_get",
                self.http.get_recording,
                recording_mbid,
                inc=_RECORDING_RELATIONSHIP_INC,
                log_context={"recording_mbid": recording_mbid},
            ) or {}
            return data.get("relations", []) or []
        except Exception as exc:
            logger.exception(
                "[MB] recording relationships failed",
                recording_mbid=recording_mbid,
                error=_error(exc),
            )
            return []

    def get_composers_for_recording(self, recording_mbid: str) -> list[str]:
        composers: list[str] = []
        for relation in self.get_recording_relationships(recording_mbid):
            if not isinstance(relation, dict):
                continue
            if str(relation.get("type") or "").casefold() in {
                "composer",
                "writer",
                "lyricist",
            }:
                target = relation.get("artist") or {}
                if isinstance(target, dict) and target.get("name"):
                    composers.append(str(target["name"]))
            work = relation.get("work") or {}
            for work_relation in (work.get("relations") if isinstance(work, dict) else []) or []:
                if not isinstance(work_relation, dict):
                    continue
                if str(work_relation.get("type") or "").casefold() in {
                    "composer",
                    "writer",
                    "lyricist",
                }:
                    target = work_relation.get("artist") or {}
                    if isinstance(target, dict) and target.get("name"):
                        composers.append(str(target["name"]))
        return list(dict.fromkeys(composers))

    def get_recording_genres(self, title: str, artist: str) -> list[str]:
        if not self.enabled or not title or not artist:
            return []
        query = (
            f'recording:"{escape_lucene_special_chars(title)}" '
            f'AND artist:"{escape_lucene_special_chars(artist)}"'
        )
        try:
            recordings = _call_with_heartbeat(
                "recording.genre_search",
                self.http.search_recordings_with_genres,
                query,
                limit=3,
                log_context={"artist": artist, "title": title},
            ) or []
            genres: list[str] = []
            for recording in recordings:
                if not isinstance(recording, dict):
                    continue
                for genre in recording.get("genres") or []:
                    name = genre.get("name") if isinstance(genre, dict) else str(genre)
                    if name and str(name) not in genres:
                        genres.append(str(name))
            return genres
        except Exception as exc:
            logger.exception(
                "[MB] recording genre search failed",
                artist=artist,
                title=title,
                error=_error(exc),
            )
            return []


# ---------------------------------------------------------------------------
# Singleton accessors
# ---------------------------------------------------------------------------

def _get_service() -> MusicBrainzService:
    """Return the default process-wide MusicBrainz service."""
    global _service
    if _service is not None:
        return _service

    started = time.monotonic()
    logger.info("[MB] default service initialization requested")
    with _INIT_LOCK:
        if _service is None:
            client = get_shared_mb_client()
            with _logged_section("singleton.default_service.create"):
                _service = MusicBrainzService(http_client=client, enabled=True)
    logger.info(
        "[MB] default service ready",
        total_s=round(time.monotonic() - started, 3),
    )
    return _service


def get_shared_mb_service() -> MusicBrainzService:
    """Return the shared MusicBrainz enrichment service."""
    global _shared_mb_service
    if _shared_mb_service is not None:
        return _shared_mb_service

    started = time.monotonic()
    logger.info("[MB] shared service initialization requested")
    with _INIT_LOCK:
        if _shared_mb_service is None:
            client = get_shared_mb_client()
            with _logged_section("singleton.shared_service.create"):
                _shared_mb_service = MusicBrainzService(http_client=client, enabled=True)
    logger.info(
        "[MB] shared service ready",
        total_s=round(time.monotonic() - started, 3),
    )
    return _shared_mb_service


def lookup_recording_metadata(title: str, artist: str) -> dict[str, Any]:
    return _get_service().lookup_recording_metadata(title, artist)


def merge_metadata(
    base: dict[str, Any],
    mb: dict[str, Any],
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _get_service().merge_metadata(base, mb, overrides)


# ---------------------------------------------------------------------------
# Release metadata
# ---------------------------------------------------------------------------

def fetch_musicbrainz_release_metadata(release_id: str) -> dict[str, Any] | None:
    """Fetch release metadata, reporting the original release-group year."""
    context = {"release_id": release_id}
    try:
        data = _call_with_heartbeat(
            "release.metadata_get",
            get_shared_mb_client().get_release,
            release_id,
            inc="recordings+artist-credits+release-groups+work-rels+genres",
            log_context=context,
        )
        if not data:
            logger.info("[MB] release metadata not found", **context)
            return None

        release_group = data.get("release-group") or {}
        original_date = str(release_group.get("first-release-date") or "")
        version_date = str(data.get("date") or "")
        original_year = _year_of(original_date)
        version_year = _year_of(version_date)
        secondary_types = _parse_secondary_types(release_group.get("secondary-types"))

        if original_year is not None and version_year not in (None, original_year):
            logger.info(
                "[MB] release version year differs from original release year",
                original_release_year=original_year,
                version_release_year=version_year,
                **context,
            )

        info: dict[str, Any] = {
            # Release-group title is the album, not the specific edition title.
            "release_title": release_group.get("title") or data.get("title"),
            "specific_release_title": data.get("title"),
            # release_year is the original album year.
            "release_year": str(original_year or version_year or ""),
            "original_release_year": str(original_year or ""),
            "version_release_year": str(version_year or ""),
            "artist": "",
            "disc_count": len(data.get("media") or []),
            "tracks": [],
            "release_mbid": data.get("id"),
            "release_group_mbid": release_group.get("id") or "",
            "compilation": int(
                "compilation" in {value.casefold() for value in secondary_types}
            ),
            "original_date": original_date or version_date,
            "original_year": str(original_year or version_year or ""),
            "album_type": _compose_album_type(
                release_group.get("primary-type") or "Album",
                secondary_types,
            ),
        }

        credits = data.get("artist-credit") or []
        if credits:
            info["artist"] = primary_album_artist(credits)
            info["artist_credit"] = build_artist_credit_string(credits)
            first_credit = credits[0] if isinstance(credits[0], dict) else {}
            artist_data = first_credit.get("artist") or {}
            info["album_artist_mbid"] = (
                str(artist_data.get("id") or "") if isinstance(artist_data, dict) else ""
            )

        absolute_number = 1
        for disc_index, medium in enumerate(data.get("media") or [], 1):
            if not isinstance(medium, dict):
                continue
            for track in medium.get("tracks") or []:
                if not isinstance(track, dict):
                    continue
                recording = track.get("recording") or {}
                track_info: dict[str, Any] = {
                    "disc_number": disc_index,
                    "track_number": track.get("position"),
                    "absolute_track_number": absolute_number,
                    "title": track.get("title") or recording.get("title"),
                    "recording_mbid": recording.get("id"),
                    "duration": track.get("length"),
                    "artist": (
                        build_artist_credit_string(recording.get("artist-credit") or [])
                        or info.get("artist_credit")
                        or info.get("artist")
                    ),
                    # Every track carries the album's original year.
                    "year": original_year or version_year,
                    "original_release_year": original_year,
                }
                absolute_number += 1

                writers: list[str] = []
                composers: list[str] = []
                lyricists: list[str] = []
                for relation in recording.get("relations") or []:
                    if not isinstance(relation, dict):
                        continue
                    if str(relation.get("type") or "").casefold() not in {
                        "performance",
                        "recording of",
                    }:
                        continue
                    work = relation.get("work") or {}
                    if not isinstance(work, dict):
                        continue
                    track_info["work_mbid"] = work.get("id") or ""
                    track_info["work_title"] = work.get("title") or ""
                    track_info["iswc"] = str(work.get("iswc") or "")
                    work_artist = primary_album_artist(work.get("artist-credit") or [])
                    if work_artist:
                        track_info["work_artist"] = work_artist
                        if _normalise_artist_key(work_artist) != _normalise_artist_key(
                            str(track_info["artist"])
                        ):
                            track_info.update(
                                is_cover=True,
                                original_cover_artist=work_artist,
                                original_title=work.get("title") or "",
                            )
                    for work_relation in work.get("relations") or []:
                        if not isinstance(work_relation, dict):
                            continue
                        relation_type = str(work_relation.get("type") or "").casefold()
                        target = work_relation.get("artist") or {}
                        name = (
                            str(target.get("name") or "")
                            if isinstance(target, dict)
                            else ""
                        )
                        if not name:
                            continue
                        if relation_type == "composer":
                            composers.append(name)
                        if relation_type in {"writer", "lyricist"}:
                            lyricists.append(name)
                        if relation_type in {"composer", "writer", "lyricist"}:
                            writers.append(name)

                if composers:
                    track_info["composer"] = ", ".join(dict.fromkeys(composers))
                if lyricists:
                    track_info["lyricist"] = ", ".join(dict.fromkeys(lyricists))
                if writers:
                    track_info["writer"] = ", ".join(dict.fromkeys(writers))

                genres = [
                    str(item.get("name") or "").strip()
                    for item in recording.get("genres") or []
                    if isinstance(item, dict) and str(item.get("name") or "").strip()
                ]
                if genres:
                    track_info["musicbrainz_genres"] = ", ".join(dict.fromkeys(genres))

                info["tracks"].append(track_info)

        try:
            from api_clients.coverartarchive import get_release_front_image_bytes
            cover = _call_with_heartbeat(
                "release.cover_art_get",
                get_release_front_image_bytes,
                release_id,
                log_context=context,
            )
            if cover:
                info["cover_art"] = cover
        except Exception as exc:
            logger.warning(
                "[MB] release cover-art lookup failed",
                error=_error(exc),
                **context,
            )

        logger.info(
            "[MB] release metadata completed",
            track_count=len(info["tracks"]),
            original_release_year=original_year,
            version_release_year=version_year,
            **context,
        )
        return info
    except Exception as exc:
        logger.exception("[MB] release metadata failed", error=_error(exc), **context)
        return None


def fetch_release_metadata(release_id: str) -> dict[str, Any] | None:
    return fetch_musicbrainz_release_metadata(release_id)


def resolve_release_id(release_id: str) -> str:
    if not release_id:
        return release_id
    context = {"release_or_group_id": release_id}
    client = get_shared_mb_client()
    try:
        data = _call_with_heartbeat(
            "release.resolve.direct",
            client.get_release,
            release_id,
            inc="",
            log_context=context,
        )
        if data and data.get("id"):
            logger.info("[MB] identifier already refers to a release", **context)
            return release_id
    except Exception as exc:
        logger.info(
            "[MB] direct release resolution did not match",
            error=_error(exc),
            **context,
        )
    try:
        releases = _call_with_heartbeat(
            "release.resolve.group_browse",
            client.browse_releases_for_group,
            release_id,
            inc="media",
            limit=50,
            log_context=context,
        ) or []

        official = [
            release
            for release in releases
            if isinstance(release, dict)
            and str(release.get("status") or "").casefold() == "official"
        ]
        candidates = [
            release
            for release in (official or releases)
            if _release_track_count(release) > 0
        ] or official or releases

        if candidates:
            def _sort_by_date(rel: dict[str, Any]) -> tuple[int, str]:
                date_str = str(rel.get("date") or "").strip() or "9999"
                return (1 if date_str == "9999" else 0, date_str)

            best = sorted(candidates, key=_sort_by_date)[0]
            resolved = str(best.get("id") or release_id)
            logger.info(
                "[MB] release group resolved to release",
                resolved_id=resolved,
                tracks=_release_track_count(best),
                status=best.get("status"),
                **context,
            )
            return resolved

        logger.info("[MB] release-group resolution found no candidates", **context)
    except Exception as exc:
        logger.exception(
            "[MB] release-group resolution failed",
            error=_error(exc),
            **context,
        )
    return release_id


# ---------------------------------------------------------------------------
# Album lookup helpers
# ---------------------------------------------------------------------------

def _lookup_existing_mbid(
    existing_mbid: str,
    artist: str,
    album: str,
) -> dict[str, Any] | None:
    if not existing_mbid:
        return None
    client = get_shared_mb_client()
    context = {"existing_mbid": existing_mbid, "artist": artist, "album": album}
    try:
        data = _call_with_heartbeat(
            "album.existing_release_get",
            client.get_release,
            existing_mbid,
            inc="artist-credits+release-groups",
            log_context=context,
        )
        if data:
            group = data.get("release-group") or {}
            return {
                "mbid": existing_mbid,
                # Release-group title, not the specific edition title.
                "title": group.get("title") or data.get("title", album),
                "artist": primary_album_artist(data.get("artist-credit") or []) or artist,
                "primary_type": group.get("primary-type", "Album"),
                "secondary_types": _parse_secondary_types(group.get("secondary-types")),
                # Original release date of the release group.
                "first_release_date": group.get("first-release-date")
                or data.get("date")
                or "",
                "cover_art_url": _cover_art_url(
                    str(group.get("id") or ""), existing_mbid
                ),
                "confidence": 1.0,
                "source": "musicbrainz",
                "is_stored_mbid": True,
                "mbid_type": "release",
            }
    except Exception as exc:
        logger.info("[MB] stored MBID was not a release", error=_error(exc), **context)
    try:
        data = _call_with_heartbeat(
            "album.existing_release_group_get",
            client.get_release_group,
            existing_mbid,
            inc="artist-credits",
            log_context=context,
        )
        if data:
            return {
                "mbid": existing_mbid,
                "title": data.get("title", album),
                "artist": _mb_artist_credit_name(data.get("artist-credit") or []) or artist,
                "primary_type": data.get("primary-type", "Album"),
                "secondary_types": _parse_secondary_types(data.get("secondary-types")),
                "first_release_date": data.get("first-release-date", ""),
                "cover_art_url": _cover_art_url(existing_mbid),
                "confidence": 1.0,
                "source": "musicbrainz",
                "is_stored_mbid": True,
                "mbid_type": "release-group",
            }
    except Exception as exc:
        logger.exception("[MB] stored MBID lookup failed", error=_error(exc), **context)
    return None


def lookup_musicbrainz_album(
    artist: str,
    album: str,
    existing_mbid: str = "",
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    if existing_mbid:
        stored = _lookup_existing_mbid(existing_mbid, artist, album)
        if stored:
            results.append(stored)
            logger.info(
                "[MB] stored MBID resolved",
                mbid_type=stored["mbid_type"],
                mbid=existing_mbid,
                title=stored["title"],
                artist=stored["artist"],
            )

    query = (
        f'release:"{escape_lucene_special_chars(album)}" '
        f'AND artist:"{escape_lucene_special_chars(artist)}"'
    )
    try:
        groups = _call_with_heartbeat(
            "album.release_group_search",
            get_shared_mb_client().search_release_groups,
            query,
            limit=10,
            log_context={"artist": artist, "album": album},
        ) or []
    except Exception:
        groups = []

    seen = {item["mbid"] for item in results}
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_id = str(group.get("id") or "")
        if not group_id or group_id in seen:
            continue
        results.append(
            {
                "mbid": group_id,
                "title": group.get("title", ""),
                "artist": _mb_artist_credit_name(group.get("artist-credit") or []),
                "primary_type": group.get("primary-type", "Album"),
                "secondary_types": _parse_secondary_types(group.get("secondary-types")),
                "first_release_date": group.get("first-release-date", ""),
                "cover_art_url": _cover_art_url(group_id),
                "confidence": round(
                    calculate_match_score(
                        group.get("title") or "",
                        group.get("artist-credit") or [],
                        album,
                        artist,
                    ),
                    3,
                ),
                "source": "musicbrainz",
                "is_stored_mbid": False,
                "mbid_type": "release-group",
            }
        )
        seen.add(group_id)

    stored_results = [item for item in results if item.get("is_stored_mbid")]
    other_results = sorted(
        [item for item in results if not item.get("is_stored_mbid")],
        key=lambda item: item.get("confidence") or 0.0,
        reverse=True,
    )
    return {"results": (stored_results + other_results)[:11]}


def _release_summary(release: dict[str, Any]) -> dict[str, Any]:
    """Normalise a MusicBrainz release into the shape callers expect."""
    media = release.get("media") or []
    release_id = str(release.get("id") or "")
    return {
        "id": release_id,
        "title": release.get("title", ""),
        "date": release.get("date", ""),
        "country": release.get("country", ""),
        "status": release.get("status", ""),
        "disambiguation": release.get("disambiguation", ""),
        "track_count": _release_track_count(release),
        "disc_count": len(media),
        "formats": sorted(
            {
                str(medium.get("format") or "").strip()
                for medium in media
                if isinstance(medium, dict) and medium.get("format")
            }
        ),
        "cover_art_url": _cover_art_url(release_id=release_id),
    }


def _browse_group_releases(release_group_mbid: str, section: str) -> list[dict[str, Any]]:
    """Browse a release group's releases with media included, memoised.

    Browsing with ``inc=media`` returns track counts directly, which removes
    the second "enrichment" browse the old code issued for the same group.
    """
    cache_key = f"browse::{release_group_mbid}"
    cached = _best_release_cache_get(cache_key)
    if cached is not None:
        return list(cached.get("releases") or [])

    raw = _call_with_heartbeat(
        section,
        get_shared_mb_client().browse_releases_for_group,
        release_group_mbid,
        inc="media+labels",
        limit=100,
        log_context={"release_group_mbid": release_group_mbid},
    ) or []
    releases = [
        _release_summary(release) for release in raw if isinstance(release, dict)
    ]
    _best_release_cache_set(cache_key, {"releases": releases})
    return releases


def get_release_group_releases(
    release_group_mbid: str,
    include_track_counts: bool = False,
) -> dict[str, Any]:
    """List a release group's releases.

    ``include_track_counts`` is retained for API compatibility; track counts
    now always come back from the single browse, so no second request is made.
    """
    del include_track_counts
    if not release_group_mbid:
        return {"success": False, "error": "No release-group MBID supplied"}
    try:
        releases = _browse_group_releases(
            release_group_mbid, "release_group.get_releases"
        )
        if not releases:
            return {"success": False, "error": "No release-group data returned"}
        return {"success": True, "releases": releases}
    except Exception as exc:
        logger.exception(
            "[MB] release-group releases lookup failed",
            release_group_mbid=release_group_mbid,
            error=_error(exc),
        )
        return {"success": False, "error": str(exc)}


def _get_local_track_stats(artist: str, album: str) -> tuple[int, int]:
    """Returns (file_count, highest_track_number)."""
    try:
        from db.engine import db_session
        from sqlalchemy import text
        with db_session() as session:
            rows = session.execute(
                text(
                    "SELECT track_number FROM tracks "
                    "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) "
                    "AND LOWER(COALESCE(album, '')) = LOWER(:album)"
                ),
                {"artist": artist, "album": album},
            ).fetchall()

            count = len(rows)
            max_track = 0
            for row in rows:
                tn_raw = str(row[0] or "").split('/')[0].strip()
                if tn_raw.isdigit():
                    max_track = max(max_track, int(tn_raw))

            return count, max_track
    except Exception as exc:
        logger.exception(
            "[MB] local track stats failed",
            artist=artist,
            album=album,
            error=_error(exc),
        )
        return 0, 0


def get_musicbrainz_best_release(
    artist: str,
    album: str,
    release_group_mbid: str,
) -> dict[str, Any]:
    context = {
        "artist": artist,
        "album": album,
        "release_group_mbid": release_group_mbid,
    }
    if not release_group_mbid:
        return {"success": False, "error": "No release-group MBID supplied"}

    result_key = (
        f"best::{release_group_mbid}::{str(artist).casefold().strip()}"
        f"::{str(album).casefold().strip()}"
    )
    cached_result = _best_release_cache_get(result_key)
    if cached_result is not None:
        logger.debug("[MB] best release cache hit", **context)
        return cached_result

    try:
        releases = _browse_group_releases(
            release_group_mbid, "release_group.best_release_browse"
        )
        releases.sort(key=lambda item: (not bool(item.get("date")), item.get("date") or ""))

        if not releases:
            logger.info("[MB] best release resolution found no releases", **context)
            empty = {
                "success": True,
                "releases": [],
                "best_release": None,
                "confidence": 0,
                "local_track_count": None,
            }
            _best_release_cache_set(result_key, empty)
            return empty

        local_count, max_track = _get_local_track_stats(artist, album)
        expected_count = max(local_count, max_track) if local_count > 0 else None

        def score(item: dict[str, Any]) -> float:
            value = 0.0
            if expected_count is not None:
                value -= abs(expected_count - _as_int(item.get("track_count"), 0)) * 100.0
            if str(item.get("status") or "").casefold() == "official":
                value += 50.0
            date = str(item.get("date") or "")
            if date[:4].isdigit():
                value += max(0.0, 2100.0 - int(date[:4])) * 0.01
            if album and item.get("title"):
                value += _similarity(album.casefold(), str(item["title"]).casefold()) * 30.0
            return value

        best = max(releases, key=score)
        if expected_count is None:
            confidence = 0.5
        else:
            difference = abs(expected_count - _as_int(best.get("track_count"), 0))
            confidence = 1.0 if difference == 0 else max(0.0, 1.0 - difference * 0.2)

        logger.info(
            "[MB] best release selected",
            best_release_id=best.get("id"),
            best_release_title=best.get("title"),
            best_release_date=best.get("date"),
            track_count=best.get("track_count"),
            expected_local_count=expected_count,
            confidence=round(confidence, 2),
            **context,
        )
        result = {
            "success": True,
            "releases": releases,
            "best_release": best,
            "confidence": round(confidence, 2),
            "local_track_count": expected_count,
        }
        _best_release_cache_set(result_key, result)
        return result
    except Exception as exc:
        logger.exception(
            "[MB] best release resolution failed",
            error=_error(exc),
            **context,
        )
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Library comparison
# ---------------------------------------------------------------------------

def compare_musicbrainz_release(
    artist: str,
    album: str,
    release_group_mbid: str,
) -> dict[str, Any]:
    """Compare a MusicBrainz release with the local album.

    The comparison uses the release group's original year, so a reissue held in
    the collection is not reported as a year difference against itself.
    """
    started = time.monotonic()
    context = {
        "artist": artist,
        "album": album,
        "release_group_mbid": release_group_mbid,
    }
    logger.info("[MB] release comparison started", **context)
    try:
        client = get_shared_mb_client()
        try:
            direct = _call_with_heartbeat(
                "compare.direct_release_get",
                client.get_release,
                release_group_mbid,
                inc="",
                log_context=context,
            )
        except Exception:
            direct = None

        if direct and direct.get("id"):
            release_id = release_group_mbid
        else:
            best_result = get_musicbrainz_best_release(artist, album, release_group_mbid)
            release_id = str(
                ((best_result or {}).get("best_release") or {}).get("id") or ""
            )
            if not release_id:
                release_id = resolve_release_id(release_group_mbid)

        mb_release = fetch_musicbrainz_release_metadata(release_id)
        if not mb_release:
            return {
                "success": False,
                "error": "Could not fetch MusicBrainz release data",
            }

        from db.engine import db_session
        from sqlalchemy import text
        with _logged_section("compare.library_tracks_read", **context):
            with db_session() as session:
                rows = session.execute(
                    text(_COMPARE_LIBRARY_TRACKS_SQL),
                    {"artist": artist, "album": album},
                ).mappings().all()
                library = [dict(row) for row in rows]

        if not library:
            logger.warning(
                "[MB] release comparison found no library tracks",
                **context,
            )
            return {
                "success": False,
                "error": "No library tracks found for this album",
                "comparison": [],
            }

        def number(value: Any, default: int) -> int:
            try:
                return int(str(value or "").split("/")[0].strip())
            except (TypeError, ValueError):
                return default

        def normalized(value: Any) -> str:
            return re.sub(r"\s+", " ", str(value or "").casefold().strip())

        def core(value: Any) -> str:
            return re.sub(r"\s*[\(\[].+$", "", normalized(value)).strip()

        def seconds(value: Any) -> float | None:
            try:
                result = float(value)
                result = result / 1000.0 if result > 10000 else result
                return result if result > 0 else None
            except (TypeError, ValueError):
                return None

        def duration_display(value: float | None) -> str | None:
            if value is None:
                return None
            rounded = int(round(value))
            return f"{rounded // 60}:{rounded % 60:02d}"

        def ignored_fields(track: dict[str, Any]) -> set[str]:
            try:
                parsed = json.loads(track.get("mb_ignored_fields") or "[]")
                return {str(item) for item in parsed} if isinstance(parsed, list) else set()
            except Exception:
                return set()

        library.sort(
            key=lambda track: (
                number(track.get("disc_number"), 1),
                number(track.get("track_number"), 999),
            )
        )
        by_number = {
            (
                number(track.get("disc_number"), 1),
                number(track.get("track_number"), 999),
            ): track
            for track in library
        }
        by_title = {
            (number(track.get("disc_number"), 1), normalized(track.get("title"))): track
            for track in library
        }

        matched_ids: set[Any] = set()
        comparison: list[dict[str, Any]] = []
        # Original album year, not the year of the held edition.
        mb_year = str(mb_release.get("original_release_year") or mb_release.get("release_year") or "")

        for mb_track in mb_release.get("tracks") or []:
            disc = number(mb_track.get("disc_number"), 1)
            track_number = number(mb_track.get("track_number"), -1)
            mb_title = str(mb_track.get("title") or "")
            norm_title = normalized(mb_title)
            core_title = core(mb_title)

            candidate = by_number.get((disc, track_number)) if track_number >= 0 else None
            if candidate and difflib.SequenceMatcher(
                None, norm_title, normalized(candidate.get("title"))
            ).ratio() < 0.30:
                candidate = None
            if candidate is None:
                candidate = by_title.get((disc, norm_title))
            if candidate is None:
                pool = [
                    track
                    for track in library
                    if number(track.get("disc_number"), 1) == disc
                    and track.get("id") not in matched_ids
                ]
                scored = [
                    (
                        difflib.SequenceMatcher(
                            None, norm_title, normalized(track.get("title"))
                        ).ratio(),
                        track,
                    )
                    for track in pool
                ]
                if scored:
                    best_ratio, best_track = max(scored, key=lambda item: item[0])
                    candidate = best_track if best_ratio >= 0.80 else None
            if candidate is None and core_title != norm_title:
                candidate = by_title.get((disc, core_title))

            mb_duration = seconds(mb_track.get("duration"))
            entry: dict[str, Any] = {
                "mb_track_number": None if track_number < 0 else track_number,
                "mb_disc_number": disc,
                "mb_title": mb_title,
                "mb_artist": mb_track.get("artist") or "",
                "mb_recording_id": str(mb_track.get("recording_mbid") or ""),
                "mb_year": mb_year,
                "mb_duration": duration_display(mb_duration),
                "mb_duration_sec": int(mb_duration) if mb_duration else None,
                "mb_writer": mb_track.get("writer") or "",
                "mb_work_mbid": mb_track.get("work_mbid") or "",
                "mb_work_title": mb_track.get("work_title") or "",
                "mb_work_artist": mb_track.get("work_artist") or "",
                "mb_is_cover": bool(mb_track.get("is_cover")),
                "mb_original_cover_artist": mb_track.get("original_cover_artist") or "",
                "mb_musicbrainz_genres": mb_track.get("musicbrainz_genres") or "",
                "mb_artist_credit": mb_track.get("artist") or "",
                "library_track_id": None,
                "library_title": None,
                "library_track_number": None,
                "library_disc_number": None,
                "library_artist": None,
                "library_year": None,
                "library_duration": None,
                "matched": False,
                "needs_update": False,
                "diff_fields": [],
            }

            if candidate is not None and candidate.get("id") not in matched_ids:
                matched_ids.add(candidate["id"])
                local_duration = seconds(candidate.get("duration"))
                entry.update(
                    {
                        "matched": True,
                        "library_track_id": candidate.get("id"),
                        "library_title": candidate.get("title", ""),
                        "library_track_number": candidate.get("track_number"),
                        "library_disc_number": number(candidate.get("disc_number"), 1),
                        "library_artist": candidate.get("artist", ""),
                        "library_year": str(candidate.get("year") or ""),
                        "library_duration": duration_display(local_duration),
                    }
                )

                differences: list[str] = []
                local_title = str(candidate.get("title") or "")
                stripped_cover = re.sub(
                    r"\s*\([^)]*\bcover\b[^)]*\)",
                    "",
                    local_title,
                    flags=re.IGNORECASE,
                ).strip()
                if (
                    mb_title
                    and mb_title.casefold() != local_title.casefold()
                    and stripped_cover.casefold() != mb_title.casefold()
                ):
                    differences.append("title")
                if track_number >= 0 and str(track_number) != str(
                    candidate.get("track_number") or ""
                ):
                    differences.append("track_number")
                if mb_year and mb_year != str(candidate.get("year") or ""):
                    differences.append("year")
                if entry["mb_recording_id"] and not str(candidate.get("mbid") or "").strip():
                    differences.append("mbid")
                if (
                    mb_duration is not None
                    and local_duration is not None
                    and abs(mb_duration - local_duration) > 5.0
                ):
                    differences.append("duration")
                if number(candidate.get("disc_number"), 1) != disc:
                    differences.append("disc_number")

                differences = [
                    item for item in differences if item not in ignored_fields(candidate)
                ]
                entry["diff_fields"] = differences
                entry["needs_update"] = bool(differences)

            comparison.append(entry)

        extras = [
            {
                "library_track_id": track["id"],
                "library_title": track.get("title", ""),
                "library_track_number": track.get("track_number"),
                "library_disc_number": number(track.get("disc_number"), 1),
                "library_artist": track.get("artist", ""),
            }
            for track in library
            if track["id"] not in matched_ids
        ]

        result = {
            "success": True,
            "mb_title": str(mb_release.get("release_title") or ""),
            "mb_specific_release_title": str(
                mb_release.get("specific_release_title") or ""
            ),
            "mb_year": mb_year,
            "mb_original_release_year": str(
                mb_release.get("original_release_year") or ""
            ),
            "mb_version_release_year": str(
                mb_release.get("version_release_year") or ""
            ),
            "mb_artist": str(mb_release.get("artist") or ""),
            "mb_release_mbid": str(mb_release.get("release_mbid") or release_id),
            "mb_release_group_mbid": release_group_mbid,
            "release_group_mbid": release_group_mbid,
            "release_mbid": release_id,
            "mb_album_artist_mbid": str(mb_release.get("album_artist_mbid") or ""),
            "mb_albumtype": str(mb_release.get("album_type") or ""),
            "mb_disc_count": int(mb_release.get("disc_count") or 0),
            "mb_artist_credit": str(mb_release.get("artist_credit") or ""),
            "comparison": comparison,
            "extra_tracks": extras,
            "tracks_needing_update": sum(
                1 for item in comparison if item.get("needs_update")
            ),
            "total_tracks": len(comparison),
        }
        logger.info(
            "[MB] release comparison completed",
            matched=sum(1 for item in comparison if item.get("matched")),
            extras=len(extras),
            updates=result["tracks_needing_update"],
            original_release_year=mb_year,
            elapsed_s=round(time.monotonic() - started, 3),
            **context,
        )
        return result
    except Exception as exc:
        logger.exception("[MB] release comparison failed", error=_error(exc), **context)
        return {"success": False, "error": "Could not fetch MusicBrainz release data"}
