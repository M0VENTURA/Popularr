"""MusicBrainz enrichment and lookup service.

Owns MusicBrainz interpretation and matching rules. This module performs no
Track mutation except through the explicit Link/Align write helpers below,
which only ever write the fields their name promises (mbid for Link;
title/track_number/disc_number for Align) so the two album-page actions stay
independently safe to run.
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
    format_duration_mmss as Format_duration_mmss,
    is_track_artist_placeholder,
    normalize_string as Normalize_string,
    normalize_title_for_lookup as Normalize_title_for_lookup,
    normalize_title_for_lucene_query as Normalize_title_for_lucene_query,
    normalize_title_for_mbid_match as Normalize_title_for_mbid_match,
    strip_featured_artist as Strip_featured_artist,
    strip_search_keywords as Strip_search_keywords,
    strip_single_release_suffix as Strip_single_release_suffix,
    titles_match_for_review as Titles_match_for_review,
    track_duration_seconds as Track_duration_seconds,
)
from services.catalog.album_classification_service import (
    detect_greatest_hits_album,
    is_live_or_alternate_album,
)

Logger = structlog.get_logger(__name__)

T = TypeVar("T")

DURATION_TOLERANCE_SECONDS = 5


def length_tolerance_seconds() -> float:
    try:
        from helpers.config_helpers import get_length_tolerance_seconds

        return float(get_length_tolerance_seconds())
    except Exception:
        return float(DURATION_TOLERANCE_SECONDS)


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
_MBID_NEGATIVE_TTL_SECONDS = 7 * 24 * 3600
_MBID_CACHE_MAX_SIZE = 5000
_RELEASE_GROUP_MATCH_FLOOR = 0.6

_TRACK_COUNT_REFINE_LIMIT = 3
_RECORDING_RELATIONSHIP_INC = "artist-rels+work-rels+work-level-rels"

_TRACKLIST_TITLE_FLOOR = 0.55
_TRACKLIST_MATCH_FLOOR = 0.6

_ALIGN_WRITABLE_FIELDS = ("title", "track_number", "disc_number")
_LINK_WRITABLE_FIELDS = ("mbid",)

_COMPARE_LIBRARY_TRACKS_SQL = """
    SELECT id, title, track_number, disc_number, artist, year,
           mbid, file_path, duration, mb_ignored_fields, is_cover
    FROM tracks
    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
      AND LOWER(COALESCE(album, '')) = LOWER(:album)
    ORDER BY COALESCE(disc_number, '1'), COALESCE(track_number, '999')
