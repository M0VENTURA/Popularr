"""Low-level Discogs HTTP client.

This module is intentionally HTTP-focused. It owns:
- Discogs request throttling (Thread-safe)
- 429 Retry-After handling
- Temporary 5xx retry/circuit-breaker behaviour
- Raw Discogs endpoint calls
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

import structlog

from api_clients import session as shared_session, timeout_safe_session
from api_clients.http_utils import create_retry_client
from services.infrastructure.api_rate_limiter import get_rate_limiter

logger = structlog.get_logger(__name__)

try:
    _rate_limiter = get_rate_limiter()
except Exception:
    _rate_limiter = None

DISCOGS_BASE_URL = "https://api.discogs.com"
DEFAULT_USER_AGENT = "Popularr/1.0 +https://github.com/M0VENTURA/Popularr"

# Rate Limit & Circuit Breaker Globals
_DISCOGS_LAST_REQUEST_TIME = 0.0
_DISCOGS_MIN_INTERVAL = 1.0  # Strict 1 req/sec for Discogs to avoid 429 bans
_DISCOGS_RATE_LIMIT_UNTIL = 0.0
_DISCOGS_CIRCUIT_BREAKER_OPEN = False
_DISCOGS_CIRCUIT_BREAKER_RESET_TIME = 0.0
_DISCOGS_CONSECUTIVE_ERRORS = 0

_DISCOGS_THROTTLE_LOCK = threading.Lock()
_DISCOGS_MAX_COOLDOWN = 60.0
_DISCOGS_REQUEST_BUDGET_SECONDS = 30.0

#: How many same-named candidates the album-overlap rule may score when
#: resolving an artist id automatically. Each candidate costs ONE
#: ``/artists/{id}/releases`` request at the 1 req/sec Discogs budget, so this
#: cap is what keeps a shared name from turning a lookup into a crawl.
_MAX_ALBUM_SCORED_CANDIDATES = 3


def _normalize_artist_name(value: str) -> str:
    """Compare-friendly artist name: casefolded, punctuation-free, no "the".

    Mirrors the normalisation the rest of the app uses for artist keys, so
    "The Beatles" / "Beatles, The" / "beatles" compare equal here too.
    """
    text = str(value or "").casefold().strip()
    text = re.sub(r"[^0-9a-z]+", " ", text)
    text = re.sub(r"\bthe\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_release_title(value: str) -> str:
    """Compare-friendly release title for the album-overlap rule.

    Discogs release titles carry format/pressing suffixes the library titles
    do not ("Album, LP, Reissue"), so the comparison is on the leading title
    text before any bracketed or dash-separated qualifier.
    """
    text = str(value or "").casefold().strip()
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\s+-\s+.*$", " ", text)
    text = re.sub(r"[^0-9a-z]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def build_discogs_session() -> Any:
    return create_retry_client(
        user_agent=DEFAULT_USER_AGENT,
        retries=3,
        backoff=1.0,
        status_forcelist=(500, 502, 503, 504),
    )


def get_retry_after_seconds(response: Any, default: float = 60.0) -> float:
    retry_after_raw = response.headers.get("Retry-After") if response is not None else None
    try:
        retry_after = float(retry_after_raw) if retry_after_raw is not None else float(default)
    except (TypeError, ValueError):
        retry_after = float(default)
    return max(1.0, retry_after)


def _set_rate_limit_window(wait_seconds: float) -> None:
    global _DISCOGS_RATE_LIMIT_UNTIL
    with _DISCOGS_THROTTLE_LOCK:
        capped = min(max(0.0, wait_seconds), _DISCOGS_MAX_COOLDOWN)
        _DISCOGS_RATE_LIMIT_UNTIL = max(_DISCOGS_RATE_LIMIT_UNTIL, time.time() + capped)


def throttle_discogs() -> None:
    """Respect Discogs request pacing and any active 429 cooldown without blocking locks."""
    global _DISCOGS_LAST_REQUEST_TIME

    # Deployment-wide budget FIRST: the app and the queue worker are separate
    # processes, so the module-local lock below alone enforced 1 req/s per
    # PROCESS (2+ rps combined) — the burst that Discogs answers with read
    # timeouts.  The shared limiter makes the reservation cross-process, with
    # the local lock kept as fallback when the limiter is unavailable.
    if _rate_limiter:
        try:
            _rate_limiter.throttle_discogs()
            return
        except Exception as exc:
            logger.debug(
                "External Discogs rate limiter failed, using local fallback",
                error=str(exc),
            )

    sleep_time = 0.0
    with _DISCOGS_THROTTLE_LOCK:
        now = time.time()
        cooldown_wait = _DISCOGS_RATE_LIMIT_UNTIL - now
        elapsed = now - _DISCOGS_LAST_REQUEST_TIME
        min_wait = _DISCOGS_MIN_INTERVAL - elapsed
        
        wait = max(cooldown_wait, min_wait, 0.0)
        if wait > 0:
            sleep_time = wait
            _DISCOGS_LAST_REQUEST_TIME = now + sleep_time
        else:
            _DISCOGS_LAST_REQUEST_TIME = now

    if sleep_time > 0:
        time.sleep(sleep_time)


def check_circuit_breaker() -> bool:
    global _DISCOGS_CIRCUIT_BREAKER_OPEN, _DISCOGS_CIRCUIT_BREAKER_RESET_TIME
    with _DISCOGS_THROTTLE_LOCK:
        if _DISCOGS_CIRCUIT_BREAKER_OPEN:
            if time.time() < _DISCOGS_CIRCUIT_BREAKER_RESET_TIME:
                return False
            _DISCOGS_CIRCUIT_BREAKER_OPEN = False
            logger.warning("Discogs circuit breaker reset - resuming requests")
    return True


def record_discogs_error(error_type: str) -> None:
    global _DISCOGS_CIRCUIT_BREAKER_OPEN, _DISCOGS_CIRCUIT_BREAKER_RESET_TIME, _DISCOGS_CONSECUTIVE_ERRORS
    with _DISCOGS_THROTTLE_LOCK:
        _DISCOGS_CONSECUTIVE_ERRORS += 1
        if _DISCOGS_CONSECUTIVE_ERRORS >= 5 and error_type in {"502", "503"}:
            _DISCOGS_CIRCUIT_BREAKER_OPEN = True
            _DISCOGS_CIRCUIT_BREAKER_RESET_TIME = time.time() + 300
            logger.error("Discogs circuit breaker OPEN", reason=f"Repeated {error_type} errors")


def clear_discogs_errors() -> None:
    global _DISCOGS_CONSECUTIVE_ERRORS
    with _DISCOGS_THROTTLE_LOCK:
        _DISCOGS_CONSECUTIVE_ERRORS = 0


class DiscogsHttpClient:
    def __init__(self, token: str, http_session: Any = None, enabled: bool = True, user_agent: str = DEFAULT_USER_AGENT):
        self.token = token or ""
        self.enabled = enabled
        self.base_url = DISCOGS_BASE_URL
        self.user_agent = user_agent or DEFAULT_USER_AGENT

        if http_session is None or http_session is shared_session or http_session is timeout_safe_session:
            self.session = build_discogs_session()
        else:
            self.session = http_session

        self.headers = {
            "Authorization": f"Discogs token={self.token}" if self.token else "",
            "User-Agent": self.user_agent,
        }

    def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float = 10.0,
        max_retries: int = 2,
    ) -> dict[str, Any]:
        if not self.enabled or not self.token:
            return {}

        if not check_circuit_breaker():
            logger.debug("Discogs request skipped", reason="Circuit breaker is open")
            return {}

        url = path_or_url if path_or_url.startswith("http") else f"{self.base_url}{path_or_url}"
        retry_delay = 1.0
        _deadline = time.monotonic() + _DISCOGS_REQUEST_BUDGET_SECONDS

        for attempt in range(max_retries + 1):
            if time.monotonic() > _deadline:
                logger.warning("Discogs request budget exceeded — giving up", url=url)
                return {}
            
            throttle_discogs()
            
            try:
                response = self.session.request(method, url, headers=self.headers, params=params, timeout=timeout)

                if response.status_code == 429:
                    wait_seconds = min(get_retry_after_seconds(response), _DISCOGS_MAX_COOLDOWN)
                    _set_rate_limit_window(wait_seconds)
                    logger.warning("Discogs rate limited", wait_seconds=int(wait_seconds))
                    if time.monotonic() + min(wait_seconds, 1.0) > _deadline:
                        logger.warning("Discogs request budget exceeded during 429 cooldown", url=url)
                        return {}
                    time.sleep(wait_seconds)
                    continue

                if response.status_code in {502, 503}:
                    record_discogs_error(str(response.status_code))
                    if attempt < max_retries:
                        if time.monotonic() + retry_delay > _deadline:
                            logger.warning("Discogs request budget exceeded during backoff", url=url)
        search_artists(self, artist_name: str, limit: int = 10, timeout: float = 10.0) -> list[dict[str, Any]]:
        """Artist candidates for a name, in Discogs relevance order.

        Returns only entries that carry an id and a name, because every caller
        compares names — a nameless hit cannot be judged and must not be
        silently used as "the" artist.
        """
        if not artist_name:
            return []
        try:
            per_page = max(1, min(int(limit or 10), 25))
        except (TypeError, ValueError):
            per_page = 10
        results = self.search_database(
            {"q": artist_name, "type": "artist", "per_page": per_page},
            timeout=timeout,
        )
        candidates: list[dict[str, Any]] = []
        for item in results or []:
            if not isinstance(item, dict):
                continue
            artist_id = item.get("id")
            name = str(item.get("title") or "").strip()
            if not artist_id or not name:
                continue
            candidates.append({
                "id": str(artist_id),
                "name": name,
                "resource_url": str(item.get("resource_url") or ""),
                "thumb": str(item.get("thumb") or ""),
            })
        return candidates

    def matched_artist_albums(
        self,
        artist_id: str | int,
        albums: list[str] | None,
        timeout: float = 10.0,
    ) -> list[str]:
        """Which of the LIBRARY's album titles this Discogs artist also has.

        The discriminator the search endpoint needs: several Discogs artists
        can share a name, and the right one is the artist whose release list
        contains the albums the library actually holds. One request per
        candidate (``/artists/{id}/releases``), so callers cap how many
        candidates they score.
        """
        wanted = {_normalize_release_title(a) for a in (albums or []) if str(a or "").strip()}
        if not artist_id or not wanted:
            return []
        try:
            releases = self.get_artist_releases(artist_id, per_page=100, timeout=timeout)
        except Exception as exc:
            logger.debug("Discogs artist releases failed", artist_id=str(artist_id), error=str(exc))
            return []

        matched: list[str] = []
        seen: set[str] = set()
        for release in releases or []:
            if not isinstance(release, dict):
                continue
            title = _normalize_release_title(release.get("title") or "")
            if title and title in wanted and title not in seen:
                seen.add(title)
                matched.append(str(release.get("title") or "").strip())
        return matched

    def get_artist_id(
        self,
        artist_name: str,
        albums: list[str] | None = None,
        timeout: float = 10.0,
    ) -> int | str | None:
        """Resolve one artist name to a Discogs artist id.

        ⚠️ Taking ``results[0]`` (the old behaviour) is what matched the WRONG
        artist: Discogs ranks its search by its own relevance, so a name shared
        by several artists resolved to whichever Discogs listed first — not the
        one whose release list contains the albums this library holds.

        Resolution order, cheapest evidence first:

        1. a candidate whose name matches EXACTLY (normalised) is preferred
           over the rest — a tribute band or a different spelling loses here
           without costing a request;
        2. with several candidates left AND the caller supplying album titles,
           each candidate's release list is compared against those albums
           (capped at ``_MAX_ALBUM_SCORED_CANDIDATES`` requests) and the best
           overlap wins — this is the "same albums" rule;
        3. otherwise the first candidate stands, so behaviour is unchanged for
           a single unambiguous hit.
        """
        if not artist_name:
            return None
        candidates = self.search_artists(artist_name, limit=10, timeout=timeout)
        if not candidates:
            return None

        target = _normalize_artist_name(artist_name)
        exact = [c for c in candidates if _normalize_artist_name(c["name"]) == target]
        pool = exact or candidates
        if len(pool) == 1:
            return pool[0]["id"]

        if albums:
            best: dict[str, Any] | None = None
            best_count = 0
            for candidate in pool[:_MAX_ALBUM_SCORED_CANDIDATES]:
                matched = self.matched_artist_albums(candidate["id"], albums, timeout=timeout)
                if len(matched) > best_count:
                    best_count = len(matched)
                    best = candidate
            if best is not None:
                logger.debug(
                    "Discogs artist resolved by album overlap",
                    artist=artist_name,
                    discogs_id=best["id"],
                    discogs_name=best["name"],
                    matched_albums=best_count,
                )
                return best["id"]

        return pool[0]["id"] return {}

                response.raise_for_status()
                clear_discogs_errors()
                
                payload = response.json()
                return payload if isinstance(payload, dict) else {}
                
            except Exception as exc:
                if attempt < max_retries:
                    if time.monotonic() + retry_delay > _deadline:
                        logger.warning("Discogs request budget exceeded during exception backoff", url=url)
                        return {}
                    time.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                logger.debug("Discogs request failed permanently", url=url, error=str(exc))
                return {}
        return {}

    def search_database(self, params: dict[str, Any], timeout: float = 10.0) -> list[dict[str, Any]]:
        payload = self._request("GET", "/database/search", params=params, timeout=timeout)
        results = payload.get("results", [])
        return results if isinstance(results, list) else []

    def search_artists(self, artist_name: str, limit: int = 10, timeout: float = 10.0) -> list[dict[str, Any]]:
        """Artist candidates for a name, in Discogs relevance order.

        Returns only entries that carry an id and a name, because every caller
        compares names — a nameless hit cannot be judged and must not be
        silently used as "the" artist.
        """
        if not artist_name:
            return []
        try:
            per_page = max(1, min(int(limit or 10), 25))
        except (TypeError, ValueError):
            per_page = 10
        results = self.search_database(
            {"q": artist_name, "type": "artist", "per_page": per_page},
            timeout=timeout,
        )
        candidates: list[dict[str, Any]] = []
        for item in results or []:
            if not isinstance(item, dict):
                continue
            artist_id = item.get("id")
            name = str(item.get("title") or "").strip()
            if not artist_id or not name:
                continue
            candidates.append({
                "id": str(artist_id),
                "name": name,
                "resource_url": str(item.get("resource_url") or ""),
                "thumb": str(item.get("thumb") or ""),
            })
        return candidates

    def matched_artist_albums(
        self,
        artist_id: str | int,
        albums: list[str] | None,
        timeout: float = 10.0,
    ) -> list[str]:
        """Which of the LIBRARY's album titles this Discogs artist also has.

        The discriminator the search endpoint needs: several Discogs artists
        can share a name, and the right one is the artist whose release list
        contains the albums the library actually holds. One request per
        candidate (``/artists/{id}/releases``), so callers cap how many
        candidates they score.
        """
        wanted = {_normalize_release_title(a) for a in (albums or []) if str(a or "").strip()}
        if not artist_id or not wanted:
            return []
        try:
            releases = self.get_artist_releases(artist_id, per_page=100, timeout=timeout)
        except Exception as exc:
            logger.debug("Discogs artist releases failed", artist_id=str(artist_id), error=str(exc))
            return []

        matched: list[str] = []
        seen: set[str] = set()
        for release in releases or []:
            if not isinstance(release, dict):
                continue
            title = _normalize_release_title(release.get("title") or "")
            if title and title in wanted and title not in seen:
                seen.add(title)
                matched.append(str(release.get("title") or "").strip())
        return matched

    def get_artist_id(
        self,
        artist_name: str,
        albums: list[str] | None = None,
        timeout: float = 10.0,
    ) -> int | str | None:
        """Resolve one artist name to a Discogs artist id.

        ⚠️ Taking ``results[0]`` (the old behaviour) is what matched the WRONG
        artist: Discogs ranks its search by its own relevance, so a name shared
        by several artists resolved to whichever Discogs listed first — not the
        one whose release list contains the albums this library holds.

        Resolution order, cheapest evidence first:

        1. a candidate whose name matches EXACTLY (normalised) is preferred
           over the rest — a tribute band or a different spelling loses here
           without costing a request;
        2. with several candidates left AND the caller supplying album titles,
           each candidate's release list is compared against those albums
           (capped at ``_MAX_ALBUM_SCORED_CANDIDATES`` requests) and the best
           overlap wins — this is the "same albums" rule;
        3. otherwise the first candidate stands, so behaviour is unchanged for
           a single unambiguous hit.
        """
        if not artist_name:
            return None
        candidates = self.search_artists(artist_name, limit=10, timeout=timeout)
        if not candidates:
            return None

        target = _normalize_artist_name(artist_name)
        exact = [c for c in candidates if _normalize_artist_name(c["name"]) == target]
        pool = exact or candidates
        if len(pool) == 1:
            return pool[0]["id"]

        if albums:
            best: dict[str, Any] | None = None
            best_count = 0
            for candidate in pool[:_MAX_ALBUM_SCORED_CANDIDATES]:
                matched = self.matched_artist_albums(candidate["id"], albums, timeout=timeout)
                if len(matched) > best_count:
                    best_count = len(matched)
                    best = candidate
            if best is not None:
                logger.debug(
                    "Discogs artist resolved by album overlap",
                    artist=artist_name,
                    discogs_id=best["id"],
                    discogs_name=best["name"],
                    matched_albums=best_count,
                )
                return best["id"]

        return pool[0]["id"]

    def get_release(self, release_id: str | int, timeout: float = 10.0) -> dict[str, Any]:
        if not release_id:
            return {}
        return self._request("GET", f"/releases/{release_id}", timeout=timeout)

    def get_master(self, master_id: str | int, timeout: float = 10.0) -> dict[str, Any]:
        if not master_id:
            return {}
        return self._request("GET", f"/masters/{master_id}", timeout=timeout)

    def get_artist(self, artist_id: str | int, timeout: float = 10.0) -> dict[str, Any]:
        if not artist_id:
            return {}
        return self._request("GET", f"/artists/{artist_id}", timeout=timeout)

    def get_artist_releases(self, artist_id: str | int, per_page: int = 100, timeout: float = 10.0) -> list[dict[str, Any]]:
        if not artist_id:
            return []
        payload = self._request("GET", f"/artists/{artist_id}/releases", params={"per_page": per_page}, timeout=timeout)
        releases = payload.get("releases", [])
        return releases if isinstance(releases, list) else []

    def get_resource_url(self, url: str, timeout: float = 10.0) -> dict[str, Any]:
        if not url:
            return {}
        return self._request("GET", url, timeout=timeout)

    def _get_all_paginated(self, path: str, params: dict[str, Any] | None = None, max_pages: int = 10) -> list[dict[str, Any]]:
        all_items: list[dict[str, Any]] = []
        page = 1
        p_params = dict(params or {})
        p_params.setdefault("per_page", 100)

        while True:
            p_params["page"] = page
            payload = self._request("GET", path, params=p_params)
            if not payload:
                break

            items_key = None
            for key in ("releases", "items", "results", "versions"):
                if isinstance(payload.get(key), list):
                    items_key = key
                    break

            if items_key:
                all_items.extend(payload[items_key])

            pagination = payload.get("pagination", {}) or {}
            pages = pagination.get("pages", 1) or 1

            if page >= pages or (max_pages and page >= max_pages):
                break
            page += 1
        return all_items

    def get_artist_releases_all(self, artist_id: str | int, max_pages: int = 10) -> list[dict[str, Any]]:
        if not artist_id:
            return []
        return self._get_all_paginated(f"/artists/{artist_id}/releases", max_pages=max_pages)

    def get_label(self, label_id: str | int, timeout: float = 10.0) -> dict[str, Any]:
        if not label_id:
            return {}
        return self._request("GET", f"/labels/{label_id}", timeout=timeout)

    def get_label_releases(self, label_id: str | int, per_page: int = 100, timeout: float = 10.0) -> list[dict[str, Any]]:
        if not label_id:
            return []
        payload = self._request("GET", f"/labels/{label_id}/releases", params={"per_page": per_page}, timeout=timeout)
        releases = payload.get("releases", [])
        return releases if isinstance(releases, list) else []

    def get_label_releases_all(self, label_id: str | int, max_pages: int = 10) -> list[dict[str, Any]]:
        if not label_id:
            return []
        return self._get_all_paginated(f"/labels/{label_id}/releases", max_pages=max_pages)


def fetch_image_bytes(client_or_token: Any, image_url: str) -> bytes | None:
    try:
        if isinstance(client_or_token, DiscogsHttpClient):
            headers = client_or_token.headers
            session = client_or_token.session
        else:
            from api_clients.http_utils import create_retry_client
            session = create_retry_client()
            headers = {"Authorization": f"Discogs token={client_or_token}", "User-Agent": DEFAULT_USER_AGENT}

        response = session.get(image_url, headers=headers, timeout=15)
        if response.status_code == 200:
            return response.content
        return None
    except Exception as exc:
        logger.debug("Failed to fetch Discogs binary artwork", error=str(exc))
        return None
