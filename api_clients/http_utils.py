"""HTTP utility helpers for API clients.

Provides a retry-capable httpx client factory and a custom transport
that handles TLS issues gracefully.

Replaced the old ``requests``/``urllib3``-based implementation with
``httpx`` for better connection pooling, HTTP/2 support, and async
compatibility.
"""

from __future__ import annotations

import datetime
import ipaddress
import logging
import math
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


# ---------------------------------------------------------------------------
# Private / loopback host detection
# ---------------------------------------------------------------------------

def is_private_host(host: str | None) -> bool:
    """True when *host* is a private, loopback or link-local address.

    ⭐⭐ THE OLD TEST WAS A STRING PREFIX AND IT WAS WRONG IN BOTH DIRECTIONS:

    * ``host.startswith("172.")`` matches PUBLIC space. ``172.16.0.0/12`` is the
      private range, but ``172.217.x.x`` is Google and ``172.64.x.x`` is
      Cloudflare — every request to those skipped rate limiting entirely.
    * ``host.startswith("10.")`` matches any hostname beginning with the
      DIGITS "10", and misses nothing only by luck.
    * ``192.168.`` is correct but by coincidence.

    ``ipaddress`` decides it properly, and it also refuses to guess: a hostname
    (``localhost``, ``navidrome.local``) is NOT parsed as an IP, so the small
    explicit name list covers the cases a string check was actually for.
    """
    name = str(host or "").strip().lower().rstrip(".")
    if not name:
        return False
    if name in ("localhost", "localhost.localdomain", "ip6-localhost"):
        return True

    # Strip an IPv6 zone index (``fe80::1%eth0``) and brackets.
    candidate = name.split("%", 1)[0].strip("[]")
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False

    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    )


def _is_internal_host(host: str | None) -> bool:
    """Backwards-compatible alias for :func:`is_private_host`."""
    return is_private_host(host)


def _throttle_shared(provider: str) -> None:
    """Reserve a slot on the DEPLOYMENT-WIDE rate limiter, or quietly skip.

    The limiter is imported lazily: ``services.infrastructure`` is a higher
    layer than ``api_clients``, and importing it at module scope would make this
    low-level module depend on the service layer at import time.

    ⚠️ NEVER RAISES. This runs inside the retry transport, where an exception
    would replace a retryable network failure with a limiter failure — turning
    a recoverable 503 into a hard error. A broken limiter degrades to the
    in-process per-domain pacing, which is what the module already had.
    """
    try:
        from services.infrastructure.api_rate_limiter import get_rate_limiter

        limiter = get_rate_limiter()
        throttle = getattr(limiter, f"throttle_{provider}", None)
        if callable(throttle):
            throttle()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "Shared rate limiter unavailable, relying on in-process pacing",
            provider=provider,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Retry-After parsing
# ---------------------------------------------------------------------------

