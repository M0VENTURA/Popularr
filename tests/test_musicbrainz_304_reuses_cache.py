"""A MusicBrainz 304 must reuse the cached payload, not fail.

Reported: ``MusicBrainz request failed permanently endpoint='artist/'
status_code=304 error="Redirect response '304 Not Modified'…"``.

The ETag conditional-GET layer (audit G7) sends ``If-None-Match`` so an
unchanged resource revalidates with a ~300-byte exchange instead of
re-downloading the body. The 304 handler existed — but it sat AFTER
``response.raise_for_status()``, and httpx (0.28+) raises
``HTTPStatusError`` for a 3xx "Redirect response '304 Not Modified'"
**even with ``follow_redirects=True``**. So every revalidation raised
before the handler was reached, was logged as a permanent failure, and
returned ``{}`` — the cached payload it existed to reuse was thrown away,
making the whole conditional-GET layer unreachable.

The fix handles 304 BEFORE ``raise_for_status()``.
"""

from __future__ import annotations

import httpx
import pytest

ETAG = 'W/"abc123"'
BODY = {"id": "rg-1", "title": "Wasted Lands", "primary-type": "Single"}


def _client(handler, monkeypatch):
    """MusicBrainzHttpClient over a MockTransport, throttle disabled."""
    from api_clients import musicbrainz_http as mbhttp
    from api_clients.musicbrainz_http import MusicBrainzHttpClient

    # The strict throttle sleeps up to 1.2s per request; skip it in tests.
    monkeypatch.setattr(mbhttp, "_strict_throttle", lambda: None)
    mbhttp._ETAG_CACHE.clear()
    # Each test starts with a closed circuit — a prior test's exhausted 5xx
    # retries leave it open, which would short-circuit the next test's
    # request before it ever reaches the transport.
    mbhttp._CIRCUIT_OPEN_UNTIL = 0.0

    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return MusicBrainzHttpClient(http_session=http)


def _spy_logger(monkeypatch):
    """Capture log events (structlog capture is defeated by suite ordering)."""
    from api_clients import musicbrainz_http as mbhttp

    events: list[str] = []

    class _Spy:
        def debug(self, event, **_k):
            events.append(f"debug:{event}")

        def info(self, event, **_k):
            events.append(f"info:{event}")

        def warning(self, event, **_k):
            events.append(f"warning:{event}")

        def __getattr__(self, _n):
            return lambda *_a, **_k: None

    monkeypatch.setattr(mbhttp, "logger", _Spy())
    return events


# ---------------------------------------------------------------------------
# 1. The reported bug: a 304 returns the cached payload
# ---------------------------------------------------------------------------

def test_304_returns_the_cached_payload_not_a_failure(monkeypatch) -> None:
    """First call stores the ETag; the second (304) returns the SAME body."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.headers.get("if-none-match"):
            return httpx.Response(304)
        return httpx.Response(200, json=BODY, headers={"etag": ETAG})

    client = _client(handler, monkeypatch)
    events = _spy_logger(monkeypatch)

    first = client.get("release-group/rg-1", params={"fmt": "json"})
    second = client.get("release-group/rg-1", params={"fmt": "json"})

    assert first == BODY
    assert second == BODY, (
        "a 304 must return the cached payload — returning {} here is the "
        "reported bug (the conditional-GET layer threw away its own cache)"
    )
    # The second request DID send the validator.
    assert calls[1].headers.get("if-none-match") == ETAG
    # And it must not be logged as a failure.
    assert not any("failed permanently" in e for e in events)


def test_304_is_not_logged_as_a_permanent_failure(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match"):
            return httpx.Response(304)
        return httpx.Response(200, json=BODY, headers={"etag": ETAG})

    client = _client(handler, monkeypatch)
    events = _spy_logger(monkeypatch)

    client.get("release-group/rg-1", params={"fmt": "json"})
    client.get("release-group/rg-1", params={"fmt": "json"})

    assert not any("failed permanently" in e for e in events), (
        "a 304 is a successful revalidation, not a failure"
    )
    assert not any("304" in e and "warning" in e for e in events)


def test_304_does_not_open_the_circuit_breaker(monkeypatch) -> None:
    from api_clients import musicbrainz_http as mbhttp

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match"):
            return httpx.Response(304)
        return httpx.Response(200, json=BODY, headers={"etag": ETAG})

    client = _client(handler, monkeypatch)
    mbhttp._CIRCUIT_OPEN_UNTIL = 0.0

    client.get("release-group/rg-1", params={"fmt": "json"})
    client.get("release-group/rg-1", params={"fmt": "json"})

    assert mbhttp._CIRCUIT_OPEN_UNTIL == 0.0, (
        "a 304 must not trip the circuit breaker"
    )


# ---------------------------------------------------------------------------
# 2. Defensive: a 304 with no cached body is a clean miss, not a raise
# ---------------------------------------------------------------------------

def test_304_without_cached_entry_returns_empty_without_raising(monkeypatch) -> None:
    """We never send If-None-Match without a cache, but a stray 304 (a
    proxy, say) must not raise or be logged as a permanent failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304)

    client = _client(handler, monkeypatch)
    events = _spy_logger(monkeypatch)

    result = client.get("artist/", params={"query": "x", "fmt": "json"})

    assert result == {}
    assert not any("failed permanently" in e for e in events)


# ---------------------------------------------------------------------------
# 3. Unchanged behaviour: a normal 200 still works and stores the ETag
# ---------------------------------------------------------------------------

def test_200_stores_the_etag_and_returns_the_body(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=BODY, headers={"etag": ETAG})

    client = _client(handler, monkeypatch)
    payload = client.get("release-group/rg-1", params={"fmt": "json"})

    assert payload == BODY


def test_different_params_do_not_share_a_cache_entry(monkeypatch) -> None:
    """The ETag key includes every param — two queries must not collide."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=BODY, headers={"etag": ETAG})

    client = _client(handler, monkeypatch)
    client.get("release-group/", params={"query": "a", "fmt": "json"})
    client.get("release-group/", params={"query": "b", "fmt": "json"})

    # The second request must NOT have sent the first's validator.
    assert "a" in seen[0] and "b" in seen[1]


def test_a_5xx_still_retries_and_opens_circuit(monkeypatch) -> None:
    """The 304 fix must not disturb the genuine 5xx path."""
    from api_clients import musicbrainz_http as mbhttp

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = _client(handler, monkeypatch)
    mbhttp._CIRCUIT_OPEN_UNTIL = 0.0

    result = client.get("artist/", params={"query": "x", "fmt": "json"})

    assert result == {}
    # After the retries exhaust, the circuit opens for 60s.
    assert mbhttp._CIRCUIT_OPEN_UNTIL > 0.0


def test_404_is_still_a_debug_not_a_warning(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    client = _client(handler, monkeypatch)
    events = _spy_logger(monkeypatch)

    result = client.get("recording/does-not-exist", params={"fmt": "json"})

    assert result == {}
    assert any("not found" in e for e in events), (
        "404 must stay on the debug path, not be lumped with the 304 fix"
    )
