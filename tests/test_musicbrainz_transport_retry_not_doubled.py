"""A transport failure must not be retried twice.

REPORTED LOG: during a resolver outage every MusicBrainz request produced
TWELVE ``[HTTP-TRACE] ... attempt N/4`` lines and then "failed permanently
after retries".

Why twelve: the shared session is built by ``create_retry_client(retries=3)``
(``api_clients/__init__.py``), so ``_RetryTransport`` already makes FOUR
attempts. ``MusicBrainzHttpClient.get`` then had its OWN three-attempt loop,
which multiplied the stack: 3 x 4 = 12 DNS lookups per call, with the backoff
sleeps in between. The second layer bought no new information — by the time
the exception reaches it, the transport has exhausted every attempt.

Transport-level failures (ConnectError / TimeoutException /
RemoteProtocolError) are now handed straight back to the caller, which also
lets the per-host cooldown open instead of re-paying the whole stack.
"""

from __future__ import annotations

import httpx


class _FailingSession:
    """Stands in for the shared session; every GET fails to connect."""

    def __init__(self) -> None:
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        raise httpx.ConnectError(
            "simulated: [Errno -3] Temporary failure in name resolution"
        )


class _SpyLogger:
    def __init__(self) -> None:
        self.events: list[str] = []

    def warning(self, event, **_kwargs):
        self.events.append(str(event))

    def debug(self, *_a, **_k):
        pass

    def info(self, *_a, **_k):
        pass

    def __getattr__(self, _name):
        return lambda *_a, **_k: None


def _client(monkeypatch):
    from api_clients import musicbrainz_http as mbhttp
    from api_clients.musicbrainz_http import MusicBrainzHttpClient

    monkeypatch.setattr(mbhttp, "_strict_throttle", lambda: None)
    mbhttp._CIRCUIT_OPEN_UNTIL = 0.0
    spy = _SpyLogger()
    monkeypatch.setattr(mbhttp, "logger", spy)
    session = _FailingSession()
    return MusicBrainzHttpClient(http_session=session), session, spy


def test_a_connect_error_is_not_retried_by_the_wrapper(monkeypatch):
    client, session, _spy = _client(monkeypatch)

    assert client.get("recording/abc") == {}

    assert session.calls == 1, (
        "the wrapper re-ran a transport failure the shared session had "
        "already retried — that is the 12-attempt-per-track multiplier"
    )


def test_the_log_says_the_transport_already_retried(monkeypatch):
    client, _session, spy = _client(monkeypatch)

    client.get("recording/abc")

    assert "MusicBrainz request failed after transport retries" in spy.events, (
        f"the failure was not reported as transport-exhausted: {spy.events}"
    )
    assert "MusicBrainz request failed permanently after retries" not in spy.events
