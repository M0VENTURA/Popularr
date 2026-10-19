"""Regression tests for the Discogs throttle / retry stalls.

Reproduces the "N (of N) futures unfinished" scan symptom: when Discogs
returns a 429 with a long Retry-After, the shared throttle used to sleep
WHILE HOLDING the throttle lock, so every per-track worker blocked behind it
and the whole album's futures timed out past the 300s deadline.

Covers:
- ``throttle_discogs`` releases the lock before sleeping through a cooldown.
- ``_set_rate_limit_window`` caps a single 429 cooldown.
- the retry transport caps an uncapped ``Retry-After`` header.
"""

from __future__ import annotations

import threading
import time


class TestThrottleDiscogsReleasesLock:
    def test_lock_free_during_cooldown_sleep(self):
        import api_clients.discogs_http as dh

        dh._DISCOGS_RATE_LIMIT_UNTIL = time.time() + 2.0
        dh._DISCOGS_LAST_REQUEST_TIME = 0.0

        result = {"done": False}

        def _run():
            dh.throttle_discogs()
            result["done"] = True

        t = threading.Thread(target=_run)
        t.start()
        time.sleep(0.2)

        # While throttle_discogs is sleeping through the cooldown, the lock
        # must be acquirable — otherwise every other worker blocks behind it
        # and all per-track futures time out.
        acquired = dh._DISCOGS_THROTTLE_LOCK.acquire(timeout=0.5)
        if acquired:
            dh._DISCOGS_THROTTLE_LOCK.release()

        t.join(timeout=5)

        assert acquired is True
        assert result["done"] is True


class TestDiscogsCooldownCap:
    def test_set_rate_limit_window_caps_long_cooldown(self):
        import api_clients.discogs_http as dh

        dh._DISCOGS_RATE_LIMIT_UNTIL = 0.0
        dh._set_rate_limit_window(300.0)

        # A single 429 must not arm a cooldown beyond the cap.
        assert dh._DISCOGS_RATE_LIMIT_UNTIL <= time.time() + dh._DISCOGS_MAX_COOLDOWN + 0.01

    def test_set_rate_limit_window_keeps_short_cooldown(self):
        import api_clients.discogs_http as dh

        dh._DISCOGS_RATE_LIMIT_UNTIL = 0.0
        dh._set_rate_limit_window(5.0)

        assert dh._DISCOGS_RATE_LIMIT_UNTIL <= time.time() + 5.0 + 0.01


class TestRetryTransportCapsRetryAfter:
    def test_uncapped_retry_after_does_not_stall(self):
        import httpx
        from api_clients import http_utils

        def _resp(status):
            r = httpx.Response(status, request=httpx.Request("GET", "http://x"))
            r.headers["Retry-After"] = "300"
            return r

        class _FakeTransport:
            def __init__(self, responses):
                self._responses = list(responses)
                self.calls = 0

            def handle_request(self, request):
                self.calls += 1
                return self._responses.pop(0)

        transport = http_utils._RetryTransport(retries=1, backoff=0.1)
        fake = _FakeTransport([_resp(429), _resp(200)])
        transport._transport = fake

        # Shrink the cap so the test is fast; the point is the 300s header is
        # NOT honoured verbatim.
        original_cap = http_utils._MAX_RETRY_AFTER
        http_utils._MAX_RETRY_AFTER = 0.05
        try:
            start = time.time()
            resp = transport.handle_request(httpx.Request("GET", "http://x"))
            elapsed = time.time() - start
        finally:
            http_utils._MAX_RETRY_AFTER = original_cap

        assert fake.calls == 2
        assert resp.status_code == 200
        # Bounded well under the 300s header (and under the 300s album deadline).
        assert elapsed < 5.0


