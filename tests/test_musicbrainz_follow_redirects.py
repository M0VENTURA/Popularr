"""MusicBrainz 301 redirects (merged MBIDs) must be FOLLOWED, not failures.

Reported: ``MusicBrainz request failed permanently endpoint='recording/…'
status_code=301 error="Redirect response '301 Moved Permanently'…"``.

MusicBrainz merges duplicate recordings and 301-redirects the old MBID to
the new one.  httpx does NOT follow redirects unless asked, and the client
treats any non-(400/404/5xx) status as a permanent failure — so every
lookup of a merged recording returned ``{}`` even though the data was one
redirect away.  ``create_retry_client`` now defaults ``follow_redirects=True``.
"""

from __future__ import annotations

import httpx


def test_shared_sessions_follow_redirects_by_default() -> None:
    from api_clients import session, timeout_safe_session
    from api_clients.http_utils import create_retry_client

    assert session.follow_redirects is True
    assert timeout_safe_session.follow_redirects is True
    assert create_retry_client().follow_redirects is True


def _redirecting_transport(old_path: str, new_path: str) -> httpx.MockTransport:
    """301 ``old_path`` → ``new_path``, which answers a merged-recording JSON."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == old_path:
            return httpx.Response(
                301, headers={"Location": f"https://musicbrainz.org{new_path}"}
            )
        if request.url.path == new_path:
            return httpx.Response(200, json={"id": "new-mbid", "title": "Merged"})
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


def test_a_merged_mbid_redirect_resolves_to_the_new_recording(monkeypatch) -> None:
    from api_clients import musicbrainz_http as mbhttp
    from api_clients.musicbrainz_http import MusicBrainzHttpClient

    old_path = "/ws/2/recording/ec14fb08-0000-0000-0000-000000000000"
    new_path = "/ws/2/recording/11111111-0000-0000-0000-000000000000"

    http = httpx.Client(
        transport=_redirecting_transport(old_path, new_path),
        follow_redirects=True,
    )
    client = MusicBrainzHttpClient(http_session=http)

    # Spy on the module logger directly rather than structlog's
    # ``capture_logs``: whichever structlog configuration an earlier test in
    # the full suite leaves behind defeats the capture, so the log assertion
    # passed alone and failed in the suite run.
    events: list[str] = []

    class _SpyLogger:
        def info(self, event, **_kwargs):
            events.append(str(event))

        def __getattr__(self, _name):
            return lambda *_a, **_k: None

    monkeypatch.setattr(mbhttp, "logger", _SpyLogger())

    payload = client.get(
        "recording/ec14fb08-0000-0000-0000-000000000000",
        params={"inc": "ids", "fmt": "json"},
    )

    assert payload.get("id") == "new-mbid", (
        "the 301 must be followed — a merged MBID is one redirect away from "
        "its data, not a permanent failure"
    )
    assert any("redirected" in event for event in events), (
        "the merged-MBID redirect must be visible in the logs"
    )


def test_without_follow_redirects_a_301_degrades_to_empty_not_a_crash() -> None:
    """Even an opt-out client must return ``{}`` (logged), never raise."""
    from api_clients.musicbrainz_http import MusicBrainzHttpClient

    old_path = "/ws/2/recording/ec14fb08-0000-0000-0000-000000000000"
    new_path = "/ws/2/recording/11111111-0000-0000-0000-000000000000"

    http = httpx.Client(
        transport=_redirecting_transport(old_path, new_path),
        follow_redirects=False,
    )
    client = MusicBrainzHttpClient(http_session=http)

    payload = client.get(
        "recording/ec14fb08-0000-0000-0000-000000000000",
        params={"inc": "ids", "fmt": "json"},
    )

    assert payload == {}, "a non-followed 301 must degrade to {} without raising"
