"""The slskd server-state gate reads the REAL payload shapes.

Reported (2026-10-12): the queue log showed

    Soulseek server not connected — search not attempted query='...'
    [QUEUED] ... → slskd disconnected, search skipped

for EVERY search while slskd was actually connected. Root cause: the
server-state gate added by the slskd hardening read ONLY the modern boolean
fields (``isConnected`` / ``isLoggedIn``) of the application state, but real
slskd — including the version this deployment runs — reports connection as a
C# enum-flags STRING in ``server.state`` (``"Connected, LoggedIn"``,
``"Connected"``, ``"Disconnected"``), the shape the ORIGINAL client
(``old_system/api_clients/slskd.py``) parsed and that historical searches
used successfully. With no boolean present the gate was permanently False and
every search was skipped.

Fix: ``SlskdHttpClient.is_connected`` parses BOTH shapes (explicit
``isConnected``/``isLoggedIn`` booleans first, then the flag string), and
``get_server_state`` falls back to the ``/server`` and ``/application``
endpoints of the original client when ``application/state`` cannot answer.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from api_clients.slskd_http import SlskdHttpClient  # noqa: E402
from services.downloads.slskd_service import SlskdService  # noqa: E402


# ---------------------------------------------------------------------------
# Fake HTTP session — canned JSON per endpoint, recording calls
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, payload: Any, status: int = 200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._payload


class _FakeSession:
    def __init__(self, routes: dict[str, Any], statuses: dict[str, int] | None = None):
        self.routes = routes
        self.statuses = statuses or {}
        self.calls: list[str] = []

    def request(self, method: str, url: str, **kwargs: Any):
        endpoint = url.split("/api/v0/", 1)[1]
        self.calls.append(f"{method} {endpoint}")
        status = self.statuses.get(endpoint, 200)
        if status >= 400:
            return _Resp(None, status)
        return _Resp(self.routes.get(endpoint, {}), status)


def _client(session: _FakeSession) -> SlskdHttpClient:
    return SlskdHttpClient(web_url="http://slskd:5030", api_key="k", http_session=session)


# ── Realistic payload shapes (recorded-style fixtures) ─────────────────

# The shape the DEPLOYED slskd reports: server sub-object with a flag STRING.
FLAG_STRING_STATE = {
    "application/state": {
        "state": "Started",
        "server": {
            "address": "1.2.3.4",
            "port": 2242,
            "state": "Connected, LoggedIn",
            "uptime": "1:02:05:33",
        },
    }
}

# The modern shape the hardening tests (and the old boolean check) used.
BOOLEAN_STATE = {
    "application/state": {
        "state": "Started",
        "server": {"isConnected": True, "state": "Connected, LoggedIn"},
    }
}

# The OLD endpoint shape: GET /server returns the server object directly.
LEGACY_SERVER_ENDPOINT = {
    "server": {"state": "Connected, LoggedIn", "port": 2242, "address": "1.2.3.4"}
}

# The OLD /application fallback: server nested under the app payload.
LEGACY_APPLICATION_ENDPOINT = {
    "application": {"version": "0.18.0", "server": {"isConnected": True}}
}


# ---------------------------------------------------------------------------
# HTTP layer — parsing both connection shapes + endpoint fallbacks
# ---------------------------------------------------------------------------

class TestHttpIsConnectedParsesTheRealPayloads:

    def test_flag_string_server_state_counts_as_connected(self):
        session = _FakeSession(dict(FLAG_STRING_STATE))
        assert _client(session).is_connected() is True

    def test_single_flag_connected_counts_as_connected(self):
        routes = {"application/state": {"server": {"state": "Connected"}}}
        assert _client(_FakeSession(routes)).is_connected() is True

    def test_modern_boolean_still_counts_as_connected(self):
        session = _FakeSession(dict(BOOLEAN_STATE))
        assert _client(session).is_connected() is True

    def test_is_logged_in_boolean_counts_as_connected(self):
        routes = {"application/state": {"server": {"isLoggedIn": True}}}
        assert _client(_FakeSession(routes)).is_connected() is True

    def test_disconnected_flag_string_returns_false(self):
        routes = {"application/state": {"server": {"state": "Disconnected"}}}
        assert _client(_FakeSession(routes)).is_connected() is False

    def test_explicit_false_boolean_is_authoritative(self):
        # The original client preferred the explicit boolean when present.
        routes = {"application/state": {"server": {"isConnected": False, "state": "Connected, LoggedIn"}}}
        assert _client(_FakeSession(routes)).is_connected() is False

    def test_empty_application_state_falls_back_to_server_endpoint(self):
        session = _FakeSession(dict(LEGACY_SERVER_ENDPOINT))
        assert _client(session).is_connected() is True
        assert "GET application/state" in session.calls
        assert "GET server" in session.calls, "the older /server endpoint must be probed"

    def test_legacy_application_endpoint_fallback(self):
        session = _FakeSession(
            dict(LEGACY_APPLICATION_ENDPOINT),
            statuses={"server": 404},
        )
        assert _client(session).is_connected() is True
        assert "GET server" in session.calls

    def test_server_endpoint_404_falls_through_to_application(self):
        session = _FakeSession(
            dict(LEGACY_APPLICATION_ENDPOINT),
            statuses={"application/state": 404, "server": 404},
        )
        assert _client(session).is_connected() is True

    def test_all_endpoints_dead_returns_false(self):
        session = _FakeSession({}, statuses={"application/state": 500, "server": 500, "application": 500})
        assert _client(session).is_connected() is False

    def test_top_level_server_state_is_recognised(self):
        # Some versions carry the fields at the top level of application/state.
        routes = {"application/state": {"state": "Connected, LoggedIn", "pid": 123}}
        assert _client(_FakeSession(routes)).is_connected() is True


# ---------------------------------------------------------------------------
# Service layer — the gate in search_and_filter uses the real client
# ---------------------------------------------------------------------------

def _stub_searchable(client: SlskdHttpClient) -> SlskdService:
    """SlskdService with the gate going through the REAL http client.

    Search machinery stubbed so the test can only observe the gate outcome.
    """
    svc = SlskdService(http_client=client)
    svc.maybe_clear_stale_searches = lambda budget_seconds=6: False
    svc.started: list[str] = []
    svc.start_search = lambda query, timeout=None: svc.started.append(query) or "s1"
    svc.get_search_results = lambda search_id, timeout=None: ([], "InProgress", False)
    return svc


class TestSearchAndFilterUsesTheParsedGate:

    def test_search_proceeds_when_server_reports_the_flag_string(self):
        # THE regression: a healthy server reporting "Connected, LoggedIn"
        # used to read as disconnected and every search was skipped.
        client = _client(_FakeSession(dict(FLAG_STRING_STATE)))
        svc = _stub_searchable(client)
        result = svc.search_and_filter("some song", wait_seconds=0)
        assert result is not None, "the gate must pass for the flag-string payload"
        assert svc.started == ["some song"]

    def test_search_proceeds_on_the_modern_boolean_shape(self):
        client = _client(_FakeSession(dict(BOOLEAN_STATE)))
        svc = _stub_searchable(client)
        assert svc.search_and_filter("some song", wait_seconds=0) is not None

    def test_disconnected_flag_string_skips_the_search(self):
        client = _client(_FakeSession({"application/state": {"server": {"state": "Disconnected"}}}))
        svc = _stub_searchable(client)
        assert svc.search_and_filter("some song", wait_seconds=0) is None
        assert svc.started == [], "a disconnected server must not be searched"

    def test_unreachable_slskd_skips_the_search(self):
        client = _client(_FakeSession({}, statuses={"application/state": 500, "server": 500, "application": 500}))
        svc = _stub_searchable(client)
        assert svc.search_and_filter("some song", wait_seconds=0) is None
        assert svc.started == []