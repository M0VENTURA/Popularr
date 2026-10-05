"""A dead host must fail FAST instead of spending the scan's budget on retries.

Why this exists
---------------
From a live scan log:

    [HTTP-TRACE] [track_worker_0] ... https://api.listenbrainz.org/1/popularity/recording
        (attempt 1/4) - ReadTimeout(...)      ← and 2/4, 3/4, 4/4
    [SCAN] section abandoned (timeout) section='album_track_phase'
        elapsed_s=900.002 budget_seconds=900.0 artist='Various Artists'
    Popularity scan processed 0 tracks — albums were abandoned on budget

A read timeout is RETRIED (``httpx.TimeoutException`` is in ``_is_retryable``),
so a host that answers nothing costs ``attempts x read-timeout`` **per call** —
four attempts x the 20s ListenBrainz read timeout is ~80 seconds, on every
single request. Four track workers, one unreachable provider, and the album's
entire 900 s budget is gone in retries: the album is then abandoned having
processed nothing, and the log fills with timeout after timeout.

The fix opens a short **per-host** window after two consecutive requests have
timed out on *every* attempt. Requests inside that window raise
``httpx.ReadTimeout`` immediately — the same exception the caller already
handles today — so no caller changes and no new failure mode, only ~80 s
returned to the scan per call.

Two strikes rather than one: a single transient timeout must not make a host
untouchable, or one slow cover-art fetch from Navidrome would stall every
import for a minute.
"""

from __future__ import annotations

import httpx
import pytest

from api_clients import http_utils

#: Internal addresses skip the per-domain rate limiter, so these tests do not
#: sleep between attempts.
HOST = "192.168.1.50"


class _FakeTransport:
    """Stands in for the real HTTP transport: always fails (or always answers)."""

    def __init__(self, *, fail: bool):
        self.calls = 0
        self._fail = fail

    def handle_request(self, request):
        self.calls += 1
        if self._fail:
            raise httpx.ReadTimeout("simulated: the peer never answered")
        return httpx.Response(200, request=request, content=b"{}")

    def close(self):
        pass

    @property
    def is_closed(self):
        return False


def _transport(*, fail: bool, retries: int = 1) -> tuple:
    """A retry transport whose real HTTP layer is replaced by *fail*."""
    transport = http_utils._RetryTransport(retries=retries, backoff=0.0)
    fake = _FakeTransport(fail=fail)
    transport._transport = fake
    return transport, fake


def _get(transport) -> httpx.Response:
    return transport.handle_request(httpx.Request("GET", f"http://{HOST}/probe"))


# The per-test reset lives in ``tests/conftest.py::reset_host_timeout_cooldowns``
# (autouse): this state is global BY DESIGN, so every test in the suite -- not
# just this file -- has to start with no host mid-window. A second, local
# fixture here would only duplicate it and suggest the isolation is file-scoped
# when it is not.


class TestFailFastAfterRepeatedTimeouts:
    def test_one_full_timeout_does_not_open_the_window(self):
        """A single blip must not make the host untouchable."""
        transport, fake = _transport(fail=True)

        with pytest.raises(httpx.TimeoutException):
            _get(transport)

        assert fake.calls == 2, "retries still happened on the first failure"
        assert http_utils._host_is_cooling_down(HOST) == 0.0

    def test_the_second_full_timeout_opens_it(self):
        transport, fake = _transport(fail=True)

        for _ in range(2):
            with pytest.raises(httpx.TimeoutException):
                _get(transport)

        assert http_utils._host_is_cooling_down(HOST) > 0

    def test_requests_inside_the_window_do_not_touch_the_network(self):
        transport, fake = _transport(fail=True)
        for _ in range(2):
            with pytest.raises(httpx.TimeoutException):
                _get(transport)
        calls_before = fake.calls

        # The whole point: no more attempts, no more ~80s.
        with pytest.raises(httpx.TimeoutException):
            _get(transport)

        assert fake.calls == calls_before, (
            "a cooling-down host was still contacted — the scan budget keeps "
            "being spent on a peer that is not answering"
        )

    def test_the_exception_is_one_the_callers_already_handle(self):
        """No caller may need changing: it must still be a TimeoutException."""
        transport, _ = _transport(fail=True)
        for _ in range(2):
            with pytest.raises(httpx.TimeoutException):
                _get(transport)
        with pytest.raises(httpx.TimeoutException):
            _get(transport)

    def test_a_success_resets_the_strikes(self):
        """Two unrelated blips separated by a good call must not open a window."""
        transport, _ = _transport(fail=True)
        with pytest.raises(httpx.TimeoutException):
            _get(transport)

        working, _ = _transport(fail=False)
        assert isinstance(_get(working), httpx.Response)
        # The strike was recorded against the host, and a good answer clears it.
        http_utils._note_host_success(HOST)
        assert http_utils._HOST_TIMEOUT_STRIKES.get(HOST) is None

        with pytest.raises(httpx.TimeoutException):
            _get(transport)
        assert http_utils._host_is_cooling_down(HOST) == 0.0, (
            "the first failure after a success must start the count again"
        )

    def test_a_working_host_is_untouched(self):
        transport, fake = _transport(fail=False)
        assert isinstance(_get(transport), httpx.Response)
        assert fake.calls == 1
        assert http_utils._host_is_cooling_down(HOST) == 0.0

    def test_the_window_expires(self):
        transport, _ = _transport(fail=True)
        for _ in range(2):
            with pytest.raises(httpx.TimeoutException):
                _get(transport)
        assert http_utils._host_is_cooling_down(HOST) > 0

        # Pretend the window has elapsed.
        http_utils._HOST_TIMEOUT_COOLDOWN[HOST] = 0.0
        working, _ = _transport(fail=False)
        assert isinstance(_get(working), httpx.Response)

    def test_one_dead_host_does_not_cool_down_another(self):
        """ListenBrainz being down must not stall MusicBrainz or Navidrome."""
        dead, _ = _transport(fail=True)
        for _ in range(2):
            with pytest.raises(httpx.TimeoutException):
                _get(dead)

        other = "192.168.1.51"
        assert http_utils._host_is_cooling_down(other) == 0.0

    def test_the_window_is_per_host_not_per_client(self):
        """Two clients to the same host share the verdict — that is the point."""
        first, _ = _transport(fail=True)
        for _ in range(2):
            with pytest.raises(httpx.TimeoutException):
                _get(first)

        second, fake = _transport(fail=True)
        with pytest.raises(httpx.TimeoutException):
            _get(second)
        assert fake.calls == 0, (
            "a second client to the same dead host still paid for attempts"
        )