def parse_retry_after(value: Any, *, default: float | None = None) -> float | None:
    """Seconds to wait from a ``Retry-After`` header, or *default*.

    Accepts both RFC 9110 forms: delta-seconds (``"120"``) and an HTTP-date.

    ⭐⭐ WHY THIS IS A FUNCTION AND NOT ``float(...)`` INLINE. The old code did
    ``min(float(retry_after), 60)``, which passes through anything ``float()``
    accepts:

    * ``"-5"`` → ``-5.0`` → ``time.sleep(-5)`` raises ``ValueError``;
    * ``"nan"`` → ``nan`` (``min(nan, 60)`` is ``nan``) → ``time.sleep(nan)``
      raises ``ValueError``;
    * ``"inf"`` → clamped, fine, but ``"-inf"`` is not.

    A malformed header must never raise: the caller is inside a retry loop and
    the only thing worse than waiting too little is crashing the retry policy.
    """
    if value is None:
        return default

    raw = str(value).strip()
    if not raw:
        return default

    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        seconds = None

    if seconds is not None:
        if not math.isfinite(seconds):
            # nan / inf / -inf carry no wait information.
            return default
        if seconds < 0:
            # ⚠️ A negative delta is MALFORMED, not "wait zero". Returning 0
            # here would retry immediately — the opposite of what a Retry-After
            # is for — so it falls back to the backoff schedule instead.
            return default
        return min(seconds, _MAX_RETRY_AFTER)

    # HTTP-date form.
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(raw)
    except Exception:
        return default

    if when is None:
        return default
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)

    delta = (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    if not math.isfinite(delta):
        return default
    return max(0.0, min(delta, _MAX_RETRY_AFTER))

#: ── Per-host timeout cooldown ────────────────────────────────────────────────
#:
#: A host that answers NOTHING costs ``attempts x read-timeout`` per call:
#: 4 attempts x 20s = ~80s for ListenBrainz, on EVERY request. During a scan
#: that consumed the whole 900s per-album budget in retries alone and the album
#: was then abandoned having processed 0 tracks, while the log filled with
#: ReadTimeout after ReadTimeout.
#:
#: Once a host has failed twice with every attempt timing out, the next request
#: to it fails IMMEDIATELY with the same exception the caller already handles
#: (``httpx.ReadTimeout``) — so no caller changes and no new failure mode, just
#: ~80s returned to the scan. The window is per HOST, not per client, so one
#: dead provider (ListenBrainz) cannot stall the others (MusicBrainz, Navidrome).
_HOST_TIMEOUT_COOLDOWN: dict[str, float] = {}
_HOST_TIMEOUT_STRIKES: dict[str, int] = {}
#: The exception a cooling-down host raises. A timeout cooldown keeps raising
#: ``ReadTimeout`` (the failure the caller already handles); a name-resolution /
#: connection cooldown raises ``ConnectError``, which is ALSO the exact class
#: that failure produced before the cooldown existed — so callers need no change.
_HOST_COOLDOWN_ERROR: dict[str, type[BaseException]] = {}
_HOST_COOLDOWN_SECONDS = 60.0
#: Two, not one: a single transient failure must not make a host untouchable —
#: Navidrome imports would stall for a minute over one slow cover-art fetch or
#: one resolver hiccup.
_HOST_COOLDOWN_STRIKES = 2
_COOLDOWN_LOCK = threading.Lock()


def _host_is_cooling_down(host: str) -> float:
    """Seconds left in *host*'s cooldown, or ``0`` when it is not cooling."""
    if not host:
        return 0.0
    now = time.monotonic()
    with _COOLDOWN_LOCK:
        until = _HOST_TIMEOUT_COOLDOWN.get(host, 0.0)
    return max(0.0, until - now)


def _host_cooldown_error(host: str) -> type[BaseException]:
    """The exception class a cooling-down *host* should raise."""
    if not host:
        return httpx.ReadTimeout
    with _COOLDOWN_LOCK:
        return _HOST_COOLDOWN_ERROR.get(host, httpx.ReadTimeout)


def _note_host_success(host: str) -> None:
    if not host:
        return
    with _COOLDOWN_LOCK:
        _HOST_TIMEOUT_STRIKES.pop(host, None)
        _HOST_COOLDOWN_ERROR.pop(host, None)


def _note_host_failure(host: str, error_type: type[BaseException]) -> None:
    """Record a request whose attempts ALL failed; open the window at 2.

    BOTH an all-timeout request and an all-connection-error request count:
    ``[Errno -3] Temporary failure in name resolution`` is a live example of
    the latter. A resolver outage retried four times per call, on every call,
    wasted the scan budget exactly like a dead peer did — and the existing
    cooldown never fired because it only watched ``TimeoutException``.
    """
    if not host:
        return
    with _COOLDOWN_LOCK:
        strikes = _HOST_TIMEOUT_STRIKES.get(host, 0) + 1
        _HOST_TIMEOUT_STRIKES[host] = strikes
        if strikes < _HOST_COOLDOWN_STRIKES:
            return
        _HOST_TIMEOUT_STRIKES.pop(host, None)
        _HOST_TIMEOUT_COOLDOWN[host] = time.monotonic() + _HOST_COOLDOWN_SECONDS
        _HOST_COOLDOWN_ERROR[host] = error_type
    # NOTE: this module's ``logger`` is the stdlib one, so the context goes in
    # the message rather than as kwargs (structlog-style calls raise here).
    logger.warning(
        "[HTTP] host cooldown opened — failing fast for "
        f"{_HOST_COOLDOWN_SECONDS:.0f}s: {strikes} consecutive requests to "
        f"{host} failed on every attempt ({error_type.__name__})"
    )


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

        #: Set when this request is answered at all; used to decide whether a
        #: timeout means "this host is down" rather than "this endpoint is slow".
        host = (request.url.host or "").lower()
        remaining = _host_is_cooling_down(host)
        if remaining > 0:
            # Same exception the caller already handles for this host, minus
            # another ~80s of attempts that cannot succeed.
            raise _host_cooldown_error(host)(
                f"{host} is in a failure cooldown ({remaining:.0f}s left)"
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
            # ⭐ Proper private-network detection — see ``is_private_host``.
            is_internal = is_private_host(host)
            thread_name = threading.current_thread().name

            # ⭐⭐ DEPLOYMENT-WIDE THROTTLE ON EVERY RETRY ATTEMPT.
            #
            # MusicBrainz's terms are about REQUESTS, not about successful
            # calls: a retry is another request and counts against the same
            # 1 req/s budget. ``musicbrainz_http.get()`` reserves a slot once
            # per CALL (before its own attempt loop), so attempt 1 is already
            # accounted for and must NOT be charged twice — charging it here
            # as well would halve throughput for every scan. Attempts 2+ were
            # the gap: they reached the wire having reserved nothing.
            #
            # In-process only: the shared limiter's own flock spans the whole
            # deployment, so this is not a second budget.
            if attempt_no > 1 and (host or "").lower() == "musicbrainz.org":
                _throttle_shared("musicbrainz")

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

            # ⭐ A validated delay: a negative, NaN, infinite or unparseable
            # header falls back to the backoff schedule instead of raising
            # inside the retry policy (see ``parse_retry_after``).
            backoff = min(self._backoff * (2 ** (retry_state.attempt_number - 1)), 60.0)
            wait = parse_retry_after(retry_after, default=backoff)
            if wait is None:
                wait = backoff

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
                    response = _attempt()
                    _note_host_success(host)
                    return response
        except _RetryBudgetExceeded:
            if last_status_response is not None:
                return last_status_response
            raise
        except httpx.TimeoutException:
            # Every attempt timed out: the peer is not answering at all, not
            # "one slow response". Record it so the next call to this host can
            # fail fast instead of re-paying attempts x read-timeout.
            _note_host_failure(host, httpx.ReadTimeout)
            if last_status_response is not None:
                return last_status_response
            raise
        except httpx.ConnectError:
            # Every attempt failed to CONNECT — a DNS outage
            # (``[Errno -3] Temporary failure in name resolution``), a refused
            # connection, or a TLS handshake failure. Retrying this request
            # again on the next call cannot help either; treat the host as down
            # so the cooldown stops the log from filling with one warning per
            # attempt for every track.
            _note_host_failure(host, httpx.ConnectError)
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
    follow_redirects: bool = True,
) -> httpx.Client:
    """Create an ``httpx.Client`` preconfigured with retry/backoff.

    ``follow_redirects`` defaults ON because httpx does NOT follow redirects
    by default, and the APIs we talk to use them: MusicBrainz 301-redirects
    merged/deleted MBIDs to their new location, and treating that as a
    permanent ``301 Moved Permanently`` failure meant every lookup of a
    merged recording returned nothing.
    """
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
        follow_redirects=follow_redirects,
    )

    return client


__all__ = ["_RetryTransport", "create_retry_client"]
