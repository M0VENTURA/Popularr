"""A retried transient network failure is NOT an error.

REPORTED (pasted log line):

    [ERROR] [api_clients.http_utils] [HTTP-TRACE] [bounded-call-post_singles_enrichment]
    Network I/O FAILED for .../similar-artists/json - ReadTimeout('The read operation timed out')

ROOT CAUSE: that `logger.error` sits INSIDE ``_RetryTransport._attempt``, which
runs inside ``for attempt in retrying``. A ReadTimeout is listed in
``_is_retryable``, so the retry policy EXPECTS it, and the caller handles the
exhausted case too (``get_similar_artists`` catches and returns ``[]``). Every
retried attempt was therefore reported as a hard failure — ERROR with nothing
actionable.

⚠️ BOTH DIRECTIONS ARE ASSERTED. Silencing the retryable case is only correct if
a genuinely final failure still shouts; a fix that quietened everything would
hide real breakage, so that case is a control, not an afterthought.
"""
from __future__ import annotations

import logging

import httpx
import pytest


class _RecordingHandler(logging.Handler):
    """Captures (level, message) straight from the module's logger.

    Attached directly to ``api_clients.http_utils`` rather than using caplog,
    so the result does not depend on the app's logging configuration or on
    propagation being enabled.
    """

    def __init__(self):
        super().__init__()
        self.records: list[tuple[int, str]] = []

    def emit(self, record):
        self.records.append((record.levelno, record.getMessage()))

    def messages(self, level: int) -> list[str]:
        return [m for lvl, m in self.records if lvl == level]


class _RaisingTransport(httpx.BaseTransport):
    def __init__(self, exc):
        self._exc = exc
        self.calls = 0

    def handle_request(self, request):
        self.calls += 1
        raise self._exc


class _FailThenSucceed(httpx.BaseTransport):
    """Fails ``fail_times``, then returns ``ok``."""

    def __init__(self, fail_times: int, ok: httpx.Response):
        self._fail_times = fail_times
        self._ok = ok
        self.calls = 0

    def handle_request(self, request):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise httpx.ReadTimeout("The read operation timed out")
        return self._ok


@pytest.fixture
def armed():
    """Build a real _RetryTransport with a fake inner transport + recorder.

    ``backoff=0.0`` keeps the retries instant; ``retries`` is passed explicitly
    so the tests do not depend on production tuning.

    ``_RetryTransport`` builds its own ``httpx.HTTPTransport``, so the fake is
    installed by REPLACING ``_transport`` afterwards — the retry and logging
    code under test is untouched.
    """
    created: list[logging.Handler] = []

    def _make(transport, retries: int = 1):
        from api_clients.http_utils import _RetryTransport

        client = _RetryTransport(retries=retries, backoff=0.0)
        client._transport = transport
        handler = _RecordingHandler()
        logger = logging.getLogger("api_clients.http_utils")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        created.append(handler)
        return client, handler, logger

    yield _make

    logger = logging.getLogger("api_clients.http_utils")
    for handler in created:
        logger.removeHandler(handler)


LB_URL = "https://labs.api.listenbrainz.org/similar-artists/json"


class TestARecoveredRetryIsNotReportedAsAnError:
    def test_a_retried_timeout_that_succeeds_logs_no_error(self, armed):
        request = httpx.Request("GET", LB_URL)
        ok = httpx.Response(200, json={"payload": {"artists": []}}, request=request)
        transport = _FailThenSucceed(1, ok)
        client, handler, _logger = armed(transport)

        response = client.handle_request(request)

        assert transport.calls == 2, "the request really was retried"
        assert response.status_code == 200
        assert handler.messages(logging.ERROR) == [], (
            "a timeout that was retried and then SUCCEEDED must not be reported "
            "as an error"
        )
        assert handler.messages(logging.WARNING), (
            "it should still be visible as a warning, not silent"
        )


class TestAnExhaustedRetryableTimeoutIsNotAnError:
    def test_a_retryable_timeout_is_logged_at_warning_not_error(self, armed):
        """The exact reported case: ReadTimeout against the ListenBrainz labs API."""
        request = httpx.Request("GET", LB_URL)
        transport = _RaisingTransport(httpx.ReadTimeout("The read operation timed out"))
        client, handler, _logger = armed(transport)

        with pytest.raises(httpx.ReadTimeout):
            client.handle_request(request)

        assert transport.calls == 2, "1 initial attempt + 1 retry"
        assert handler.messages(logging.ERROR) == [], (
            "ReadTimeout is in _is_retryable, so it is EXPECTED behaviour and "
            "the caller handles it; ERROR made it look like a hard failure"
        )
        warnings = handler.messages(logging.WARNING)
        assert len(warnings) == 2, "one warning per attempt"
        assert all("attempt" in m for m in warnings), (
            "each warning must say WHICH attempt, or a retried failure is "
            "indistinguishable from a repeating one"
        )
        assert any("/2" in m for m in warnings), (
            "the attempt count must be shown against the total"
        )


class TestAGenuinelyFinalFailureStillShouts:
    """CONTROL — the counterpart that stops the fix from muting everything."""

    def test_a_non_retryable_failure_is_logged_at_error(self, armed):
        # ValueError is not in _is_retryable's list, so it is final by
        # definition: no retry will happen.
        request = httpx.Request("GET", "https://example.invalid/x")
        transport = _RaisingTransport(ValueError("malformed request"))
        client, handler, _logger = armed(transport)

        with pytest.raises(ValueError):
            client.handle_request(request)

        assert transport.calls == 1, "a non-retryable error must not be retried"
        errors = handler.messages(logging.ERROR)
        assert len(errors) == 1, (
            "a genuinely final, unhandled transport failure must still be an "
            "ERROR — otherwise the fix would hide real breakage"
        )
        assert "FAILED" in errors[0]

    def test_a_retryable_failure_still_propagates(self, armed):
        """Quietening the LOG must not swallow the EXCEPTION.

        The caller decides whether the failure is fatal, so the exception has to
        reach it.
        """
        request = httpx.Request("GET", LB_URL)
        transport = _RaisingTransport(httpx.ReadTimeout("boom"))
        client, _handler, _logger = armed(transport)

        with pytest.raises(httpx.ReadTimeout):
            client.handle_request(request)

    def test_a_successful_call_logs_nothing(self, armed):
        request = httpx.Request("GET", LB_URL)
        ok = httpx.Response(200, json={}, request=request)
        client, handler, _logger = armed(_FailThenSucceed(0, ok))

        client.handle_request(request)

        assert handler.records == [], (
            "a clean call must not log anything at all"
        )