class TestDiscogsRequestBudget:
    """The Discogs ``_request`` loop must give up after a hard wall-clock
    budget instead of spinning on an ever-extending shared 429 cooldown —
    the reported 240s+ singles-detection hang where 8 tracks sat in flight."""

    def test_request_budget_constant_is_set(self):
        import api_clients.discogs_http as dh

        assert dh._DISCOGS_REQUEST_BUDGET_SECONDS > 0
        assert dh._DISCOGS_REQUEST_BUDGET_SECONDS < dh._DISCOGS_MAX_COOLDOWN * 4

    def test_budget_exceeded_returns_empty(self):
        """When the shared cooldown keeps extending past the request budget,
        ``_request`` returns {} instead of blocking indefinitely."""
        import api_clients.discogs_http as dh

        class _FakeSession:
            def request(self, method, url, headers=None, params=None, timeout=None):
                # Simulate a permanent 429 with a fresh cooldown every call.
                dh._set_rate_limit_window(30.0)
                raise _Fake429()

        class _Fake429(Exception):
            pass

        client = dh.DiscogsHttpClient(token="tok")
        client.session = _FakeSession()

        # Shrink the budget so the test is fast; the point is bounded exit.
        # Shrink the budget so the test is fast; the point is bounded exit.
        # NOTE: throttle_discogs() enforces a 1s pacing floor per iteration,
        # so a budget below ~1s is dominated by that floor — 1.5s lets the
        # loop run ~2 iterations before the budget fires (without the budget
        # it would run all max_retries+1 iterations and take 9s+).
        original = dh._DISCOGS_REQUEST_BUDGET_SECONDS
        dh._DISCOGS_REQUEST_BUDGET_SECONDS = 1.5
        try:
            start = time.time()
            payload = client._request("GET", "/database/search", params={"q": "x"})
            elapsed = time.time() - start
        finally:
            dh._DISCOGS_REQUEST_BUDGET_SECONDS = original
            # The fake armed a real shared cooldown — clear it so other tests
            # in the same process don't sleep on throttle_discogs().
            dh._DISCOGS_RATE_LIMIT_UNTIL = 0.0

        assert payload == {}
        assert elapsed < 5.0


class TestCooldownHonouredOnSharedLimiterPath:
    """A 429's cooldown must be waited out EVEN when the cross-process shared
    limiter is in use.

    The shared-limiter branch RETURNED before the local cooldown logic, so in a
    deployment the cooldown a 429 armed was never respected: parallel worker
    threads kept firing at 1 req/s into a limit Discogs had just refused, which
    turned one 429 into a storm — and each of those requests burned its own 30s
    budget and gave up with no data."""

    def test_cooldown_is_waited_before_shared_limiter_slot(self):
        import api_clients.discogs_http as dh

        calls: list[float] = []

        class _StubLimiter:
            def throttle_discogs(self) -> None:
                calls.append(time.monotonic())

        original_limiter = dh._rate_limiter
        original_cooldown = dh._DISCOGS_RATE_LIMIT_UNTIL
        original_last = dh._DISCOGS_LAST_REQUEST_TIME
        dh._rate_limiter = _StubLimiter()
        dh._DISCOGS_RATE_LIMIT_UNTIL = time.time() + 0.5
        dh._DISCOGS_LAST_REQUEST_TIME = 0.0
        try:
            start = time.monotonic()
            dh.throttle_discogs()
        finally:
            dh._rate_limiter = original_limiter
            dh._DISCOGS_RATE_LIMIT_UNTIL = original_cooldown
            dh._DISCOGS_LAST_REQUEST_TIME = original_last

        # The shared limiter must still be asked for a slot...
        assert len(calls) == 1
        # ...but only AFTER the cooldown has been waited out.
        assert calls[0] - start >= 0.4


class Test429LongCooldownDoesNotBurnBudget:
    """A 429 carrying a 60s Retry-After must NOT be slept against a 30s budget.

    The guard read ``min(wait_seconds, 1.0)`` — effectively "is one second
    left?" — so a 60s cooldown cleared it, slept the full 60 seconds, and the
    request was then abandoned at the top of the next iteration anyway."""

    def test_long_retry_after_gives_up_within_budget(self):
        import api_clients.discogs_http as dh

        class _Resp:
            status_code = 429
            headers = {"Retry-After": "60"}

        class _Session:
            def request(self, *args, **kwargs):
                return _Resp()

        client = dh.DiscogsHttpClient(token="tok")
        client.session = _Session()

        original_budget = dh._DISCOGS_REQUEST_BUDGET_SECONDS
        dh._DISCOGS_REQUEST_BUDGET_SECONDS = 3.0
        dh._DISCOGS_RATE_LIMIT_UNTIL = 0.0
        try:
            start = time.monotonic()
            payload = client._request("GET", "/database/search", params={"q": "x"})
            elapsed = time.monotonic() - start
        finally:
            dh._DISCOGS_REQUEST_BUDGET_SECONDS = original_budget
            # The 429 armed a real shared cooldown — clear it so later tests in
            # this process do not sleep on it.
            dh._DISCOGS_RATE_LIMIT_UNTIL = 0.0

        assert payload == {}
        # Bounded by the request budget, NOT by the 60s header.
        assert elapsed < 10.0
