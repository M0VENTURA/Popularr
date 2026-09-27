"""HTTP utility helpers for API clients.

Provides a retry-capable httpx client factory and a custom transport
that handles TLS issues gracefully.

Replaced the old ``requests``/``urllib3``-based implementation with
``httpx`` for better connection pooling, HTTP/2 support, and async
compatibility.
"""

from __future__ import annotations

import datetime
import logging
import ssl
import time
import threading
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Global Rate Limiting (Cross-Thread)
# ---------------------------------------------------------------------------
_GLOBAL_LOCK = threading.Lock()
_DOMAIN_LOCKS: dict[str, threading.Lock] = {}
# Tracks the NEXT allowed execution time, not the last call time.
_DOMAIN_NEXT_ALLOWED: dict[str, float] = {}
_MIN_DELAY_SECONDS = 1.05  # Ensures strictly < 1 req/sec per domain

# ---------------------------------------------------------------------------
# Connection-pool limits (shared session)
# ---------------------------------------------------------------------------
_POOL_MAX_CONNECTIONS = 200
_POOL_MAX_KEEPALIVE = 50
_POOL_TIMEOUT = 30.0

_CONNECT_TIMEOUT_SECONDS = 10.0
_MAX_RETRY_AFTER = 60.0
_TOTAL_RETRY_WAIT_BUDGET = 40.0


def _build_pool_limits() -> httpx.Limits:
    """Build ``httpx.Limits`` for the shared session, httpx-version-safe."""
    try:
        return httpx.Limits(
            max_connections=_POOL_MAX_CONNECTIONS,
            max_keepalive_connections=_POOL_MAX_KEEPALIVE,
            pool_timeout=_POOL_TIMEOUT,
        )
    except TypeError:  # httpx < 0.25 — no pool_timeout kwarg
        return httpx.Limits(
            max_connections=_POOL_MAX_CONNECTIONS,
            max_keepalive_connections=_POOL_MAX_KEEPALIVE,
        )


logger = logging.getLogger(__name__)

# Explicitly suppress noisy internal logging from httpcore and httpx transport checks
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)


def is_ssl_cert_error(exc: BaseException) -> bool:
    """True when the exception chain contains an SSL certificate failure."""
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    cause = getattr(exc, "__cause__", None)
    depth = 0
    while cause is not None and depth < 6:
        if isinstance(cause, ssl.SSLCertVerificationError):
            return True
        try:
            if "CERTIFICATE_VERIFY_FAILED" in str(cause):
                return True
        except Exception:
            pass
        cause = getattr(cause, "__cause__", None)
        depth += 1
    try:
        if "CERTIFICATE_VERIFY_FAILED" in str(exc):
            return True
    except Exception:
        pass
    return False


