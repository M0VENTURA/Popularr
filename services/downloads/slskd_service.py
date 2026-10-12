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

import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
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

#: A search we did NOT start is "truly orphaned" only after this long —
#: terminal-state foreign searches (e.g. a manual search in the slskd web UI)
#: are left alone entirely; an active foreign search is only cancelled once it
#: has outlived this generous window.
ORPHANED_SEARCH_TIMEOUT_MS = 30 * 60 * 1000

# Search lifecycle states (distinct from download-transfer states).
# slskd reports a search as "InProgress" while peers are still responding
# and "Queued" while it is waiting to start.  Using a dedicated set (rather
# than overloading the transfer-state ACTIVE_STATES) means a future slskd
# version that returns a different in-progress search state string is not
# misread as "complete".
SEARCH_ACTIVE_STATES = frozenset({"InProgress", "Queued", "Requested", "None", "Initializing", "In Progress"})

# Terminal search states — also parsed as a flag set: state strings are
# "Completed, TimedOut" / "Completed, ResponseLimitReached" / … and a state
# in SEARCH_TERMINAL_FLAGS ends the search regardless of its qualifier.
SEARCH_TERMINAL_STATES = frozenset({
    "Completed", "Completed, Cancelled", "Completed, Errored",
    "Completed, TimedOut", "Completed, ResponseLimitReached",
    "Completed, FileLimitReached", "Completed, Succeeded", "Completed, Rejected",
    "Cancelled", "Cancelled, TimedOut", "Errored", "TimedOut",
    "Succeeded", "Rejected", "Failed", "Error",
})

#: Substrings that make a search state a terminal one ("Completed, TimedOut"
#: has two flag tokens: "Completed" and "TimedOut").
SEARCH_TERMINAL_FLAGS = frozenset(("completed", "succeeded", "timedout", "errored", "cancelled", "rejected", "failed", "error"))

# Lossless container extensions.  ``ape``/``wv`` are lossless; ``m4a`` is
# deliberately NOT here (an m4a is usually AAC).
LOSSLESS_EXTENSIONS = frozenset(("flac", "wav", "aiff", "alac", "ape", "wv"))

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


def _is_not_found_error(exc: BaseException) -> bool:
    """True when ``exc`` is an HTTP 404 (the resource stopped existing).

    httpx raises ``HTTPStatusError`` carrying the response, so the 404 status
    is checked both on the exception and on ``exc.response``.  Used by
    ``get_search_results``: a search that has been DROPPED (404) mid-poll must
    be treated as COMPLETE, not left polled until the hard timeout.
    """
    status = getattr(exc, "status_code", None)
    if status == 404:
        return True
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status == 404


def _to_int(value: Any, default: int = 0) -> int:
    """Safe int conversion for slskd payload fields.

    slskd peers can return non-numeric strings (or omit the field entirely);
    one bad value must drop that file, not the whole response.
    """
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _extension_from_filename(filename: str) -> str:
    """Container extension from the file name (used when the payload omits it)."""
    name = (filename or "").strip()
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


def _raw_file_count(responses: list) -> int:
    """Total files across responses WITHOUT quality filtering.

    Drives the empty-search early abort: a search streaming many files that
    ALL fail the quality gate is not empty, so it must not be cancelled as one.
    Tolerates both ``SearchResponse`` objects and plain dicts (test fakes).
    """
    total = 0
    for resp in responses or []:
        files = resp.files if isinstance(resp, SearchResponse) else (resp.get("files") if isinstance(resp, dict) else None)
        total += len(files or [])
    return total


