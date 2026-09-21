"""MusicBrainz enrichment and lookup service.

Owns MusicBrainz interpretation and matching rules. This module performs no
Track mutation except through the explicit Link/Align write helpers below,
which only ever write the fields their name promises (mbid for Link;
title/track_number/disc_number for Align) so the two album-page actions stay
independently safe to run.

Album identity rules:
-	The album name supplied by the caller (the library album) is authoritative.
  A recording's specific release title never replaces it.
-	The album year comes from the matched release group's ``first-release-date``
  (the original release year), not from the edition/version held in the
  Collection and not from each recording's first linked release.

Operational behaviour:
-	Re-entrant singleton lock so shared-service creation can request the shared
  HTTP client without deadlocking.
- Structured start, completion, skip, and failure logs for every operation.
- A single shared heartbeat monitor thread warns about in-flight calls that
  Have not returned, instead of one thread per call.
-	Atomic, bounded, timestamped MBID cache writes, flushed on a dirty flag
  Rather than on every match.
-	Plain Cover Art Archive URLs.

Request-volume controls:
-	Per-artist release-group results are memoised, so the artist-wide single
  Detection fallback fires once per artist instead of once per track.
-	Best-release browses are memoised per release group, so release-group
  Scoring does not re-browse the same group for each candidate.
-	Availability of the HTTP client is checked before firing broad fallback
  Queries, so a MusicBrainz outage does not amplify into heavier requests.
-	``compare_musicbrainz_release`` only browses a release-group when a direct
  release lookup fails. Browsing unconditionally before checking the direct
  lookup wasted a full release-group round trip on every compare where the
  caller already knew the concrete release MBID — the ~2.7s slow-compare
  regression this module's tests guard against.

Web-service correctness notes:
-	``recording-level-rels`` is a RELEASE-level subquery ("include relationships
  For the recordings on this release"). It is not a valid ``inc`` value on the
  Recording resource and makes MusicBrainz answer 400, so recording
  Relationship lookups must not request it.
-	An artist's ``begin-area`` is frequently a city, so it cannot be used as a
  Country without checking the area type.

Public exports required by other modules:
    Get_shared_mb_client, get_shared_mb_service, lookup_recording_metadata,
    Merge_metadata, fetch_musicbrainz_release_metadata, fetch_release_metadata,
    Resolve_release_id, lookup_musicbrainz_album, get_release_group_releases,
    Get_musicbrainz_best_release, compare_musicbrainz_release,
    Link_album_mbids, align_album_tracklist
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
    escape_lucene_special_chars as Escape_lucene_special_chars,
)
from helpers.normalization_service import (
    edition_annotations_compatible as Edition_annotations_compatible,
    extract_edition_annotation as Extract_edition_annotation,
    normalize_string as Normalize_string,
    normalize_title_for_lookup as Normalize_title_for_lookup,
    normalize_title_for_lucene_query as Normalize_title_for_lucene_query,
    normalize_title_for_mbid_match as Normalize_title_for_mbid_match,
    strip_featured_artist as Strip_featured_artist,
    strip_search_keywords as Strip_search_keywords,
    strip_single_release_suffix as Strip_single_release_suffix,
)

Logger = structlog.get_logger(__name__)

T = TypeVar("T")

__all__ = [
    "MusicBrainzService",
    "align_album_tracklist",
    "build_artist_credit_string",
    "calculate_match_score",
    "compare_musicbrainz_release",
    "fetch_musicbrainz_release_metadata",
    "fetch_release_metadata",
    "get_musicbrainz_best_release",
    "get_release_group_releases",
    "get_shared_mb_client",
    "get_shared_mb_service",
    "link_album_mbids",
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
    for candidate in ("/data", "/config", "/var/lib/popularr"):
        if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
            return os.path.join(candidate, "mbid_cache.json")
    return "mbid_cache.json"

CACHE_FILE = os.getenv("MUSICBRAINZ_CACHE_FILE", "") or _default_cache_file()

_HEARTBEAT_SECONDS = max(5.0, float(os.getenv("MUSICBRAINZ_HEARTBEAT_SECONDS", "30")))
_MONITOR_TICK_SECONDS = 2.0

_CACHE_FLUSH_SECONDS = max(
    5.0, float(os.getenv("MUSICBRAINZ_CACHE_FLUSH_SECONDS", "30"))
)

_CACHE_IO_LOCK = threading.Lock()
_INIT_LOCK = threading.RLock()

_MB_BATCH_CHUNK = 20
_MB_BATCH_SIMILARITY_FLOOR = 0.6
_MBID_CACHE_SIMILARITY_FLOOR = 0.6
_MBID_CACHE_TTL_SECONDS = 30 * 24 * 3600
_MBID_CACHE_MAX_SIZE = 5000
_RELEASE_GROUP_MATCH_FLOOR = 0.6

_TRACK_COUNT_REFINE_LIMIT = 3
_RECORDING_RELATIONSHIP_INC = "artist-rels+work-rels+work-level-rels"

#: Per-track title similarity for calling a local track "present" on a
#: candidate release.  Equal to the floor `_match_mb_tracks_to_library` uses for
#: its own fuzzy fallback, so both agree on what "the same track" means.
_TRACKLIST_TITLE_FLOOR = 0.55

#: Default share of a LOCAL album's tracks that must be found on a candidate
#: release before that release-group is accepted for a Various Artists
#: compilation.  See `release_group_tracklist_similarity`.
_TRACKLIST_MATCH_FLOOR = 0.6

# Fields the Align auto-fix is allowed to overwrite on a track row. Kept as a
# fixed whitelist (never derived from request input) so _update_track_fields
# can safely interpolate column names into an UPDATE statement.
_ALIGN_WRITABLE_FIELDS = ("title", "track_number", "disc_number")
_LINK_WRITABLE_FIELDS = ("mbid",)

_COMPARE_LIBRARY_TRACKS_SQL = """
    SELECT id, title, track_number, disc_number, artist, year,
           Mbid, file_path, duration, mb_ignored_fields
    FROM tracks
    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
      AND LOWER(COALESCE(album, '')) = LOWER(:album)
    ORDER BY COALESCE(disc_number, '1'), COALESCE(track_number, '999')