"""

_LIBRARY_TRACK_COLUMNS = (
    "id", "title", "track_number", "disc_number", "artist", "year",
    "mbid", "file_path", "duration", "mb_ignored_fields", "is_cover",
)


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
    started = time.monotonic()
    Logger.debug("[MB] section started", section=section, **context)
    try:
        yield
    except Exception as exc:
        Logger.exception(
            "[MB] section failed",
            section=section,
            elapsed_s=round(time.monotonic() - started, 3),
            error=_error(exc),
            **context,
        )
        raise
    else:
        Logger.debug(
            "[MB] section completed",
            section=section,
            elapsed_s=round(time.monotonic() - started, 3),
            **context,
        )


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
            Logger.warning(
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

    Logger.debug("[MB] call started", section=section, **context)
    try:
        result = func(*args, **kwargs)
    except Exception as exc:
        Logger.exception(
            "[MB] call failed",
            section=section,
            elapsed_s=round(time.monotonic() - started, 3),
            error=_error(exc),
            **context,
        )
        raise
    else:
        Logger.debug(
            "[MB] call completed",
            section=section,
            elapsed_s=round(time.monotonic() - started, 3),
            **context,
        )
        return result
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(call_id, None)


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

def credit_artist_mbid(artist_credit: Any) -> str | None:
    for credit in artist_credit or []:
        if isinstance(credit, dict):
            artist = credit.get("artist")
            if isinstance(artist, dict) and artist.get("id"):
                return str(artist["id"])
    return None

def remix_of_recording_id(recording: Any) -> str:
    for relation in (recording or {}).get("relations") or []:
        if not isinstance(relation, dict):
            continue
        if str(relation.get("type") or "").casefold() != "remix of":
            continue
        target = relation.get("recording") or {}
        if isinstance(target, dict) and target.get("id"):
            return str(target["id"])
    return ""


def remixers_of_recording(recording: Any) -> list[str]:
    names: list[str] = []
    for relation in (recording or {}).get("relations") or []:
        if not isinstance(relation, dict):
            continue
        if str(relation.get("type") or "").casefold() != "remixer":
            continue
        artist = relation.get("artist") or {}
        if isinstance(artist, dict) and artist.get("name"):
            names.append(str(artist["name"]))
    return list(dict.fromkeys(names))


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
    title_score = _similarity(Normalize_string(local_album), Normalize_string(mb_title))
    artist_score = _similarity(
        Normalize_string(local_artist),
        Normalize_string(primary_album_artist(mb_artist_credit)),
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
    name = str(area.get("name") or "").strip()
    if not name:
        return ""
    area_type = str(area.get("type") or "").strip().casefold()
    has_country_iso = bool(area.get("iso-3166-1-codes") or area.get("iso_3166_1_codes"))
    if area_type != "country" and not has_country_iso:
        return ""
    return name

def _artist_country_name(data: Any) -> str:
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
    for candidate in (artist or "", Strip_featured_artist(artist or "")):
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
    album_val = str(album or "").strip().casefold()
    if not album_val or not recording:
        return False
    for release in recording.get("releases") or []:
        if not isinstance(release, dict):
            continue
        release_group = release.get("release-group") or {}
        candidates = (
            release.get("title") or "",
            release_group.get("title") if isinstance(release_group, dict) else "",
        )
        if any(value and _similarity(str(value), album_val) >= 0.6 for value in candidates):
            return True
    return False

def _release_track_count(release: Any) -> int:
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
    checker = getattr(client, "is_available", None)
    if not callable(checker):
        return True
    try:
        return bool(checker())
    except Exception:
        return True


_SHARED_MB_CLIENT: MusicBrainzHttpClient | None = None
_service: "MusicBrainzService | None" = None
_shared_mb_service: "MusicBrainzService | None" = None

def get_shared_mb_client() -> MusicBrainzHttpClient:
    global _SHARED_MB_CLIENT
    if _SHARED_MB_CLIENT is not None:
        return _SHARED_MB_CLIENT
    with _INIT_LOCK:
        if _SHARED_MB_CLIENT is None:
            try:
                _SHARED_MB_CLIENT = MusicBrainzHttpClient(enabled=True)
            except Exception:
                raise
    return _SHARED_MB_CLIENT


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
        on_miss = kwargs.get("on_miss")
        context = {
            "artist": artist,
            "track": title,
            "limit": limit,
            "is_live_release": is_live_release,
            "edition_annotation": edition_annotation,
        }
        if not self.enabled or not title or not artist:
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
                    return mbid, round(score, 3)
                if (
                    not mbid
                    and cached_at is not None
                    and now - cached_at < _MBID_NEGATIVE_TTL_SECONDS
                ):
                    self._mbid_cache.move_to_end(cache_key)
                    try:
                        if callable(on_miss):
                            on_miss()
                    except Exception:
                        pass
                    return "", 0.0

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
                log_context=context,
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
            else:
                self._record_mbid(cache_key, "", 0.0)
                try:
                    if callable(on_miss):
                        on_miss()
                except Exception:
                    pass
            self._maybe_flush_cache()
            return best_mbid, round(best_score, 3)
        except Exception:
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
            return {}
        on_miss = kwargs.get("on_miss")
        try:
            _known_mbid = str(kwargs.get("mbid") or "").strip()
            if _known_mbid:
                mbid, confidence = _known_mbid, 1.0
            else:
                mbid, confidence = self.get_suggested_mbid(
                    title,
                    artist,
                    is_live_release=is_live_release,
                    edition_annotation=edition_annotation,
                    on_miss=on_miss,
                )
            if not mbid:
                return {}
            recording = _call_with_heartbeat(
                "recording.get",
                self.http.get_recording,
                mbid,
                inc="artist-credits+releases+release-groups+work-rels+work-level-rels+artist-rels+genres",
                log_context={**context, "mbid": mbid},
            )
            if not recording and _known_mbid:
                mbid, confidence = self.get_suggested_mbid(
                    title,
                    artist,
                    is_live_release=is_live_release,
                    edition_annotation=edition_annotation,
                    on_miss=on_miss,
                )
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
                return {}
            return self._recording_to_metadata(recording, mbid, confidence, album_name=album)
        except Exception:
            return {}

    def lookup_recordings_by_mbid_bulk(self, mbids: list[str], *, album_name: str | None = None, original_release_year: int | None = None, **kwargs: Any) -> dict[str, dict[str, Any]]:
        if not self.enabled or not mbids:
            return {}

        context = {
            "mbid_count": len(mbids),
            "authoritative_album_name": album_name,
            "original_release_year": original_release_year,
        }
        try:
            _bulk_kwargs: dict[str, Any] = {}
            _release_mbid = str(kwargs.get("release_mbid") or "").strip()
            if _release_mbid:
                _bulk_kwargs["release_mbid"] = _release_mbid
            try:
                payload = _call_with_heartbeat(
                    "recording.bulk_get",
                    self.http.get_recordings_bulk,
                    mbids,
                    inc="artist-credits+releases+release-groups+work-rels+genres",
                    log_context=context,
                    **_bulk_kwargs,
                ) or {}
            except TypeError:
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
            return results
        except Exception:
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

        writers: list[str] = []
        work_mbid = ""
        cover_work_mbid = ""
        remix_of_recording_mbid = ""
        remix_of_title = ""
        remixers: list[str] = []
        try:
            remix_of_recording_mbid = remix_of_recording_id(recording)
            remixers = remixers_of_recording(recording)
            for relation in recording.get("relations") or []:
                if not isinstance(relation, dict):
                    continue
                _rel_type = str(relation.get("type") or "").casefold()
                if _rel_type == "remix of":
                    _target = relation.get("recording") or {}
                    if isinstance(_target, dict) and _target.get("title"):
                        remix_of_title = str(_target["title"])
                    continue
                if _rel_type not in {"performance", "recording of"}:
                    continue
                work = relation.get("work") or {}
                if not isinstance(work, dict):
                    continue
                if work.get("id"):
                    _attrs = {
                        str(a).strip().casefold()
                        for a in (relation.get("attributes") or [])
                        if str(a).strip()
                    }
                    if "cover" in _attrs:
                        cover_work_mbid = str(work.get("id"))
                        work_mbid = cover_work_mbid
                    elif not work_mbid:
                        work_mbid = str(work.get("id"))
                for work_relation in work.get("relations") or []:
                    if not isinstance(work_relation, dict):
                        continue
                    if str(work_relation.get("type") or "").casefold() not in {"composer", "writer", "lyricist"}:
                        continue
                    target = work_relation.get("artist") or {}
                    if isinstance(target, dict) and target.get("name"):
                        writers.append(str(target["name"]))
        except Exception:
            pass

        genres = [
            str(item.get("name") or "").strip()
            for item in recording.get("genres") or []
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        genres = list(dict.fromkeys(genres))

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
            "is_cover_relation": bool(cover_work_mbid),
            "cover_work_mbid": cover_work_mbid,
            "genres": genres,
            "is_remix": bool(remix_of_recording_mbid),
            "remix_of_recording_mbid": remix_of_recording_mbid,
            "remix_of_title": remix_of_title,
            "remixer": ", ".join(dict.fromkeys(remixers)),
        }

    def lookup_original_album_year(self, artist: str, album: str, **kwargs: Any) -> int | None:
        context = {"artist": artist, "album": album}
        if not self.enabled or not artist or not album:
            return None

        cache_key = f"{artist.casefold().strip()}::{album.casefold().strip()}"
        with self._mem_lock:
            if cache_key in self._album_year_cache:
                return self._album_year_cache[cache_key]

        clean_album = Strip_search_keywords(album)
        query = (
            f'artist:"{Escape_lucene_special_chars(artist)}" '
            f'AND releasegroup:"{Escape_lucene_special_chars(clean_album)}"'
        )
        try:
            groups = _call_with_heartbeat(
                "album.original_year_search",
                self.http.search_release_groups,
                query,
                limit=5,
                log_context={**context, "query": query},
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
                        log_context={**context, "query": fallback_query},
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
        except Exception:
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
                    log_context=chunk_context,
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
                log_context={"artist": artist},
            ) or []
            data = result[0] if result and isinstance(result[0], dict) else {}
            # Fall back to ISO country code if available or ensure area type is explicitly country
            iso_code = str(data.get("country") or "").strip()
            if iso_code:
                return iso_code
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
                log_context={"artist": artist, "title": title, "mbid": mbid},
            )
            return [
                str(item["name"])
                for item in (recording or {}).get("genres") or []
                if isinstance(item, dict) and item.get("name")
            ]
        except Exception:
            return []

    def search_releasegroup_matches(self, artist_name: str, album_name: str, limit: int = 10, **kwargs: Any) -> list[dict[str, Any]]:
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
                log_context={**context, "query": exact_query},
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
                        log_context={**context, "query": fallback_query},
                    ) or []
                except Exception:
                    groups = []

        matches: list[dict[str, Any]] = []

        with _logged_section(
            "release_group.scoring", candidate_count=len(groups), **context
        ):
            for group in groups:
                if not isinstance(group, dict):
                    continue
                try:
                    secondary_types = _parse_secondary_types(group.get("secondary-types"))
                    primary_lower = str(group.get("primary-type") or group.get("primary_type") or "").strip().casefold()
                    group_title = str(group.get("title") or "")
                    
                    score = calculate_match_score(
                        group_title,
                        group.get("artist-credit") or [],
                        album_name,
                        artist_name,
                    )

                    # Penalize subset-inflated titles that add extra song/tour words (e.g. live tour DVDs)
                    if _title_adds_a_second_song(
                        Normalize_title_for_lookup(album_name),
                        Normalize_title_for_lookup(group_title),
                    ):
                        score *= 0.65

                    # Penalize non-audio / untyped release groups (e.g. video DVDs with primary_type="")
                    if not primary_lower:
                        score *= 0.7

                    sec_lower = {str(t).casefold() for t in secondary_types}
                    if "live" in sec_lower and not is_live_or_alternate_album(album_name):
                        score *= 0.7
                    if "compilation" in sec_lower and not (
                        "compilation" in album_name.casefold()
                        or detect_greatest_hits_album(album_name, artist_name)
                    ):
                        score *= 0.8
                    if not secondary_types and primary_lower == "album":
                        score *= 1.15

                    matches.append({
                        "id": group.get("id"),
                        "title": group_title,
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
                releases = _browse_group_releases(group_id, "release_group.track_count_refine")
                counts = [_as_int(r.get("track_count"), 0) for r in releases if _as_int(r.get("track_count"), 0) > 0]
                if counts:
                    best_diff = min(abs(expected_count - c) for c in counts)
                    match["match_score"] -= best_diff * 0.05

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
                log_context={
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
                log_context={"recording_mbid": recording_mbid},
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
                log_context={"artist": artist, "title": title, "query": query},
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
            log_context={"artist": artist},
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
            log_context=context,
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
            log_context={"mbid": mbid, "title": title},
        )
        releases = (recording or {}).get("releases") or []

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
            client = get_shared_mb_client()
            _service = MusicBrainzService(http_client=client, enabled=True)
    return _service


def get_shared_mb_service() -> MusicBrainzService:
    global _shared_mb_service
    if _shared_mb_service is not None:
        return _shared_mb_service
    with _INIT_LOCK:
        if _shared_mb_service is None:
            client = get_shared_mb_client()
            _shared_mb_service = MusicBrainzService(http_client=client, enabled=True)
    return _shared_mb_service


def lookup_recording_metadata(
    title: str,
    artist: str,
    *,
    album: str | None = None,
    is_live_release: bool = False,
    edition_annotation: str | None = None,
    mbid: str | None = None,
    on_miss: Any = None,
) -> dict[str, Any]:
    return _get_service().lookup_recording_metadata(
        title,
        artist,
        album=album,
        is_live_release=is_live_release,
        edition_annotation=edition_annotation,
        mbid=mbid,
        on_miss=on_miss,
    )


def merge_metadata(base: dict[str, Any], mb: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    return _get_service().merge_metadata(base, mb, overrides)


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


_RELEASE_FETCH_INC = (
    "recordings+artist-credits+release-groups+media+labels"
    "+work-rels+recording-level-rels+work-level-rels+artist-rels+genres"
    "+isrcs"
)


def _fetch_raw_release(release_id: str, client: Any = None) -> dict[str, Any] | None:
    client_obj = client or get_shared_mb_client()
    return _call_with_heartbeat(
        "release.fetch_metadata",
        client_obj.get_release,
        release_id,
        inc=_RELEASE_FETCH_INC,
        log_context={"release_id": release_id},
    )


def fetch_musicbrainz_release_metadata(release_id: str) -> dict[str, Any] | None:
    if not release_id:
        return None
    try:
        release = _fetch_raw_release(release_id)
    except Exception:
        return None

    if not isinstance(release, dict) or not release.get("id"):
        return None

    return _flatten_release(release, release_id)


def _join_genre_names(raw: Any) -> str:
    names: list[str] = []
    for item in raw or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            if name and name.casefold() not in {n.casefold() for n in names}:
                names.append(name)
    return ", ".join(names)


def _flatten_release(release: dict[str, Any], release_id: str) -> dict[str, Any]:
    artist_credit = release.get("artist-credit") or []
    release_group = release.get("release-group") or {}
    if not isinstance(release_group, dict):
        release_group = {}

    first_release_date = str(release_group.get("first-release-date") or "")
    original_release_year = _year_of(first_release_date) or _year_of(release.get("date"))
    version_release_year = _year_of(release.get("date"))

    secondary_types = _parse_secondary_types(
        release_group.get("secondary-types") or release_group.get("secondary_types")
    )
    album_type = _compose_album_type(_release_group_primary_type(release_group), secondary_types)

    first_artist = artist_credit[0] if artist_credit and isinstance(artist_credit[0], dict) else {}
    artist_ref = first_artist.get("artist") if isinstance(first_artist, dict) else None
    album_artist_mbid = str(artist_ref.get("id") or "") if isinstance(artist_ref, dict) else ""

    primary_artist = primary_album_artist(artist_credit) or _mb_artist_credit_name(artist_credit)
    joined_artist_credit = build_artist_credit_string(artist_credit)

    tracks: list[dict[str, Any]] = []
    media = release.get("media") or []
    absolute_track_number = 1
    for disc_index, medium in enumerate(media, start=1):
        if not isinstance(medium, dict):
            continue
        disc_number = _as_int(medium.get("position"), disc_index) or disc_index
        for track in medium.get("tracks") or []:
            if not isinstance(track, dict):
                continue
            recording = track.get("recording") or {}
            if not isinstance(recording, dict):
                recording = {}
            position = track.get("position") if track.get("position") is not None else track.get("number")
            length = track.get("length") if track.get("length") is not None else recording.get("length")
            track_title = str(track.get("title") or recording.get("title") or "")

            genres = [
                str(item.get("name") or "").strip()
                for item in recording.get("genres") or []
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            ]
            genres = list(dict.fromkeys(genres))

            _recording_credit = (
                build_artist_credit_string(recording.get("artist-credit") or [])
                if recording.get("artist-credit")
                else ""
            )
            _track_credit = (
                build_artist_credit_string(track.get("artist-credit") or [])
                if track.get("artist-credit")
                else ""
            )
            if is_track_artist_placeholder(_recording_credit):
                _recording_credit = ""
            if is_track_artist_placeholder(_track_credit):
                _track_credit = ""
            track_artist = (
                _recording_credit or _track_credit
                or joined_artist_credit or primary_artist
            )

            entry: dict[str, Any] = {
                "disc_number": disc_number,
                "track_number": _as_int(position, 0) or None,
                "absolute_track_number": absolute_track_number,
                "title": track_title,
                "artist": track_artist,
                "recording_mbid": str(recording.get("id") or ""),
                "duration": length,
                "mb_disc_number": disc_number,
                "mb_track_number": _as_int(position, 0),
                "mb_title": track_title,
                "mb_recording_mbid": str(recording.get("id") or ""),
                "mb_duration": length,
                "mb_genres": list(genres),
                "isrc": _first_isrc(recording),
                "tracktotal": (
                    str(medium.get("track-count"))
                    if _as_int(medium.get("track-count"), 0) > 0
                    else None
                ),
                "artist_mbid": credit_artist_mbid(
                    recording.get("artist-credit")
                ),
                "is_remix": bool(remix_of_recording_id(recording)),
                "remix_of_recording_mbid": remix_of_recording_id(recording),
            }
            if genres:
                entry["musicbrainz_genres"] = ", ".join(genres)

            try:
                writers: list[str] = []
                composers: list[str] = []
                lyricists: list[str] = []
                for relation in recording.get("relations") or []:
                    if not isinstance(relation, dict):
                        continue
                    if str(relation.get("type") or "").casefold() not in {"performance", "recording of"}:
                        continue
                    work = relation.get("work") or {}
                    if not isinstance(work, dict):
                        continue
                    if work.get("id"):
                        entry["work_mbid"] = str(work["id"])
                    if work.get("title"):
                        entry["work_title"] = str(work["title"])
                    iswc = str(work.get("iswc") or "").strip()
                    if iswc:
                        entry["iswc"] = iswc
                    for work_relation in work.get("relations") or []:
                        if not isinstance(work_relation, dict):
                            continue
                        work_type = str(work_relation.get("type") or "").casefold()
                        target = work_relation.get("artist") or {}
                        if not isinstance(target, dict) or not target.get("name"):
                            continue
                        name = str(target["name"])
                        if work_type == "composer":
                            composers.append(name)
                        elif work_type == "lyricist":
                            lyricists.append(name)
                        if work_type in {"composer", "writer", "lyricist"}:
                            writers.append(name)
                    work_credit = work.get("artist-credit") or []
                    if work_credit:
                        work_artist = primary_album_artist(work_credit)
                        entry["work_artist"] = work_artist
                        release_artist_key = _normalise_artist_key(track_artist)
                        work_artist_key = _normalise_artist_key(work_artist)
                        if (
                            release_artist_key
                            and work_artist_key
                            and release_artist_key != work_artist_key
                        ):
                            entry["is_cover"] = True
                            entry["original_cover_artist"] = work_artist
                            if work.get("title"):
                                entry["original_title"] = str(work["title"])
                if composers:
                    entry["composer"] = ", ".join(dict.fromkeys(composers))
                if lyricists:
                    entry["lyricist"] = ", ".join(dict.fromkeys(lyricists))
                if writers:
                    entry["writer"] = ", ".join(dict.fromkeys(writers))
            except Exception:
                pass

            tracks.append(entry)
            absolute_track_number += 1

    return {
        "release_mbid": str(release.get("id") or release_id),
        "release_group_mbid": str(release_group.get("id") or ""),
        "release_group_title": str(release_group.get("title") or ""),
        "release_title": str(release.get("title") or ""),
        "release_group_genres": _join_genre_names(release_group.get("genres")),
        "release_genres": _join_genre_names(release.get("genres")),
        "specific_release_title": str(release.get("title") or ""),
        "compilation": 1 if "compilation" in {t.casefold() for t in secondary_types} else 0,
        "original_date": str(release_group.get("first-release-date") or release.get("date") or ""),
        "disctotal": str(len(media)) if media else None,
        "original_year": str(original_release_year or ""),
        "original_release_year": original_release_year,
        "release_year": version_release_year or original_release_year,
        "version_release_year": version_release_year,
        "artist": primary_artist,
        "artist_credit": joined_artist_credit,
        "album_artist_mbid": album_artist_mbid,
        "album_type": album_type,
        "status": str(release.get("status") or "").strip(),
        "disc_count": len(media),
        **_release_extended_fields(release, media),
        "tracks": tracks,
    }


def fetch_release_metadata(release_id: str) -> dict[str, Any] | None:
    if not release_id:
        return None
    try:
        client = getattr(_get_service(), "http", None) or get_shared_mb_client()
        release = _fetch_raw_release(release_id, client)
    except Exception:
        return None

    if not isinstance(release, dict) or not release.get("id"):
        return None

    return _flatten_release(release, release_id)


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


def _lookup_existing_mbid(existing_mbid: str, artist: str, album: str) -> dict[str, Any] | None:
    if not existing_mbid:
        return None
    client = get_shared_mb_client()
    context = {"existing_mbid": existing_mbid, "artist": artist, "album": album}

    try:
        data = _call_with_heartbeat(
            "album.existing_release_get",
            client.get_release,
            existing_mbid,
            inc="artist-credits+release-groups+media",
            log_context=context,
        )
        if data:
            group = data.get("release-group") or {}
            mb_track_count = sum(
                _as_int(m.get("track-count"), 0)
                for m in (data.get("media") or [])
                if isinstance(m, dict)
            )
            return {
                "mbid": existing_mbid,
                "title": group.get("title") or data.get("title", album),
                "artist": primary_album_artist(data.get("artist-credit") or []) or artist,
                "primary_type": group.get("primary-type", "Album"),
                "secondary_types": _parse_secondary_types(group.get("secondary-types")),
                "first_release_date": group.get("first-release-date") or data.get("date") or "",
                "cover_art_url": _cover_art_url(str(group.get("id") or ""), existing_mbid),
                "confidence": 1.0,
                "source": "musicbrainz",
                "is_stored_mbid": True,
                "mbid_type": "release",
                "track_count": mb_track_count,
            }
    except Exception:
        pass

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
    except Exception:
        pass
    return None


def lookup_musicbrainz_album(artist: str, album: str, existing_mbid: str = "") -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    if existing_mbid:
        stored = _lookup_existing_mbid(existing_mbid, artist, album)
        if stored:
            from services.metadata.album_mbid_guard import (
                guard_album_mbid as guard_album_mbid,
                log_verdict as log_mbid_verdict,
            )

            verdict = guard_album_mbid(
                artist=artist,
                album=album,
                mb_artist=str(stored.get("artist") or ""),
                mb_album=str(stored.get("title") or ""),
            )

            local_track_count = _get_local_track_count(artist, album)
            stored_track_count = stored.get("track_count") or 0

            if verdict["ok"] and local_track_count > 0 and stored_track_count > 0:
                if abs(local_track_count - stored_track_count) >= 4:
                    verdict = {
                        "ok": False,
                        "reason": f"Track count mismatch (Local: {local_track_count}, MBID: {stored_track_count})"
                    }

            if verdict["ok"]:
                results.append(stored)
            else:
                log_mbid_verdict(verdict, artist=artist, album=album, mbid=existing_mbid)

    from helpers.normalization_service import (
        strip_search_keywords as _strip_search_keywords,
    )

    _clean_album = _strip_search_keywords(album) or album
    query = (
        f'releasegroup:"{Escape_lucene_special_chars(_clean_album)}" '
        f'AND artist:"{Escape_lucene_special_chars(artist)}"'
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
        results.append({
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
        })
        seen.add(group_id)

    stored_results = [item for item in results if item.get("is_stored_mbid")]
    other_results = sorted(
        [item for item in results if not item.get("is_stored_mbid")],
        key=lambda item: item.get("confidence") or 0.0,
        reverse=True,
    )
    return {"results": (stored_results + other_results)[:11]}


def _release_summary(release: dict[str, Any]) -> dict[str, Any]:
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
        "formats": sorted({str(medium.get("format") or "").strip() for medium in media if isinstance(medium, dict) and medium.get("format")}),
        "cover_art_url": _cover_art_url(release_id=release_id),
    }


def _browse_group_releases(release_group_mbid: str, section: str) -> list[dict[str, Any]]:
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
    releases = [_release_summary(release) for release in raw if isinstance(release, dict)]
    _best_release_cache_set(cache_key, {"releases": releases})
    return releases


def get_release_group_releases(release_group_mbid: str, include_track_counts: bool = False) -> dict[str, Any]:
    if not release_group_mbid:
        return {"success": False, "error": "release_group_mbid required", "releases": []}

    try:
        client = get_shared_mb_client()
        payload = _call_with_heartbeat(
            "release_group.releases",
            client.get_release_group,
            release_group_mbid,
            inc="releases",
            log_context={"release_group_mbid": release_group_mbid},
        ) or {}
        raw = payload.get("releases") or []
        releases = [_release_summary(release) for release in raw if isinstance(release, dict)]

        if include_track_counts and releases:
            try:
                browsed = client.browse_releases_for_group(
                    release_group_mbid, inc="media", limit=100,
                ) or []
            except TypeError:
                browsed = client.browse_releases_for_group(release_group_mbid) or []
            counts = {
                str(release.get("id")): _release_summary(release)
                for release in browsed
                if isinstance(release, dict) and release.get("id")
            }
            for release in releases:
                enriched = counts.get(str(release.get("id")))
                if not enriched:
                    continue
                release["track_count"] = enriched["track_count"]
                release["disc_count"] = enriched["disc_count"]
                release["formats"] = enriched["formats"]

        return {"success": True, "releases": releases, "release_count": len(releases)}
    except Exception as exc:
        return {"success": False, "error": _error(exc), "releases": []}


def _get_local_track_count(artist: str, album: str) -> int:
    try:
        from db.engine import db_session
        from sqlalchemy import text
        with db_session() as session:
            row = session.execute(
                text(
                    "SELECT COUNT(*) FROM tracks "
                    "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) "
                    "AND LOWER(COALESCE(album, '')) = LOWER(:album)"
                ),
                {"artist": artist, "album": album},
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return 0


def _fetch_library_tracks(artist: str, album: str) -> list[dict[str, Any]]:
    try:
        from db.engine import db_session
        from sqlalchemy import text
        with db_session() as session:
            rows = session.execute(
                text(_COMPARE_LIBRARY_TRACKS_SQL),
                {"artist": artist, "album": album},
            ).fetchall()
    except Exception:
        return []
    return [dict(zip(_LIBRARY_TRACK_COLUMNS, row)) for row in rows]


def get_musicbrainz_best_release(
    artist: str,
    album: str,
    release_group_mbid: str,
) -> dict[str, Any]:
    if not release_group_mbid:
        return {"success": False, "error": "No release-group MBID supplied"}

    result_key = (
        f"best::{release_group_mbid}::{str(artist).casefold().strip()}"
        f"::{str(album).casefold().strip()}"
    )
    cached_result = _best_release_cache_get(result_key)
    if cached_result is not None:
        return cached_result

    try:
        releases = _browse_group_releases(
            release_group_mbid, "release_group.best_release_browse"
        )
        releases.sort(key=lambda item: (not bool(item.get("date")), item.get("date") or ""))

        original_track_count = next(
            (_as_int(r.get("track_count"), 0) for r in releases if _as_int(r.get("track_count"), 0) > 0),
            None
        )

        if not releases:
            empty = {
                "success": True,
                "releases": [],
                "best_release": None,
                "confidence": 0,
                "local_track_count": None,
                "original_track_count": None,
            }
            _best_release_cache_set(result_key, empty)
            return empty

        expected_count = _get_local_track_count(artist, album)
        expected_count = expected_count if expected_count > 0 else None

        def score(item: dict[str, Any]) -> float:
            value = 0.0
            count = _as_int(item.get("track_count"), 0)
            value += float(count) * 1000.0 if count > 0 else -1000.0
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

        result = {
            "success": True,
            "releases": releases,
            "best_release": best,
            "confidence": round(confidence, 2),
            "local_track_count": expected_count,
            "original_track_count": original_track_count,
        }
        _best_release_cache_set(result_key, result)
        return result
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def _titles_agree_for_pairing(library_title: Any, mb_title: Any) -> bool:
    library = Normalize_title_for_lookup(str(library_title or ""))
    musicbrainz = Normalize_title_for_lookup(str(mb_title or ""))
    if not library or not musicbrainz:
        return True
    if library == musicbrainz:
        return True
    if _title_adds_a_second_song(library, musicbrainz):
        return False
    return _similarity(library, musicbrainz) >= _TRACKLIST_TITLE_FLOOR


_TITLE_EXTENSION_TOKENS = frozenset({
    "live", "acoustic", "unplugged", "remix", "remixed", "remaster",
    "remastered", "demo", "instrumental", "orchestral", "karaoke",
    "version", "edit", "mix", "radio", "extended", "deluxe", "bonus",
    "mono", "stereo", "single", "album", "reissue", "anniversary", "edition",
    "and", "the", "of", "a", "de", "la",
})

_CREDIT_LEAD_TOKENS = frozenset({"feat", "featuring", "ft"})


def _title_adds_a_second_song(norm_a: str, norm_b: str) -> bool:
    tokens_a = set(norm_a.split())
    tokens_b = set(norm_b.split())
    if not tokens_a or not tokens_b or tokens_a == tokens_b:
        return False
    if tokens_a < tokens_b:
        extra = tokens_b - tokens_a
    elif tokens_b < tokens_a:
        extra = tokens_a - tokens_b
    else:
        return False
    if extra & _CREDIT_LEAD_TOKENS:
        return False
    return bool(extra - _TITLE_EXTENSION_TOKENS)


def _lengths_agree_for_pairing(library_duration: Any, mb_duration: Any) -> bool | None:
    library = Track_duration_seconds(library_duration)
    musicbrainz = Track_duration_seconds(mb_duration)
    if library is None or musicbrainz is None:
        return None
    return abs(library - musicbrainz) <= length_tolerance_seconds()


def _track_number_pairing_allowed(
    library_title: Any,
    mb_title: Any,
    library_duration: Any,
    mb_duration: Any,
) -> bool:
    if _titles_agree_for_pairing(library_title, mb_title):
        return True
    lengths = _lengths_agree_for_pairing(library_duration, mb_duration)
    if lengths is None:
        return True
    return bool(lengths)


def _match_mb_tracks_to_library(
    mb_tracks: list[dict[str, Any]],
    library_tracks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    remaining = list(library_tracks)
    comparison: list[dict[str, Any]] = []

    def _disc_of(row: dict[str, Any]) -> str:
        return str(row.get("disc_number") or "1").strip() or "1"

    def _track_num_of(row: dict[str, Any]) -> str:
        return str(row.get("track_number") or "").split("/")[0].strip()

    for mb_track in mb_tracks:
        mb_disc = str(mb_track.get("mb_disc_number") or 1)
        mb_number = str(mb_track.get("mb_track_number") or "")
        mb_title = str(mb_track.get("mb_title") or "")
        mb_norm = Normalize_title_for_lookup(mb_title)
        mb_duration = mb_track.get("mb_duration")
        if mb_duration is None:
            mb_duration = mb_track.get("duration")

        named = [
            row for row in remaining
            if mb_norm
            and Normalize_title_for_lookup(str(row.get("title") or "")) == mb_norm
        ]
        match = None
        if named:
            mb_verbatim = Normalize_title_for_mbid_match(mb_title)
            exact = (
                [
                    row for row in named
                    if mb_verbatim
                    and Normalize_title_for_mbid_match(str(row.get("title") or ""))
                    == mb_verbatim
                ]
                or named
            )
            positioned = (
                [
                    row for row in exact
                    if mb_number
                    and _disc_of(row) == mb_disc
                    and _track_num_of(row) == mb_number
                    and _track_number_pairing_allowed(
                        row.get("title"), mb_title, row.get("duration"), mb_duration
                    )
                ]
                or exact
            )
            match = positioned[0]

        if match is None and mb_number:
            for row in remaining:
                if _disc_of(row) != mb_disc or _track_num_of(row) != mb_number:
                    continue
                if not _track_number_pairing_allowed(
                    row.get("title"),
                    mb_title,
                    row.get("duration"),
                    mb_duration,
                ):
                    continue
                match = row
                break

        if match is None:
            same_disc = [row for row in remaining if _disc_of(row) == mb_disc] or remaining
            best_row: dict[str, Any] | None = None
            best_score = 0.0
            for row in same_disc:
                row_norm = Normalize_title_for_lookup(str(row.get("title") or ""))
                if _title_adds_a_second_song(row_norm, mb_norm):
                    continue
                score = _similarity(row_norm, mb_norm)
                if score > best_score:
                    best_row, best_score = row, score
            if best_row is not None and best_score >= 0.55:
                match = best_row

        if match is not None:
            remaining.remove(match)

        entry: dict[str, Any] = {
            "mb_track_number": _as_int(mb_track.get("mb_track_number"), 0),
            "mb_disc_number": _as_int(mb_track.get("mb_disc_number"), 1),
            "mb_title": mb_title,
            "mb_recording_mbid": str(mb_track.get("mb_recording_mbid") or ""),
            "mb_duration": mb_track.get("mb_duration"),
            "mb_artist": str(mb_track.get("artist") or ""),
            "matched": match is not None,
        }

        if match is not None:
            try:
                ignored = set(json.loads(match.get("mb_ignored_fields") or "[]") or [])
            except (TypeError, ValueError):
                ignored = set()

            library_title = str(match.get("title") or "")
            library_track_number = match.get("track_number")
            library_disc_number = match.get("disc_number")
            library_mbid = str(match.get("mbid") or "")
            library_duration = match.get("duration")

            diff_fields: list[str] = []
            # A confirmed cover's "(X Cover)" suffix is OUR annotation. The MB
            # title without it is the same song; a later cover-detection scan
            # owns the verdict, so the Compare must not report a title rename.
            titles_confirmed = Titles_match_for_review(
                library_title,
                mb_title,
                local_is_cover=bool(_as_int(match.get("is_cover"), 0)),
            )
            if not titles_confirmed:
                diff_fields.append("title")
            if (
                str(library_track_number or "").split("/")[0].strip()
                != str(entry["mb_track_number"])
                and titles_confirmed
            ):
                diff_fields.append("track_number")
            if entry["mb_recording_mbid"] and library_mbid != entry["mb_recording_mbid"]:
                diff_fields.append("mbid")
            if str(library_disc_number or "1").strip() != str(entry["mb_disc_number"]):
                diff_fields.append("disc_number")

            library_duration_seconds = Track_duration_seconds(library_duration)
            mb_duration_seconds = Track_duration_seconds(entry["mb_duration"])
            entry["library_duration_seconds"] = library_duration_seconds
            entry["mb_duration_seconds"] = mb_duration_seconds
            entry["library_duration_display"] = Format_duration_mmss(library_duration)
            entry["mb_duration_display"] = Format_duration_mmss(entry["mb_duration"])

            if library_duration_seconds is not None and mb_duration_seconds is not None:
                if abs(library_duration_seconds - mb_duration_seconds) > length_tolerance_seconds():
                    diff_fields.append("duration")

            diff_fields = [field for field in diff_fields if field not in ignored]

            entry.update({
                "library_track_id": str(match.get("id") or ""),
                "library_title": library_title,
                "library_track_number": library_track_number,
                "library_disc_number": library_disc_number,
                "library_mbid": library_mbid,
                "library_duration": library_duration,
                "library_artist": str(match.get("artist") or ""),
                "diff_fields": diff_fields,
                "needs_update": bool(diff_fields),
            })
        else:
            entry.update({
                "library_track_id": "",
                "library_title": "",
                "library_track_number": None,
                "library_disc_number": None,
                "library_mbid": "",
                "library_duration": None,
                "library_artist": "",
                "diff_fields": [],
                "needs_update": False,
            })

        comparison.append(entry)

    extra_tracks = [
        {
            "library_track_id": str(row.get("id") or ""),
            "library_title": str(row.get("title") or ""),
            "library_track_number": row.get("track_number"),
            "library_disc_number": row.get("disc_number"),
        }
        for row in remaining
    ]

    return comparison, extra_tracks


def match_mb_tracks_to_files(
    release_metadata: dict[str, Any] | None,
    files: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if not isinstance(release_metadata, dict) or not files:
        return []

    def _track_number(value: Any) -> int | None:
        text = str(value).split("/")[0].strip() if value is not None else ""
        try:
            return int(text)
        except (TypeError, ValueError):
            return None

    def _disc_number(value: Any) -> int | None:
        text = str(value).split("/")[0].strip() if value is not None else ""
        try:
            return int(text)
        except (TypeError, ValueError):
            return None

    album_artist = str(release_metadata.get("artist") or "").strip()
    album_title = str(
        release_metadata.get("release_group_title")
        or release_metadata.get("release_title")
        or release_metadata.get("title")
        or ""
    ).strip()
    release_mbid = str(release_metadata.get("release_mbid") or "").strip()
    edition_year = str(release_metadata.get("release_year") or "").strip()
    original_year = str(
        release_metadata.get("original_year") or release_metadata.get("year") or ""
    ).strip()

    remaining = list(files)
    entries: list[dict[str, Any]] = []

    mb_tracks = [
        track
        for track in (release_metadata.get("tracks") or [])
        if isinstance(track, dict)
    ]

    def _identity(mb_track: dict[str, Any]) -> dict[str, Any]:
        mb_title = str(mb_track.get("mb_title") or mb_track.get("title") or "").strip()
        mb_number = mb_track.get("mb_track_number")
        if mb_number is None:
            mb_number = mb_track.get("track_number")
        mb_disc = mb_track.get("mb_disc_number")
        if mb_disc is None:
            mb_disc = mb_track.get("disc_number")
        return {
            "title": mb_title,
            "norm": Normalize_title_for_lookup(mb_title),
            "verbatim": Normalize_title_for_mbid_match(mb_title),
            "number": _track_number(mb_number),
            "number_raw": mb_number,
            "disc": _disc_number(mb_disc),
            "disc_raw": mb_disc,
            "duration": mb_track.get("mb_duration"),
        }

    identities = [_identity(track) for track in mb_tracks]
    chosen: list[dict[str, Any] | None] = [None] * len(mb_tracks)

    for index, identity in enumerate(identities):
        if not identity["norm"]:
            continue
        named = [
            candidate for candidate in remaining
            if Normalize_title_for_lookup(str(candidate.get("title") or ""))
            == identity["norm"]
        ]
        if not named:
            continue
        exact = (
            [
                candidate for candidate in named
                if identity["verbatim"]
                and Normalize_title_for_mbid_match(str(candidate.get("title") or ""))
                == identity["verbatim"]
            ]
            or named
        )
        positioned = (
            [
                candidate for candidate in exact
                if identity["number"] is not None
                and _track_number(candidate.get("track_number")) == identity["number"]
                and (
                    identity["disc"] is None
                    or _disc_number(candidate.get("disc_number")) is None
                    or _disc_number(candidate.get("disc_number")) == identity["disc"]
                )
                and _track_number_pairing_allowed(
                    candidate.get("title"),
                    identity["title"],
                    candidate.get("duration"),
                    identity["duration"],
                )
            ]
            or exact
        )
        chosen[index] = positioned[0]
        remaining.remove(positioned[0])

    for index, identity in enumerate(identities):
        if chosen[index] is not None or identity["number"] is None:
            continue
        for candidate in remaining:
            if _track_number(candidate.get("track_number")) != identity["number"]:
                continue
            candidate_disc = _disc_number(candidate.get("disc_number"))
            if (
                identity["disc"] is not None
                and candidate_disc is not None
                and candidate_disc != identity["disc"]
            ):
                continue
            if not _track_number_pairing_allowed(
                candidate.get("title"),
                identity["title"],
                candidate.get("duration"),
                identity["duration"],
            ):
                continue
            chosen[index] = candidate
            remaining.remove(candidate)
            break

    for index, identity in enumerate(identities):
        if chosen[index] is not None or not identity["norm"]:
            continue
        best_candidate: dict[str, Any] | None = None
        best_score = 0.0
        for candidate in remaining:
            candidate_num = _track_number(candidate.get("track_number"))
            if (
                candidate_num is not None
                and identity["number"] is not None
                and candidate_num != identity["number"]
            ):
                continue
            score = _similarity(
                Normalize_title_for_lookup(str(candidate.get("title") or "")),
                identity["norm"],
            )
            if score > best_score:
                best_candidate, best_score = candidate, score
        if best_candidate is not None and best_score >= _TRACKLIST_TITLE_FLOOR:
            chosen[index] = best_candidate
            remaining.remove(best_candidate)

    for mb_track, identity, match in zip(mb_tracks, identities, chosen):
        mb_title = identity["title"]
        mb_number = identity["number_raw"]
        mb_disc = identity["disc_raw"]
        mb_disc_num = identity["disc"]

        entry: dict[str, Any] = {
            "mb_track_number": identity["number"],
            "mb_disc_number": mb_disc_num,
            "mb_title": mb_title,
            "mb_recording_mbid": str(
                mb_track.get("mb_recording_mbid") or mb_track.get("recording_mbid") or ""
            ).strip(),
            "matched": False,
            "file_path": "",
        }

        if match is None:
            entries.append(entry)
            continue

        file_artist = str(match.get("artist") or "").strip()
        resolved_artist = (
            str(mb_track.get("artist") or "").strip() or file_artist or album_artist
        )

        entry.update(
            {
                "matched": True,
                "file_path": str(match.get("file_path") or ""),
                "title": mb_title or str(match.get("title") or "").strip(),
                "artist": resolved_artist,
                "album": album_title,
                "album_artist": album_artist,
                "track_number": mb_number if mb_number is not None else match.get("track_number"),
                "disc_number": mb_disc if mb_disc is not None else match.get("disc_number"),
                "recording_mbid": entry["mb_recording_mbid"],
                "release_mbid": release_mbid,
                "release_year": edition_year,
                "originalyear": original_year or edition_year,
            }
        )

        for _source, _target in (
            ("musicbrainz_genres", "musicbrainz_genres"),
            ("writer", "writer"),
            ("work_mbid", "work_mbid"),
            ("original_cover_artist", "original_cover_artist"),
            ("original_title", "original_title"),
            ("iswc", "iswc"),
        ):
            _value = mb_track.get(_source)
            if _value not in (None, "", [], {}):
                entry[_target] = _value
        if mb_track.get("is_cover"):
            entry["is_cover"] = True

        entries.append(entry)

    return entries


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
    artist: str,
    album: str,
    release_group_mbid: str,
) -> dict[str, Any]:
    context = {
        "artist": artist,
        "album": album,
        "release_group_mbid": release_group_mbid,
    }
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

        best_result: dict[str, Any] = {}
        if direct and direct.get("id"):
            release_id = release_group_mbid
        else:
            best_result = get_musicbrainz_best_release(artist, album, release_group_mbid)
            release_id = str(
                ((best_result or {}).get("best_release") or {}).get("id") or ""
            )
            if not release_id:
                release_id = resolve_release_id(release_group_mbid)

        original_track_count = (best_result or {}).get("original_track_count")

        mb_release = fetch_musicbrainz_release_metadata(release_id)
        if not mb_release:
            return {
                "success": False,
                "error": "Could not fetch MusicBrainz release data",
            }

        library_tracks = _fetch_library_tracks(artist, album)
        if not library_tracks:
            return {
                "success": False,
                "error": "No library tracks found for this album",
            }

        comparison, extra_tracks = _match_mb_tracks_to_library(
            mb_release.get("tracks") or [], library_tracks
        )
        tracks_needing_update = sum(1 for entry in comparison if entry.get("needs_update"))

        mb_year = str(mb_release.get("original_release_year") or mb_release.get("release_year") or "")

        result = {
            "success": True,
            "mb_title": str(mb_release.get("release_title") or ""),
            "mb_specific_release_title": str(mb_release.get("specific_release_title") or ""),
            "mb_release_group_title": str(mb_release.get("release_group_title") or ""),
            "mb_year": mb_year,
            "mb_original_release_year": str(mb_release.get("original_release_year") or ""),
            "mb_version_release_year": str(mb_release.get("version_release_year") or ""),
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
            "extra_tracks": extra_tracks,
            "tracks_needing_update": tracks_needing_update,
            "total_tracks": len(comparison),
            "mb_original_track_count": original_track_count,
        }
        return result
    except Exception:
        return {"success": False, "error": "Could not fetch MusicBrainz release data"}


def _update_track_fields(track_id: str, fields: dict[str, Any]) -> None:
    if not track_id or not fields:
        return
    
    allowed_columns = _ALIGN_WRITABLE_FIELDS + _LINK_WRITABLE_FIELDS
    safe_fields = {k: v for k, v in fields.items() if k in allowed_columns}
    
    if not safe_fields:
        return

    from db.engine import db_session
    from sqlalchemy import text
    set_clause = ", ".join(f"{column} = :{column}" for column in safe_fields)
    params = dict(safe_fields)
    params["id"] = track_id
    with db_session() as session:
        session.execute(text(f"UPDATE tracks SET {set_clause} WHERE id = :id"), params)
        session.commit()

def link_album_mbids(artist: str, album: str, release_mbid: str) -> dict[str, Any]:
    if not release_mbid:
        return {
            "success": False,
            "error": 'No MusicBrainz release linked yet. Use "Lookup on MusicBrainz" first, then save.',
        }

    result = compare_musicbrainz_release(artist, album, release_mbid)
    if not result.get("success"):
        return result

    linked = 0
    already_linked = 0
    for entry in result.get("comparison") or []:
        if not entry.get("matched"):
            continue
        new_mbid = entry.get("mb_recording_mbid")
        if not new_mbid:
            continue
        if entry.get("library_mbid") == new_mbid:
            already_linked += 1
            continue
        _update_track_fields(entry["library_track_id"], {"mbid": new_mbid})
        linked += 1

    unmatched = sum(1 for entry in result.get("comparison") or [] if not entry.get("matched"))

    return {
        "success": True,
        "linked": linked,
        "already_linked": already_linked,
        "unmatched": unmatched,
        "extra_tracks": result.get("extra_tracks") or [],
        "total_tracks": result.get("total_tracks") or 0,
        "release_mbid": result.get("release_mbid"),
    }

def align_album_tracklist(artist: str, album: str, release_mbid: str) -> dict[str, Any]:
    if not release_mbid:
        return {
            "success": False,
            "error": 'No MusicBrainz release linked yet. Use "Lookup on MusicBrainz" first, then save.',
        }

    result = compare_musicbrainz_release(artist, album, release_mbid)
    if not result.get("success"):
        return result

    changes: list[dict[str, Any]] = []
    aligned = 0
    for entry in result.get("comparison") or []:
        if not entry.get("matched") or not entry.get("needs_update"):
            continue

        diff_fields = entry.get("diff_fields") or []
        fields: dict[str, Any] = {}
        if "title" in diff_fields:
            fields["title"] = entry["mb_title"]
        if "track_number" in diff_fields:
            fields["track_number"] = str(entry["mb_track_number"])

        if not fields:
            continue

        fields["disc_number"] = str(entry.get("mb_disc_number") or 1)

        _update_track_fields(entry["library_track_id"], fields)
        changes.append({
            "library_track_id": entry["library_track_id"],
            "old_title": entry.get("library_title"),
            "new_title": fields.get("title", entry.get("library_title")),
            "old_track_number": entry.get("library_track_number"),
            "new_track_number": fields.get("track_number", entry.get("library_track_number")),
        })
        aligned += 1

    unmatched = sum(1 for entry in result.get("comparison") or [] if not entry.get("matched"))

    return {
        "success": True,
        "aligned": aligned,
        "unmatched": unmatched,
        "changes": changes,
        "extra_tracks": result.get("extra_tracks") or [],
        "total_tracks": result.get("total_tracks") or 0,
        "release_mbid": result.get("release_mbid"),
    }