def _search_elapsed_ms(search: dict) -> int:
    """Milliseconds since a search started (0 when unknown)."""
    started_at = search.get("startedAt") or search.get("StartedAt") or search.get("started_at")
    if not started_at:
        return 0
    try:
        if isinstance(started_at, str):
            started_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        else:
            started_dt = started_at
        if started_dt.tzinfo is None:
            started_dt = started_dt.replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - started_dt).total_seconds() * 1000)
    except Exception:
        return 0


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
        self.size = _to_int(self.size)
        self.bitrate = _to_int(self.bitrate)
        self.sample_rate = _to_int(self.sample_rate)
        self.length = _to_int(self.length)
        if self.bit_depth is not None:
            self.bit_depth = _to_int(self.bit_depth) or None
        # A missing payload extension is derived from the filename so the
        # lossless classification never silently degrades to "unknown".
        self.extension = (self.extension or "").lower() or _extension_from_filename(self.filename)

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
        return self.extension in LOSSLESS_EXTENSIONS

    def matches_quality(
        self,
        min_bitrate: int = 192,
        min_sample_rate: int = 44100,
        reject_reasons: dict[str, int] | None = None,
    ) -> bool:
        """Check if this file meets minimum quality thresholds.

        Missing attributes are UNKNOWN, not failing: Soulseek peers routinely
        omit ``sampleRate`` (and sometimes ``bitRate``) — a 320k MP3 with no
        sample rate used to be rejected outright, which silently hid the only
        real copy of a rare track.  Every rejection is counted by reason in
        ``reject_reasons`` (when supplied) so `no_qualifying_result` stops
        being a mystery.
        """
        def _reject(reason: str) -> bool:
            if reject_reasons is not None:
                reject_reasons[reason] = reject_reasons.get(reason, 0) + 1
            return False

        if self.is_locked:
            return _reject("locked")
        if self.is_lossless:
            if self.sample_rate == 0:
                return True  # unknown sample rate on a lossless container: keep
            return self.sample_rate >= min_sample_rate or _reject("sample_rate_below_min")
        if self.bitrate == 0:
            return True  # unknown bitrate: keep — the scorer's duration gate still protects
        if self.bitrate < min_bitrate:
            return _reject("bitrate_below_min")
        if self.sample_rate == 0:
            return True  # unknown sample rate on a lossy container: keep
        return self.sample_rate >= min_sample_rate or _reject("sample_rate_below_min")


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
        else:
            # Convert PER ITEM: a single non-dict or mixed list must not drop
            # the whole response.  Invalid items are skipped, valid ones kept.
            converted: list[SearchFile] = []
            for f in self.files:
                if isinstance(f, SearchFile):
                    converted.append(f)
                elif isinstance(f, dict):
                    converted.append(SearchFile(
                        filename=f.get("filename", ""),
                        size=f.get("size", 0),
                        bitrate=f.get("bitRate", 0),
                        sample_rate=f.get("sampleRate", 0),
                        length=f.get("length", 0),
                        bit_depth=f.get("bitDepth"),
                        extension=f.get("extension"),
                        is_locked=bool(f.get("isLocked", f.get("is_locked", False))),
                    ))
                # else: skip — a malformed entry is one missing file, not all.
            self.files = converted


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

    # ── Search OWNERSHIP (process-wide) ───────────────────────────────────
    # ``clear_stale_searches`` used to cancel EVERY terminal search and any
    # active search older than 180s — including searches the user started in
    # the slskd web UI and searches another worker was still reading.  The ids
    # this app created are tracked class-level (both entry points build a
    # fresh instance per operation, so instance state would forget ownership
    # immediately) and the cleaner only touches OUR ids plus truly orphaned
    # searches older than a generous threshold.
    _OWNED_SEARCH_LOCK = threading.Lock()
    _OWNED_SEARCH_IDS: set[str] = set()

    @classmethod
    def mark_search_owned(cls, search_id: str) -> None:
        with cls._OWNED_SEARCH_LOCK:
            cls._OWNED_SEARCH_IDS.add(str(search_id))

    @classmethod
    def unmark_search_owned(cls, search_id: str) -> None:
        with cls._OWNED_SEARCH_LOCK:
            cls._OWNED_SEARCH_IDS.discard(str(search_id))

    @classmethod
    def reset_owned_searches(cls) -> None:
        """Clear the ownership registry (used by tests and manual maintenance)."""
        with cls._OWNED_SEARCH_LOCK:
            cls._OWNED_SEARCH_IDS.clear()

    @classmethod
    def _owned_search_ids_snapshot(cls) -> set[str]:
        with cls._OWNED_SEARCH_LOCK:
            return set(cls._OWNED_SEARCH_IDS)

    def is_connected(self) -> bool:
        """True when slskd's Soulseek client is connected and logged in.

        Used as the gate before a search: a disconnected client cannot search,
        so starting one would fail and the queue would record a misleading
        ``no_results`` + a 24h backoff.  Fails OPEN only when the HTTP client
        cannot tell us at all (missing method) — a state endpoint error is
        already "not connected" at the HTTP layer.
        """
        try:
            checker = getattr(self.http, "is_connected", None)
            if checker is None:
                return True
            return bool(checker())
        except Exception:
            return True

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
    def extract_queue_position(entry: dict | None) -> int | None:
        if not isinstance(entry, dict):
            return None
        # ``is not None`` (NOT an or-chain): a queue position of 0 is a real
        # answer, and ``queueLength`` is the peer's TOTAL queue length, never
        # this file's position — it is not a valid fallback.
        for key in ("queuePosition", "queue_position", "position", "queueIndex"):
            raw = entry.get(key)
            if raw is not None:
                try:
                    return int(raw)
                except Exception:
                    return None
        return None

    def start_search(
        self,
        query: str,
        timeout: float | None = None,
        max_attempts: int = 5,
    ) -> str | None:
        """Start a Soulseek search with an idempotency-safe, client-owned id.

        The search id is generated CLIENT-SIDE and POSTed as ``id`` (slskd
        honours a client-supplied id and only generates its own when absent) —
        so a retry cannot create a SECOND search, and recovery never needs to
        match another worker's search by query text (which let one worker
        adopt and then delete a colleague's search).  After a transient
        failure the server may already have accepted the request: we check
        whether OUR id exists before retrying, and return it if it does.
        """
        if not self.http.enabled:
            return None

        search_id = str(uuid.uuid4())
        data: dict[str, Any] = {"id": search_id, "searchText": query, "filterResponses": False}
        for attempt in range(1, max_attempts + 1):
            try:
                resp = self.http.post_json("searches", data, timeout=timeout)
                if resp.status_code in [200, 201]:
                    self.mark_search_owned(search_id)
                    return search_id

                body_preview = (resp.text or "")[:200]
                retryable_429 = resp.status_code == 429
                if retryable_429 and attempt < max_attempts:
                    wait_seconds = self._retry_after_or_backoff(resp, attempt)
                    time.sleep(wait_seconds)
                    continue

                logger.warning("slskd search start failed", status_code=resp.status_code, preview=body_preview)
                break
            except Exception as exc:
                if _is_transient_error(exc) and attempt < max_attempts:
                    # The server may already have accepted the request before
                    # the timeout — check OUR id before blindly retrying.
                    if self._search_exists(search_id, timeout=timeout):
                        self.mark_search_owned(search_id)
                        logger.info(
                            "slskd search accepted despite transient error",
                            search_id=search_id,
                            error=str(exc),
                        )
                        return search_id
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
                    exc_info=not _is_connection_error(exc),
                )
                break
        return None

    @staticmethod
    def _retry_after_or_backoff(resp: Any, attempt: int) -> float:
        """Seconds to wait after a 429 — Retry-After seconds-or-HTTP-date, else
        a backoff that grows with the attempt.  Status-code based, never
        body-text based.  The ceiling is deliberately HIGH (10s): a hard
        Soulseek server rate-limit window is rarely cleared by a 2s wait."""
        import email.utils

        wait_seconds = 0.8
        retry_after = (getattr(resp, "headers", None) or {}).get("Retry-After") or ""
        retry_after = str(retry_after or "").strip()
        if retry_after:
            try:
                wait_seconds = max(0.2, float(retry_after))
                return wait_seconds
            except ValueError:
                # HTTP-date form — e.g. "Fri, 31 Dec 1999 23:59:59 GMT".
                try:
                    parsed = email.utils.parsedate_to_datetime(retry_after)
                    wait_seconds = max(0.2, (parsed.timestamp() - time.time()))
                    return wait_seconds
                except Exception:
                    pass
        return min(10.0, 1.0 * attempt)

    def _search_exists(self, search_id: str, timeout: float | None = None) -> bool:
        """Whether a search with OUR id exists on the server (idempotency).

        Prefers the HTTP client's ``get_search_state``; falls back to a raw
        ``GET /searches/{id}`` for older/injected clients.  Any failure reads
        as "unknown", and an unknown must NOT cancel a retry we need.
        """
        try:
            getter = getattr(self.http, "get_search_state", None)
            if getter is not None:
                return bool(getter(search_id, timeout=min(int(timeout or 10), 10)))
            return bool(self.http.get_json(f"searches/{quote(search_id, safe='')}", timeout=timeout, default={}))
        except Exception:
            return False

    def get_search_results(self, search_id: str, timeout: float | None = None) -> tuple[list[SearchResponse], str, bool]:
        if not self.http.enabled:
            return [], "Error", True

        state = "InProgress"
        try:
            state_data = self.http.get_json(f"searches/{quote(search_id, safe='')}", timeout=timeout, default={})
            state = state_data.get("state", "InProgress")
        except Exception as exc:
            # The search may have been dropped/expired mid-poll (404) — keep
            # polling it is pointless and burns the whole wait ceiling.
            # Treat a 404 as COMPLETE so the caller drains what it has.
            if _is_not_found_error(exc):
                logger.info("slskd search not found during poll — treated as complete", search_id=search_id)
                return [], "Deleted", True
            logger.warning(
                "slskd get state failed for search",
                search_id=search_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return [], state, False

        # Responses are fetched for EVERY state, including cancelled/errored:
        # a search that was cut short still returned whatever it had by then —
        # discarding it would lose genuine partial results.  Completeness is
        # decided by state, not by whether we bother to fetch.
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
            return responses, state, not self.is_search_active_state(state)
        except Exception as exc:
            # Same 404 treatment on the responses half: the search is gone.
            if _is_not_found_error(exc):
                logger.info("slskd search responses not found — treated as complete", search_id=search_id)
                return [], "Deleted", True
            logger.warning("slskd get responses failed for search", search_id=search_id, error=str(exc))
            return [], state, not self.is_search_active_state(state)

    @classmethod
    def is_search_active_state(cls, state: str) -> bool:
        """Active = literally an in-progress state.

        A state that is NOT in ``SEARCH_ACTIVE_STATES`` is treated as
        complete — including flag-style terminal strings and any future
        terminal variant — so an unknown state can never make the poller wait
        forever.
        """
        return state in SEARCH_ACTIVE_STATES

    @classmethod
    def is_terminal_search_state(cls, state: str) -> bool:
        """Terminal = a known terminal exact state OR a flag-style variant
        (``"Completed, ResponseLimitReached"`` carries the ``completed`` flag)."""
        if state in SEARCH_TERMINAL_STATES:
            return True
        tokens = re.sub(r"[^A-Za-z]", " ", state.lower()).split()
        return any(token in SEARCH_TERMINAL_FLAGS for token in tokens)

    def _peer_enqueue(self, username: str, files: list[dict[str, Any]], timeout: float | None = None) -> bool:
        """POST one peer's files exactly ONCE; True = the peer was asked.

        Single implementation behind both ``download_file`` and
        ``download_files``.  Prefers the HTTP client's ``enqueue_downloads``
        (with ``raise_on_error`` so a silent rejection cannot read as
        acceptance); falls back to the raw POST for clients that predate the
        flag.  None of the (username, filename) values are normalised — a
        remote path with a trailing space is a DIFFERENT path to Soulseek.
        """
        enqueue = getattr(self.http, "enqueue_downloads", None)
        if enqueue is not None:
            try:
                enqueue(username, files, timeout=timeout or 15, raise_on_error=True)
                return True
            except TypeError:
                # Signature predates the raise_on_error/timeout kwargs.
                enqueue(username, files)
                return True
            except Exception as exc:
                # A transient network error must PROPAGATE so the caller's
                # retry loop (`download_file`) can retry — swallowing it here
                # would turn a timeout into a definitive failure.
                if _is_transient_error(exc):
                    raise
                return False
        try:
            safe_user = quote(username or "", safe="")
            resp = self.http.post_json(f"transfers/downloads/{safe_user}", files, timeout=timeout)
            return getattr(resp, "status_code", None) in (200, 201, 204)
        except Exception as exc:
            if _is_transient_error(exc):
                raise
            return False

    def download_file(self, username: str, filename: str, size: int = 0, timeout: float | None = None) -> bool:
        if not self.http.enabled:
            return False
        if not size:
            # slskd requires the REAL size to ask the remote peer for the file;
            # sending a 0-sized request would be rejected by the peer, so a
            # caller that has no size must not fire the request at all.
            logger.warning(
                "slskd download_file called without a file size — request refused",
                username=username,
                filename=str(filename or "")[:80],
            )
            return False
        for attempt in range(1, 3):
            try:
                return self._peer_enqueue(
                    username,
                    [{"filename": filename, "size": int(size or 0)}],
                    timeout=timeout,
                )
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
                logger.error(
                    "slskd download exception",
                    username=username,
                    filename=filename[:50],
                    error=str(exc),
                    exc_info=not _is_connection_error(exc),
                )
                return False
        return False

    def download_files(self, files: list[dict[str, Any]], timeout: float | None = None) -> list[dict[str, Any]]:
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
            # Passed through UNCHANGED: a trailing space is a real part of the
            # remote path and Soulseek matches remote paths exactly.  Only
            # empty/None values are skipped.
            entry_user = entry.get("username")
            entry_filename = entry.get("filename")
            if entry_user is None or not str(entry_user).strip():
                continue
            if entry_filename is None or not str(entry_filename).strip():
                continue
            by_user.setdefault(str(entry_user), []).append({
                "filename": str(entry_filename),
                "size": int(entry.get("size") or 0),
            })

        results: list[dict[str, Any]] = []
        for entry_user, payload_files in by_user.items():
            peer_success = False
            peer_error: str | None = None
            try:
                peer_success = self._peer_enqueue(entry_user, payload_files, timeout=timeout)
            except Exception as exc:
                peer_error = str(exc)

            if not peer_success:
                peer_error = peer_error or "slskd rejected the request"
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
                "transfer_ids": [],
                "success": peer_success,
                "error": peer_error,
            })

        return results

    @staticmethod
    def _rank_query_tokens(query: str) -> list[str]:
        """Query tokens for relevance ranking.

        Non-ASCII characters are preserved (`Ça ira`, `Mötley`, `Björk`) via
        NFKD + combining-mark strip, and apostrophes are removed so `don't`
        keys as ``dont`` rather than being split into ``don`` + ``t``.
        """
        import unicodedata

        cleaned = unicodedata.normalize("NFKD", query or "")
        cleaned = "".join(ch for ch in cleaned if not unicodedata.combining(ch))
        cleaned = cleaned.replace("'", "")
        return [t for t in re.findall(r"[a-zA-Z0-9]+", cleaned.lower()) if len(t) >= 2]

    @staticmethod
    def _rank_qualified(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
        """Rank qualified results: relevance to the QUERY first, then peer
        availability, then quality.

        Relevance weighs BASENAME matches (the real file name) above folder-
        path matches — a query for "Track" must not be satisfied by a folder
        named Track with a different song inside it.  Peer availability ranks
        ABOVE bitrate: a 320k MP3 from an open-slot peer downloads instantly, a
        perfect FLAC behind a 4000-deep queue sits in ``Queued, Remotely``.
        """
        query_tokens = SlskdService._rank_query_tokens(query)
        token_set = frozenset(query_tokens)

        def _rank(item: dict) -> tuple:
            filename = str(item.get("filename") or "")
            base = _extension_from_filename(filename)
            # Tokenise the ACTUAL file name separately from its directory: a
            # query for "Track" must not be satisfied by a folder called Track
            # that holds a different song.  ``os.path.basename``/``dirname``
            # cope with the peer's "/" separators (and Windows "\\") alike.
            basename = os.path.basename(filename) or filename
            folder_path = os.path.dirname(filename)
            basename_tokens = frozenset(SlskdService._rank_query_tokens(basename))
            folder_tokens = frozenset(SlskdService._rank_query_tokens(folder_path))
            basename_hits = len(basename_tokens & token_set)
            folder_hits = len(folder_tokens & token_set)
            # Weighted relevance: a basename match is worth 3x a folder match,
            # so the file NAMED "Song" outranks a folder named "Song" holding a
            # different track — while a genuine folder match still earns credit
            # (the previous form credited every non-basename token, so folder
            # matches added nothing to the ranking).
            relevance = basename_hits * 3 + folder_hits
            return (
                -relevance,
                # peer availability first: OPEN-slot peers sort before
                # slot-less ones (ascending), and an unknown slot is NOT
                # treated as full — a missing value is not evidence busy.
                0 if item.get("has_free_upload_slot") is not False else 1,
                -int(item.get("bitrate") or 0),
                -int(item.get("sample_rate") or 0),
                int(item.get("queue_length") or 0),
                -(item.get("upload_speed") if isinstance(item.get("upload_speed"), (int, float)) else 0),
                str(item.get("extension") or base),
            )

        return sorted(items, key=_rank)

    def filter_results_by_quality(
        self,
        responses: list[SearchResponse],
        min_bitrate: int = 192,
        min_sample_rate: int = 44100,
        max_results: int | None = 50,
        query: str = "",
        reject_reasons: dict[str, int] | None = None,
    ) -> list[dict]:
        """Qualify a poll snapshot; ``max_results=None`` keeps EVERYTHING.

        Direct callers keep the old default cap; ``search_and_filter`` polls
        with ``max_results=None`` and applies the cap ONCE over the whole
        accumulated set, so a correct match that never made the top-50 of a
        single snapshot is still seen.
        """
        qualified: list[dict[str, Any]] = []
        for response in responses:
            for file in response.files:
                if file.matches_quality(min_bitrate, min_sample_rate, reject_reasons=reject_reasons):
                    qualified.append({
                        "username": response.username,
                        "filename": file.filename,
                        "size": file.size,               # raw byte size
                        "size_mb": round(file.size_mb, 2),
                        "bitrate": file.bitrate,
                        "sample_rate": file.sample_rate,
                        "bit_depth": file.bit_depth,
                        "extension": file.extension,
                        "is_lossless": file.is_lossless,
                        "duration": file.duration_formatted,
                        "length_seconds": file.length,
                        "has_free_upload_slot": getattr(response, "has_free_upload_slot", None),
                        "upload_speed": getattr(response, "upload_speed", None),
                        "queue_length": getattr(response, "queue_length", None),
                    })
        ranked = SlskdService._rank_qualified(qualified, query)
        if max_results is not None:
            ranked = ranked[:int(max_results)]
        return ranked

    def search_and_filter(
        self,
        query: str,
        min_bitrate: int | None = None,
        wait_seconds: int | None = None,
        poll_interval: float = 1.0,
        timeout: float | None = None,
        empty_abort_seconds: float | None = None,
        cancel_when_done: bool = True,
    ) -> list[dict] | None:
        """Search, poll to completion and return the ranked qualified files.

        Returns ``None`` when the Soulseek client is DISCONNECTED — a distinct
        outcome the caller must NOT record as ``no_results``/backoff (the
        queue would otherwise punish a connectivity problem with the search-miss
        ladder).  Returns ``[]`` for a genuinely empty search.
        """
        if min_bitrate is None:
            from helpers.config_helpers import get_search_quality_config
            min_bitrate = get_search_quality_config()["min_bitrate"]
        if wait_seconds is None:
            from helpers.config_helpers import _SLSKD_SEARCH_MAX_WAIT_SECONDS
            wait_seconds = _SLSKD_SEARCH_MAX_WAIT_SECONDS
        if empty_abort_seconds is None:
            from helpers.config_helpers import get_queue_search_tuning
            empty_abort_seconds = float(get_queue_search_tuning()["search_empty_abort_seconds"] or 0)

        # Server-state gate: a disconnected client cannot search.  This is
        # what stops the queue recording `no_results` + a 24h backoff while
        # slskd is simply not logged in.
        if not self.is_connected():
            logger.warning("Soulseek server not connected — search not attempted", query=query)
            return None

        try:
            self.maybe_clear_stale_searches(budget_seconds=6)
        except Exception:
            pass

        if min_bitrate < 0:
            min_bitrate = 0
        search_id = self.start_search(query, timeout=timeout)
        if not search_id:
            return []

        search_wall_started = time.monotonic()
        deadline = time.monotonic() + max(0, int(wait_seconds or 0))
        poll_attempt = 0
        seen: set[tuple[str, str]] = set()
        accumulated: list[dict] = []
        reject_reasons: dict[str, int] = {}
        raw_files_seen = 0

        def _drain() -> None:
            nonlocal raw_files_seen
            responses, _state, _complete = self.get_search_results(search_id, timeout=timeout)
            raw_files_seen += _raw_file_count(responses)
            for item in self.filter_results_by_quality(
                responses, min_bitrate=min_bitrate, query=query, max_results=None, reject_reasons=reject_reasons
            ):
                key = (item["username"], item["filename"].casefold())
                if key not in seen:
                    seen.add(key)
                    accumulated.append(item)

        try:
            while True:
                base_delay = float(poll_interval or 1.0)
                delay = base_delay if poll_attempt < 2 else (2.0 if poll_attempt < 10 else 5.0)
                poll_attempt += 1
                # Never sleep past the deadline: a wait_seconds shorter than
                # the first delay must still get at least one poll.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                delay = min(delay, max(0.01, remaining))
                time.sleep(delay)
                responses, _state, is_complete = self.get_search_results(search_id, timeout=timeout)
                raw_files_seen += _raw_file_count(responses)
                for item in self.filter_results_by_quality(
                    responses, min_bitrate=min_bitrate, query=query, max_results=None, reject_reasons=reject_reasons
                ):
                    key = (item["username"], item["filename"].casefold())
                    if key not in seen:
                        seen.add(key)
                        accumulated.append(item)
                if is_complete:
                    break
                # Zero-result early abort — based on RAW files, not qualified
                # ones: a search streaming many files that ALL fail the quality
                # gate is NOT empty, so it must not be cancelled as one.
                if (
                    empty_abort_seconds
                    and empty_abort_seconds > 0
                    and raw_files_seen == 0
                    and (time.monotonic() - search_wall_started) >= empty_abort_seconds
                ):
                    logger.info(
                        "Empty search cancelled early",
                        search_id=search_id,
                        query=query,
                        elapsed_s=round(time.monotonic() - search_wall_started, 1),
                    )
                    self.cancel_search(search_id, timeout=2)
                    break

            # ALWAYS one final drain: anything that arrived after the last poll
            # (or was missed by a deadline exit) must not be thrown away.
            _drain()
        finally:
            if cancel_when_done:
                try:
                    self.cancel_search(search_id, timeout=2)
                except Exception:
                    pass
            else:
                try:
                    self.unmark_search_owned(search_id)
                except Exception:
                    pass

        if reject_reasons:
            logger.debug(
                "Search filtered files by reason",
                query=query,
                raw_files=raw_files_seen,
                qualified=len(accumulated),
                reasons=reject_reasons,
            )

        # Cap ONCE, over the whole accumulated set — never per snapshot.
        ranked = SlskdService._rank_qualified(accumulated, query)
        return ranked[:50]

    def parse_transfers_response(self, raw: list | dict) -> list[dict]:
        if isinstance(raw, dict):
            raw = raw.get("downloads") or raw.get("transfers") or raw.get("items") or []
        flat = []
        seen: set[tuple[str, str, str]] = set()
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
                # slskd can return None / non-numeric values for the size and
                # byte counts in the first moments of a transfer (before it has
                # negotiated with the peer).  ``_to_int`` degrades those to 0
                # and the percentComplete fallback keeps the bar sane.
                size = _to_int(item.get("size"))
                bytes_transferred = _to_int(item.get("bytesTransferred"))
                if size:
                    progress = min(100, round((bytes_transferred / size) * 100, 2))
                else:
                    progress = _to_int(item.get("percentComplete"))
                progress = max(0, min(100, progress or 0))
                # slskd can list the same transfer BOTH nested (in a directory)
                # and at the top level — dedupe on its identity, not its shape.
                transfer_item = {
                    "id": item.get("id") or item.get("remoteToken") or item.get("token") or "",
                    "username": username,
                    "filename": item.get("filename") or item.get("fileName") or item.get("name") or item.get("path") or "",
                    "size": size,
                    "bytesTransferred": bytes_transferred,
                    "progress": progress,
                    "state": self.state_text(item.get("state") or item.get("transferState") or item.get("status")),
                    "averageSpeed": _to_int(item.get("averageSpeed")),
                    "queuePosition": self.extract_queue_position(item),
                    # NOT the generic ``path`` key — that is the remote path,
                    # not where the file landed locally.
                    "localFilePath": item.get("localFilePath") or item.get("localPath") or item.get("downloadedFilePath") or "",
                    "startedAt": item.get("startedAt") or item.get("started"),
                }
                key = (username, str(transfer_item["id"]), str(transfer_item["filename"]).casefold())
                if key in seen:
                    continue
                seen.add(key)
                flat.append(transfer_item)
        return flat

    def get_active_downloads(self, timeout: float | None = None) -> list[dict]:
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

    def get_completed_transfers(self, timeout: float | None = None) -> list[dict]:
        return [item for item in self.get_active_downloads(timeout=timeout) if self.is_success_state(item.get("state"))]

    def list_searches(self, timeout: float | None = None) -> list[dict]:
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
        """Cancel OUR stale searches (plus truly orphaned ones).

        Ownership rule (see the class notes on ``_OWNED_SEARCH_IDS``): a
        search only touches slskd when it is OURS — terminal OR stuck past
        ``STUCK_SEARCH_TIMEOUT_MS`` — or when it is NOT ours but has been
        running longer than ``ORPHANED_SEARCH_TIMEOUT_MS`` (a genuinely
        abandoned search).  Terminal-state searches the user started in the
        slskd web UI are left alone: their results may still be on screen.
        """
        deadline = time.monotonic() + max(0.1, float(budget_seconds))
        owned = type(self)._owned_search_ids_snapshot()
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
                is_owned = sid in owned
                elapsed_ms = _search_elapsed_ms(s)

                if is_owned and self.is_terminal_search_state(state):
                    should_cancel = True
                elif is_owned and elapsed_ms > STUCK_SEARCH_TIMEOUT_MS:
                    should_cancel = True
                    logger.info("Cancelling stuck owned search", search_id=sid, state=state, elapsed_ms=elapsed_ms)
                elif not is_owned and elapsed_ms > ORPHANED_SEARCH_TIMEOUT_MS:
                    should_cancel = True
                    logger.info("Cancelling orphaned search", search_id=sid, state=state, elapsed_ms=elapsed_ms)
                else:
                    should_cancel = False

                if not should_cancel:
                    continue

                if state in SEARCH_ACTIVE_STATES or state in SEARCH_TERMINAL_STATES:
                    stop_ok = self._stop_search(sid, timeout=max(0.5, min(2, deadline - time.monotonic())))
                    if not stop_ok:
                        logger.warning("Stale search stop returned unexpected status", search_id=sid, state=state)

                if time.monotonic() >= deadline:
                    logger.warning("Stale search cleanup budget exhausted")
                    break
                if self.cancel_search(sid, timeout=max(0.5, min(2, deadline - time.monotonic()))):
                    if is_owned and self.is_terminal_search_state(state):
                        logger.info("Cleared owned stale search", search_id=sid, state=state)
        except Exception as cleanup_err:
            logger.warning("Could not clear stale searches", error=str(cleanup_err))

    def _stop_search(self, search_id: str, timeout: float | None = None) -> bool:
        """Stop an ACTIVE search — prefers the HTTP client's documented
        ``stop_search`` (PUT), with a raw-PUT fallback for older clients."""
        try:
            stop = getattr(self.http, "stop_search", None)
            if stop is not None:
                return bool(stop(search_id))
            resp = self.http.put(f"searches/{quote(search_id, safe='')}", timeout=timeout)
            return getattr(resp, "status_code", None) in (200, 204, 304)
        except Exception as exc:
            logger.warning("stop search failed", search_id=search_id, error=str(exc), error_type=type(exc).__name__)
            return False

    def _delete_search(self, search_id: str, timeout: float | None = None) -> bool:
        """Delete a search — prefers ``delete_search``; 404 counts as success
        (the search is already gone)."""
        try:
            deleter = getattr(self.http, "delete_search", None)
            if deleter is not None:
                return bool(deleter(search_id))
            resp = self.http.delete(f"searches/{quote(search_id, safe='')}", timeout=timeout)
            return getattr(resp, "status_code", None) in (200, 204)
        except Exception as exc:
            logger.warning("delete search failed", search_id=search_id, error=str(exc), error_type=type(exc).__name__)
            return False

    def cancel_search(self, search_id: str, timeout: float | None = None) -> bool:
        """Stop an active search and remove it (best effort on each half).

        The active half uses the explicit stop (PUT) so the search does not
        keep streaming responses into slskd; removal treats a 404 as success
        (already deleted).  A 409/500 "concurrency" reply on delete is NOT
        treated as success — it is logged; the search's cleanup is retried at
        the next clean cycle, not silently swallowed.
        """
        self.unmark_search_owned(search_id)
        try:
            self._stop_search(search_id, timeout=timeout)
            return self._delete_search(search_id, timeout=timeout)
        except Exception as exc:
            logger.warning(
                "slskd cancel search failed",
                search_id=search_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return False

    def cancel_download(self, username: str, transfer_id: str, remove: bool = True, timeout: float | None = None) -> bool:
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

    def clear_completed_downloads(self, timeout: float | None = None) -> bool:
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
