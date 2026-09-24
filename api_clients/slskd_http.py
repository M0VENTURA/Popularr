"""Low-level slskd HTTP client.

Owns raw slskd request mechanics only. Search/result interpretation,
quality filtering, and queue/download workflows live in services.downloads.

API reference: https://slskd-api.readthedocs.io/en/latest/api.html
Endpoints verified directly against slskd's controller source:
https://github.com/slskd/slskd/blob/master/src/slskd/Search/API/Controllers/SearchesController.cs
https://github.com/slskd/slskd/blob/master/src/slskd/Transfers/API/Controllers/TransfersController.cs

IMPORTANT: the real slskd Transfers API requires BOTH `username` and the
transfer `id` (a server-assigned GUID, not the filename) to address a
specific download. Several methods below therefore now require a
`username` argument that earlier versions of this client omitted - those
earlier calls would have 404'd against a real slskd instance.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import structlog

from api_clients import session

logger = structlog.get_logger(__name__)


class SlskdHttpClient:
    """Raw slskd API wrapper."""

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
        """Start a new Soulseek search. Returns the search ID.

        The search id is generated client-side (matching the official
        slskd-python-api's behavior) rather than only relying on the
        server's response - the server accepts a client-supplied `id` and
        will generate its own only if one isn't provided. Generating it
        ourselves means we know the id immediately, even if the response
        body is ever malformed or unexpectedly empty.

        Note: slskd enforces a global (not per-client) limit of one
        concurrent "start search" request at a time and returns 429 if
        another is already in flight; a single retry is attempted for
        that specific case since it is expected/transient in a scanning
        workflow that issues many searches back-to-back.
        """
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

    def get_search_results(self, search_id: str, timeout: int = 10) -> list[dict[str, Any]]:
        """Get results (responses) for a completed search.

        Uses GET /searches/{id}/responses - the actual documented/
        implemented endpoint. An earlier version of this method called
        `/searches/{id}/results`, which does not exist in slskd and would
        404 on every call.
        """
        try:
            return self.get_json(f"searches/{search_id}/responses", timeout=timeout, default=[])
        except Exception as exc:
            logger.debug("Failed to get search results", search_id=search_id, error=str(exc))
            return []

    def stop_search(self, search_id: str) -> bool:
        """Stop a running search.

        Uses PUT /searches/{id} with no body - this is the real slskd
        route (confirmed against SearchesController.cs's `Cancel` action).
        Returns 200 if the search was stopped, or 304 if it was already
        not in progress; both are treated as "not an error" here. An
        earlier version of this method POSTed to a `/stop` sub-route that
        does not exist in slskd and would 404.
        """
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
            return resp.status_code in (200, 204)
        except Exception as exc:
            logger.debug("Failed to delete search", search_id=search_id, error=str(exc))
            return False

    # ------------------------------------------------------------------
    # Transfer management
    #
    # NOTE: every non-bulk endpoint below is scoped by BOTH `username` and
    # the transfer `id` (a server-assigned GUID) - this is a hard
    # requirement of slskd's real Transfers API, not an optional filter.
    # ------------------------------------------------------------------
    def enqueue_downloads(
        self,
        username: str,
        files: list[dict[str, Any]],
        timeout: int = 15,
        raise_on_error: bool = False,
    ) -> list[str]:
        """Queue one or more files for download from a user.

        POSTs to /transfers/downloads/{username} (username in the URL,
        confirmed against slskd's TransfersController) with a JSON body
        that is a LIST of {"filename": ..., "size": ...} objects, matching
        both the controller's `QueueDownloadRequest` shape and the
        official slskd-python-api's `enqueue(username, files)`.

        `size` is required - the Soulseek protocol needs the expected file
        size to request the file from the remote peer. Pass the exact
        `size`/`filename` values from the corresponding search result.

        Returns a list of transfer ids (as returned by newer slskd
        versions), or an empty list on older versions that return no body,
        or on failure.

        ⚠️ Because an empty list means BOTH "accepted, older slskd returned
        no body" and "the request failed", callers that must not treat a
        silent failure as success should pass ``raise_on_error=True`` and
        handle the exception. The default stays ``False`` so existing
        best-effort callers are unaffected.
        """
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
        """Convenience wrapper for enqueue_downloads() for a single file.

        `size` should always be supplied from the originating search
        result; omitting it (leaving the default of 0) may cause slskd/the
        remote peer to reject the request.
        """
        if not size:
            logger.warning("enqueue_download called without a file size; slskd requires size to request the file from the peer", username=username, filename=filename)
        return self.enqueue_downloads(username, [{"filename": filename, "size": size}], timeout=timeout)

    def get_all_downloads(self, include_removed: bool = False, timeout: int = 10) -> list[dict[str, Any]]:
        """Get all downloads, grouped by user and directory.

        Each element is a `Transfer` object: {"username": ..., "directories":
        [{"directory": ..., "files": [...]}]} - NOT a flat list of
        individual downloads. Callers must traverse directories/files to
        get individual transfer records (each of which carries its own
        `id`, needed for get_download/cancel_download/get_queue_position).
        """
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

    # Backwards-compatible alias; the name "active" was misleading since
    # this endpoint returns all downloads (including completed/failed
    # ones), not only active/in-progress transfers.
    def get_active_downloads(self, timeout: int = 10) -> list[dict[str, Any]]:
        """Deprecated alias for get_all_downloads(). See that method's
        docstring - the underlying endpoint returns ALL downloads, not
        only active/in-progress ones."""
        return self.get_all_downloads(timeout=timeout)

    def get_downloads_for_user(self, username: str, timeout: int = 10) -> dict[str, Any]:
        """Get all downloads for a specific user (GET /transfers/downloads/{username})."""
        if not username:
            return {}
        try:
            return self.get_json(f"transfers/downloads/{username}", timeout=timeout, default={})
        except Exception as exc:
            logger.debug("Failed to get downloads for user", username=username, error=str(exc))
            return {}

    def retry_download(self, username: str, transfer_id: str, timeout: int = 10) -> bool:
        """Retry a failed download.

        CAUTION: unlike the other methods in this class, a manual "retry"
        REST endpoint could not be confirmed against slskd's current
        controller source. Since slskd 0.26.0, failed-download retries are
        primarily handled automatically server-side via the
        `transfers.download.retry.*` config options. If this call
        consistently 404s against your slskd instance, prefer configuring
        automatic retries in slskd.yml instead of relying on this method.
        """
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
        """Get details for a specific download.

        Requires both `username` and the transfer `id` - slskd's route is
        GET /transfers/downloads/{username}/{id}. A username-less version
        of this method previously existed and could never have succeeded
        against a real slskd instance.
        """
        if not username or not transfer_id:
            return {}
        try:
            return self.get_json(f"transfers/downloads/{username}/{transfer_id}", timeout=timeout, default={})
        except Exception as exc:
            logger.debug("Failed to get download", username=username, transfer_id=transfer_id, error=str(exc))
            return {}

    def cancel_download(self, username: str, transfer_id: str, remove: bool = False, timeout: int = 10) -> bool:
        """Cancel (and optionally remove) a specific download.

        DELETE /transfers/downloads/{username}/{id}[?remove=true]. Both
        `username` and the transfer `id` (GUID, not filename) are required
        by slskd. A previous "legacy" filename-based path variant
        (`/transfers/downloads/{username}/{filename}`) does not correspond
        to any real slskd route and would have failed.
        """
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
        """Remove all completed downloads from the queue.

        Route confirmed as DELETE /transfers/downloads/all/completed against
        a working production caller (services.downloads.slskd_service). An
        earlier version of this method guessed "transfers/downloads/completed"
        (missing the "all/" segment) and was flagged as unverified; that
        guess was incorrect and has been corrected here.
        """
        try:
            resp = self.delete("transfers/downloads/all/completed", timeout=timeout)
            return resp.status_code in (200, 204)
        except Exception as exc:
            logger.debug("Failed to remove completed downloads", error=str(exc))
            return False

    def get_queue_position(self, username: str, transfer_id: str, timeout: int = 10) -> int | None:
        """Get the queue position for a queued download.

        GET /transfers/downloads/{username}/{id}/position. Requires both
        `username` and the transfer `id` - a username-less version of this
        method previously existed and could never have succeeded.
        """
        if not username or not transfer_id:
            return None
        try:
            data = self.get_json(f"transfers/downloads/{username}/{transfer_id}/position", timeout=timeout, default={})
            return data.get("position") if isinstance(data, dict) else None
        except Exception as exc:
            logger.debug("Failed to get queue position", username=username, transfer_id=transfer_id, error=str(exc))
            return None

    # ------------------------------------------------------------------
    # User operations (browse, info, status)
    # ------------------------------------------------------------------
    def browse_user(self, username: str) -> dict[str, Any]:
        """Browse a user's shared files."""
        try:
            return self.get_json(f"users/{username}/browse", default={}, timeout=30)
        except Exception as exc:
            logger.debug("Failed to browse user", username=username, error=str(exc))
            return {}

    def get_user_info(self, username: str) -> dict[str, Any]:
        """Get user info (description, upload slots, etc.)."""
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

    def is_connected(self) -> bool:
        """Return True when the Soulseek client is connected and logged in."""
        try:
            state = self.get_state()
            server = state.get("server", {}) if isinstance(state, dict) else {}
            return bool(server.get("isConnected") or server.get("isLoggedIn"))
        except Exception:
            return False


# =============================================================================
# Client factory (cached)
# =============================================================================
_slskd_client_cache: SlskdHttpClient | None = None
_slskd_client_cache_mtime: float | None = None


def _config_file_mtime() -> float | None:
    """Return the config.yaml mtime, or None when the file is missing."""
    try:
        cfg_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
        if os.path.exists(cfg_path):
            return os.path.getmtime(cfg_path)
    except Exception:
        pass
    return None


def get_slskd_client() -> SlskdHttpClient | None:
    """Return a configured, cached ``SlskdHttpClient`` instance.

    Reads ``slskd`` from config (``enabled`` / ``web_url`` / ``api_key``).
    The cached client is rebuilt automatically when config.yaml changes, so a
    URL/API-key edit from the config page takes effect without restarting —
    including in long-running queue-worker processes that cache their own
    copy. Returns ``None`` when slskd is disabled or misconfigured so callers
    can treat it as "Soulseek unavailable" without raising.
    """
    global _slskd_client_cache, _slskd_client_cache_mtime

    if _slskd_client_cache is not None:
        # Rebuild when the config file changed since the client was built
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

        # Use a plain session (no automatic 429 retries) for queue processing.
        # The shared api_clients session retries 429 responses with exponential
        # backoff, which turns a fast "slot busy" into a long wait per search
        # attempt. A plain session lets the caller's own retry loop control the
        # cadence (mirrors the legacy queue_processor factory).
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
