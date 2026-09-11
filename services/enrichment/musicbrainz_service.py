"""MusicBrainz enrichment and lookup service.

Owns MusicBrainz interpretation and matching rules. This module performs no
Track mutation. Database access is limited to read-only library comparison
Helpers.

Album identity rules:
-	The album name supplied by the caller (the library album) is authoritative.
  A recording’s specific release title never replaces it.
-	The album year comes from the matched release group’s ``first-release-date``
  (the original release year), not from the edition/version held in the
  Collection and not from each recording’s first linked release.

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

Web-service correctness notes:
-	``recording-level-rels`` is a RELEASE-level subquery (“include relationships
  For the recordings on this release”). It is not a valid ``inc`` value on the
  Recording resource and makes MusicBrainz answer 400, so recording
  Relationship lookups must not request it.
-	An artist’s ``begin-area`` is frequently a city, so it cannot be used as a
  Country without checking the area type.

Public exports required by other modules:
    Get_shared_mb_client, get_shared_mb_service, lookup_recording_metadata,
    Merge_metadata, fetch_musicbrainz_release_metadata, fetch_release_metadata,
    Resolve_release_id, lookup_musicbrainz_album, get_release_group_releases,
    Get_musicbrainz_best_release, compare_musicbrainz_release
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
    Escape_lucene_special_chars,
)
from helpers.normalization_service import (
    Edition_annotations_compatible,
    Normalize_string,
    Normalize_title_for_lookup,
    Normalize_title_for_lucene_query,
    Normalize_title_for_mbid_match,
    Strip_featured_artist,
    Strip_search_keywords,
    Strip_single_release_suffix,
)

Logger = structlog.get_logger(__name__)

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

_COMPARE_LIBRARY_TRACKS_SQL = """
    SELECT id, title, track_number, disc_number, artist, year,
           Mbid, file_path, duration, mb_ignored_fields
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
    text = str(value or "").strip()
    return int(text[:4]) if len(text) >= 4 and text[:4].isdigit() else None

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
                Target=_monitor_loop,
                Name="mb-heartbeat-monitor",
                Daemon=True,
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

    def get_suggested_mbid(self, Title: str, Artist: str, Limit: int = 5) -> tuple[str, float]:
        return "", 0.0

    def lookup_recording_metadata(self, title: str, artist: str) -> dict[str, Any]:
        return {}

    def lookup_recordings_by_mbid_bulk(self, Mbids: list[str], *, Album_name: str | None = None, Original_release_year: int | None = None) -> dict[str, dict[str, Any]]:
        return {}

    def _recording_to_metadata(self, Recording: dict[str, Any], Mbid: str, Confidence: float, *, Album_name: str | None = None, Original_release_year: int | None = None) -> dict[str, Any]:
        return {}

    def lookup_original_album_year(self, artist: str, album: str) -> int | None:
        return None

    def lookup_album_metadata(self, Entries: list[tuple[str, str]], Candidates_per_entry: int = 5, Album: str = "", Original_release_year: int | None = None) -> dict[str, dict[str, Any]]:
        return {}

    def is_single(self, Title: str, Artist: str, Album_track_count: int | None = None) -> bool:
        return False

    def get_artist_country(self, artist: str) -> str:
        return ""

    def get_genres(self, title: str, artist: str) -> list[str]:
        return []

    def search_releasegroup_matches(self, Artist_name: str, Album_name: str, Limit: int = 10) -> list[dict[str, Any]]:
        return []

    @staticmethod
    def merge_metadata(Base: dict[str, Any], Mb: dict[str, Any], Overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        Overrides = Overrides or {}
        def pick(*values: Any) -> Any:
            return next((value for value in values if value not in (None, "")), None)
        return {
            "title": pick(Overrides.get("title"), Mb.get("title"), Base.get("title")),
            "artist": pick(Overrides.get("artist"), Mb.get("artist"), Base.get("artist")),
            "album": pick(Overrides.get("album"), Base.get("album"), Mb.get("album")),
            "album_artist": pick(Overrides.get("album_artist"), Base.get("album_artist"), Mb.get("album_artist")),
            "year": pick(Overrides.get("year"), Mb.get("original_release_year"), Mb.get("year"), Base.get("year")),
        }

    def get_artist_relationships(self, Artist_mbid: str, Relation_type: str = "artist") -> list[dict[str, Any]]:
        return []

    def get_recording_relationships(self, recording_mbid: str) -> list[dict[str, Any]]:
        return []

    def get_composers_for_recording(self, recording_mbid: str) -> list[str]:
        return []

    def get_recording_genres(self, title: str, artist: str) -> list[str]:
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

def merge_metadata(Base: dict[str, Any], Mb: dict[str, Any], Overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    return _get_service().merge_metadata(Base, Mb, Overrides)

def fetch_musicbrainz_release_metadata(release_id: str) -> dict[str, Any] | None:
    return None

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

def _get_local_track_stats(artist: str, album: str) -> tuple[int, int]:
    try:
        from db.engine import db_session
        from sqlalchemy import text
        with db_session() as session:
            Rows = session.execute(
                text(
                    "SELECT track_number FROM tracks "
                    "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) "
                    "AND LOWER(COALESCE(album, '')) = LOWER(:album)"
                ),
                {"artist": artist, "album": album},
            ).fetchall()

            Count = len(Rows)
            Max_track = 0
            for row in Rows:
                tn_raw = str(row[0] or "").split('/')[0].strip()
                if tn_raw.isdigit():
                    Max_track = max(Max_track, int(tn_raw))

            return Count, Max_track
    except Exception as exc:
        return 0, 0

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

        Local_count, Max_track = _get_local_track_stats(Artist, Album)
        Expected_count = max(Local_count, Max_track) if Local_count > 0 else None

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

        best_result = get_musicbrainz_best_release(Artist, Album, Release_group_mbid)
        if Direct and Direct.get("id"):
            Release_id = Release_group_mbid
        else:
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
            "comparison": [],
            "extra_tracks": [],
            "tracks_needing_update": 0,
            "total_tracks": 0,
            "mb_original_track_count": original_track_count,
        }
        return Result
    except Exception as exc:
        return {"success": False, "error": "Could not fetch MusicBrainz release data"}