class _RetryTransport(httpx.BaseTransport):
    """Transport wrapper that adds retry logic on top of httpx's HTTPTransport."""

    def __init__(
        self,
        retries: int = 3,
        backoff: float = 1.0,
        status_forcelist: tuple[int, ...] = (429, 500, 502, 503, 504),
        verify: bool = True,
    ):
        self._retries = retries
        self._backoff = backoff
        self._status_forcelist = status_forcelist
        self._transport = httpx.HTTPTransport(
            verify=verify,
            retries=0,
            limits=_build_pool_limits(),
        )

    def handle_request(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        from tenacity import (
            Retrying,
            retry_if_exception,
            stop_after_attempt,
        )

        class _RetryableStatus(Exception):
            """Marker raised when the peer returned a retryable status code."""

            def __init__(self, response: httpx.Response):
                super().__init__(f"Retryable HTTP status {response.status_code}")
                self.response = response

        class _RetryBudgetExceeded(Exception):
            """Marker raised when the cumulative retry-wait budget is exhausted."""

        def _is_retryable(exc: BaseException) -> bool:
            if isinstance(exc, _RetryableStatus):
                return True
            if isinstance(exc, _RetryBudgetExceeded):
                return False
            if is_ssl_cert_error(exc):
                return False
            return isinstance(
                exc,
                (httpx.ConnectError, httpx.RemoteProtocolError, httpx.TimeoutException),
            )

        last_status_response: httpx.Response | None = None
        #: 1-based count of transport attempts, so a retried failure can say
        #: WHICH attempt it was instead of reading like a hard failure.
        attempt_no = 0

        def _attempt() -> httpx.Response:
            nonlocal last_status_response, attempt_no
            attempt_no += 1
            
            host = request.url.host
            is_internal = host in ("127.0.0.1", "localhost") or host.startswith("192.168.") or host.startswith("10.") or host.startswith("172.")
            thread_name = threading.current_thread().name

            if not is_internal:
                with _GLOBAL_LOCK:
                    if host not in _DOMAIN_LOCKS:
                        _DOMAIN_LOCKS[host] = threading.Lock()
                        _DOMAIN_NEXT_ALLOWED[host] = 0.0
                domain_lock = _DOMAIN_LOCKS[host]

                sleep_time = 0.0
                # Calculate required sleep AND update the next allowed slot INSIDE the lock.
                with domain_lock:
                    now = time.monotonic()
                    next_allowed = _DOMAIN_NEXT_ALLOWED[host]
                    if now < next_allowed:
                        sleep_time = next_allowed - now
                        _DOMAIN_NEXT_ALLOWED[host] = next_allowed + _MIN_DELAY_SECONDS
                    else:
                        _DOMAIN_NEXT_ALLOWED[host] = now + _MIN_DELAY_SECONDS

                # Sleep OUTSIDE the lock.
                if sleep_time > 0:
                    time.sleep(sleep_time)
                
                # Execute the network request OUTSIDE the lock.
                try:
                    response = self._transport.handle_request(request)
                except Exception as e:
                    # ⚠️ A RETRIED attempt is NOT an error, and this log used to
                    # say it was: it sits INSIDE the retry loop, so a transient
                    # ReadTimeout that the policy retries — and that the caller
                    # handles, e.g. get_similar_artists returning [] — was
                    # reported as a hard failure once PER ATTEMPT. That is the
                    # reported "[HTTP-TRACE] ... ReadTimeout" ERROR noise from
                    # labs.api.listenbrainz.org.
                    #
                    # The level now states what actually happened: a RETRYABLE
                    # failure is a WARNING naming the attempt, while anything
                    # the policy will NOT retry (an SSL certificate error, a
                    # malformed request) is final by definition and stays ERROR.
                    # The exception still propagates, so a caller that treats it
                    # as fatal reports it itself, with the context only it has.
                    if _is_retryable(e):
                        logger.warning(
                            f"[HTTP-TRACE] [{thread_name}] Network I/O attempt "
                            f"failed for {request.url} "
                            f"(attempt {attempt_no}/{self._retries + 1}) - {repr(e)}"
                        )
                    else:
                        logger.error(
                            f"[HTTP-TRACE] [{thread_name}] Network I/O FAILED for "
                            f"{request.url} - {repr(e)}"
                        )
                    raise
            else:
                try:
                    response = self._transport.handle_request(request)
                except Exception as e:
                    # Same distinction as above, for a local/internal host.
                    if _is_retryable(e):
                        logger.warning(
                            f"[HTTP-TRACE] [{thread_name}] Internal Network I/O "
                            f"attempt failed for {request.url} "
                            f"(attempt {attempt_no}/{self._retries + 1}) - {repr(e)}"
                        )
                    else:
                        logger.error(
                            f"[HTTP-TRACE] [{thread_name}] Internal Network I/O "
                            f"FAILED for {request.url} - {repr(e)}"
                        )
                    raise

            if response.status_code in self._status_forcelist:
                if last_status_response is not None and last_status_response is not response:
                    try:
                        last_status_response.close()
                    except Exception:
                        pass
                last_status_response = response
                raise _RetryableStatus(response)
                
            if last_status_response is not None and last_status_response is not response:
                try:
                    last_status_response.close()
                except Exception:
                    pass
                last_status_response = None

            return response

        _retry_waits: dict[str, float] = {"total": 0.0}

        def _wait(retry_state):
            exc = retry_state.outcome.exception()
            retry_after = None
            if isinstance(exc, _RetryableStatus):
                response = getattr(exc, "response", None)
                if response is not None:
                    retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = min(float(retry_after), _MAX_RETRY_AFTER)
                except ValueError:
                    from email.utils import parsedate_to_datetime
                    try:
                        when = parsedate_to_datetime(retry_after)
                        if when.tzinfo is None:
                            when = when.replace(tzinfo=datetime.timezone.utc)
                        wait = min(
                            max(0.0, (when - datetime.now(datetime.timezone.utc)).total_seconds()),
                            _MAX_RETRY_AFTER,
                        )
                    except Exception:
                        wait = min(self._backoff * (2 ** (retry_state.attempt_number - 1)), 60.0)
            else:
                n = retry_state.attempt_number
                wait = min(self._backoff * (2 ** (n - 1)), 60.0)

            if _retry_waits["total"] + wait > _TOTAL_RETRY_WAIT_BUDGET:
                raise _RetryBudgetExceeded()
            _retry_waits["total"] += wait
            return wait

        retrying = Retrying(
            stop=stop_after_attempt(self._retries + 1),
            wait=_wait,
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )

        try:
            for attempt in retrying:
                with attempt:
                    return _attempt()
        except _RetryBudgetExceeded:
            if last_status_response is not None:
                return last_status_response
            raise
        except Exception:
            if last_status_response is not None:
                return last_status_response
            raise

    def close(self) -> None:
        self._transport.close()

    @property
    def is_closed(self) -> bool:
        return self._transport.is_closed


MINIMUM_TLS_VERSION = ssl.TLSVersion.TLSv1_2


def create_retry_client(
    user_agent: str | None = None,
    retries: int = 5,
    backoff: float = 1.2,
    status_forcelist: tuple[int, ...] = (429, 500, 502, 503, 504),
    verify: bool = True,
    timeout: float = 30.0,
) -> httpx.Client:
    """Create an ``httpx.Client`` preconfigured with retry/backoff."""
    transport = _RetryTransport(
        retries=retries,
        backoff=backoff,
        status_forcelist=status_forcelist,
        verify=verify,
    )

    headers = {}
    if user_agent:
        headers["User-Agent"] = user_agent

    client = httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(
            connect=_CONNECT_TIMEOUT_SECONDS,
            read=timeout,
            write=timeout,
            pool=_POOL_TIMEOUT,
        ),
        headers=headers,
        verify=verify,
        limits=_build_pool_limits(),
    )

    return client


__all__ = ["_RetryTransport", "create_retry_client"]