"""

_LIBRARY_TRACK_COLUMNS = (
    "id", "title", "track_number", "disc_number", "artist", "year",
    "mbid", "file_path", "duration", "mb_ignored_fields",
)

# ---------------------------------------------------------------------------
# Diagnostics helpers
# ---------------------------------------------------------------------------

def _error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"

def _year_of(value: Any) -> int | None:
    text_value = str(value or "").strip()
    return int(text_value[:4]) if len(text_value) >= 4 and text_value[:4].isdigit() else None

def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default

@contextmanager
def _logged_section(section: str, **context: Any) -> Iterator[None]:
    Started = time.monotonic()
    Logger.info("[MB] section started", section=section, **context)
    try:
        yield
    except Exception as exc:
        Logger.exception(
            "[MB] section failed",
            Section=section,
            Elapsed_s=round(time.monotonic() - Started, 3),
            Error=_error(exc),
            **context,
        )
        raise
    else:
        Logger.info(
            "[MB] section completed",
            Section=section,
            Elapsed_s=round(time.monotonic() - Started, 3),
            **context,
        )

# ---------------------------------------------------------------------------
# Shared heartbeat monitor
# ---------------------------------------------------------------------------

_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: "OrderedDict[int, dict[str, Any]]" = OrderedDict()
_INFLIGHT_IDS = itertools.count()
_MONITOR_THREAD: threading.Thread | None = None

def _monitor_loop() -> None:
    while True:
        time.sleep(_MONITOR_TICK_SECONDS)
        Now = time.monotonic()
        Due: list[dict[str, Any]] = []
        with _INFLIGHT_LOCK:
            for entry in _INFLIGHT.values():
                if Now >= entry["next_warn"]:
                    entry["next_warn"] = Now + _HEARTBEAT_SECONDS
                    Due.append(
                        {
                            "section": entry["section"],
                            "elapsed_s": round(Now - entry["started"], 1),
                            "context": entry["context"],
                        }
                    )
        for item in Due:
            Logger.warning(
                "[MB] call still running",
                Section=item["section"],
                Elapsed_s=item["elapsed_s"],
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
    Section: str,
    Func: Callable[..., T],
    *args: Any,
    Log_context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> T:
    Context = dict(Log_context or {})
    Started = time.monotonic()
    _ensure_monitor()
    Call_id = next(_INFLIGHT_IDS)
    with _INFLIGHT_LOCK:
        _INFLIGHT[Call_id] = {
            "section": Section,
            "started": Started,
            "context": Context,
            "next_warn": Started + _HEARTBEAT_SECONDS,
        }

    Logger.info("[MB] call started", section=Section, **Context)
    try:
        Result = Func(*args, **kwargs)
    except Exception as exc:
        Logger.exception(
            "[MB] call failed",
            Section=Section,
            Elapsed_s=round(time.monotonic() - Started, 3),
            Error=_error(exc),
            **Context,
        )
        raise
    else:
        Logger.info(
            "[MB] call completed",
            Section=Section,
            Elapsed_s=round(time.monotonic() - Started, 3),
            **Context,
        )
        return Result
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(Call_id, None)

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
    Parts: list[str] = []
    for credit in artist_credit or []:
        if isinstance(credit, dict):
            Parts.append(str(credit.get("name") or ""))
            Parts.append(str(credit.get("joinphrase") or ""))
        else:
            Parts.append(str(credit))
    return "".join(Parts).strip()

def primary_album_artist(artist_credit: list[Any] | str) -> str:
    if isinstance(artist_credit, list) and artist_credit:
        First = artist_credit[0]
        if isinstance(First, dict):
            return str(First.get("name") or "").strip()
        return str(First or "").strip()
    if isinstance(artist_credit, str):
        return artist_credit.strip()
    return ""

def calculate_match_score(
    Mb_title: str,
    Mb_artist_credit: list[Any] | str,
    Local_album: str,
    Local_artist: str,
) -> float:
    Title_score = _similarity(Normalize_string(Local_album), Normalize_string(Mb_title))
    Artist_score = _similarity(
        Normalize_string(Local_artist),
        Normalize_string(primary_album_artist(Mb_artist_credit)),
    )
    return (Title_score * 0.6) + (Artist_score * 0.4)

def _parse_secondary_types(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(part).strip() for part in raw if str(part).strip()]
    return []

def _compose_album_type(primary_type: str, secondary_types: list[str]) -> str:
    Primary = str(primary_type or "album").strip().casefold() or "album"
    Meaningful = {
        "compilation", "live", "remix", "soundtrack", "spokenword",
        "demo", "dj-mix", "mixtape", "interview", "audiobook", "ep",
    }
    Secondary = next(
        (
            str(value).casefold()
            for value in secondary_types or []
            if str(value).casefold() in Meaningful
        ),
        "",
    )
    return f"{Primary}+{Secondary}" if Secondary else Primary

def _release_group_primary_type(group: Any) -> str:
    if not isinstance(group, dict):
        return ""
    return str(
        group.get("primary-type")
        or group.get("primary_type")
        or group.get("type")
        or ""
    ).casefold()

def _country_area_name(area: Any) -> str:
    if not isinstance(area, dict):
        return ""
    Name = str(area.get("name") or "").strip()
    if not Name:
        return ""
    Area_type = str(area.get("type") or "").strip().casefold()
    if Area_type and Area_type != "country":
        return ""
    return Name

def _artist_country_name(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("area", "begin-area", "beginArea"):
        Name = _country_area_name(data.get(key))
        if Name:
            return Name
    return ""

def _artist_lookup_candidates(artist: str) -> list[str]:
    Result: list[str] = []
    Seen: set[str] = set()
    for candidate in (artist or "", Strip_featured_artist(artist or "")):
        Key = str(candidate or "").casefold().strip()
        if Key and Key not in Seen:
            Result.append(str(candidate).strip())
            Seen.add(Key)
    return Result

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
        Value = normalize_isrc(raw)
        if Value:
            return Value
    return normalize_isrc(recording.get("isrc")) or None

def _recording_matches_album(recording: dict[str, Any], album: str) -> bool:
    Album = str(album or "").strip().casefold()
    if not Album or not recording:
        return False
    for release in recording.get("releases") or []:
        if not isinstance(release, dict):
            continue
        Release_group = release.get("release-group") or {}
        Candidates = (
            release.get("title") or "",
            Release_group.get("title") if isinstance(Release_group, dict) else "",
        )
        if any(value and _similarity(str(value), Album) >= 0.6 for value in Candidates):
            return True
    return False

def _release_track_count(release: Any) -> int:
    if not isinstance(release, dict):
        return 0
    Direct = _as_int(release.get("track-count"), 0)
    if Direct > 0:
        return Direct
    return sum(
        _as_int(medium.get("track-count"), 0)
        for medium in release.get("media") or []
        if isinstance(medium, dict)
    )

_NON_STUDIO_SECONDARY_TYPES = frozenset({
    "live", "compilation", "remix", "soundtrack", "spokenword", "demo",
    "dj-mix", "mixtape", "interview", "audiobook",
})

_ALBUM_IDENTITY_MATCH_FLOOR = 0.6

def _release_group_title_of(release: Any) -> str:
    if not isinstance(release, dict):
        return ""
    group = release.get("release-group") or {}
    if not isinstance(group, dict):
        return ""
    return str(group.get("title") or "").strip()

def _release_group_primary_type_of(release: Any) -> str:
    if not isinstance(release, dict):
        return ""
    group = release.get("release-group") or {}
    if not isinstance(group, dict):
        return ""
    return str(group.get("primary-type") or group.get("primary_type") or "").strip().casefold()

def _release_group_secondary_types_of(release: Any) -> list[str]:
    if not isinstance(release, dict):
        return []
    group = release.get("release-group") or {}
    if not isinstance(group, dict):
        return []
    return _parse_secondary_types(group.get("secondary-types") or group.get("secondary_types"))

def _release_album_identity(release: Any) -> str:
    return _release_group_title_of(release) or str(
        (release.get("title") if isinstance(release, dict) else "") or ""
    ).strip()

def _edition_title_matches(release_title: str, album_name: str) -> bool:
    if not release_title or not album_name:
        return False
    if not Extract_edition_annotation(album_name):
        return False
    if not Edition_annotations_compatible(release_title, album_name):
        return False
    return _similarity(release_title, album_name) >= 0.9

def _select_primary_release(releases: Any, album_name: str | None) -> dict[str, Any]:
    candidates = [r for r in (releases or []) if isinstance(r, dict)]
    if not candidates:
        return {}

    anchor = str(album_name or "").strip()

    if len(candidates) == 1:
        return candidates[0]

    def _sort_key(release: dict[str, Any]) -> tuple[Any, ...]:
        return (str(release.get("date") or "9999"), str(release.get("id") or ""))

    if anchor:
        edition_matches = [
            r for r in candidates
            if _edition_title_matches(str(r.get("title") or ""), anchor)
        ]
        if edition_matches:
            return min(edition_matches, key=_sort_key)

    if anchor:
        matching = [
            r for r in candidates
            if _release_album_identity(r)
            and _similarity(_release_album_identity(r), anchor) >= _ALBUM_IDENTITY_MATCH_FLOOR
        ]
        if matching:
            return min(matching, key=_sort_key)

    if anchor:
        return {}

    def _is_studio(release: dict[str, Any]) -> bool:
        primary = _release_group_primary_type_of(release)
        if primary not in ("", "album"):
            return False
        return not any(
            str(secondary).strip().casefold() in _NON_STUDIO_SECONDARY_TYPES
            for secondary in _release_group_secondary_types_of(release)
        )

    studio = [r for r in candidates if _is_studio(r)]
    if studio:
        studio.sort(
            key=lambda r: (
                0 if _release_group_title_of(r) else 1,
                *_sort_key(r),
            )
        )
        return studio[0]

    return min(candidates, key=_sort_key)

def _recording_live_affinity(recording: Any) -> bool | None:
    if not isinstance(recording, dict):
        return None
    releases = [r for r in (recording.get("releases") or []) if isinstance(r, dict)]
    if not releases:
        return None
    for release in releases:
        secondary = {
            str(s).strip().casefold()
            for s in _release_group_secondary_types_of(release)
        }
        if "live" in secondary:
            return True
    return False

def _client_available(client: Any) -> bool:
    Checker = getattr(client, "is_available", None)
    if not callable(Checker):
        return True
    try:
        return bool(Checker())
    except Exception:
        return True

# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

_SHARED_MB_CLIENT: MusicBrainzHttpClient | None = None
_service: "MusicBrainzService | None" = None
_shared_mb_service: "MusicBrainzService | None" = None

def get_shared_mb_client() -> MusicBrainzHttpClient:
    global _SHARED_MB_CLIENT
    if _SHARED_MB_CLIENT is not None:
        return _SHARED_MB_CLIENT
    Started = time.monotonic()
    Logger.info("[MB] shared HTTP client initialization requested")
    with _INIT_LOCK:
        if _SHARED_MB_CLIENT is None:
            Creation_started = time.monotonic()
            try:
                _SHARED_MB_CLIENT = MusicBrainzHttpClient(enabled=True)
            except Exception as exc:
                Logger.exception(
                    "[MB] shared HTTP client creation failed",
                    Elapsed_s=round(time.monotonic() - Creation_started, 3),
                    Error=_error(exc),
                )
                raise
        else:
            Logger.debug("[MB] shared HTTP client was initialized by another caller")

    return _SHARED_MB_CLIENT

# ---------------------------------------------------------------------------
# Best-release memoisation
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
    with _BEST_RELEASE_LOCK:
        _BEST_RELEASE_CACHE.clear()

# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class MusicBrainzService:
    def __init__(
        self,
        http_client: MusicBrainzHttpClient | None = None,
        enabled: bool = True,
    ) -> None:
        self.enabled = enabled
        self.http = http_client or MusicBrainzHttpClient(enabled=enabled)
        self._artist_singles_cache: dict[str, list[dict[str, Any]]] = {}
        self._album_year_cache: dict[str, int | None] = {}
        self._single_result_cache: dict[str, bool] = {}
        self._mem_lock = threading.Lock()
        self._cache_dirty = False
        self._cache_last_save = time.monotonic()
        self._mbid_cache: "OrderedDict[str, Any]" = OrderedDict()
        self._missing_release_group_warned = False
        atexit.register(self.flush_cache)

    def flush_cache(self) -> None:
        """Force any pending MBID cache changes to disk."""
        self._maybe_flush_cache(force=True)

    @staticmethod
    def _cache_key(title: str, artist: str, is_live: bool = False, annotation: str | None = None) -> str:
        base = f"{artist.casefold().strip()}::{title.casefold().strip()}"
        if is_live:
            base = f"{base}::live"
        if annotation:
            base = f"{base}::{annotation}"
        return base

    def get_suggested_mbid(self, title: str, artist: str, limit: int = 5, **kwargs: Any) -> tuple[str, float]:
        is_live_release = bool(kwargs.get("is_live_release"))
        edition_annotation = str(kwargs.get("edition_annotation") or "").strip() or None
        context = {
            "artist": artist,
            "track": title,
            "limit": limit,
            "is_live_release": is_live_release,
            "edition_annotation": edition_annotation,
        }
        if not self.enabled or not title or not artist:
            Logger.info(
                "[MB] recording suggestion skipped",
                reason="service disabled or incomplete input",
                **context,
            )
            return "", 0.0

        cache_key = self._cache_key(
            title, artist, is_live=is_live_release, annotation=edition_annotation,
        )
        now = time.time()
        with self._mem_lock:
            cached = self._mbid_cache.get(cache_key)
            if isinstance(cached, (list, tuple)) and len(cached) >= 2:
                mbid = str(cached[0] or "")
                score = float(cached[1] or 0)
                cached_at = float(cached[2]) if len(cached) >= 3 else None
                if mbid and (cached_at is None or now - cached_at < _MBID_CACHE_TTL_SECONDS):
                    self._mbid_cache.move_to_end(cache_key)
                    Logger.info(
                        "[MB] recording suggestion cache hit",
                        mbid=mbid,
                        score=round(score, 3),
                        **context,
                    )
                    return mbid, round(score, 3)

        query_title = Normalize_title_for_lucene_query(Strip_search_keywords(title))
        query = (
            f'recording:"{Escape_lucene_special_chars(query_title)}" '
            f'AND artist:"{Escape_lucene_special_chars(artist)}"'
        )
        try:
            recordings = _call_with_heartbeat(
                "recording.search",
                self.http.search_recordings,
                query,
                limit=limit,
                inc="releases+release-groups",
                Log_context=context,
            ) or []
            best_mbid, best_score = "", 0.0
            best_rank: tuple[int, int, float] | None = None
            normalized_title = Normalize_title_for_mbid_match(title)
            track_annotation = Extract_edition_annotation(title)
            album_annotation = edition_annotation if track_annotation is None else None
            
            for recording in recordings:
                if not isinstance(recording, dict):
                    continue
                candidate_title = str(recording.get("title") or "")
                candidate_annotation = Extract_edition_annotation(candidate_title)
                annotation_ok = bool(album_annotation) and candidate_annotation == album_annotation
                if not annotation_ok and not Edition_annotations_compatible(title, candidate_title):
                    continue
                score = _mbid_similarity(
                    normalized_title,
                    Normalize_title_for_mbid_match(candidate_title),
                )
                if score <= 0:
                    continue

                _affinity = _recording_live_affinity(recording)
                if _affinity is None:
                    _live_rank = 1
                else:
                    _live_rank = 2 if _affinity == is_live_release else 0
                _rank = (_live_rank, 1 if annotation_ok else 0, score)

                if best_rank is None or _rank > best_rank:
                    best_rank = _rank
                    best_mbid = str(recording.get("id") or "")
                    best_score = score

            if best_mbid and best_score >= _MBID_CACHE_SIMILARITY_FLOOR:
                self._record_mbid(cache_key, best_mbid, round(best_score, 3))
                self._maybe_flush_cache()

            Logger.info(
                "[MB] recording suggestion completed",
                mbid=best_mbid or None,
                score=round(best_score, 3),
                candidate_count=len(recordings),
                **context,
            )
            return best_mbid, round(best_score, 3)
        except Exception as exc:
            Logger.exception("[MB] recording suggestion failed", error=_error(exc), **context)
            return "", 0.0

    def lookup_recording_metadata(self, title: str, artist: str, *, album: str | None = None, is_live_release: bool = False, edition_annotation: str | None = None, **kwargs: Any) -> dict[str, Any]:
        context = {
            "title": title,
            "artist": artist,
            "album": album,
            "is_live_release": bool(is_live_release),
            "edition_annotation": edition_annotation,
        }
        if not title or not artist:
            Logger.info("[MB] recording metadata skipped", reason="incomplete input", **context)
            return {}
        try:
            mbid, confidence = self.get_suggested_mbid(
                title,
                artist,
                is_live_release=is_live_release,
                edition_annotation=edition_annotation,
            )
            if not mbid:
                return {}
            recording = _call_with_heartbeat(
                "recording.get",
                self.http.get_recording,
                mbid,
                inc="artist-credits+releases+release-groups+work-rels+genres",
                Log_context={**context, "mbid": mbid},
            )
            if not recording:
                Logger.info("[MB] recording metadata empty", mbid=mbid, **context)
                return {}
            return self._recording_to_metadata(recording, mbid, confidence, album_name=album)
        except Exception as exc:
            Logger.exception("[MB] recording metadata lookup failed", error=_error(exc), **context)
            return {}

    def lookup_recordings_by_mbid_bulk(self, mbids: list[str], *, album_name: str | None = None, original_release_year: int | None = None, **kwargs: Any) -> dict[str, dict[str, Any]]:
        if not self.enabled or not mbids:
            Logger.info(
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
                Log_context=context,
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
            Logger.info(
                "[MB] bulk recording lookup completed",
                returned=len(results),
                requested=len(mbids),
                **context,
            )
            return results
        except Exception as exc:
            Logger.exception("[MB] bulk recording lookup failed", error=_error(exc), **context)
            return {}

    def _recording_to_metadata(self, recording: dict[str, Any], mbid: str, confidence: float, *, album_name: str | None = None, original_release_year: int | None = None, **kwargs: Any) -> dict[str, Any]:
        credits = recording.get("artist-credit") or []
        full_artist_string = build_artist_credit_string(credits)
        
        first = credits[0] if credits else {}
        if isinstance(first, dict):
            artist = full_artist_string or str(first.get("name") or "").strip()
            artist_data = first.get("artist") or {}
            artist_mbid = (
                str(artist_data.get("id") or "").strip()
                if isinstance(artist_data, dict)
                else ""
            )
        else:
            artist = full_artist_string or str(first or "").strip()
            artist_mbid = ""
            
        releases = recording.get("releases") or []
        authoritative_album = str(album_name or "").strip()

        specific_release = _select_primary_release(releases, authoritative_album)
        specific_title = str(specific_release.get("title") or "").strip()
        version_release_year = _year_of(specific_release.get("date"))

        release_group_title = _release_group_title_of(specific_release)
        effective_album = authoritative_album or release_group_title or specific_title
        effective_year = (
            original_release_year
            if original_release_year is not None
            else version_release_year
        )

        if (authoritative_album and specific_title
                and authoritative_album.casefold() != specific_title.casefold()):
            Logger.debug(
                "[MB] specific release title ignored in favour of album name",
                recording_mbid=mbid,
                authoritative_album_name=authoritative_album,
                ignored_release_title=specific_title,
            )
        if (original_release_year is not None and version_release_year is not None
                and version_release_year != original_release_year):
            Logger.debug(
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
                if str(relation.get("type") or "").casefold() not in {"performance", "recording of"}:
                    continue
                work = relation.get("work") or {}
                if not isinstance(work, dict):
                    continue
                if work.get("id"):
                    work_mbid = str(work.get("id"))
                for work_relation in work.get("relations") or []:
                    if not isinstance(work_relation, dict):
                        continue
                    if str(work_relation.get("type") or "").casefold() not in {"composer", "writer", "lyricist"}:
                        continue
                    target = work_relation.get("artist") or {}
                    if isinstance(target, dict) and target.get("name"):
                        writers.append(str(target["name"]))
        except Exception as exc:
            Logger.warning(
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
            "album": effective_album,
            "album_artist": primary_album_artist(specific_release.get("artist-credit") or recording.get("artist-credit") or []),
            "isrc": _first_isrc(recording),
            "year": effective_year,
            "original_release_year": original_release_year,
            "version_release_year": version_release_year,
            "musicbrainz_release_title": specific_title,
            "release_title": specific_title,
            "release_year": version_release_year,
            "recording_mbid": mbid,
            "confidence": confidence,
            "writer": ", ".join(dict.fromkeys(writers)),
            "work_mbid": work_mbid,
            "genres": list(dict.fromkeys(genres)),
        }

    def lookup_original_album_year(self, artist: str, album: str, **kwargs: Any) -> int | None:
        context = {"artist": artist, "album": album}
        if not self.enabled or not artist or not album:
            Logger.info(
                "[MB] original album year lookup skipped",
                reason="service disabled or incomplete input",
                **context,
            )
            return None

        cache_key = f"{artist.casefold().strip()}::{album.casefold().strip()}"
        with self._mem_lock:
            if cache_key in self._album_year_cache:
                cached_year = self._album_year_cache[cache_key]
                Logger.info(
                    "[MB] original album year cache hit",
                    original_release_year=cached_year,
                    **context,
                )
                return cached_year

        clean_album = Strip_search_keywords(album)
        query = (
            f'artist:"{Escape_lucene_special_chars(artist)}" '
            f'AND releasegroup:"{Escape_lucene_special_chars(clean_album)}"'
        )
        started = time.monotonic()
        Logger.info(
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
                Log_context={**context, "query": query},
            ) or []

            if not groups and clean_album and _client_available(self.http):
                terms = Normalize_title_for_lucene_query(clean_album)
                if terms:
                    fallback_query = (
                        f'artist:"{Escape_lucene_special_chars(artist)}" '
                        f"AND releasegroup:{terms}"
                    )
                    groups = _call_with_heartbeat(
                        "album.original_year_fallback_search",
                        self.http.search_release_groups,
                        fallback_query,
                        limit=5,
                        Log_context={**context, "query": fallback_query},
                    ) or []

            ranked: list[tuple[float, int, dict[str, Any]]] = []
            for group in groups:
                if not isinstance(group, dict):
                    continue
                year = _year_of(group.get("first-release-date"))
                if year is None:
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
                with self._mem_lock:
                    self._album_year_cache[cache_key] = None
                return None

            score, year, group = ranked[0]
            if score < _RELEASE_GROUP_MATCH_FLOOR:
                with self._mem_lock:
                    self._album_year_cache[cache_key] = None
                return None

            with self._mem_lock:
                self._album_year_cache[cache_key] = year
            return year
        except Exception as exc:
            Logger.exception(
                "[MB] original album year lookup failed",
                error=_error(exc),
                elapsed_s=round(time.monotonic() - started, 3),
                **context,
            )
            return None

    def lookup_album_metadata(self, entries: list[tuple[str, str]], candidates_per_entry: int = 5, album: str = "", original_release_year: int | None = None, **kwargs: Any) -> dict[str, dict[str, Any]]:
        if not self.enabled:
            return {}

        album = str(album or "").strip()
        unique = sorted({
            (str(title or "").strip(), str(artist or "").strip())
            for title, artist in entries or []
            if title and artist
        })
        if not unique:
            return {}

        album_artist = unique[0][1]
        effective_year = original_release_year
        if effective_year is None and album and album_artist:
            effective_year = self.lookup_original_album_year(album_artist, album)

        local_track_count = len(unique)

        results: dict[str, dict[str, Any]] = {}
        for chunk_start in range(0, len(unique), _MB_BATCH_CHUNK):
            chunk = unique[chunk_start:chunk_start + _MB_BATCH_CHUNK]
            query_groups = [
                f'(recording:"{Escape_lucene_special_chars(Normalize_title_for_lucene_query(title))}" '
                f'AND artist:"{Escape_lucene_special_chars(artist)}")'
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
                    Log_context=chunk_context,
                ) or []
            except Exception:
                continue

            batch: list[tuple[str, str, float]] = []
            for title, artist in chunk:
                normalized = Normalize_title_for_mbid_match(title)
                candidates_ranked = []
                for recording in recordings:
                    if not isinstance(recording, dict):
                        continue
                    candidate_title = str(recording.get("title") or "")
                    if not Edition_annotations_compatible(title, candidate_title):
                        continue

                    base_score = _mbid_similarity(
                        normalized,
                        Normalize_title_for_mbid_match(candidate_title),
                    )
                    if base_score < _MB_BATCH_SIMILARITY_FLOOR:
                        continue

                    anchor = _recording_matches_album(recording, album)
                    penalty = 0.0
                    if local_track_count > 0:
                        best_diff = 999
                        for rel in recording.get("releases") or []:
                            rel_track_count = _release_track_count(rel)
                            if rel_track_count > 0:
                                diff = abs(local_track_count - rel_track_count)
                                if diff < best_diff:
                                    best_diff = diff
                        if best_diff != 999:
                            penalty = best_diff * 0.05

                    candidates_ranked.append((base_score - penalty, base_score, anchor, recording))

                if not candidates_ranked:
                    continue

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
                    if album:
                        track_metadata["album"] = album
                    if effective_year is not None:
                        track_metadata["year"] = effective_year
                        track_metadata["original_release_year"] = effective_year
                    results[key] = track_metadata
                self._maybe_flush_cache()

        return results

    def is_single(self, title: str, artist: str, album_track_count: int | None = None, **kwargs: Any) -> bool:
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
        except Exception:
            return False

    def get_artist_country(self, artist: str, **kwargs: Any) -> str:
        if not self.enabled or not artist:
            return ""
        try:
            result = _call_with_heartbeat(
                "artist.country_search",
                self.http.search_artists,
                f'artist:"{Escape_lucene_special_chars(artist)}"',
                limit=1,
                inc="area",
                Log_context={"artist": artist},
            ) or []
            data = result[0] if result and isinstance(result[0], dict) else {}
            return _artist_country_name(data)
        except Exception:
            return ""

    def get_genres(self, title: str, artist: str, **kwargs: Any) -> list[str]:
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
                Log_context={"artist": artist, "title": title, "mbid": mbid},
            )
            return [
                str(item["name"])
                for item in (recording or {}).get("genres") or []
                if isinstance(item, dict) and item.get("name")
            ]
        except Exception:
            return []

    def search_releasegroup_matches(self, artist_name: str, album_name: str, limit: int = 10, **kwargs: Any) -> list[dict[str, Any]]:
        started = time.monotonic()
        context = {"artist": artist_name, "album": album_name, "limit": limit}

        if not self.enabled or not artist_name or not album_name:
            return []

        expected_count = _get_local_track_count(artist_name, album_name) or None
        clean_album = Strip_search_keywords(album_name)
        escaped_artist = Escape_lucene_special_chars(artist_name)
        exact_query = (
            f'artist:"{escaped_artist}" '
            f'AND releasegroup:"{Escape_lucene_special_chars(clean_album)}"'
        )
        try:
            groups = _call_with_heartbeat(
                "release_group.exact_search",
                self.http.search_release_groups,
                exact_query,
                limit=limit,
                Log_context={**context, "query": exact_query},
            ) or []
        except Exception:
            groups = []

        if not groups and clean_album and _client_available(self.http):
            terms = Normalize_title_for_lucene_query(clean_album)
            if terms:
                fallback_query = f'artist:"{escaped_artist}" AND releasegroup:{terms}'
                try:
                    groups = _call_with_heartbeat(
                        "release_group.fallback_search",
                        self.http.search_release_groups,
                        fallback_query,
                        limit=limit,
                        Log_context={**context, "query": fallback_query},
                    ) or []
                except Exception:
                    groups = []

        matches: list[dict[str, Any]] = []

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

                    sec_lower = {str(t).casefold() for t in secondary_types}
                    if "live" in sec_lower and "live" not in album_name.casefold():
                        score *= 0.7
                    if "compilation" in sec_lower and "compilation" not in album_name.casefold():
                        score *= 0.8
                    if not secondary_types and str(group.get("primary-type") or "").casefold() == "album":
                        score *= 1.15

                    matches.append({
                        "id": group.get("id"),
                        "title": group.get("title"),
                        "primary_type": group.get("primary-type"),
                        "match_score": score,
                        "secondary_types": secondary_types,
                        "first_release_date": group.get("first-release-date") or "",
                    })
                except Exception:
                    continue

        matches.sort(key=lambda item: item.get("match_score", 0.0), reverse=True)

        if expected_count and matches:
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

        for match in matches:
            match["match_score"] = round(float(match.get("match_score") or 0.0), 3)

        matches.sort(key=lambda item: item.get("match_score", 0.0), reverse=True)
        return matches

    @staticmethod
    def merge_metadata(base: dict[str, Any], mb: dict[str, Any], overrides: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        overrides = overrides or {}
        def pick(*values: Any) -> Any:
            return next((value for value in values if value not in (None, "")), None)
        return {
            "title": pick(overrides.get("title"), mb.get("title"), base.get("title")),
            "artist": pick(overrides.get("artist"), mb.get("artist"), base.get("artist")),
            "album": pick(overrides.get("album"), mb.get("album"), base.get("album")),
            "album_artist": pick(overrides.get("album_artist"), base.get("album_artist"), mb.get("album_artist")),
            "year": pick(overrides.get("year"), mb.get("original_release_year"), mb.get("year"), base.get("year")),
        }

    def get_artist_relationships(self, artist_mbid: str, relation_type: str = "artist", **kwargs: Any) -> list[dict[str, Any]]:
        if not self.enabled or not artist_mbid:
            return []
        try:
            data = _call_with_heartbeat(
                "artist.relationships_get",
                self.http.get_artist,
                artist_mbid,
                inc="artist-rels",
                Log_context={
                    "artist_mbid": artist_mbid,
                    "relation_type": relation_type,
                },
            ) or {}
            relations = data.get("relations") or []
            return [
                relation for relation in relations
                if isinstance(relation, dict)
                and str(relation.get("type") or "").casefold() == relation_type.casefold()
            ]
        except Exception:
            return []

    def get_recording_relationships(self, recording_mbid: str, **kwargs: Any) -> list[dict[str, Any]]:
        if not self.enabled or not recording_mbid:
            return []
        try:
            data = _call_with_heartbeat(
                "recording.relationships_get",
                self.http.get_recording,
                recording_mbid,
                inc=_RECORDING_RELATIONSHIP_INC,
                Log_context={"recording_mbid": recording_mbid},
            ) or {}
            return data.get("relations", []) or []
        except Exception:
            return []

    def get_composers_for_recording(self, recording_mbid: str, **kwargs: Any) -> list[str]:
        composers: list[str] = []
        def _collect(artist: Any) -> None:
            if isinstance(artist, dict) and artist.get("name"):
                composers.append(str(artist["name"]))

        for relation in self.get_recording_relationships(recording_mbid):
            if not isinstance(relation, dict):
                continue
            if str(relation.get("type") or "").casefold() in {
                "composer", "writer", "lyricist",
            }:
                _collect(relation.get("artist"))
            work = relation.get("work") or {}
            if not isinstance(work, dict):
                continue
            for work_relation in work.get("relations") or []:
                if not isinstance(work_relation, dict):
                    continue
                if str(work_relation.get("type") or "").casefold() in {
                    "composer", "writer", "lyricist",
                }:
                    _collect(work_relation.get("artist"))
        return list(dict.fromkeys(composers))

    def get_recording_genres(self, title: str, artist: str, **kwargs: Any) -> list[str]:
        if not self.enabled or not title or not artist:
            return []
        try:
            query_title = Normalize_title_for_lucene_query(Strip_search_keywords(title))
            query = (
                f'recording:"{Escape_lucene_special_chars(query_title)}" '
                f'AND artist:"{Escape_lucene_special_chars(artist)}"'
            )
            recordings = _call_with_heartbeat(
                "recording.genre_search",
                self.http.search_recordings,
                query,
                limit=5,
                Log_context={"artist": artist, "title": title, "query": query},
            ) or []
            genres: list[str] = []
            for recording in recordings:
                if not isinstance(recording, dict):
                    continue
                for item in recording.get("genres") or []:
                    if isinstance(item, dict) and item.get("name"):
                        genres.append(str(item["name"]))
            return list(dict.fromkeys(genres))
        except Exception:
            return []

    def _maybe_flush_cache(self, force: bool = False) -> None:
        with self._mem_lock:
            if not self._cache_dirty:
                return
            if not force and (time.monotonic() - self._cache_last_save) < _CACHE_FLUSH_SECONDS:
                return
            payload = {
                "entries": {
                    key: list(value)
                    for key, value in self._mbid_cache.items()
                    if isinstance(value, (list, tuple))
                }
            }
            self._cache_dirty = False
            self._cache_last_save = time.monotonic()
        path = os.path.dirname(CACHE_FILE)
        if path:
            try:
                os.makedirs(path, exist_ok=True)
            except Exception:
                pass
        try:
            with _CACHE_IO_LOCK:
                with open(CACHE_FILE, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
        except Exception:
            pass

    def _record_mbid(self, key: str, mbid: str, confidence: float) -> None:
        with self._mem_lock:
            self._mbid_cache[key] = [mbid, round(float(confidence), 3), time.time()]
            self._mbid_cache.move_to_end(key)
            while len(self._mbid_cache) > _MBID_CACHE_MAX_SIZE:
                self._mbid_cache.popitem(last=False)
            self._cache_dirty = True

    def _artist_release_groups(self, artist: str) -> list[dict[str, Any]]:
        if not self.enabled or not artist:
            return []
        try:
            artist_mbid, _confidence = self.get_suggested_mbid(artist, artist)
            if not artist_mbid:
                return []
            payload = _call_with_heartbeat(
                "artist.release_groups_browse",
                self.http.browse_artist_release_groups,
                artist_mbid,
                inc="",
                limit=100,
                Log_context={"artist": artist, "artist_mbid": artist_mbid},
            ) or {}
            groups = payload.get("release-groups") or []
            return [group for group in groups if isinstance(group, dict)]
        except Exception:
            return []

    def _artist_singles_fallback(self, artist: str) -> list[dict[str, Any]]:
        key = str(artist or "").casefold().strip()
        if not key:
            return []
        with self._mem_lock:
            cached = self._artist_singles_cache.get(key)
        if cached is not None:
            return cached

        if not _client_available(self.http):
            return []

        groups = _call_with_heartbeat(
            "single.release_group_artist_fallback",
            self.http.search_release_groups,
            f'artist:"{Escape_lucene_special_chars(artist)}"',
            limit=100,
            Log_context={"artist": artist},
        ) or []
        groups = [group for group in groups if isinstance(group, dict)]
        with self._mem_lock:
            self._artist_singles_cache[key] = groups
        return groups

    def _release_group_has_single_release(self, title: str, artist: str) -> bool:
        query_title = Normalize_title_for_lucene_query(Strip_search_keywords(title))
        query = (
            f'releasegroup:"{Escape_lucene_special_chars(query_title)}" '
            f'AND artist:"{Escape_lucene_special_chars(artist)}"'
        )
        context = {"artist": artist, "title": title}
        groups = _call_with_heartbeat(
            "single.release_group_search",
            self.http.search_release_groups,
            query,
            limit=10,
            Log_context=context,
        ) or []
        if not groups:
            groups = self._artist_singles_fallback(artist)
        normalized = Normalize_title_for_lookup(title)
        for group in groups:
            if not isinstance(group, dict):
                continue
            if _release_group_primary_type(group) not in {"single", "ep"}:
                continue
            group_title = str(group.get("title") or "")
            if not Edition_annotations_compatible(title, group_title):
                continue
            if _similarity(normalized, Normalize_title_for_lookup(group_title)) >= 0.7:
                return True
        return False

    def _recording_search_has_single_release(self, title: str, artist: str) -> bool:
        query_title = Normalize_title_for_lucene_query(Strip_search_keywords(title))
        query = (
            f'recording:"{Escape_lucene_special_chars(query_title)}" '
            f'AND artist:"{Escape_lucene_special_chars(artist)}"'
        )
        recordings = _call_with_heartbeat(
            "single.recording_search",
            self.http.search_recordings,
            query,
            limit=10,
            Log_context={"artist": artist, "title": title},
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
        if not Edition_annotations_compatible(title, release_group_title):
            return False
        left = Normalize_title_for_lookup(Strip_single_release_suffix(title) or title)
        right = Normalize_title_for_lookup(
            Strip_single_release_suffix(release_group_title) or release_group_title
        )
        return left == right or _similarity(left, right) >= 0.85

    def _recording_has_single_release(self, mbid: str, title: str = "") -> bool:
        recording = _call_with_heartbeat(
            "single.recording_get",
            self.http.get_recording,
            mbid,
            inc="releases+release-groups",
            Log_context={"mbid": mbid, "title": title},
        )
        releases = (recording or {}).get("releases") or []

        if releases and not any(
            isinstance(release, dict) and release.get("release-group")
            for release in releases
        ):
            if not self._missing_release_group_warned:
                self._missing_release_group_warned = True
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

    def clear_transient_caches(self) -> None:
        with self._mem_lock:
            self._mbid_cache.clear()
            self._album_year_cache.clear()
            self._single_result_cache.clear()
        clear_release_caches()
        clearer = getattr(self.http, "clear_caches", None)
        if callable(clearer):
            clearer()

def _get_service() -> MusicBrainzService:
    global _service
    if _service is not None:
        return _service
    with _INIT_LOCK:
        if _service is None:
            Client = get_shared_mb_client()
            _service = MusicBrainzService(http_client=Client, enabled=True)
    return _service

def get_shared_mb_service() -> MusicBrainzService:
    global _shared_mb_service
    if _shared_mb_service is not None:
        return _shared_mb_service
    with _INIT_LOCK:
        if _shared_mb_service is None:
            Client = get_shared_mb_client()
            _shared_mb_service = MusicBrainzService(http_client=Client, enabled=True)
    return _shared_mb_service

def lookup_recording_metadata(
    title: str,
    artist: str,
    *,
    album: str | None = None,
    is_live_release: bool = False,
    edition_annotation: str | None = None,
) -> dict[str, Any]:
    return _get_service().lookup_recording_metadata(
        title,
        artist,
        album=album,
        is_live_release=is_live_release,
        edition_annotation=edition_annotation,
    )

def merge_metadata(base: dict[str, Any], mb: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    return _get_service().merge_metadata(base, mb, overrides)

# ---------------------------------------------------------------------------
# Release metadata fetch 
# ---------------------------------------------------------------------------

def _unique_join(values: Any, *, limit: int = 8) -> str:
    seen: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.append(text)
    return " / ".join(seen[:limit])


def _release_event_country(release: dict[str, Any]) -> str:
    events = release.get("release-events") or release.get("release_events") or []
    if isinstance(events, list):
        dates: list[tuple[str, str]] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            area = event.get("area") or {}
            name = str(area.get("name") or "").strip() if isinstance(area, dict) else ""
            if name:
                dates.append((str(event.get("date") or "9999"), name))
        if dates:
            dates.sort(key=lambda item: item[0])
            return dates[0][1]
    return str(release.get("country") or "").strip()


def _release_extended_fields(release: dict[str, Any], media: Any) -> dict[str, str]:
    labels: list[str] = []
    catalogs: list[str] = []
    label_info = release.get("label-info") or release.get("label_info") or []
    if isinstance(label_info, list):
        for entry in label_info:
            if not isinstance(entry, dict):
                continue
            label = entry.get("label") or {}
            if isinstance(label, dict) and label.get("name"):
                labels.append(str(label["name"]))
            if entry.get("catalog-number"):
                catalogs.append(str(entry["catalog-number"]))
            elif entry.get("catalog_number"):
                catalogs.append(str(entry["catalog_number"]))

    formats: list[str] = []
    if isinstance(media, list):
        for medium in media:
            if isinstance(medium, dict) and medium.get("format"):
                formats.append(str(medium["format"]))

    return {
        "recordlabel": _unique_join(labels),
        "catalognumber": _unique_join(catalogs),
        "barcode": str(release.get("barcode") or "").strip(),
        "releasedate": str(release.get("date") or "").strip(),
        "media": _unique_join(formats),
        "releasecountry": _release_event_country(release),
    }


def fetch_musicbrainz_release_metadata(release_id: str) -> dict[str, Any] | None:
    """Fetch and flatten a MusicBrainz release for comparison/Link/Align.

    Returns None on any failure so callers can surface a clean error instead
    of raising. On success, returns the release's identity fields plus a
    flat ``tracks`` list (one entry per medium/track) shaped for
    ``_match_mb_tracks_to_library``.
    """
    if not release_id:
        return None
    try:
        Client = get_shared_mb_client()
        Release = _call_with_heartbeat(
            "release.fetch_metadata",
            Client.get_release,
            release_id,
            inc="recordings+artist-credits+release-groups+media+labels+work-rels+recording-level-rels+work-level-rels+artist-rels+genres",
            Log_context={"release_id": release_id},
        )
    except Exception as exc:
        Logger.warning(
            "[MB] release metadata fetch failed",
            release_id=release_id,
            error=_error(exc),
        )
        return None

    if not isinstance(Release, dict) or not Release.get("id"):
        return None

    Artist_credit = Release.get("artist-credit") or []
    Release_group = Release.get("release-group") or {}
    if not isinstance(Release_group, dict):
        Release_group = {}

    First_release_date = str(Release_group.get("first-release-date") or "")
    Original_release_year = _year_of(First_release_date) or _year_of(Release.get("date"))
    Version_release_year = _year_of(Release.get("date"))

    Secondary_types = _parse_secondary_types(
        Release_group.get("secondary-types") or Release_group.get("secondary_types")
    )
    Album_type = _compose_album_type(_release_group_primary_type(Release_group), Secondary_types)

    First_artist = Artist_credit[0] if Artist_credit and isinstance(Artist_credit[0], dict) else {}
    Artist_ref = First_artist.get("artist") if isinstance(First_artist, dict) else None
    Album_artist_mbid = str(Artist_ref.get("id") or "") if isinstance(Artist_ref, dict) else ""

    Tracks: list[dict[str, Any]] = []
    Media = Release.get("media") or []
    for disc_index, medium in enumerate(Media, start=1):
        if not isinstance(medium, dict):
            continue
        Disc_number = _as_int(medium.get("position"), disc_index) or disc_index
        for track in medium.get("tracks") or []:
            if not isinstance(track, dict):
                continue
            Recording = track.get("recording") or {}
            if not isinstance(Recording, dict):
                Recording = {}
            Position = track.get("position") if track.get("position") is not None else track.get("number")
            Length = track.get("length") if track.get("length") is not None else Recording.get("length")
            
            genres = [
                str(item.get("name") or "").strip()
                for item in Recording.get("genres") or []
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            ]
            
            Tracks.append({
                "mb_disc_number": Disc_number,
                "mb_track_number": _as_int(Position, 0),
                "mb_title": str(track.get("title") or Recording.get("title") or ""),
                "mb_recording_mbid": str(Recording.get("id") or ""),
                "mb_duration": Length,
                "mb_genres": list(dict.fromkeys(genres)),
            })

    return {
        "release_mbid": str(Release.get("id") or release_id),
        "release_group_mbid": str(Release_group.get("id") or ""),
        "release_group_title": str(Release_group.get("title") or ""),
        "release_title": str(Release.get("title") or ""),
        "specific_release_title": str(Release.get("title") or ""),
        "original_release_year": Original_release_year,
        "release_year": Version_release_year or Original_release_year,
        "version_release_year": Version_release_year,
        "artist": build_artist_credit_string(Artist_credit) or _mb_artist_credit_name(Artist_credit),
        "album_artist_mbid": Album_artist_mbid,
        "album_type": Album_type,
        "disc_count": len(Media),
        "artist_credit": build_artist_credit_string(Artist_credit),
        **_release_extended_fields(Release, Media),
        "tracks": Tracks,
    }

def fetch_release_metadata(release_id: str) -> dict[str, Any] | None:
    return fetch_musicbrainz_release_metadata(release_id)

def resolve_release_id(release_id: str) -> str:
    if not release_id:
        return release_id

    try:
        http = _get_service().http
        data = http.get_release(release_id, inc="")
        if data and data.get("id"):
            return release_id
    except Exception:
        pass

    try:
        releases = http.browse_releases_for_group(release_id, inc="media", limit=50)
        if releases:
            def _total_tracks(rel: dict[str, Any]) -> int:
                return sum(
                    _as_int(m.get("track-count"), 0)
                    for m in (rel.get("media") or [])
                    if isinstance(m, dict)
                )

            official = [
                r for r in releases
                if str(r.get("status") or "").strip().lower() == "official"
            ]
            candidates = [r for r in (official or releases) if _total_tracks(r) > 0]
            candidates = candidates or (official or releases)
            best = max(candidates, key=_total_tracks)
            resolved = str(best.get("id") or "")
            return resolved or release_id
    except Exception:
        pass

    return release_id


def _lookup_existing_mbid(Existing_mbid: str, Artist: str, Album: str) -> dict[str, Any] | None:
    if not Existing_mbid:
        return None
    Client = get_shared_mb_client()
    Context = {"existing_mbid": Existing_mbid, "artist": Artist, "album": Album}

    try:
        Data = _call_with_heartbeat(
            "album.existing_release_get",
            Client.get_release,
            Existing_mbid,
            inc="artist-credits+release-groups+media",
            Log_context=Context,
        )
        if Data:
            Group = Data.get("release-group") or {}
            mb_track_count = sum(
                _as_int(m.get("track-count"), 0)
                for m in (Data.get("media") or [])
                if isinstance(m, dict)
            )
            return {
                "mbid": Existing_mbid,
                "title": Group.get("title") or Data.get("title", Album),
                "artist": primary_album_artist(Data.get("artist-credit") or []) or Artist,
                "primary_type": Group.get("primary-type", "Album"),
                "secondary_types": _parse_secondary_types(Group.get("secondary-types")),
                "first_release_date": Group.get("first-release-date") or Data.get("date") or "",
                "cover_art_url": _cover_art_url(str(Group.get("id") or ""), Existing_mbid),
                "confidence": 1.0,
                "source": "musicbrainz",
                "is_stored_mbid": True,
                "mbid_type": "release",
                "track_count": mb_track_count,
            }
    except Exception:
        pass

    try:
        Data = _call_with_heartbeat(
            "album.existing_release_group_get",
            Client.get_release_group,
            Existing_mbid,
            inc="artist-credits",
            Log_context=Context,
        )
        if Data:
            return {
                "mbid": Existing_mbid,
                "title": Data.get("title", Album),
                "artist": _mb_artist_credit_name(Data.get("artist-credit") or []) or Artist,
                "primary_type": Data.get("primary-type", "Album"),
                "secondary_types": _parse_secondary_types(Data.get("secondary-types")),
                "first_release_date": Data.get("first-release-date", ""),
                "cover_art_url": _cover_art_url(Existing_mbid),
                "confidence": 1.0,
                "source": "musicbrainz",
                "is_stored_mbid": True,
                "mbid_type": "release-group",
            }
    except Exception:
        pass
    return None


def lookup_musicbrainz_album(Artist: str, Album: str, Existing_mbid: str = "") -> dict[str, Any]:
    Results: list[dict[str, Any]] = []
    if Existing_mbid:
        Stored = _lookup_existing_mbid(Existing_mbid, Artist, Album)
        if Stored:
            from services.metadata.album_mbid_guard import (
                guard_album_mbid as Guard_album_mbid,
                log_verdict as Log_mbid_verdict,
            )

            Verdict = Guard_album_mbid(
                artist=Artist,
                album=Album,
                mb_artist=str(Stored.get("artist") or ""),
                mb_album=str(Stored.get("title") or ""),
            )

            local_track_count = _get_local_track_count(Artist, Album)
            stored_track_count = Stored.get("track_count") or 0

            if Verdict["ok"] and local_track_count > 0 and stored_track_count > 0:
                if abs(local_track_count - stored_track_count) >= 4:
                    Verdict = {
                        "ok": False,
                        "reason": f"Track count mismatch (Local: {local_track_count}, MBID: {stored_track_count})"
                    }

            if Verdict["ok"]:
                Results.append(Stored)
            else:
                Log_mbid_verdict(Verdict, artist=Artist, album=Album, mbid=Existing_mbid)

    Query = (
        f'release:"{Escape_lucene_special_chars(Album)}" '
        f'AND artist:"{Escape_lucene_special_chars(Artist)}"'
    )
    try:
        Groups = _call_with_heartbeat(
            "album.release_group_search",
            get_shared_mb_client().search_release_groups,
            Query,
            limit=10,
            Log_context={"artist": Artist, "album": Album},
        ) or []
    except Exception:
        Groups = []

    Seen = {item["mbid"] for item in Results}
    for Group in Groups:
        if not isinstance(Group, dict):
            continue
        Group_id = str(Group.get("id") or "")
        if not Group_id or Group_id in Seen:
            continue
        Results.append({
            "mbid": Group_id,
            "title": Group.get("title", ""),
            "artist": _mb_artist_credit_name(Group.get("artist-credit") or []),
            "primary_type": Group.get("primary-type", "Album"),
            "secondary_types": _parse_secondary_types(Group.get("secondary-types")),
            "first_release_date": Group.get("first-release-date", ""),
            "cover_art_url": _cover_art_url(Group_id),
            "confidence": round(
                calculate_match_score(
                    Group.get("title") or "",
                    Group.get("artist-credit") or [],
                    Album,
                    Artist,
                ),
                3,
            ),
            "source": "musicbrainz",
            "is_stored_mbid": False,
            "mbid_type": "release-group",
        })
        Seen.add(Group_id)

    Stored_results = [item for item in Results if item.get("is_stored_mbid")]
    Other_results = sorted(
        [item for item in Results if not item.get("is_stored_mbid")],
        key=lambda item: item.get("confidence") or 0.0,
        reverse=True,
    )
    return {"results": (Stored_results + Other_results)[:11]}

def _release_summary(release: dict[str, Any]) -> dict[str, Any]:
    Media = release.get("media") or []
    Release_id = str(release.get("id") or "")
    return {
        "id": Release_id,
        "title": release.get("title", ""),
        "date": release.get("date", ""),
        "country": release.get("country", ""),
        "status": release.get("status", ""),
        "disambiguation": release.get("disambiguation", ""),
        "track_count": _release_track_count(release),
        "disc_count": len(Media),
        "formats": sorted({str(medium.get("format") or "").strip() for medium in Media if isinstance(medium, dict) and medium.get("format")}),
        "cover_art_url": _cover_art_url(release_id=Release_id),
    }

def _browse_group_releases(release_group_mbid: str, section: str) -> list[dict[str, Any]]:
    Cache_key = f"browse::{release_group_mbid}"
    Cached = _best_release_cache_get(Cache_key)
    if Cached is not None:
        return list(Cached.get("releases") or [])
    Raw = _call_with_heartbeat(
        section,
        get_shared_mb_client().browse_releases_for_group,
        release_group_mbid,
        inc="media+labels",
        limit=100,
        Log_context={"release_group_mbid": release_group_mbid},
    ) or []
    Releases = [_release_summary(release) for release in Raw if isinstance(release, dict)]
    _best_release_cache_set(Cache_key, {"releases": Releases})
    return Releases

def get_release_group_releases(Release_group_mbid: str, Include_track_counts: bool = False) -> dict[str, Any]:
    if not Release_group_mbid:
        return {"success": False, "error": "release_group_mbid required", "releases": []}

    try:
        Client = get_shared_mb_client()
        Payload = _call_with_heartbeat(
            "release_group.releases",
            Client.get_release_group,
            Release_group_mbid,
            inc="releases",
            Log_context={"release_group_mbid": Release_group_mbid},
        ) or {}
        Raw = Payload.get("releases") or []
        Releases = [_release_summary(release) for release in Raw if isinstance(release, dict)]

        if Include_track_counts and Releases:
            try:
                Browsed = Client.browse_releases_for_group(
                    Release_group_mbid, inc="media", limit=100,
                ) or []
            except TypeError:
                Browsed = Client.browse_releases_for_group(Release_group_mbid) or []
            Counts = {
                str(release.get("id")): _release_summary(release)
                for release in Browsed
                if isinstance(release, dict) and release.get("id")
            }
            for release in Releases:
                Enriched = Counts.get(str(release.get("id")))
                if not Enriched:
                    continue
                release["track_count"] = Enriched["track_count"]
                release["disc_count"] = Enriched["disc_count"]
                release["formats"] = Enriched["formats"]

        return {"success": True, "releases": Releases, "release_count": len(Releases)}
    except Exception as exc:
        return {"success": False, "error": _error(exc), "releases": []}

# ---------------------------------------------------------------------------
# Local library reads
# ---------------------------------------------------------------------------

def _get_local_track_count(artist: str, album: str) -> int:
    try:
        from db.engine import db_session
        from sqlalchemy import text
        with db_session() as session:
            Row = session.execute(
                text(
                    "SELECT COUNT(*) FROM tracks "
                    "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) "
                    "AND LOWER(COALESCE(album, '')) = LOWER(:album)"
                ),
                {"artist": artist, "album": album},
            ).fetchone()
            return int(Row[0]) if Row and Row[0] is not None else 0
    except Exception:
        return 0

def _fetch_library_tracks(artist: str, album: str) -> list[dict[str, Any]]:
    try:
        from db.engine import db_session
        from sqlalchemy import text
        with db_session() as session:
            Rows = session.execute(
                text(_COMPARE_LIBRARY_TRACKS_SQL),
                {"artist": artist, "album": album},
            ).fetchall()
    except Exception:
        return []
    return [dict(zip(_LIBRARY_TRACK_COLUMNS, row)) for row in Rows]

def get_musicbrainz_best_release(
    Artist: str,
    Album: str,
    Release_group_mbid: str,
) -> dict[str, Any]:
    if not Release_group_mbid:
        return {"success": False, "error": "No release-group MBID supplied"}

    Result_key = (
        f"best::{Release_group_mbid}::{str(Artist).casefold().strip()}"
        f"::{str(Album).casefold().strip()}"
    )
    Cached_result = _best_release_cache_get(Result_key)
    if Cached_result is not None:
        return Cached_result

    try:
        Releases = _browse_group_releases(
            Release_group_mbid, "release_group.best_release_browse"
        )
        Releases.sort(key=lambda item: (not bool(item.get("date")), item.get("date") or ""))

        original_track_count = next(
            (_as_int(r.get("track_count"), 0) for r in Releases if _as_int(r.get("track_count"), 0) > 0),
            None
        )

        if not Releases:
            Empty = {
                "success": True,
                "releases": [],
                "best_release": None,
                "confidence": 0,
                "local_track_count": None,
                "original_track_count": None,
            }
            _best_release_cache_set(Result_key, Empty)
            return Empty

        Expected_count = _get_local_track_count(Artist, Album)
        Expected_count = Expected_count if Expected_count > 0 else None

        def score(item: dict[str, Any]) -> float:
            Value = 0.0
            if Expected_count is not None:
                Value -= abs(Expected_count - _as_int(item.get("track_count"), 0)) * 100.0
            if str(item.get("status") or "").casefold() == "official":
                Value += 50.0
            Date = str(item.get("date") or "")
            if Date[:4].isdigit():
                Value += max(0.0, 2100.0 - int(Date[:4])) * 0.01
            if Album and item.get("title"):
                Value += _similarity(Album.casefold(), str(item["title"]).casefold()) * 30.0
            return Value

        Best = max(Releases, key=score)
        if Expected_count is None:
            Confidence = 0.5
        else:
            Difference = abs(Expected_count - _as_int(Best.get("track_count"), 0))
            Confidence = 1.0 if Difference == 0 else max(0.0, 1.0 - Difference * 0.2)

        Result = {
            "success": True,
            "releases": Releases,
            "best_release": Best,
            "confidence": round(Confidence, 2),
            "local_track_count": Expected_count,
            "original_track_count": original_track_count,
        }
        _best_release_cache_set(Result_key, Result)
        return Result
    except Exception as exc:
        return {"success": False, "error": str(exc)}

# ---------------------------------------------------------------------------
# Track matching engine 
# ---------------------------------------------------------------------------

def _match_mb_tracks_to_library(
    mb_tracks: list[dict[str, Any]],
    library_tracks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    Remaining = list(library_tracks)
    Comparison: list[dict[str, Any]] = []

    def _disc_of(row: dict[str, Any]) -> str:
        return str(row.get("disc_number") or "1").strip() or "1"

    def _track_num_of(row: dict[str, Any]) -> str:
        return str(row.get("track_number") or "").split("/")[0].strip()

    for mb_track in mb_tracks:
        Mb_disc = str(mb_track.get("mb_disc_number") or 1)
        Mb_number = str(mb_track.get("mb_track_number") or "")
        Mb_title = str(mb_track.get("mb_title") or "")

        Match = next(
            (
                row for row in Remaining
                if _disc_of(row) == Mb_disc and _track_num_of(row) == Mb_number
            ),
            None,
        )

        if Match is None:
            Same_disc = [row for row in Remaining if _disc_of(row) == Mb_disc] or Remaining
            Best_row: dict[str, Any] | None = None
            Best_score = 0.0
            for row in Same_disc:
                Score = _similarity(
                    Normalize_title_for_lookup(str(row.get("title") or "")),
                    Normalize_title_for_lookup(Mb_title),
                )
                if Score > Best_score:
                    Best_row, Best_score = row, Score
            if Best_row is not None and Best_score >= 0.55:
                Match = Best_row

        if Match is not None:
            Remaining.remove(Match)

        Entry: dict[str, Any] = {
            "mb_track_number": _as_int(mb_track.get("mb_track_number"), 0),
            "mb_disc_number": _as_int(mb_track.get("mb_disc_number"), 1),
            "mb_title": Mb_title,
            "mb_recording_mbid": str(mb_track.get("mb_recording_mbid") or ""),
            "mb_duration": mb_track.get("mb_duration"),
            "matched": Match is not None,
        }

        if Match is not None:
            try:
                Ignored = set(json.loads(Match.get("mb_ignored_fields") or "[]") or [])
            except (TypeError, ValueError):
                Ignored = set()

            Library_title = str(Match.get("title") or "")
            Library_track_number = Match.get("track_number")
            Library_disc_number = Match.get("disc_number")
            Library_mbid = str(Match.get("mbid") or "")
            Library_duration = Match.get("duration")

            Diff_fields: list[str] = []
            if Library_title.strip().casefold() != Mb_title.strip().casefold():
                Diff_fields.append("title")
            if str(Library_track_number or "").split("/")[0].strip() != str(Entry["mb_track_number"]):
                Diff_fields.append("track_number")
            if Entry["mb_recording_mbid"] and Library_mbid != Entry["mb_recording_mbid"]:
                Diff_fields.append("mbid")
            if str(Library_disc_number or "1").strip() != str(Entry["mb_disc_number"]):
                Diff_fields.append("disc_number")
            if Entry["mb_duration"] is not None:
                try:
                    Lib_ms = int(float(Library_duration or 0))
                    Mb_ms = int(Entry["mb_duration"])
                    if abs(Lib_ms - Mb_ms) > 2000:
                        Diff_fields.append("duration")
                except (TypeError, ValueError):
                    pass

            Diff_fields = [field for field in Diff_fields if field not in Ignored]

            Entry.update({
                "library_track_id": str(Match.get("id") or ""),
                "library_title": Library_title,
                "library_track_number": Library_track_number,
                "library_disc_number": Library_disc_number,
                "library_mbid": Library_mbid,
                "library_duration": Library_duration,
                "diff_fields": Diff_fields,
                "needs_update": bool(Diff_fields),
            })
        else:
            Entry.update({
                "library_track_id": "",
                "library_title": "",
                "library_track_number": None,
                "library_disc_number": None,
                "library_mbid": "",
                "library_duration": None,
                "diff_fields": [],
                "needs_update": False,
            })

        Comparison.append(Entry)

    Extra_tracks = [
        {
            "library_track_id": str(row.get("id") or ""),
            "library_title": str(row.get("title") or ""),
            "library_track_number": row.get("track_number"),
            "library_disc_number": row.get("disc_number"),
        }
        for row in Remaining
    ]

    return Comparison, Extra_tracks

def _title_present_in(
    title: str,
    candidates: list[str],
    floor: float = _TRACKLIST_TITLE_FLOOR,
) -> bool:
    normalised = Normalize_title_for_lookup(str(title or ""))
    if not normalised:
        return False
    for candidate in candidates:
        if not candidate:
            continue
        if _similarity(normalised, Normalize_title_for_lookup(candidate)) >= floor:
            return True
    return False

def release_group_tracklist_similarity(
    release_group_mbid: str,
    library_tracks: list[dict[str, Any]],
    artist: str = "",
    album: str = "",
    prefer_release_id: str | None = None,
) -> float:
    if not release_group_mbid or not library_tracks:
        return 0.0

    release_id = str(prefer_release_id or "").strip()
    if not release_id and artist and album:
        try:
            best = get_musicbrainz_best_release(artist, album, release_group_mbid)
        except Exception:
            best = {}
        if isinstance(best, dict):
            release_id = str(
                ((best.get("best_release") or {}).get("id")) or ""
            ).strip()

    if not release_id:
        try:
            releases = _browse_group_releases(
                release_group_mbid, "tracklist.verify_browse"
            )
        except Exception:
            return 0.0
        if not releases:
            return 0.0
        release_id = str(releases[0].get("id") or "").strip()
    if not release_id:
        return 0.0

    try:
        metadata = fetch_musicbrainz_release_metadata(release_id)
    except Exception:
        return 0.0
    if not metadata:
        return 0.0

    mb_titles = [
        str(track.get("mb_title") or "")
        for track in (metadata.get("tracks") or [])
        if isinstance(track, dict)
    ]
    if not mb_titles:
        return 0.0

    present = sum(
        1
        for track in library_tracks
        if isinstance(track, dict)
        and _title_present_in(str(track.get("title") or ""), mb_titles)
    )
    return present / len(library_tracks)

def release_group_tracklist_passes(
    release_group_mbid: str,
    library_tracks: list[dict[str, Any]],
    floor: float | None = None,
    artist: str = "",
    album: str = "",
    prefer_release_id: str | None = None,
) -> tuple[bool, float]:
    threshold = _TRACKLIST_MATCH_FLOOR if floor is None else float(floor)
    similarity = release_group_tracklist_similarity(
        release_group_mbid,
        library_tracks,
        artist=artist,
        album=album,
        prefer_release_id=prefer_release_id,
    )
    return similarity >= threshold, similarity

def compare_musicbrainz_release(
    Artist: str,
    Album: str,
    Release_group_mbid: str,
) -> dict[str, Any]:
    Started = time.monotonic()
    Context = {
        "artist": Artist,
        "album": Album,
        "release_group_mbid": Release_group_mbid,
    }
    try:
        Client = get_shared_mb_client()
        try:
            Direct = _call_with_heartbeat(
                "compare.direct_release_get",
                Client.get_release,
                Release_group_mbid,
                inc="",
                Log_context=Context,
            )
        except Exception:
            Direct = None

        best_result: dict[str, Any] = {}
        if Direct and Direct.get("id"):
            Release_id = Release_group_mbid
        else:
            best_result = get_musicbrainz_best_release(Artist, Album, Release_group_mbid)
            Release_id = str(
                ((best_result or {}).get("best_release") or {}).get("id") or ""
            )
            if not Release_id:
                Release_id = resolve_release_id(Release_group_mbid)

        original_track_count = (best_result or {}).get("original_track_count")

        Mb_release = fetch_musicbrainz_release_metadata(Release_id)
        if not Mb_release:
            return {
                "success": False,
                "error": "Could not fetch MusicBrainz release data",
            }

        Library_tracks = _fetch_library_tracks(Artist, Album)
        if not Library_tracks:
            return {
                "success": False,
                "error": "No library tracks found for this album",
            }

        Comparison, Extra_tracks = _match_mb_tracks_to_library(
            Mb_release.get("tracks") or [], Library_tracks
        )
        Tracks_needing_update = sum(1 for entry in Comparison if entry.get("needs_update"))

        Mb_year = str(Mb_release.get("original_release_year") or Mb_release.get("release_year") or "")

        Result = {
            "success": True,
            "mb_title": str(Mb_release.get("release_title") or ""),
            "mb_specific_release_title": str(Mb_release.get("specific_release_title") or ""),
            "mb_release_group_title": str(Mb_release.get("release_group_title") or ""),
            "mb_year": Mb_year,
            "mb_original_release_year": str(Mb_release.get("original_release_year") or ""),
            "mb_version_release_year": str(Mb_release.get("version_release_year") or ""),
            "mb_artist": str(Mb_release.get("artist") or ""),
            "mb_release_mbid": str(Mb_release.get("release_mbid") or Release_id),
            "mb_release_group_mbid": Release_group_mbid,
            "release_group_mbid": Release_group_mbid,
            "release_mbid": Release_id,
            "mb_album_artist_mbid": str(Mb_release.get("album_artist_mbid") or ""),
            "mb_albumtype": str(Mb_release.get("album_type") or ""),
            "mb_disc_count": int(Mb_release.get("disc_count") or 0),
            "mb_artist_credit": str(Mb_release.get("artist_credit") or ""),
            "comparison": Comparison,
            "extra_tracks": Extra_tracks,
            "tracks_needing_update": Tracks_needing_update,
            "total_tracks": len(Comparison),
            "mb_original_track_count": original_track_count,
        }
        return Result
    except Exception as exc:
        return {"success": False, "error": "Could not fetch MusicBrainz release data"}

# ---------------------------------------------------------------------------
# Track write helper 
# ---------------------------------------------------------------------------

def _update_track_fields(track_id: str, fields: dict[str, Any]) -> None:
    if not track_id or not fields:
        return
    
    allowed_columns = _ALIGN_WRITABLE_FIELDS + _LINK_WRITABLE_FIELDS
    safe_fields = {k: v for k, v in fields.items() if k in allowed_columns}
    
    if not safe_fields:
        return

    from db.engine import db_session
    from sqlalchemy import text
    Set_clause = ", ".join(f"{column} = :{column}" for column in safe_fields)
    Params = dict(safe_fields)
    Params["id"] = track_id
    with db_session() as session:
        session.execute(text(f"UPDATE tracks SET {Set_clause} WHERE id = :id"), Params)
        session.commit()

def link_album_mbids(Artist: str, Album: str, Release_mbid: str) -> dict[str, Any]:
    if not Release_mbid:
        return {
            "success": False,
            "error": 'No MusicBrainz release linked yet. Use "Lookup on MusicBrainz" first, then save.',
        }

    Result = compare_musicbrainz_release(Artist, Album, Release_mbid)
    if not Result.get("success"):
        return Result

    Linked = 0
    Already_linked = 0
    for Entry in Result.get("comparison") or []:
        if not Entry.get("matched"):
            continue
        New_mbid = Entry.get("mb_recording_mbid")
        if not New_mbid:
            continue
        if Entry.get("library_mbid") == New_mbid:
            Already_linked += 1
            continue
        _update_track_fields(Entry["library_track_id"], {"mbid": New_mbid})
        Linked += 1

    Unmatched = sum(1 for entry in Result.get("comparison") or [] if not entry.get("matched"))

    return {
        "success": True,
        "linked": Linked,
        "already_linked": Already_linked,
        "unmatched": Unmatched,
        "extra_tracks": Result.get("extra_tracks") or [],
        "total_tracks": Result.get("total_tracks") or 0,
        "release_mbid": Result.get("release_mbid"),
    }

def align_album_tracklist(Artist: str, Album: str, Release_mbid: str) -> dict[str, Any]:
    if not Release_mbid:
        return {
            "success": False,
            "error": 'No MusicBrainz release linked yet. Use "Lookup on MusicBrainz" first, then save.',
        }

    Result = compare_musicbrainz_release(Artist, Album, Release_mbid)
    if not Result.get("success"):
        return Result

    Changes: list[dict[str, Any]] = []
    Aligned = 0
    for Entry in Result.get("comparison") or []:
        if not Entry.get("matched") or not Entry.get("needs_update"):
            continue

        Diff_fields = Entry.get("diff_fields") or []
        Fields: dict[str, Any] = {}
        if "title" in Diff_fields:
            Fields["title"] = Entry["mb_title"]
        if "track_number" in Diff_fields:
            Fields["track_number"] = str(Entry["mb_track_number"])

        if not Fields:
            continue

        Fields["disc_number"] = str(Entry.get("mb_disc_number") or 1)

        _update_track_fields(Entry["library_track_id"], Fields)
        Changes.append({
            "library_track_id": Entry["library_track_id"],
            "old_title": Entry.get("library_title"),
            "new_title": Fields.get("title", Entry.get("library_title")),
            "old_track_number": Entry.get("library_track_number"),
            "new_track_number": Fields.get("track_number", Entry.get("library_track_number")),
        })
        Aligned += 1

    Unmatched = sum(1 for entry in Result.get("comparison") or [] if not entry.get("matched"))

    return {
        "success": True,
        "aligned": Aligned,
        "unmatched": Unmatched,
        "changes": Changes,
        "extra_tracks": Result.get("extra_tracks") or [],
        "total_tracks": Result.get("total_tracks") or 0,
        "release_mbid": Result.get("release_mbid"),
    }
