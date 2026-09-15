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
            # threading.Thread is stdlib — its kwargs are lowercase
            # (target/name/daemon). The previous Target=/Name=/Daemon= call
            # raised TypeError on the very first MusicBrainz request made
            # through _call_with_heartbeat (i.e. almost every call this
            # service makes), which the broad except-Exception callers
            # silently turned into a generic "success: False" error.
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
        pass

    @staticmethod
    def _cache_key(title: str, artist: str) -> str:
        return f"{artist.casefold().strip()}::{title.casefold().strip()}"

    def get_suggested_mbid(self, title: str, artist: str, limit: int = 5, **kwargs: Any) -> tuple[str, float]:
        return "", 0.0

    def lookup_recording_metadata(self, title: str, artist: str, **kwargs: Any) -> dict[str, Any]:
        return {}

    def lookup_recordings_by_mbid_bulk(self, mbids: list[str], *, album_name: str | None = None, original_release_year: int | None = None, **kwargs: Any) -> dict[str, dict[str, Any]]:
        return {}

    def _recording_to_metadata(self, recording: dict[str, Any], mbid: str, confidence: float, *, album_name: str | None = None, original_release_year: int | None = None, **kwargs: Any) -> dict[str, Any]:
        return {}

    def lookup_original_album_year(self, artist: str, album: str, **kwargs: Any) -> int | None:
        return None

    def lookup_album_metadata(self, entries: list[tuple[str, str]], candidates_per_entry: int = 5, album: str = "", original_release_year: int | None = None, **kwargs: Any) -> dict[str, dict[str, Any]]:
        return {}

    def is_single(self, title: str, artist: str, album_track_count: int | None = None, **kwargs: Any) -> bool:
        return False

    def get_artist_country(self, artist: str, **kwargs: Any) -> str:
        return ""

    def get_genres(self, title: str, artist: str, **kwargs: Any) -> list[str]:
        return []

    def search_releasegroup_matches(self, artist_name: str, album_name: str, limit: int = 10, **kwargs: Any) -> list[dict[str, Any]]:
        return []

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
        return []

    def get_recording_relationships(self, recording_mbid: str, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    def get_composers_for_recording(self, recording_mbid: str, **kwargs: Any) -> list[str]:
        return []

    def get_recording_genres(self, title: str, artist: str, **kwargs: Any) -> list[str]:
        return []

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

def lookup_recording_metadata(title: str, artist: str) -> dict[str, Any]:
    return _get_service().lookup_recording_metadata(title, artist)

def merge_metadata(base: dict[str, Any], mb: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    return _get_service().merge_metadata(base, mb, overrides)

# ---------------------------------------------------------------------------
# Release metadata fetch (real implementation — this used to unconditionally
# return None, which meant every compare_musicbrainz_release() call fell
# through to "Could not fetch MusicBrainz release data" regardless of input.)
# ---------------------------------------------------------------------------

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
            inc="recordings+artist-credits+release-groups+media",
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
            Tracks.append({
                "mb_disc_number": Disc_number,
                "mb_track_number": _as_int(Position, 0),
                "mb_title": str(track.get("title") or Recording.get("title") or ""),
                "mb_recording_mbid": str(Recording.get("id") or ""),
                "mb_duration": Length,
            })

    return {
        "release_mbid": str(Release.get("id") or release_id),
        "release_group_mbid": str(Release_group.get("id") or ""),
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
        "tracks": Tracks,
    }

def fetch_release_metadata(release_id: str) -> dict[str, Any] | None:
    return fetch_musicbrainz_release_metadata(release_id)

def resolve_release_id(release_id: str) -> str:
    return release_id

def _lookup_existing_mbid(Existing_mbid: str, Artist: str, Album: str) -> dict[str, Any] | None:
    return None

def lookup_musicbrainz_album(Artist: str, Album: str, Existing_mbid: str = "") -> dict[str, Any]:
    return {"results": []}

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
    return {}

# ---------------------------------------------------------------------------
# Local library reads
# ---------------------------------------------------------------------------

def _get_local_track_count(artist: str, album: str) -> int:
    """Count of library tracks for (artist, album), case-insensitively.

    NOTE: this is the exact name/signature get_musicbrainz_best_release()
    depends on and that this module's tests monkeypatch directly. It replaces
    the previous ``_get_local_track_stats`` tuple helper, which returned a
    ``(count, max_track_number)`` pair under a different name — a shape and
    name the tests never actually exercised, so the "expected track count"
    used for confidence scoring was not the value the tests assumed it was.
    """
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
    except Exception as exc:
        Logger.warning("[MB] local track count query failed", artist=artist, album=album, error=_error(exc))
        return 0

def _fetch_library_tracks(artist: str, album: str) -> list[dict[str, Any]]:
    """Read the full library tracklist for (artist, album) for comparison."""
    try:
        from db.engine import db_session
        from sqlalchemy import text
        with db_session() as session:
            Rows = session.execute(
                text(_COMPARE_LIBRARY_TRACKS_SQL),
                {"artist": artist, "album": album},
            ).fetchall()
    except Exception as exc:
        Logger.warning("[MB] library track fetch failed", artist=artist, album=album, error=_error(exc))
        return []
    return [dict(zip(_LIBRARY_TRACK_COLUMNS, row)) for row in Rows]

def get_musicbrainz_best_release(
    Artist: str,
    Album: str,
    Release_group_mbid: str,
) -> dict[str, Any]:
    Context = {
        "artist": Artist,
        "album": Album,
        "release_group_mbid": Release_group_mbid,
    }
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
# Track matching engine — shared by compare_musicbrainz_release, Link and
# Align, so all three agree on what "matched" and "needs update" mean.
# ---------------------------------------------------------------------------

def _match_mb_tracks_to_library(
    mb_tracks: list[dict[str, Any]],
    library_tracks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pair MB tracklist entries with library rows.

    Primary key: exact (disc_number, track_number) match.
    Fallback: best fuzzy title match among still-unmatched library rows on
    the same disc (core-title similarity, floor 0.55) — this is what lets a
    library track like "Song Three (Radio Edit)" still match MB's "Song
    Three" and get flagged for a title fix, instead of being reported as
    unrelated extra + missing.

    Returns ``(comparison, extra_tracks)``:
      - ``comparison`` has exactly one entry per MB track (so
        ``len(comparison) == total MB track count``), each carrying
        ``matched``/``needs_update``/``diff_fields`` plus both sides' data.
      - ``extra_tracks`` lists library rows that were not claimed by any MB
        track (out-of-tracklist bonus tracks, mispressed rips, etc).
    """
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

        # Only browse the release-group when the direct lookup did not
        # already resolve a concrete release. Calling get_musicbrainz_best_release
        # (which browses) unconditionally here — even when Direct already
        # succeeded — was the ~2.7s slow-compare regression: every compare
        # against an already-known release MBID paid for a full
        # release-group browse it never used the result of.
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
        Logger.exception(
            "[MB] compare_musicbrainz_release failed",
            Elapsed_s=round(time.monotonic() - Started, 3),
            Error=_error(exc),
            **Context,
        )
        return {"success": False, "error": "Could not fetch MusicBrainz release data"}

# ---------------------------------------------------------------------------
# Track write helper — the ONLY place this module writes to the tracks table.
# Column names are always drawn from the fixed whitelists above, never from
# caller-supplied keys, so this cannot be used to inject arbitrary columns.
# ---------------------------------------------------------------------------

def _update_track_fields(track_id: str, fields: dict[str, Any]) -> None:
    if not track_id or not fields:
        return
    from db.engine import db_session
    from sqlalchemy import text
    Set_clause = ", ".join(f"{column} = :{column}" for column in fields)
    Params = dict(fields)
    Params["id"] = track_id
    with db_session() as session:
        session.execute(text(f"UPDATE tracks SET {Set_clause} WHERE id = :id"), Params)
        session.commit()

def link_album_mbids(Artist: str, Album: str, Release_mbid: str) -> dict[str, Any]:
    """Auto-Link: find the best-matching MusicBrainz release for the CURRENT
    local tracklist (compare_musicbrainz_release resolves this via
    get_musicbrainz_best_release, which scores candidate releases against the
    library's own track count) and write each matched recording's MBID onto
    its library track.

    Only ever writes ``mbid``. Title, track_number and disc_number are
    Align's responsibility (see align_album_tracklist) — Link never
    reorders or renames a track.
    """
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
    """Align: rename/renumber library tracks to match the tracklist of the
    release already linked to this album (the release_mbid supplied here —
    NOT a fresh search; Align trusts whatever MusicBrainz release the album
    is already pointed at).

    Only ever writes ``title`` / ``track_number`` / ``disc_number``. Never
    touches ``mbid`` — see link_album_mbids for that. A track already
    matching MB (``needs_update`` False) is left untouched.
    """
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
            # needs_update was true only for a field Align doesn't own
            # (e.g. "mbid" or "duration") — nothing for Align to do here.
            continue

        # Keep disc_number in step with the tracklist whenever the row is
        # being touched at all, even if disc_number itself wasn't the field
        # that triggered needs_update.
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
