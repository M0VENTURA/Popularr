"""Low-level slskd HTTP client.

Owns raw slskd request mechanics only. Search/result interpretation,
quality filtering, and queue/download workflows live in services.downloads.

API reference: https://slskd-api.readthedocs.io/en/latest/api.html
Endpoints verified directly against slskd's controller source:
https://github.com/slskd/slskd/blob/master/src/slskd/Search/API/Controllers/SearchesController.cs
https://github.com/slskd/slskd/blob/master/src/slskd/Transfers/API/Controllers/TransfersController.cs

IMPORTANT: the real slskd Transfers API requires BOTH `username` and the
transfer `id` (a server-assigned GUID, not the filename) to address a
specific download.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import structlog

from api_clients import session

logger = structlog.get_logger(__name__)


class SlskdHttpClient:
    """Raw slskd API wrapper with extended search and transfer management."""

    def __init__(
        self,
        web_url: str,
        api_key: str = "",
        http_session: Any = None,
        enabled: bool = True,
        default_timeout: int | None = 60
    ):
        self.web_url = (web_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.session = http_session or session
        self.enabled = enabled
        self.default_timeout = default_timeout
        self.base_url = f"{self.web_url}/api/v0"
        self.headers = {"X-API-Key": self.api_key} if self.api_key else {}

    # ------------------------------------------------------------------
    # Core request helpers
    # ------------------------------------------------------------------
    def request(self, method: str, endpoint: str, *, timeout: int | None = None, **kwargs: Any) -> Any:
        if not self.enabled:
            raise RuntimeError("slskd client is disabled")
        timeout = timeout or self.default_timeout
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        return self.session.request(method, url, headers=self.headers, timeout=timeout, **kwargs)

    def get_json(self, endpoint: str, *, timeout: int | None = None, default: Any = None, **kwargs: Any) -> Any:
        resp = self.request("GET", endpoint, timeout=timeout, **kwargs)
        resp.raise_for_status()
        payload = resp.json()
        return payload if payload is not None else default

    def post_json(self, endpoint: str, payload: Any, *, timeout: int | None = None) -> Any:
        return self.request("POST", endpoint, json=payload, timeout=timeout)

    def put(self, endpoint: str, *, timeout: int | None = None, **kwargs: Any) -> Any:
        return self.request("PUT", endpoint, timeout=timeout, **kwargs)

    def delete(self, endpoint: str, *, timeout: int | None = None, **kwargs: Any) -> Any:
        return self.request("DELETE", endpoint, timeout=timeout, **kwargs)

    # ------------------------------------------------------------------
    # Search management
    # ------------------------------------------------------------------
    def start_search(self, query: str, timeout: int = 20) -> str | None:
        """Start a new Soulseek search. Returns the search ID."""
        search_id = str(uuid.uuid4())
        body = {"id": search_id, "searchText": query}

        for attempt in range(2):
            try:
                resp = self.post_json("searches", body, timeout=timeout)
                if getattr(resp, "status_code", None) == 429 and attempt == 0:
                    logger.debug("slskd search start rate-limited (concurrent search in progress), retrying once", query=query)
                    continue
                resp.raise_for_status()
                data = resp.json() if hasattr(resp, "json") else resp
                returned_id = (data if isinstance(data, str) else data.get("id") or data.get("searchId")) or None
                return returned_id or search_id
            except Exception as exc:
                logger.debug("Failed to start search", query=query, error=str(exc))
                return None
        return None

    def list_searches(self, timeout: int = 8) -> list[dict[str, Any]]:
        """List all searches and their states."""
        try:
            return self.get_json("searches", timeout=timeout, default=[])
        except Exception as exc:
            logger.debug("Failed to list searches", error=str(exc))
            return []

    def get_search_state(self, search_id: str, include_responses: bool = False, timeout: int = 10) -> dict[str, Any]:
        """Get the state of a search (GET /searches/{id})."""
        try:
            return self.get_json(
                f"searches/{search_id}",
                timeout=timeout,
                default={},
                params={"includeResponses": str(include_responses).lower()},
            )
        except Exception as exc:
            logger.debug("Failed to get search state", search_id=search_id, error=str(exc))
            return {}

    def get_search_responses(self, search_id: str, timeout: int = 10) -> list[dict[str, Any]]:
        """Get responses (peer results) for a running or completed search.

        GET /searches/{id}/responses. Essential for polling results incrementally
        and breaking out of search loops early once a qualifying track is found.
        """
        try:
            return self.get_json(f"searches/{search_id}/responses", timeout=timeout, default=[])
        except Exception as exc:
            logger.debug("Failed to get search responses", search_id=search_id, error=str(exc))
            return []

    def get_search_results(self, search_id: str, timeout: int = 10) -> list[dict[str, Any]]:
        """Get results (responses) for a completed search."""
        return self.get_search_responses(search_id, timeout=timeout)

    def stop_search(self, search_id: str) -> bool:
        """Stop a running search (PUT /searches/{id})."""
        try:
            resp = self.put(f"searches/{search_id}")
            return resp.status_code in (200, 204, 304)
        except Exception as exc:
            logger.debug("Failed to stop search", search_id=search_id, error=str(exc))
            return False

    def delete_search(self, search_id: str) -> bool:
        """Delete a search and its results."""
        try:
            resp = self.delete(f"searches/{search_id}")
            return resp.status_code in (200, 204, 404)
        except Exception as exc:
            logger.debug("Failed to delete search", search_id=search_id, error=str(exc))
            return False

    # ------------------------------------------------------------------
    # Transfer management
    # ------------------------------------------------------------------
    def enqueue_downloads(
        self,
        username: str,
        files: list[dict[str, Any]],
        timeout: int = 15,
        raise_on_error: bool = False,
    ) -> list[str]:
        """Queue one or more files for download from a user."""
        if not username or not files:
            return []
        try:
            resp = self.post_json(f"transfers/downloads/{username}", list(files), timeout=timeout)
            resp.raise_for_status()
            if not getattr(resp, "content", None):
                return []
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.debug("Failed to enqueue download(s)", username=username, file_count=len(files), error=str(exc))
            if raise_on_error:
                raise
            return []

    def enqueue_download(self, username: str, filename: str, size: int = 0, timeout: int = 15) -> list[str]:
        """Convenience wrapper for enqueue_downloads() for a single file."""
        if not size:
            logger.warning("enqueue_download called without a file size", username=username, filename=filename)
        return self.enqueue_downloads(username, [{"filename": filename, "size": size}], timeout=timeout)

    def get_all_downloads(self, include_removed: bool = False, timeout: int = 10) -> list[dict[str, Any]]:
        """Get all downloads, grouped by user and directory."""
        try:
            return self.get_json(
                "transfers/downloads",
                timeout=timeout,
                default=[],
                params={"includeRemoved": str(include_removed).lower()},
            )
        except Exception as exc:
            logger.debug("Failed to get all downloads", error=str(exc))
            return []

    def get_active_downloads(self, timeout: int = 10) -> list[dict[str, Any]]:
        """Alias for get_all_downloads()."""
        return self.get_all_downloads(timeout=timeout)

    def get_downloads_for_user(self, username: str, timeout: int = 10) -> dict[str, Any]:
        """Get all downloads for a specific user."""
        if not username:
            return {}
        try:
            return self.get_json(f"transfers/downloads/{username}", timeout=timeout, default={})
        except Exception as exc:
            logger.debug("Failed to get downloads for user", username=username, error=str(exc))
            return {}

    def retry_download(self, username: str, transfer_id: str, timeout: int = 10) -> bool:
        """Retry a failed download."""
        try:
            resp = self.post_json(
                f"transfers/downloads/{username}/{transfer_id}/retry",
                {},
                timeout=timeout,
            )
            return resp.status_code in (200, 201, 204) if hasattr(resp, "status_code") else True
        except Exception as exc:
            logger.debug("Failed to retry download", username=username, transfer_id=transfer_id, error=str(exc))
            return False

    def get_download(self, username: str, transfer_id: str, timeout: int = 10) -> dict[str, Any]:
        """Get details for a specific download."""
        if not username or not transfer_id:
            return {}
        try:
            return self.get_json(f"transfers/downloads/{username}/{transfer_id}", timeout=timeout, default={})
        except Exception as exc:
            logger.debug("Failed to get download", username=username, transfer_id=transfer_id, error=str(exc))
            return {}

    def cancel_download(self, username: str, transfer_id: str, remove: bool = False, timeout: int = 10) -> bool:
        """Cancel (and optionally remove) a specific download."""
        if not username or not transfer_id:
            logger.debug("cancel_download requires both username and transfer_id", username=username, transfer_id=transfer_id)
            return False
        try:
            resp = self.delete(
                f"transfers/downloads/{username}/{transfer_id}",
                timeout=timeout,
                params={"remove": str(remove).lower()},
            )
            return resp.status_code in (200, 204)
        except Exception as exc:
            logger.debug("Failed to cancel download", username=username, transfer_id=transfer_id, error=str(exc))
            return False

    def get_events(self, timeout: int = 10) -> list[dict[str, Any]]:
        """Get recent slskd events."""
        try:
            return self.get_json("events", timeout=timeout, default=[])
        except Exception as exc:
            logger.debug("Failed to get events", error=str(exc))
            return []

    def remove_completed_downloads(self, timeout: int = 10) -> bool:
        """Remove all completed downloads from the queue."""
        try:
            resp = self.delete("transfers/downloads/all/completed", timeout=timeout)
            return resp.status_code in (200, 204)
        except Exception as exc:
            logger.debug("Failed to remove completed downloads", error=str(exc))
            return False

    def get_queue_position(self, username: str, transfer_id: str, timeout: int = 10) -> int | None:
        """Get the queue position for a queued download."""
        if not username or not transfer_id:
            return None
        try:
            data = self.get_json(f"transfers/downloads/{username}/{transfer_id}/position", timeout=timeout, default={})
            return data.get("position") if isinstance(data, dict) else None
        except Exception as exc:
            logger.debug("Failed to get queue position", username=username, transfer_id=transfer_id, error=str(exc))
            return None

    def cancel_all_stalled_downloads(self, timeout: int = 10) -> int:
        """Cancel downloads that are stuck or errored out across all users."""
        cancelled_count = 0
        downloads = self.get_all_downloads(include_removed=False, timeout=timeout)
        
        for user_transfer in downloads:
            username = user_transfer.get("username")
            for directory in user_transfer.get("directories", []):
                for file_rec in directory.get("files", []):
                    status = file_rec.get("status", "").lower()
                    transfer_id = file_rec.get("id")
                    
                    if status in ("cancelled", "errored", "rejected", "timeout") and transfer_id and username:
                        if self.cancel_download(username, transfer_id, remove=True):
                            cancelled_count += 1
                            
        return cancelled_count

    # ------------------------------------------------------------------
    # User operations
    # ------------------------------------------------------------------
    def browse_user(self, username: str) -> dict[str, Any]:
        """Browse a user's shared files."""
        try:
            return self.get_json(f"users/{username}/browse", default={}, timeout=30)
        except Exception as exc:
            logger.debug("Failed to browse user", username=username, error=str(exc))
            return {}

    def get_user_info(self, username: str) -> dict[str, Any]:
        """Get user info."""
        try:
            return self.get_json(f"users/{username}/info", default={})
        except Exception as exc:
            logger.debug("Failed to get user info", username=username, error=str(exc))
            return {}

    def get_user_status(self, username: str) -> dict[str, Any]:
        """Get user online/away status."""
        try:
            return self.get_json(f"users/{username}/status", default={})
        except Exception as exc:
            logger.debug("Failed to get user status", username=username, error=str(exc))
            return {}

    # ------------------------------------------------------------------
    # Application & session helpers
    # ------------------------------------------------------------------
    def get_state(self) -> dict[str, Any]:
        """Get the full slskd application state."""
        try:
            return self.get_json("application/state", default={})
        except Exception as exc:
            logger.debug("Failed to get application state", error=str(exc))
            return {}

    def get_server_state(self) -> dict[str, Any]:
        """Return the slskd server sub-state across slskd versions."""
        state = self.get_state()
        if isinstance(state, dict):
            server = state.get("server")
            if isinstance(server, dict) and server:
                return server
            if state.get("state") or state.get("isConnected") is not None:
                return state
        for endpoint in ("server", "application"):
            try:
                data = self.get_json(endpoint, default={})
            except Exception as exc:
                logger.debug("Failed to get slskd server state", endpoint=endpoint, error=str(exc))
                continue
            if not isinstance(data, dict):
                continue
            if endpoint == "application":
                data = data.get("server")
            if isinstance(data, dict) and data:
                return data
        return {}

    def is_connected(self) -> bool:
        """Return True when the Soulseek client is connected and logged in."""
        server = self.get_server_state()
        if not isinstance(server, dict) or not server:
            return False
        if "isConnected" in server:
            return bool(server["isConnected"])
        if "isLoggedIn" in server:
            return bool(server["isLoggedIn"])
        raw = str(server.get("state") or server.get("status") or "").strip().lower()
        if not raw:
            return False
        flags = {f.strip() for f in raw.split(",") if f.strip()}
        return bool(flags.intersection({"connected", "loggedin"}))


