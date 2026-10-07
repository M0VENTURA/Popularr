"""Low-level Discogs HTTP client.

This module is intentionally HTTP-focused. It owns:
- Discogs request throttling (Thread-safe)
- 429 Retry-After handling
- Temporary 5xx retry/circuit-breaker behaviour
- Raw Discogs endpoint calls
"""

from __future__ import annotations

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
                            return {}
                        time.sleep(retry_delay)
                        retry_delay *= 2
                        continue
                    return {}

                if response.status_code in {401, 403}:
                    logger.error("Discogs authentication/permission failure", status=response.status_code)
                    return {}

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

    def get_artist_id(self, artist_name: str, timeout: float = 10.0) -> int | str | None:
        if not artist_name:
            return None
        results = self.search_database({"q": artist_name, "type": "artist"}, timeout=timeout)
        if results and isinstance(results, list):
            first = results[0]
            if isinstance(first, dict):
                return first.get("id")
        return None

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
