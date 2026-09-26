"""slskd download/search service.

This service owns slskd workflow logic:
- search retry/slot-busy behaviour
- result parsing
- quality filtering
- download tracking
- transfer parsing and cleanup

Raw HTTP is handled by api_clients.slskd_http.
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

import structlog

from api_clients.slskd_http import SlskdHttpClient

logger = structlog.get_logger(__name__)

#: How many searches may run between automatic stale-search cleanups.
#: Read from ``slskd.search_cleanup_interval`` (see the Config page); the
#: default keeps the slskd round-trip off 9 out of every 10 searches.
_DEFAULT_SEARCH_CLEANUP_INTERVAL = 10


def _configured_cleanup_interval() -> int:
    """Return the configured cleanup cadence, falling back to the default.

    Delegates to ``helpers.config_helpers.get_slskd_search_cleanup_interval`` so
    the setting has ONE definition (the Config page reads the same key).  A
    config read failure degrades to the default rather than disabling cleanup.
    """
    try:
        from helpers.config_helpers import get_slskd_search_cleanup_interval

        return int(get_slskd_search_cleanup_interval())
    except Exception:
        return _DEFAULT_SEARCH_CLEANUP_INTERVAL

STUCK_SEARCH_TIMEOUT_MS = 3 * 60 * 1000
EMPTY_TERMINAL_STATES = frozenset({"Completed, Cancelled", "Completed, Errored", "Cancelled", "Errored"})

# Search lifecycle states (distinct from download-transfer states).
# slskd reports a search as "InProgress" while peers are still responding
# and "Queued" while it is waiting to start.  Using a dedicated set (rather
# than overloading the transfer-state ACTIVE_STATES) means a future slskd
# version that returns a different in-progress search state string is not
# misread as "complete".
SEARCH_ACTIVE_STATES = frozenset({"InProgress", "Queued", "Requested", "None", "Initializing", "In Progress"})

# Transient httpx exception names that warrant a retry rather than an
# immediate queue-item failure.
_TRANSIENT_ERROR_NAMES = frozenset({
    "ReadTimeout",
    "ConnectTimeout",
    "ConnectError",
    "ConnectionError",
    "ReadError",
    "RemoteProtocolError",
    "TimeoutException",
    "TimeoutError",
    "WriteTimeout",
})


def _is_transient_error(exc: BaseException) -> bool:
    """Return True when ``exc`` looks like a retryable network/timeout error."""
    if type(exc).__name__ in _TRANSIENT_ERROR_NAMES:
        return True
    if any(base.__name__ in _TRANSIENT_ERROR_NAMES for base in type(exc).__mro__):
        return True
    return False


def _is_connection_error(exc: BaseException) -> bool:
    """Return True when ``exc`` is a CONNECTIVITY failure (host unreachable /
    refused / timed out) rather than a logic or payload error.

    Used to decide whether to dump a full traceback: connect failures happen
    every queue cycle while slskd is down, so they log at error with just the
    message; everything else keeps exc_info for debuggability.
    """
    # Any transient network error (timeout / read error / connection reset)
    # is a connectivity failure — slskd being down or restarting surfaces as
    # these every poll cycle, so they must not spam full tracebacks.
    if _is_transient_error(exc):
        return True
    if type(exc).__name__ in ("ConnectError", "ConnectTimeout", "ConnectionRefusedError", "OSError"):
        return True
    if any(base.__name__ in ("ConnectError", "ConnectTimeout") for base in type(exc).__mro__):
        return True
    # httpx.ConnectError wraps httpcore.ConnectError; the message includes
    # the platform errno text.  Match the errno number too.
    msg = str(exc)
    if "No route to host" in msg or "Connection refused" in msg or "Name or service not known" in msg:
        return True
    return False


@dataclass
class SearchFile:
    filename: str
    size: int
    bitrate: int
    sample_rate: int
    length: int
    bit_depth: int | None = None
    extension: str | None = None
    is_locked: bool = False

    def __post_init__(self):
        self.size = int(self.size or 0)
        self.bitrate = int(self.bitrate or 0)
        self.sample_rate = int(self.sample_rate or 0)
        self.length = int(self.length or 0)
        if self.bit_depth is not None:
            self.bit_depth = int(self.bit_depth) if self.bit_depth else None

    @property
    def size_mb(self) -> float:
        return self.size / (1024 * 1024) if self.size else 0

    @property
    def duration_formatted(self) -> str:
        if not self.length:
            return "0:00"
        return f"{self.length // 60}:{self.length % 60:02d}"

    @property
    def is_lossless(self) -> bool:
        return (self.extension or "").lower() in ("flac", "wav", "aiff", "alac")

    def matches_quality(self, min_bitrate: int = 192, min_sample_rate: int = 44100) -> bool:
        """Check if this file meets minimum quality thresholds."""
        if self.is_locked:
            return False
        if self.is_lossless:
            return self.sample_rate >= min_sample_rate
        return self.bitrate >= min_bitrate and self.sample_rate >= min_sample_rate


@dataclass
class SearchResponse:
    username: str
    files: list[SearchFile]
    has_free_upload_slot: bool = True
    upload_speed: int | None = None
    queue_length: int | None = None

    def __post_init__(self):
        if not self.files:
            self.files = []
        elif isinstance(self.files[0], dict):
            self.files = [
                SearchFile(
                    filename=f.get("filename", ""),
                    size=f.get("size", 0),
                    bitrate=f.get("bitRate", 0),
                    sample_rate=f.get("sampleRate", 0),
                    length=f.get("length", 0),
                    bit_depth=f.get("bitDepth"),
                    extension=f.get("extension"),
                    is_locked=bool(f.get("isLocked", f.get("is_locked", False))),
                )
                for f in self.files
            ]


class SlskdService:
    """Application-level slskd behaviour."""

    STATE_REQUESTED = "Requested"
    STATE_QUEUED_REMOTELY = "Queued, Remotely"
    STATE_QUEUED_LOCALLY = "Queued, Locally"
    STATE_INITIALIZING = "Initializing"
    STATE_IN_PROGRESS = "InProgress"
    STATE_SUCCEEDED = "Completed, Succeeded"
    STATE_CANCELLED = "Completed, Cancelled"
    STATE_TIMED_OUT = "Completed, TimedOut"
    STATE_ERRORED = "Completed, Errored"
    STATE_REJECTED = "Completed, Rejected"

    ACTIVE_STATES = frozenset([
        STATE_REQUESTED, STATE_QUEUED_REMOTELY, STATE_QUEUED_LOCALLY,
        STATE_INITIALIZING, STATE_IN_PROGRESS, "Queued", "In Progress", "Downloading",
    ])
    FAILED_STATES = frozenset([
        STATE_CANCELLED, STATE_TIMED_OUT, STATE_ERRORED, STATE_REJECTED,
        "Cancelled", "TimedOut", "Errored", "Failed", "Rejected", "Error",
    ])

    def __init__(self, http_client: SlskdHttpClient, search_cleanup_interval: int | None = None):
        self.http = http_client
        # Per-instance override; ``None`` means "use the shared cadence".
        self._cleanup_interval_override = (
            None if search_cleanup_interval is None else int(search_cleanup_interval)
        )

    # ── Stale-search cleanup CADENCE (process-wide) ───────────────────────
    # ``clear_stale_searches`` costs a ``GET /searches`` round-trip (and a
    # DELETE per stale search found).  slskd serialises API operations, so
    # doing that before EVERY search delayed the search itself for no benefit —
    # most of the time there is nothing stale to remove.
    #
    # The cleanup therefore runs ONCE PER WINDOW of N searches: search #1,
    # then #11, #21, …  An interval of 0 or less DISABLES it (never clean
    # automatically).
    #
    # ⚠️ The counters are CLASS attributes, not instance state, and that is
    # load-bearing.  Both entry points construct a FRESH ``SlskdService`` per
    # operation:
    #   * ``routes/download_search_routes.py::slskd_search`` builds one per
    #     HTTP request;
    #   * ``download_pipeline_service`` builds one per queued download.
    # An instance counter would therefore reset on every call and the cleanup
    # would run every single time — exactly the behaviour being removed.
    _cleanup_lock = threading.Lock()
    _searches_since_cleanup = 0

    @classmethod
    def reset_cleanup_cadence(cls) -> None:
        """Reset the shared counter (used by tests and manual maintenance)."""
        with cls._cleanup_lock:
            cls._searches_since_cleanup = 0

    @classmethod
    def _effective_cleanup_interval(cls) -> int:
        # Deliberately NOT cached: ``get_config()`` is already an in-memory
        # cache, and a class-level copy would survive a Config-page save — the
        # operator would change the interval and see no effect until restart.
        return _configured_cleanup_interval()

    @staticmethod
    def state_text(raw_state: Any) -> str:
        if raw_state is None:
            return ""
        if isinstance(raw_state, dict):
            raw_state = raw_state.get("state") or raw_state.get("name") or raw_state.get("value")
        return str(raw_state).strip()

    @classmethod
    def is_success_state(cls, raw_state: Any) -> bool:
        state = cls.state_text(raw_state)
        state_lower = state.lower()
        return bool(state == cls.STATE_SUCCEEDED or "succeed" in state_lower or state_lower in {"completed", "complete", "succeeded"})

    @staticmethod
    def extract_queue_position(entry: dict | None) -> Optional[int]:
        if not isinstance(entry, dict):
            return None
        raw = entry.get("queuePosition") or entry.get("queue_position") or entry.get("position") or entry.get("queueIndex") or entry.get("queueLength")
        try:
            return int(raw) if raw is not None else None
        except Exception:
            return None

    def find_active_search_by_query(self, query: str, within_seconds: int = 60) -> Optional[dict]:
        """Return the most-recent active search whose query matches *query*."""
        _ACTIVE_STATES = {"None", "Queued", "Requested", "InProgress", "Initializing", "In Progress"}
        try:
            searches = self.list_searches(timeout=8)
        except Exception:
            return None
            
        now = datetime.now(timezone.utc)
        target = str(query or "").strip().lower()
        matches = []
        
        for s in searches or []:
            state = str(s.get("state") or s.get("State") or "")
            if state not in _ACTIVE_STATES:
                continue
            search_text = str(s.get("searchText") or s.get("query") or "").strip()
            if target and search_text.lower() != target:
                continue
            started_at = s.get("startedAt") or s.get("StartedAt") or s.get("started_at")
            if not started_at:
                matches.append(s)
                continue
            try:
                if isinstance(started_at, str):
                    started_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                else:
                    started_dt = started_at
                if started_dt.tzinfo is None:
                    started_dt = started_dt.replace(tzinfo=timezone.utc)
                if (now - started_dt).total_seconds() <= within_seconds:
                    matches.append(s)
            except Exception:
                matches.append(s)
                
        if not matches:
            return None
            
        matches.sort(key=lambda s: s.get("startedAt") or s.get("StartedAt") or "", reverse=True)
        return matches[0]

    def start_search(self, query: str, timeout: Optional[int] = None, max_attempts: int = 5, recover: bool = True) -> Optional[str]:
        if not self.http.enabled:
            return None
            
        data = {"searchText": query, "filterResponses": False}
        for attempt in range(1, max_attempts + 1):
            try:
                resp = self.http.post_json("searches", data, timeout=timeout)
                if resp.status_code in [200, 201]:
                    payload = resp.json() or {}
                    return payload.get("id") or payload.get("searchId")
                    
                body_preview = (resp.text or "")[:200]
                retryable_429 = resp.status_code == 429 and (
                    "only one concurrent operation" in body_preview.lower()
                    or "wait until the previous request completes" in body_preview.lower()
                )
                if retryable_429 and attempt < max_attempts:
                    wait_seconds = 0.8
                    retry_after = (resp.headers.get("Retry-After") or "").strip()
                    if retry_after:
                        try:
                            wait_seconds = max(0.2, float(retry_after))
                        except Exception:
                            pass
                    else:
                        wait_seconds = min(2.0, 0.4 * attempt)
                    time.sleep(wait_seconds)
                    continue
                    
                logger.warning("slskd search start failed", status_code=resp.status_code, preview=body_preview)
                break
            except Exception as exc:
                if _is_transient_error(exc) and attempt < max_attempts:
                    wait_seconds = min(2.0, 0.4 * attempt)
                    logger.warning(
                        "slskd search start transient error",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        error=str(exc),
                        error_type=type(exc).__name__,
                        wait_seconds=wait_seconds,
                    )
                    time.sleep(wait_seconds)
                    continue
                    
                logger.error(
                    "slskd search failed",
                    query=query,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    base_url=getattr(self.http, "base_url", "?"),
                    exc_info=True,
                )
                break

        if recover:
            try:
                recovered = self.find_active_search_by_query(query, within_seconds=60)
                if recovered:
                    recovered_id = (
                        recovered.get("id")
                        or recovered.get("searchId")
                        or recovered.get("Id")
                        or ""
                    )
                    if recovered_id:
                        logger.info("slskd recovered active search after start failed", search_id=recovered_id, query=query)
                        return str(recovered_id)
            except Exception as exc:
                logger.debug("slskd search recovery lookup failed", query=query, error=str(exc))
        return None

    def get_search_results(self, search_id: str, timeout: Optional[int] = None) -> tuple[list[SearchResponse], str, bool]:
        if not self.http.enabled:
            return [], "Error", True
            
        state = "InProgress"
        try:
            state_data = self.http.get_json(f"searches/{search_id}", timeout=timeout, default={})
            state = state_data.get("state", "InProgress")
        except Exception as exc:
            logger.warning(
                "slskd get state failed for search",
                search_id=search_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return [], state, False
            
        if state in EMPTY_TERMINAL_STATES:
            return [], state, True
            
        try:
            raw = self.http.get_json(f"searches/{quote(search_id, safe='')}/responses", timeout=timeout, default=[])
            responses = []
            for raw_resp in raw or []:
                if isinstance(raw_resp, dict):
                    responses.append(SearchResponse(
                        username=raw_resp.get("username", "Unknown"),
                        files=raw_resp.get("files", []),
                        has_free_upload_slot=raw_resp.get("hasFreeUploadSlot", raw_resp.get("HasFreeUploadSlot", True)),
                        upload_speed=raw_resp.get("uploadSpeed"),
                        queue_length=raw_resp.get("queueLength"),
                    ))
            return responses, state, state not in SEARCH_ACTIVE_STATES
        except Exception as exc:
            logger.warning("slskd get responses failed for search", search_id=search_id, error=str(exc))
            return [], state, state not in SEARCH_ACTIVE_STATES

    def download_file(self, username: str, filename: str, size: int = 0, timeout: Optional[int] = None) -> bool:
        if not self.http.enabled:
            return False
        if not size:
            logger.warning(
                "slskd download_file called without a file size — slskd requires the real size to request the file from the peer",
                username=username,
                filename=str(filename or "")[:80],
            )
        for attempt in range(1, 3):
            try:
                safe_user = quote(username or "", safe="")
                resp = self.http.post_json(f"transfers/downloads/{safe_user}", [{"filename": filename, "size": int(size or 0)}], timeout=timeout)
                return resp.status_code in [200, 201, 204]
            except Exception as exc:
                if _is_transient_error(exc) and attempt < 2:
                    wait_seconds = 1.0
                    logger.warning(
                        "slskd download transient error",
                        attempt=attempt,
                        error=str(exc),
                        error_type=type(exc).__name__,
                        wait_seconds=wait_seconds,
                    )
                    time.sleep(wait_seconds)
                    continue
                logger.error("slskd download exception", username=username, filename=filename[:50], error=str(exc), exc_info=True)
                return False
        return False

    def download_files(self, files: list[dict[str, Any]], timeout: Optional[int] = None) -> list[dict[str, Any]]:
        """Queue several files for download, batching by peer username.

        ``files`` is the shape the search page already produces::

            [{"username": "peer", "filename": "Music/x.flac", "size": 1234}, ...]

        Returns one entry per peer so the caller can distinguish "slskd
        accepted the request" from "slskd accepted nothing"::

            [{"username": "peer", "requested": 2, "success": True}, ...]

        ``requested`` counts the files actually handed to slskd, which is
        NOT the same as the number of transfers that will complete — the
        caller must not treat a non-zero ``requested`` as a finished
        download.  It only means the remote peer was asked for the file.

        Losing this method (it existed as ``SlskdClient.download_files``
        before the routes were split out of ``app.py``) silently broke every
        multi-file download, because ``/api/slskd/download`` then only
        accepted the single ``username``/``filename`` payload shape.
        """
        if not self.http.enabled:
            return []

        by_user: dict[str, list[dict[str, Any]]] = {}
        for entry in files or []:
            if not isinstance(entry, dict):
                continue
            entry_user = str(entry.get("username") or "").strip()
            entry_filename = str(entry.get("filename") or "").strip()
            if not entry_user or not entry_filename:
                continue
            by_user.setdefault(entry_user, []).append({
                "filename": entry_filename,
                "size": int(entry.get("size") or 0),
            })

        results: list[dict[str, Any]] = []
        for entry_user, payload_files in by_user.items():
            transfer_ids: list[str] = []
            peer_success = True
            peer_error: str | None = None
            try:
                # ``raise_on_error=True`` is load-bearing: without it a failed
                # POST is indistinguishable from an older slskd that accepted
                # the request without a body, and the caller would report a
                # silent no-op as success.
                transfer_ids = self.http.enqueue_downloads(
                    entry_user, payload_files, timeout=timeout or 15, raise_on_error=True
                ) or []
            except TypeError:
                # Older client without the ``raise_on_error``/``timeout`` kwargs.
                try:
                    transfer_ids = self.http.enqueue_downloads(entry_user, payload_files) or []
                except Exception as exc:
                    peer_success, peer_error = False, str(exc)
            except Exception as exc:
                peer_success, peer_error = False, str(exc)

            if not peer_success:
                logger.error(
                    "slskd batch download failed",
                    username=entry_user,
                    file_count=len(payload_files),
                    error=peer_error,
                )

            results.append({
                "username": entry_user,
                # ``requested`` counts the files handed to slskd — NOT the
                # number of transfers that will complete.  Only the remote peer
                # being asked for the file is proven here.
                "requested": len(payload_files) if peer_success else 0,
                "transfer_ids": transfer_ids,
                "success": peer_success,
                "error": peer_error,
            })

        return results

    def filter_results_by_quality(self, responses: list[SearchResponse], min_bitrate: int = 192, min_sample_rate: int = 44100, max_results: int = 50) -> list[dict]:
        qualified = []
        for response in responses:
            for file in response.files:
                if file.matches_quality(min_bitrate, min_sample_rate):
                    qualified.append({
                        "username": response.username,
                        "filename": file.filename,
                        "size": file.size,               # <--- Restored raw byte size
                        "size_mb": round(file.size_mb, 2),
                        "bitrate": file.bitrate,
                        "sample_rate": file.sample_rate,
                        "bit_depth": file.bit_depth,
                        "extension": file.extension,
                        "is_lossless": file.is_lossless,
                        "duration": file.duration_formatted,
                        "length_seconds": file.length,
                        "has_free_upload_slot": getattr(response, "has_free_upload_slot", True),
                        "upload_speed": getattr(response, "upload_speed", None),
                        "queue_length": getattr(response, "queue_length", None),
                    })
        qualified.sort(key=lambda item: (-item["bitrate"], -item["sample_rate"]))
        return qualified[:max_results]

    def search_and_filter(self, query: str, min_bitrate: int | None = None, wait_seconds: int | None = None, poll_interval: float = 1.0, timeout: Optional[int] = None) -> list[dict]:
        if min_bitrate is None:
            from helpers.config_helpers import get_search_quality_config
            min_bitrate = get_search_quality_config()["min_bitrate"]
        if wait_seconds is None:
            from helpers.config_helpers import _SLSKD_SEARCH_MAX_WAIT_SECONDS
            wait_seconds = _SLSKD_SEARCH_MAX_WAIT_SECONDS
            
        try:
            self.maybe_clear_stale_searches(budget_seconds=6)
        except Exception:
            pass
            
        search_id = self.start_search(query, timeout=timeout)
        if not search_id:
            return []

        deadline = time.time() + max(0, int(wait_seconds or 0))
        poll_attempt = 0
        seen: set[tuple[str, str]] = set()
        accumulated: list[dict] = []
        
        while True:
            delay = 1.0 if poll_attempt < 5 else (2.0 if poll_attempt < 10 else 5.0)
            poll_attempt += 1
            if time.time() + delay > deadline:
                break
            time.sleep(delay)
            responses, _state, is_complete = self.get_search_results(search_id, timeout=timeout)
            for item in self.filter_results_by_quality(responses, min_bitrate=min_bitrate):
                key = (item["username"], item["filename"])
                if key not in seen:
                    seen.add(key)
                    accumulated.append(item)
            if is_complete:
                break
                
        if not accumulated:
            responses, _state, _complete = self.get_search_results(search_id, timeout=timeout)
            for item in self.filter_results_by_quality(responses, min_bitrate=min_bitrate):
                key = (item["username"], item["filename"])
                if key not in seen:
                    seen.add(key)
                    accumulated.append(item)
        return accumulated

    def parse_transfers_response(self, raw: list | dict) -> list[dict]:
        if isinstance(raw, dict):
            raw = raw.get("downloads") or raw.get("transfers") or raw.get("items") or []
        flat = []
        for user_entry in raw or []:
            if not isinstance(user_entry, dict):
                continue
            username = user_entry.get("username", "Unknown")
            entries = []
            if user_entry.get("filename") and not user_entry.get("directories"):
                entries.append(user_entry)
            for directory in user_entry.get("directories") or []:
                entries.extend(directory.get("files") or directory.get("downloads") or [])
            entries.extend(user_entry.get("files") or user_entry.get("downloads") or [])
            for item in entries:
                if not isinstance(item, dict):
                    continue
                size = int(item.get("size", 0) or 0)
                bytes_transferred = int(item.get("bytesTransferred", 0) or 0)
                progress = min(100, round((bytes_transferred / size) * 100, 2)) if size else int(item.get("percentComplete", 0) or 0)
                flat.append({
                    "id": item.get("id") or item.get("remoteToken") or item.get("token") or "",
                    "username": username,
                    "filename": item.get("filename") or item.get("fileName") or item.get("name") or item.get("path") or "",
                    "size": size,
                    "bytesTransferred": bytes_transferred,
                    "progress": progress,
                    "state": self.state_text(item.get("state") or item.get("transferState") or item.get("status")),
                    "averageSpeed": int(item.get("averageSpeed", 0) or 0),
                    "queuePosition": self.extract_queue_position(item),
                    "localFilePath": item.get("localFilePath") or item.get("localPath") or item.get("downloadedFilePath") or item.get("path") or "",
                    "startedAt": item.get("startedAt") or item.get("started"),
                })
        return flat

    def get_active_downloads(self, timeout: Optional[int] = None) -> list[dict]:
        try:
            raw = self.http.get_json("transfers/downloads", timeout=timeout, default=[])
            return self.parse_transfers_response(raw)
        except Exception as exc:
            # Connectivity failures (host unreachable / timeout) are expected
            # when slskd is down or restarting — the queue poll hits this
            # every cycle, so don't dump a full traceback for them.  Only
            # genuinely unexpected errors (malformed payload, logic bugs)
            # get exc_info.
            is_conn = _is_connection_error(exc)
            logger.error(
                "slskd get active downloads failed",
                error=str(exc),
                exc_info=False if is_conn else True,
            )
            return []

    def get_completed_transfers(self, timeout: Optional[int] = None) -> list[dict]:
        return [item for item in self.get_active_downloads(timeout=timeout) if self.is_success_state(item.get("state"))]

    def list_searches(self, timeout: Optional[int] = None) -> list[dict]:
        try:
            payload = self.http.get_json("searches", timeout=timeout, default=[])
            return payload if isinstance(payload, list) else []
        except Exception:
            return []

    def maybe_clear_stale_searches(self, budget_seconds: float = 8) -> bool:
        """Run the stale-search cleanup at most once per N searches.

        Returns True when the cleanup actually ran.

        This is the entry point search paths should use.  ``clear_stale_searches``
        itself stays public and unconditional for callers that genuinely want it
        on demand (manual maintenance, tests).

        Cadence: the 1st search of each window cleans up, then every Nth after
        that.  An interval of ``0`` or less DISABLES it — never clean up
        automatically.  A failure is swallowed (logged by
        ``clear_stale_searches``) so a cleanup problem can never fail the user's
        search.
        """
        cls = type(self)
        interval = (
            self._cleanup_interval_override
            if self._cleanup_interval_override is not None
            else cls._effective_cleanup_interval()
        )
        if interval <= 0:
            return False

        with cls._cleanup_lock:
            # Count the SEARCH first, then decide.  Counting after the decision
            # would make the first call of every window skip.
            cls._searches_since_cleanup += 1
            should_clean = (cls._searches_since_cleanup - 1) % interval == 0
            if should_clean:
                # ⚠️ Reset to 1, NOT 0.  This search has just been counted, so
                # restoring it to 0 loses that count and makes the next call
                # re-satisfy ``(counter - 1) % interval == 0`` — i.e. the
                # cleanup then runs on EVERY search, which is the exact bug
                # this gate exists to remove.
                cls._searches_since_cleanup = 1

        if not should_clean:
            return False

        try:
            self.clear_stale_searches(budget_seconds=budget_seconds)
        except Exception as exc:
            # clear_stale_searches already swallows its own errors; this is a
            # belt-and-braces guard so a future edit cannot break the search.
            logger.warning("Stale search cleanup failed (search continues)", error=str(exc))
        return True

    def clear_stale_searches(self, budget_seconds: float = 8) -> None:
        """Cancel terminal-state (or long-running stuck) searches in slskd."""
        _TERMINAL_STATES = {
            "Completed, TimedOut", "Completed, ResponseLimitReached",
            "Completed, FileLimitReached", "Completed, Cancelled",
            "Completed, Errored", "Completed", "Succeeded",
            "Cancelled", "Errored", "TimedOut",
        }
        deadline = time.monotonic() + max(0.1, float(budget_seconds))
        try:
            existing = self.list_searches(timeout=4)
            for s in existing or []:
                if time.monotonic() >= deadline:
                    logger.warning("Stale search cleanup budget exhausted")
                    break
                sid = s.get("id") or s.get("searchId") or s.get("Id")
                state = str(s.get("state") or s.get("State") or "")
                if not sid:
                    continue

                should_cancel = state in _TERMINAL_STATES
                if not should_cancel and state in self.ACTIVE_STATES:
                    started_at = s.get("startedAt") or s.get("StartedAt") or s.get("started_at")
                    elapsed_ms = 0
                    if started_at:
                        try:
                            if isinstance(started_at, str):
                                started_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                            else:
                                started_dt = started_at
                            if started_dt.tzinfo is None:
                                started_dt = started_dt.replace(tzinfo=timezone.utc)
                            elapsed_ms = int((datetime.now(timezone.utc) - started_dt).total_seconds() * 1000)
                        except Exception:
                            elapsed_ms = 0
                    if elapsed_ms > STUCK_SEARCH_TIMEOUT_MS:
                        should_cancel = True
                        logger.info("Cancelling stuck active search", search_id=sid, state=state, elapsed_ms=elapsed_ms)

                if should_cancel:
                    time_left = deadline - time.monotonic()
                    if time_left <= 0:
                        logger.warning("Stale search cleanup budget exhausted")
                        break
                    if state in self.ACTIVE_STATES:
                        try:
                            self.http.put(f"searches/{sid}", timeout=max(0.5, min(2, time_left)))
                        except Exception:
                            pass
                    time_left = deadline - time.monotonic()
                    if time_left <= 0:
                        logger.warning("Stale search cleanup budget exhausted")
                        break
                    self.cancel_search(sid, timeout=max(0.5, min(2, time_left)))
                    if state in _TERMINAL_STATES:
                        logger.info("Cleared stale search", search_id=sid, state=state)
        except Exception as cleanup_err:
            logger.warning("Could not clear stale searches", error=str(cleanup_err))

    def cancel_search(self, search_id: str, timeout: Optional[int] = None) -> bool:
        try:
            resp = self.http.delete(f"searches/{quote(search_id or '', safe='')}", timeout=timeout)
            if resp.status_code in [200, 204]:
                return True
            body = (resp.text or "")[:400]
            return resp.status_code in [409, 500] and "concurrency" in body.lower()
        except Exception as exc:
            logger.warning(
                "slskd cancel search failed",
                search_id=search_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return False

    def cancel_download(self, username: str, transfer_id: str, remove: bool = True, timeout: Optional[int] = None) -> bool:
        try:
            # URL-encode both path segments — Soulseek usernames can legally
            # contain spaces, #, &, %, ? and unicode, which would otherwise
            # corrupt the path/query string.
            safe_user = quote(username or "", safe="")
            safe_id = quote(transfer_id or "", safe="")
            resp = self.http.delete(f"transfers/downloads/{safe_user}/{safe_id}?remove={str(remove).lower()}", timeout=timeout)
            return resp.status_code in [200, 204]
        except Exception as exc:
            logger.warning(
                "slskd cancel download failed",
                username=username,
                transfer_id=transfer_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return False

    def clear_completed_downloads(self, timeout: Optional[int] = None) -> bool:
        try:
            resp = self.http.delete("transfers/downloads/all/completed", timeout=timeout)
            return resp.status_code in [200, 204]
        except Exception as exc:
            logger.warning(
                "slskd clear completed downloads failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return False