# =============================================================================
# Client factory (cached)
# =============================================================================
_slskd_client_cache: SlskdHttpClient | None = None
_slskd_client_cache_mtime: float | None = None


def _config_file_mtime() -> float | None:
    try:
        cfg_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
        if os.path.exists(cfg_path):
            return os.path.getmtime(cfg_path)
    except Exception:
        pass
    return None


def get_slskd_client() -> SlskdHttpClient | None:
    global _slskd_client_cache, _slskd_client_cache_mtime

    if _slskd_client_cache is not None:
        if _config_file_mtime() != _slskd_client_cache_mtime:
            _slskd_client_cache = None
            _slskd_client_cache_mtime = None
        else:
            return _slskd_client_cache

    try:
        from helpers.config_helpers import get_slskd_config
        cfg = get_slskd_config()
        if not cfg.get("enabled"):
            logger.warning("Soulseek (slskd) is not enabled in config")
            return None

        web_url = str(cfg.get("web_url") or "").strip() or "http://localhost:5030"
        api_key = str(cfg.get("api_key") or "").strip()

        import httpx as _httpx
        _plain_session = _httpx.Client(timeout=60.0)
        _slskd_client_cache = SlskdHttpClient(
            web_url=web_url,
            api_key=api_key,
            http_session=_plain_session,
            enabled=True,
        )
        _slskd_client_cache_mtime = _config_file_mtime()
        return _slskd_client_cache

    except Exception as exc:
        logger.error("Error getting SlskdHttpClient", error=str(exc))
        return None
