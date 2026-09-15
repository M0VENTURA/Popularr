"""Low-level MusicBrainz HTTP client.

Strictly adheres to MusicBrainz API Rules:
1. Hard 1 request/sec global limit via thread-locked turnstile.
2. Compliant User-Agent with version and contact URL.
3. Exponential backoff on 429/503 responses (handled by http_utils).
4. Aggressive, thread-safe in-memory caching to minimize server load.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Any

import httpx
import structlog

from api_clients import session
from services.infrastructure.api_rate_limiter import get_rate_limiter

logger = structlog.get_logger(__name__)


def get_version() -> str:
    try:
        version_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "VERSION")
        with open(version_file, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except Exception:
        return "2.0.0-alpha"


USER_AGENT = f"Popularr/{get_version()} ( https://github.com/M0VENTURA/Popularr )"

MUSICBRAINZ_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

def _is_valid_mbid(mbid: str) -> bool:
    return bool(mbid) and bool(MUSICBRAINZ_UUID_RE.match(str(mbid).strip()))

try:
    _rate_limiter = get_rate_limiter()
except Exception:
    _rate_limiter = None


def escape_lucene_special_chars(text: str) -> str:
    special_chars = ['+', '-', '!', '(', ')', '{', '}', '[', ']', '^', '"', '~', '*', '?', ':', '\\', '/']
    escaped = (text or "").replace('\\', '\\\\')
    for char in special_chars:
        if char != '\\':
            escaped = escaped.replace(char, '\\' + char)
    return escaped


_THROTTLE_LOCK = threading.Lock()
_LAST_MB_REQUEST_TIME = 0.0

_CIRCUIT_LOCK = threading.Lock()
_CIRCUIT_OPEN_UNTIL = 0.0

_RECORDING_INC_SUPERSET = "artist-credits+releases+release-groups+work-rels+recording-rels+artist-rels+genres+tags"
_ISRC_INC_SUPERSET = "artist-credits+releases+work-rels"
_RELEASE_INC_SUPERSET = "recordings+artist-credits+media+release-groups+labels+work-rels+recording-rels+genres"


class _LruCache:
    """Thread-safe, size-bounded LRU cache using fast JSON serialization instead of deepcopy."""

    def __init__(self, max_size: int):
        self._data: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()
        self._max_size = max_size

    def get(self, key: str) -> Any:
        with self._lock:
            if key not in self._data:
                return None
            self._data.move_to_end(key)
            return json.loads(self._data[key])

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = json.dumps(value)
            while len(self._data) > self._max_size:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_RECORDING_DETAIL_CACHE = _LruCache(max_size=4000)
_ISRC_LOOKUP_CACHE = _LruCache(max_size=2000)
_RELEASE_DETAIL_CACHE = _LruCache(max_size=2000)


def _strict_throttle() -> None:
    global _LAST_MB_REQUEST_TIME

    if _rate_limiter:
        try:
            _rate_limiter.throttle_musicbrainz()
            return
        except Exception as exc:
            logger.debug("External MusicBrainz rate limiter failed, using local fallback", error=str(exc))

    sleep_time = 0.0
    with _THROTTLE_LOCK:
        now = time.monotonic()
        elapsed = now - _LAST_MB_REQUEST_TIME
        if elapsed < 1.2:
            sleep_time = 1.2 - elapsed
            _LAST_MB_REQUEST_TIME = now + sleep_time
        else:
            _LAST_MB_REQUEST_TIME = now
            
    if sleep_time > 0:
        time.sleep(sleep_time)


class MusicBrainzHttpClient:
    def __init__(self, http_session: Any = None, enabled: bool = True):
        self.session = http_session or session
        self.enabled = enabled
        self.base_url = "https://musicbrainz.org/ws/2/"
        self.headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}

    def clear_caches(self) -> None:
        _RECORDING_DETAIL_CACHE.clear()
        _ISRC_LOOKUP_CACHE.clear()
        _RELEASE_DETAIL_CACHE.clear()

    def is_available(self) -> bool:
        return time.monotonic() >= _CIRCUIT_OPEN_UNTIL

    def get(self, endpoint: str, *, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        global _CIRCUIT_OPEN_UNTIL

        if not self.enabled:
            return {}
            
        with _CIRCUIT_LOCK:
            if time.monotonic() < _CIRCUIT_OPEN_UNTIL:
                logger.warning("Circuit breaker open: dropping request to MusicBrainz", endpoint=endpoint)
                return {}

        url = f"{self.base_url}{endpoint.lstrip('/')}"
        query_params = params or {}

        max_retries = 3
        for attempt in range(max_retries):
            try:
                _strict_throttle()
                response = self.session.get(
                    url,
                    params=query_params,
                    headers=self.headers,
                    timeout=timeout,
                )
                response.raise_for_status()
                payload = response.json()
                return payload if isinstance(payload, dict) else {}
                
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (502, 503, 504):
                    if attempt < max_retries - 1:
                        backoff = 2 ** (attempt + 1)
                        logger.debug("MusicBrainz 5xx received, retrying...", endpoint=endpoint, backoff=backoff)
                        time.sleep(backoff)
                        continue
                    else:
                        with _CIRCUIT_LOCK:
                            _CIRCUIT_OPEN_UNTIL = time.monotonic() + 60.0
                        logger.warning(
                            "MusicBrainz overloaded (5xx). Circuit breaker open for 60s.",
                            endpoint=endpoint,
                            status_code=exc.response.status_code,
                        )
                        return {}
                    
                if exc.response.status_code in (400, 404):
                    logger.debug(
                        "MusicBrainz request not found (permanent)",
                        endpoint=endpoint,
                        status_code=exc.response.status_code,
                        error=str(exc),
                    )
                else:
                    logger.warning(
                        "MusicBrainz request failed permanently",
                        endpoint=endpoint,
                        status_code=exc.response.status_code,
                        error=str(exc),
                    )
                return {}
            except Exception as exc:
                if attempt < max_retries - 1:
                    time.sleep(2 ** (attempt + 1))
                    continue
                logger.warning("MusicBrainz request failed permanently after retries", endpoint=endpoint, error=str(exc))
                return {}
        return {}

    def search_release_groups(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        payload = self.get("release-group/", params={"query": query, "fmt": "json", "limit": max(1, min(limit, 100))})
        return payload.get("release-groups", []) if isinstance(payload.get("release-groups"), list) else []

    def search_releases(self, query: str, limit: int = 10, inc: str = "") -> list[dict[str, Any]]:
        params = {"query": query, "fmt": "json", "limit": max(1, min(limit, 100))}
        if inc:
            params["inc"] = inc
        payload = self.get("release/", params=params)
        return payload.get("releases", []) if isinstance(payload.get("releases"), list) else []

    def search_recordings(self, query: str, limit: int = 10, inc: str = "") -> list[dict[str, Any]]:
        params = {"query": query, "fmt": "json", "limit": max(1, min(limit, 100))}
        if inc:
            params["inc"] = inc
        payload = self.get("recording/", params=params)
        return payload.get("recordings", []) if isinstance(payload.get("recordings"), list) else []

    def search_artists(self, query: str, limit: int = 10, inc: str = "") -> list[dict[str, Any]]:
        params = {"query": query, "fmt": "json", "limit": max(1, min(limit, 100))}
        if inc:
            params["inc"] = inc
        payload = self.get("artist/", params=params)
        return payload.get("artists", []) if isinstance(payload.get("artists"), list) else []

    def get_artist_country(self, artist: str) -> str:
        if not self.enabled or not artist:
            return ""
        try:
            results = self.search_artists(
                f'artist:"{escape_lucene_special_chars(artist)}"',
                limit=1,
                inc="area",
            )
            if not results:
                return ""
            data = results[0]
            return (
                (data.get("area") or {}).get("name")
                or (data.get("begin-area") or {}).get("name")
                or ""
            )
        except Exception:
            return ""

    def get_release(self, release_mbid: str, inc: str | None = None, timeout: float = 30.0) -> dict[str, Any]:
        if not _is_valid_mbid(release_mbid):
            return {}

        actual_inc = _RELEASE_INC_SUPERSET if inc is None else inc
        cache_key = f"{release_mbid}::{actual_inc}"

        cached = _RELEASE_DETAIL_CACHE.get(cache_key)
        if cached is not None:
            return cached

        params = {"fmt": "json"}
        if actual_inc:
            params["inc"] = actual_inc
            
        data = self.get(f"release/{release_mbid}", params=params, timeout=timeout)
        if data:
            _RELEASE_DETAIL_CACHE.set(cache_key, data)
        return data

    def get_release_group(self, release_group_mbid: str, inc: str = "", timeout: float = 30.0) -> dict[str, Any]:
        if not _is_valid_mbid(release_group_mbid):
            return {}
        params: dict[str, Any] = {"fmt": "json"}
        if inc:
            params["inc"] = inc
        return self.get(f"release-group/{release_group_mbid}", params=params, timeout=timeout)

    def get_recording(self, recording_mbid: str, inc: str | None = None, timeout: float = 30.0) -> dict[str, Any]:
        if not _is_valid_mbid(recording_mbid):
            return {}

        actual_inc = _RECORDING_INC_SUPERSET if inc is None else inc
        cache_key = f"{recording_mbid}::{actual_inc}"

        cached = _RECORDING_DETAIL_CACHE.get(cache_key)
        if cached is not None:
            return cached

        params = {"fmt": "json"}
        if actual_inc:
            params["inc"] = actual_inc
            
        data = self.get(f"recording/{recording_mbid}", params=params, timeout=timeout)
        if data:
            _RECORDING_DETAIL_CACHE.set(cache_key, data)
        return data

    def get_recordings_bulk(self, recording_mbids: list[str], inc: str = "", timeout: float = 30.0) -> dict[str, Any]:
        valid_ids = [m for m in recording_mbids if _is_valid_mbid(m)]
        if not valid_ids:
            return {}

        mbid_string = " OR ".join(valid_ids)
        params = {
            "fmt": "json", 
            "query": f"rid:({mbid_string})", 
            "limit": min(100, len(valid_ids))
        }
        
        if inc:
            params["inc"] = inc

        data = self.get("recording/", params=params, timeout=timeout)
        return {"recordings": data.get("recordings", []) if data else []}

    def get_artist(self, artist_mbid: str, inc: str = "", timeout: float = 30.0) -> dict[str, Any]:
        if not _is_valid_mbid(artist_mbid):
            return {}
        params = {"fmt": "json"}
        if inc:
            params["inc"] = inc
        return self.get(f"artist/{artist_mbid}", params=params, timeout=timeout)

    def get_artist_members(self, artist_mbid: str, timeout: float = 30.0) -> list[dict[str, Any]]:
        if not self.enabled or not artist_mbid:
            return []
        try:
            data = self.get_artist(artist_mbid, inc="artist-rels", timeout=timeout)
            relations = data.get("relations", [])
            members = []
            allowed_types = {"member of band", "member", "has member", "founder", "co-founder"}
            for rel in relations:
                rtype = rel.get("type", "").lower()
                if rtype not in allowed_types:
                    continue
                target = rel.get("artist", {})
                if not target or not target.get("name"):
                    continue
                members.append({
                    "name": target.get("name"),
                    "relation_type": rtype,
                    "begin": rel.get("begin", ""),
                    "end": rel.get("end", ""),
                    "ended": rel.get("ended", False),
                    "attributes": rel.get("attributes", []),
                })
            return members
        except Exception:
            return []

    def browse_releases_for_group(self, release_group_mbid: str, inc: str = "media", limit: int = 50) -> list[dict[str, Any]]:
        payload = self.get("release", params={"fmt": "json", "release-group": release_group_mbid, "inc": inc, "limit": limit})
        return payload.get("releases", []) if isinstance(payload.get("releases"), list) else []

    def browse_work_recordings(self, work_mbid: str, inc: str = "artist-credits", limit: int = 100) -> list[dict[str, Any]]:
        if not self.enabled or not work_mbid:
            return []
        params = {"fmt": "json", "work": work_mbid, "limit": min(limit, 100)}
        if inc:
            params["inc"] = inc
        payload = self.get("recording", params=params)
        return payload.get("recordings", []) if isinstance(payload.get("recordings"), list) else []

    def browse_artist_releases(self, artist_mbid: str, inc: str = "", limit: int = 25, offset: int = 0) -> dict[str, Any]:
        params = {"fmt": "json", "artist": artist_mbid, "limit": min(limit, 100), "offset": offset}
        if inc:
            params["inc"] = inc
        payload = self.get("release", params=params)
        return {
            "releases": payload.get("releases", []) or [],
            "release_count": payload.get("release-count", 0) or 0,
            "release_offset": payload.get("release-offset", offset) or offset,
        }

    def browse_artist_release_groups(self, artist_mbid: str, inc: str = "", limit: int = 25, offset: int = 0) -> dict[str, Any]:
        params = {"fmt": "json", "artist": artist_mbid, "limit": min(limit, 100), "offset": offset}
        if inc:
            params["inc"] = inc
        payload = self.get("release-group", params=params)
        return {
            "release_groups": payload.get("release-groups", []) or [],
            "release_group_count": payload.get("release-group-count", 0) or 0,
            "release_group_offset": payload.get("release-group-offset", offset) or offset,
        }

    def lookup_by_isrc(self, isrc: str, inc: str = "") -> list[dict[str, Any]]:
        if not isrc:
            return []
            
        clean_isrc = str(isrc).strip().strip("{}[]").upper()
        if not clean_isrc:
            return []

        cache_key = clean_isrc
        cached = _ISRC_LOOKUP_CACHE.get(cache_key)
        if cached is not None:
            return cached

        params = {"fmt": "json", "inc": _ISRC_INC_SUPERSET}
        
        payload = self.get(f"isrc/{clean_isrc}", params=params)
        recordings = payload.get("recordings", []) if isinstance(payload.get("recordings"), list) else []
        
        _ISRC_LOOKUP_CACHE.set(cache_key, recordings)
        return recordings

    def search_recordings_with_genres(self, query: str, limit: int = 25) -> list[dict[str, Any]]:
        return self.search_recordings(query, limit=limit, inc="genres")
