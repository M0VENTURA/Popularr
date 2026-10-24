"""MusicBrainz enrichment and lookup service.
​Owns MusicBrainz interpretation and matching rules.
"""
from future import annotations
​import atexit
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
​import structlog
​try:
from rapidfuzz import fuzz as _rapidfuzz_fuzz  # type: ignore[import-untyped]
_HAVE_RAPIDFUZZ = True
except ImportError:
_rapidfuzz_fuzz = None
_HAVE_RAPIDFUZZ = False
​from api_clients.musicbrainz_http import (
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
​Logger = structlog.get_logger(name)
T = TypeVar("T")
​def _default_cache_file() -> str:
for candidate in ("/data", "/config", "/var/lib/popularr"):
if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
return os.path.join(candidate, "mbid_cache.json")
return "mbid_cache.json"
​CACHE_FILE = os.getenv("MUSICBRAINZ_CACHE_FILE", "") or _default_cache_file()
​_HEARTBEAT_SECONDS = max(5.0, float(os.getenv("MUSICBRAINZ_HEARTBEAT_SECONDS", "30")))
_MONITOR_TICK_SECONDS = 2.0
_CACHE_FLUSH_SECONDS = max(5.0, float(os.getenv("MUSICBRAINZ_CACHE_FLUSH_SECONDS", "30")))
​_CACHE_IO_LOCK = threading.Lock()
_INIT_LOCK = threading.RLock()
​_MB_BATCH_CHUNK = 20
_MB_BATCH_SIMILARITY_FLOOR = 0.6
_MBID_CACHE_SIMILARITY_FLOOR = 0.6
_MBID_CACHE_TTL_SECONDS = 30 * 24 * 3600
_MBID_NEGATIVE_TTL_SECONDS = 7 * 24 * 3600
_MBID_CACHE_MAX_SIZE = 5000
_RELEASE_GROUP_MATCH_FLOOR = 0.6
​_TRACK_COUNT_REFINE_LIMIT = 3
_RECORDING_RELATIONSHIP_INC = "artist-rels+work-rels+work-level-rels"
DURATION_TOLERANCE_SECONDS = 5
​def length_tolerance_seconds() -> float:
try:
from helpers.config_helpers import get_length_tolerance_seconds
return float(get_length_tolerance_seconds())
except Exception:
return float(DURATION_TOLERANCE_SECONDS)
​all = [
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
​def _error(exc: BaseException) -> str:
return f"{type(exc).name}: {exc}"
​def _year_of(value: Any) -> int | None:
text_value = str(value or "").strip()
return int(text_value[:4]) if len(text_value) >= 4 and text_value[:4].isdigit() else None
​def _as_int(value: Any, default: int = 0) -> int:
try:
return int(str(value).strip())
except (TypeError, ValueError):
return default
​@contextmanager
def _logged_section(section: str, context: Any) -> Iterator[None]:
started = time.monotonic()
Logger.debug("[MB] section started", section=section, context)
try:
yield
except Exception as exc:
Logger.exception(
"[MB] section failed",
section=section,
elapsed_s=round(time.monotonic() - started, 3),
error=_error(exc),
context,
)
raise
else:
Logger.debug(
"[MB] section completed",
section=section,
elapsed_s=round(time.monotonic() - started, 3),
context,
)
​_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: "OrderedDict[int, dict[str, Any]]" = OrderedDict()
_INFLIGHT_IDS = itertools.count()
_MONITOR_THREAD: threading.Thread | None = None
​def _monitor_loop() -> None:
while True:
time.sleep(_MONITOR_TICK_SECONDS)
now = time.monotonic()
due: list[dict[str, Any]] = []
with _INFLIGHT_LOCK:
for entry in _INFLIGHT.values():
if now >= entry["next_warn"]:
entry["next_warn"] = now + _HEARTBEAT_SECONDS
due.append({
"section": entry["section"],
"elapsed_s": round(now - entry["started"], 1),
"context": entry["context"],
})
for item in due:
Logger.warning(
"[MB] call still running",
section=item["section"],
elapsed_s=item["elapsed_s"],
item["context"],
)
​def _ensure_monitor() -> None:
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
​def _call_with_heartbeat(
section: str,
func: Callable[..., Any],
*args: Any,
log_context: dict[str, Any] | None = None,
kwargs: Any,
) -> Any:
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
​Logger.debug("[MB] call started", section=section, context)
try:
result = func(*args, kwargs)
except Exception as exc:
Logger.exception(
"[MB] call failed",
section=section,
elapsed_s=round(time.monotonic() - started, 3),
error=_error(exc),
context,
)
raise
else:
Logger.debug(
"[MB] call completed",
section=section,
elapsed_s=round(time.monotonic() - started, 3),
context,
)
return result
finally:
with _INFLIGHT_LOCK:
_INFLIGHT.pop(call_id, None)
​def _similarity(a: str, b: str) -> float:
from services.popularity.popularity_math import fuzzy_match_score
return fuzzy_match_score(a, b)
​def _mbid_similarity(a: str, b: str) -> float:
if not a or not b:
return 0.0
if a == b:
return 1.0
if _HAVE_RAPIDFUZZ and _rapidfuzz_fuzz is not None:
return _rapidfuzz_fuzz.token_sort_ratio(a, b) / 100.0
return difflib.SequenceMatcher(None, a, b).ratio()
​def build_artist_credit_string(artist_credit: list[Any]) -> str:
parts: list[str] = []
for credit in artist_credit or []:
if isinstance(credit, dict):
parts.append(str(credit.get("name") or ""))
parts.append(str(credit.get("joinphrase") or ""))
else:
parts.append(str(credit))
return "".join(parts).strip()
​def credit_artist_mbid(artist_credit: Any) -> str | None:
for credit in artist_credit or []:
if isinstance(credit, dict):
artist = credit.get("artist")
if isinstance(artist, dict) and artist.get("id"):
return str(artist["id"])
return None
​def remix_of_recording_id(recording: Any) -> str:
for relation in (recording or {}).get("relations") or []:
if not isinstance(relation, dict):
continue
if str(relation.get("type") or "").casefold() != "remix of":
continue
target = relation.get("recording") or {}
if isinstance(target, dict) and target.get("id"):
return str(target["id"])
return ""
​def remixers_of_recording(recording: Any) -> list[str]:
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
​def primary_album_artist(artist_credit: list[Any] | str) -> str:
if isinstance(artist_credit, list) and artist_credit:
first = artist_credit[0]
if isinstance(first, dict):
return str(first.get("name") or "").strip()
return str(first or "").strip()
if isinstance(artist_credit, str):
return artist_credit.strip()
return ""
​def calculate_match_score(
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
​def _parse_secondary_types(raw: Any) -> list[str]:
if raw is None:
return []
if isinstance(raw, str):
return [part.strip() for part in raw.split(",") if part.strip()]
if isinstance(raw, (list, tuple)):
return [str(part).strip() for part in raw if str(part).strip()]
return []
​def _compose_album_type(primary_type: str, secondary_types: list[str]) -> str:
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
​def _release_group_primary_type(group: Any) -> str:
if not isinstance(group, dict):
return ""
return str(
group.get("primary-type")
or group.get("primary_type")
or group.get("type")
or ""
).casefold()
​def _country_area_name(area: Any) -> str:
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
​def _artist_country_name(data: Any) -> str:
if not isinstance(data, dict):
return ""
for key in ("area", "begin-area", "beginArea"):
name = _country_area_name(data.get(key))
if name:
return name
return ""
​def _artist_lookup_candidates(artist: str) -> list[str]:
result: list[str] = []
seen: set[str] = set()
for candidate in (artist or "", Strip_featured_artist(artist or "")):
key = str(candidate or "").casefold().strip()
if key and key not in seen:
result.append(str(candidate).strip())
seen.add(key)
return result
​def _normalise_artist_key(value: str) -> str:
return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
​def _mb_artist_credit_name(artist_credit: list[Any] | str) -> str:
return primary_album_artist(artist_credit)
​def _cover_art_url(release_group_id: str = "", release_id: str = "") -> str:
if release_group_id:
return f"https://coverartarchive.org/release-group/{release_group_id}/front-250"
if release_id:
return f"https://coverartarchive.org/release/{release_id}/front-250"
return ""
​def _first_isrc(recording: dict[str, Any]) -> str | None:
from helpers.normalization_service import normalize_isrc
for raw in recording.get("isrcs") or recording.get("isrc-list") or []:
value = normalize_isrc(raw)
if value:
return value
return normalize_isrc(recording.get("isrc")) or None
​def _recording_matches_album(recording: dict[str, Any], album: str) -> bool:
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
if any(val and _similarity(str(val), album_val) >= 0.6 for val in candidates):
return True
return False
​def _release_track_count(release: Any) -> int:
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
​_NON_STUDIO_SECONDARY_TYPES = frozenset({
"live", "compilation", "remix", "soundtrack", "spokenword", "demo",
"dj-mix", "mixtape", "interview", "audiobook",
})
​_ALBUM_IDENTITY_MATCH_FLOOR = 0.6
​def _release_group_title_of(release: Any) -> str:
if not isinstance(release, dict):
return ""
group = release.get("release-group") or {}
if not isinstance(group, dict):
return ""
return str(group.get("title") or "").strip()
​def _release_group_primary_type_of(release: Any) -> str:
if not isinstance(release, dict):
return ""
group = release.get("release-group") or {}
if not isinstance(group, dict):
return ""
return str(group.get("primary-type") or group.get("primary_type") or "").strip().casefold()
​def _release_group_secondary_types_of(release: Any) -> list[str]:
if not isinstance(release, dict):
return []
group = release.get("release-group") or {}
if not isinstance(group, dict):
return []
return _parse_secondary_types(group.get("secondary-types") or group.get("secondary_types"))
​def _release_album_identity(release: Any) -> str:
return _release_group_title_of(release) or str(
(release.get("title") if isinstance(release, dict) else "") or ""
).strip()
​def _edition_title_matches(release_title: str, album_name: str) -> bool:
if not release_title or not album_name:
return False
if not Extract_edition_annotation(album_name):
return False
if not Edition_annotations_compatible(release_title, album_name):
return False
return _similarity(release_title, album_name) >= 0.9
​def _select_primary_release(releases: Any, album_name: str | None) -> dict[str, Any]:
candidates = [r for r in (releases or []) if isinstance(r, dict)]
if not candidates:
return {}
​anchor = str(album_name or "").strip()
if len(candidates) == 1:
return candidates[0]
​def _sort_key(release: dict[str, Any]) -> tuple[Any, ...]:
return (str(release.get("date") or "9999"), str(release.get("id") or ""))
​if anchor:
edition_matches = [
r for r in candidates
if _edition_title_matches(str(r.get("title") or ""), anchor)
]
if edition_matches:
return min(edition_matches, key=_sort_key)
​if anchor:
matching = [
r for r in candidates
if _release_album_identity(r)
and _similarity(_release_album_identity(r), anchor) >= _ALBUM_IDENTITY_MATCH_FLOOR
]
if matching:
return min(matching, key=_sort_key)
​def _is_studio(release: dict[str, Any]) -> bool:
primary = _release_group_primary_type_of(release)
if primary not in ("", "album"):
return False
return not any(
str(secondary).strip().casefold() in _NON_STUDIO_SECONDARY_TYPES
for secondary in _release_group_secondary_types_of(release)
)
​studio = [r for r in candidates if _is_studio(r)]
if studio:
studio.sort(
key=lambda r: (
0 if _release_group_title_of(r) else 1,
*_sort_key(r),
)
)
return studio[0]
​return min(candidates, key=_sort_key)
​def _recording_live_affinity(recording: Any) -> bool | None:
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
​def _client_available(client: Any) -> bool:
checker = getattr(client, "is_available", None)
if not callable(checker):
return True
try:
return bool(checker())
except Exception:
return True
​_SHARED_MB_CLIENT: MusicBrainzHttpClient | None = None
_service: "MusicBrainzService | None" = None
_shared_mb_service: "MusicBrainzService | None" = None
​def get_shared_mb_client() -> MusicBrainzHttpClient:
global _SHARED_MB_CLIENT
if _SHARED_MB_CLIENT is not None:
return _SHARED_MB_CLIENT
Logger.info("[MB] shared HTTP client initialization requested")
with _INIT_LOCK:
if _SHARED_MB_CLIENT is None:
_SHARED_MB_CLIENT = MusicBrainzHttpClient(enabled=True)
return _SHARED_MB_CLIENT
​_BEST_RELEASE_LOCK = threading.Lock()
_BEST_RELEASE_CACHE: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_BEST_RELEASE_CACHE_MAX = 500
​def _best_release_cache_get(key: str) -> dict[str, Any] | None:
with _BEST_RELEASE_LOCK:
if key not in _BEST_RELEASE_CACHE:
return None
_BEST_RELEASE_CACHE.move_to_end(key)
return _BEST_RELEASE_CACHE[key]
​def _best_release_cache_set(key: str, value: dict[str, Any]) -> None:
with _BEST_RELEASE_LOCK:
_BEST_RELEASE_CACHE[key] = value
_BEST_RELEASE_CACHE.move_to_end(key)
while len(_BEST_RELEASE_CACHE) > _BEST_RELEASE_CACHE_MAX:
_BEST_RELEASE_CACHE.popitem(last=False)
​def clear_release_caches() -> None:
with _BEST_RELEASE_LOCK:
_BEST_RELEASE_CACHE.clear()
​class MusicBrainzService:
def init(
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
​def flush_cache(self) -> None:
self._maybe_flush_cache(force=True)
​@staticmethod
def _cache_key(title: str, artist: str, is_live: bool = False, annotation: str | None = None) -> str:
base = f"{artist.casefold().strip()}::{title.casefold().strip()}"
if is_live:
base = f"{base}::live"
if annotation:
base = f"{base}::{annotation}"
return base
​def get_suggested_mbid(self, title: str, artist: str, limit: int = 5, kwargs: Any) -> tuple[str, float]:
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
​cache_key = self._cache_key(
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
if not mbid and cached_at is not None and now - cached_at < _MBID_NEGATIVE_TTL_SECONDS:
self._mbid_cache.move_to_end(cache_key)
try:
if callable(on_miss):
on_miss()
except Exception:
pass
return "", 0.0
​query_title = Normalize_title_for_lucene_query(Strip_search_keywords(title))
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
​for recording in recordings:
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
​affinity = _recording_live_affinity(recording)
live_rank = 1 if affinity is None else (2 if affinity == is_live_release else 0)
rank = (live_rank, 1 if annotation_ok else 0, score)
​if best_rank is None or rank > best_rank:
best_rank = rank
best_mbid = str(recording.get("id") or "")
best_score = score
​if best_mbid and best_score >= _MBID_CACHE_SIMILARITY_FLOOR:
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
​def lookup_recording_metadata(self, title: str, artist: str, *, album: str | None = None, is_live_release: bool = False, edition_annotation: str | None = None, kwargs: Any) -> dict[str, Any]:
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
known_mbid = str(kwargs.get("mbid") or "").strip()
if known_mbid:
mbid, confidence = known_mbid, 1.0
else:
mbid, confidence = self.get_suggested_mbid(
title, artist, is_live_release=is_live_release,
edition_annotation=edition_annotation, on_miss=on_miss,
)
if not mbid:
return {}
recording = _call_with_heartbeat(
"recording.get",
self.http.get_recording,
mbid,
inc="artist-credits+releases+release-groups+work-rels+work-level-rels+artist-rels+genres",
log_context={context, "mbid": mbid},
)
if not recording and known_mbid:
mbid, confidence = self.get_suggested_mbid(
title, artist, is_live_release=is_live_release,
edition_annotation=edition_annotation, on_miss=on_miss,
)
if not mbid:
return {}
recording = _call_with_heartbeat(
"recording.get",
self.http.get_recording,
mbid,
inc="artist-credits+releases+release-groups+work-rels+genres",
log_context={context, "mbid": mbid},
)
if not recording:
return {}
return self._recording_to_metadata(recording, mbid, confidence, album_name=album)
except Exception:
return {}
​def lookup_recordings_by_mbid_bulk(self, mbids: list[str], *, album_name: str | None = None, original_release_year: int | None = None, kwargs: Any) -> dict[str, dict[str, Any]]:
if not self.enabled or not mbids:
return {}
​context = {
"mbid_count": len(mbids),
"authoritative_album_name": album_name,
"original_release_year": original_release_year,
}
try:
bulk_kwargs: dict[str, Any] = {}
release_mbid = str(kwargs.get("release_mbid") or "").strip()
if release_mbid:
bulk_kwargs["release_mbid"] = release_mbid
try:
payload = _call_with_heartbeat(
"recording.bulk_get",
self.http.get_recordings_bulk,
mbids,
inc="artist-credits+releases+release-groups+work-rels+genres",
log_context=context,
bulk_kwargs,
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
recording, mbid, 1.0, album_name=album_name, original_release_year=original_release_year,
)
return results
except Exception:
return {}
​def _recording_to_metadata(self, recording: dict[str, Any], mbid: str, confidence: float, *, album_name: str | None = None, original_release_year: int | None = None, kwargs: Any) -> dict[str, Any]:
credits = recording.get("artist-credit") or []
full_artist_string = build_artist_credit_string(credits)
​first = credits[0] if credits else {}
if isinstance(first, dict):
artist = full_artist_string or str(first.get("name") or "").strip()
artist_data = first.get("artist") or {}
artist_mbid = str(artist_data.get("id") or "").strip() if isinstance(artist_data, dict) else ""
else:
artist = full_artist_string or str(first or "").strip()
artist_mbid = ""
​releases = recording.get("releases") or []
authoritative_album = str(album_name or "").strip()
​specific_release = _select_primary_release(releases, authoritative_album)
specific_title = str(specific_release.get("title") or "").strip()
version_release_year = _year_of(specific_release.get("date"))
​release_group_title = _release_group_title_of(specific_release)
effective_album = authoritative_album or release_group_title or specific_title
effective_year = original_release_year if original_release_year is not None else version_release_year
​writers: list[str] = []
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
rel_type = str(relation.get("type") or "").casefold()
if rel_type == "remix of":
target = relation.get("recording") or {}
if isinstance(target, dict) and target.get("title"):
remix_of_title = str(target["title"])
continue
if rel_type not in {"performance", "recording of"}:
continue
work = relation.get("work") or {}
if not isinstance(work, dict):
continue
if work.get("id"):
attrs = {
str(a).strip().casefold()
for a in (relation.get("attributes") or [])
if str(a).strip()
}
if "cover" in attrs:
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
​genres = [
str(item.get("name") or "").strip()
for item in recording.get("genres") or []
if isinstance(item, dict) and str(item.get("name") or "").strip()
]
genres = list(dict.fromkeys(genres))
​return {
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
​def lookup_original_album_year(self, artist: str, album: str, kwargs: Any) -> int | None:
if not self.enabled or not artist or not album:
return None
​cache_key = f"{artist.casefold().strip()}::{album.casefold().strip()}"
with self._mem_lock:
if cache_key in self._album_year_cache:
return self._album_year_cache[cache_key]
​clean_album = Strip_search_keywords(album)
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
log_context={"artist": artist, "album": album},
) or []
​if not groups and clean_album and _client_available(self.http):
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
log_context={"artist": artist, "album": album},
) or []
​ranked: list[tuple[float, int, dict[str, Any]]] = []
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
​ranked.sort(key=lambda item: item[0], reverse=True)
if not ranked or ranked[0][0] < _RELEASE_GROUP_MATCH_FLOOR:
with self._mem_lock:
self._album_year_cache[cache_key] = None
return None
​year = ranked[0][1]
with self._mem_lock:
self._album_year_cache[cache_key] = year
return year
except Exception:
return None
​def search_releasegroup_matches(self, artist_name: str, album_name: str, limit: int = 10, kwargs: Any) -> list[dict[str, Any]]:
context = {"artist": artist_name, "album": album_name, "limit": limit}
if not self.enabled or not artist_name or not album_name:
return []
​expected_count = _get_local_track_count(artist_name, album_name) or None
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
log_context=context,
) or []
except Exception:
groups = []
​if not groups and clean_album and _client_available(self.http):
terms = Normalize_title_for_lucene_query(clean_album)
if terms:
fallback_query = f'artist:"{escaped_artist}" AND releasegroup:{terms}'
try:
groups = _call_with_heartbeat(
"release_group.fallback_search",
self.http.search_release_groups,
fallback_query,
limit=limit,
log_context=context,
) or []
except Exception:
groups = []
​matches: list[dict[str, Any]] = []
for group in groups:
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
​sec_lower = {str(t).casefold() for t in secondary_types}
primary_lower = _release_group_primary_type(group)
group_title = str(group.get("title") or "")
​if _title_adds_a_second_song(
Normalize_title_for_lookup(album_name),
Normalize_title_for_lookup(group_title),
):
score *= 0.65
​if not primary_lower:
score *= 0.7
​if "live" in sec_lower and not is_live_or_alternate_album(album_name):
score *= 0.7
if "compilation" in sec_lower and not (
"compilation" in album_name.casefold()
or detect_greatest_hits_album(album_name, artist_name)
):
score *= 0.8
if not secondary_types and primary_lower == "album":
score *= 1.15
​matches.append({
"id": group.get("id"),
"title": group_title,
"primary_type": primary_lower,
"match_score": score,
"secondary_types": secondary_types,
"first_release_date": group.get("first-release-date") or "",
})
except Exception:
continue
​matches.sort(key=lambda item: item.get("match_score", 0.0), reverse=True)
​if expected_count and matches:
for match in matches[:_TRACK_COUNT_REFINE_LIMIT]:
group_id = str(match.get("id") or "")
if not group_id:
continue
releases = _browse_group_releases(group_id, "release_group.track_count_refine")
counts = [_as_int(r.get("track_count"), 0) for r in releases if _as_int(r.get("track_count"), 0) > 0]
if counts:
best_diff = min(abs(expected_count - c) for c in counts)
match["match_score"] -= best_diff * 0.05
​for match in matches:
match["match_score"] = round(float(match.get("match_score") or 0.0), 3)
​matches.sort(key=lambda item: item.get("match_score", 0.0), reverse=True)
return matches
​@staticmethod
def merge_metadata(base: dict[str, Any], mb: dict[str, Any], overrides: dict[str, Any] | None = None, kwargs: Any) -> dict[str, Any]:
overrides = overrides or {}
def pick(*values: Any) -> Any:
return next((val for val in values if val not in (None, "")), None)
return {
"title": pick(overrides.get("title"), mb.get("title"), base.get("title")),
"artist": pick(overrides.get("artist"), mb.get("artist"), base.get("artist")),
"album": pick(overrides.get("album"), mb.get("album"), base.get("album")),
"album_artist": pick(overrides.get("album_artist"), base.get("album_artist"), mb.get("album_artist")),
"year": pick(overrides.get("year"), mb.get("original_release_year"), mb.get("year"), base.get("year")),
}
​def get_artist_country(self, artist: str, kwargs: Any) -> str:
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
country_code = str(data.get("country") or "").strip()
if country_code:
return country_code
return _artist_country_name(data)
except Exception:
return ""
​def _maybe_flush_cache(self, force: bool = False) -> None:
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
​def _record_mbid(self, key: str, mbid: str, confidence: float) -> None:
with self._mem_lock:
self._mbid_cache[key] = [mbid, round(float(confidence), 3), time.time()]
self._mbid_cache.move_to_end(key)
while len(self._mbid_cache) > _MBID_CACHE_MAX_SIZE:
self._mbid_cache.popitem(last=False)
self._cache_dirty = True
​def clear_transient_caches(self) -> None:
with self._mem_lock:
self._mbid_cache.clear()
self._album_year_cache.clear()
self._single_result_cache.clear()
clear_release_caches()
clearer = getattr(self.http, "clear_caches", None)
if callable(clearer):
clearer()
​def _get_service() -> MusicBrainzService:
global _service
if _service is not None:
return _service
with _INIT_LOCK:
if _service is None:
client = get_shared_mb_client()
_service = MusicBrainzService(http_client=client, enabled=True)
return _service
​def get_shared_mb_service() -> MusicBrainzService:
global _shared_mb_service
if _shared_mb_service is not None:
return _shared_mb_service
with _INIT_LOCK:
if _shared_mb_service is None:
client = get_shared_mb_client()
_shared_mb_service = MusicBrainzService(http_client=client, enabled=True)
return _shared_mb_service
​def lookup_recording_metadata(title: str, artist: str, kwargs: Any) -> dict[str, Any]:
return _get_service().lookup_recording_metadata(title, artist, kwargs)
​def merge_metadata(base: dict[str, Any], mb: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
return _get_service().merge_metadata(base, mb, overrides)
​def _unique_join(values: Any, *, limit: int = 8) -> str:
seen: list[str] = []
for value in values or []:
text = str(value or "").strip()
if text and text not in seen:
seen.append(text)
return " / ".join(seen[:limit])
​def _release_event_country(release: dict[str, Any]) -> str:
events = release.get("release-events") or release.get("release_events") or []
if isinstance(events, list):
dates: list[tuple[str, str]] = []
for event in events:
if not isinstance(event, dict):
continue
area = event.get("area") or {}
name = _country_area_name(area)
if name:
dates.append((str(event.get("date") or "9999"), name))
if dates:
dates.sort(key=lambda item: item[0])
return dates[0][1]
return str(release.get("country") or "").strip()
​def _release_extended_fields(release: dict[str, Any], media: Any) -> dict[str, str]:
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
​formats: list[str] = []
if isinstance(media, list):
for medium in media:
if isinstance(medium, dict) and medium.get("format"):
formats.append(str(medium["format"]))
​return {
"recordlabel": _unique_join(labels),
"catalognumber": _unique_join(catalogs),
"barcode": str(release.get("barcode") or "").strip(),
"releasedate": str(release.get("date") or "").strip(),
"media": _unique_join(formats),
"releasecountry": _release_event_country(release),
}
​_RELEASE_FETCH_INC = (
"recordings+artist-credits+release-groups+media+labels"
"+work-rels+recording-level-rels+work-level-rels+artist-rels+genres+isrcs"
)
​def _fetch_raw_release(release_id: str, client: Any = None) -> dict[str, Any] | None:
client_val = client or get_shared_mb_client()
return _call_with_heartbeat(
"release.fetch_metadata",
client_val.get_release,
release_id,
inc=_RELEASE_FETCH_INC,
log_context={"release_id": release_id},
)
​def fetch_musicbrainz_release_metadata(release_id: str) -> dict[str, Any] | None:
if not release_id:
return None
try:
release = _fetch_raw_release(release_id)
except Exception:
return None
if not isinstance(release, dict) or not release.get("id"):
return None
return _flatten_release(release, release_id)
​def _join_genre_names(raw: Any) -> str:
names: list[str] = []
for item in raw or []:
if isinstance(item, dict):
name = str(item.get("name") or "").strip()
if name and name.casefold() not in {n.casefold() for n in names}:
names.append(name)
return ", ".join(names)
​def _flatten_release(release: dict[str, Any], release_id: str) -> dict[str, Any]:
artist_credit = release.get("artist-credit") or []
release_group = release.get("release-group") or {}
if not isinstance(release_group, dict):
release_group = {}
​first_release_date = str(release_group.get("first-release-date") or "")
original_release_year = _year_of(first_release_date) or _year_of(release.get("date"))
version_release_year = _year_of(release.get("date"))
​secondary_types = _parse_secondary_types(
release_group.get("secondary-types") or release_group.get("secondary_types")
)
album_type = _compose_album_type(_release_group_primary_type(release_group), secondary_types)
​first_artist = artist_credit[0] if artist_credit and isinstance(artist_credit[0], dict) else {}
artist_ref = first_artist.get("artist") if isinstance(first_artist, dict) else None
album_artist_mbid = str(artist_ref.get("id") or "") if isinstance(artist_ref, dict) else ""
​primary_artist = primary_album_artist(artist_credit) or _mb_artist_credit_name(artist_credit)
joined_artist_credit = build_artist_credit_string(artist_credit)
​tracks: list[dict[str, Any]] = []
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
​genres = [
str(item.get("name") or "").strip()
for item in recording.get("genres") or []
if isinstance(item, dict) and str(item.get("name") or "").strip()
]
genres = list(dict.fromkeys(genres))
​recording_credit = (
build_artist_credit_string(recording.get("artist-credit") or [])
if recording.get("artist-credit")
else ""
)
track_credit = (
build_artist_credit_string(track.get("artist-credit") or [])
if track.get("artist-credit")
else ""
)
if is_track_artist_placeholder(recording_credit):
recording_credit = ""
if is_track_artist_placeholder(track_credit):
track_credit = ""
track_artist = (
recording_credit or track_credit
or joined_artist_credit or primary_artist
)
​entry: dict[str, Any] = {
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
"artist_mbid": credit_artist_mbid(recording.get("artist-credit")),
"is_remix": bool(remix_of_recording_id(recording)),
"remix_of_recording_mbid": remix_of_recording_id(recording),
}
if genres:
entry["musicbrainz_genres"] = ", ".join(genres)
tracks.append(entry)
absolute_track_number += 1
​return {
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
_release_extended_fields(release, media),
"tracks": tracks,
}
​def fetch_release_metadata(release_id: str) -> dict[str, Any] | None:
return fetch_musicbrainz_release_metadata(release_id)
​def resolve_release_id(release_id: str) -> str:
if not release_id:
return release_id
try:
http = _get_service().http
data = http.get_release(release_id, inc="")
if data and data.get("id"):
return release_id
except Exception:
pass
return release_id
​def release_group_tracklist_similarity(
release_group_mbid: str,
library_tracks: list[dict[str, Any]],
artist: str = "",
album: str = "",
prefer_release_id: str | None = None,
) -> float:
if not release_group_mbid or not library_tracks:
return 0.0
release_id = str(prefer_release_id or "").strip()
if not release_id:
try:
releases = _browse_group_releases(release_group_mbid, "tracklist.verify_browse")
if releases:
release_id = str(releases[0].get("id") or "").strip()
except Exception:
return 0.0
if not release_id:
return 0.0
metadata = fetch_musicbrainz_release_metadata(release_id)
if not metadata:
return 0.0
mb_titles = [str(t.get("mb_title") or "") for t in (metadata.get("tracks") or []) if isinstance(t, dict)]
if not mb_titles:
return 0.0
present = sum(
1 for track in library_tracks
if isinstance(track, dict) and _title_present_in(str(track.get("title") or ""), mb_titles)
)
return present / len(library_tracks)
​def _title_present_in(title: str, candidates: list[str], floor: float = 0.55) -> bool:
norm = Normalize_title_for_lookup(str(title or ""))
if not norm:
return False
for candidate in candidates:
if candidate and _similarity(norm, Normalize_title_for_lookup(candidate)) >= floor:
return True
return False
​def release_group_tracklist_passes(
release_group_mbid: str,
library_tracks: list[dict[str, Any]],
floor: float | None = None,
artist: str = "",
album: str = "",
prefer_release_id: str | None = None,
) -> tuple[bool, float]:
threshold = 0.6 if floor is None else float(floor)
similarity = release_group_tracklist_similarity(
release_group_mbid, library_tracks, artist=artist, album=album, prefer_release_id=prefer_release_id
)
return similarity >= threshold, similarity
​def _get_local_track_count(artist: str, album: str) -> int:
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
​def _browse_group_releases(release_group_mbid: str, section: str) -> list[dict[str, Any]]:
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
​def _title_adds_a_second_song(norm_a: str, norm_b: str) -> bool:
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
return bool(extra - frozenset({
"live", "acoustic", "unplugged", "remix", "remixed", "remaster",
"remastered", "demo", "instrumental", "orchestral", "karaoke",
"version", "edit", "mix", "radio", "extended", "deluxe", "bonus",
"mono", "stereo", "single", "album", "reissue", "anniversary", "edition",
"and", "the", "of", "a", "de", "la", "feat", "featuring", "ft"
}))