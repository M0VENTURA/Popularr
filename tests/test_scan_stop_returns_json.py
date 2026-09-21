"""The /scan/stop-* routes must answer the format their caller can read.

REPORTED: "Error: Server returned HTML instead of JSON (HTTP 200). This usually
means an auth/session redirect or a server error page." — shown when clicking
the stop buttons on the dashboard.

``JSON.parse`` throws that message from
``test_site/static/js/utils/api.js`` (and ``static/js/downloads.js``) when the
response body is HTML rather than JSON.

TWO independent causes, both fixed:

1. **The routes were HTML form endpoints.**  ``/scan/stop-popularity`` and
   friends did ``await flash(...)`` then ``redirect(url_for("ui.dashboard"))``
   so a browser form post showed a banner. The dashboard calls the SAME URLs
   with ``fetch`` + ``Content-Type: application/json``. The redirect was
   followed to the dashboard, which answered ``200 text/html``, so parsing
   failed. **The ``HTTP 200`` in the message is the tell: the stop flag WAS
   set and the request succeeded — the response was simply a web page.**

2. **The auth gate mistook the route for a page request.**  ``_is_api_request()``
   only checked ``request.path.startswith("/api/")``, and ``/scan/stop-*`` is
   not under ``/api/``. With an expired session the gate returned
   ``302 -> /login``; the client followed it and received the login page. This
   is the "auth/session redirect" the error message names.

Content negotiation fixes cause 1 without breaking the form path, and asking
for JSON now yields a 401 JSON error for cause 2 instead of an HTML redirect.
"""

from __future__ import annotations

import pytest

#: Every stop endpoint the dashboard/JS calls, with the method it uses.
STOP_ENDPOINTS = [
    "/scan/stop",
    "/scan/stop-popularity",
    "/scan/stop-singles",
    "/scan/stop-all",
    "/scan/stop-navidrome",
    "/scan/stop-essentia-mood",
    "/scan/stop-mood",
    "/scan/stop-mp3-import",
]


@pytest.fixture()
def configured(monkeypatch):
    """Force the app into configured mode so the auth gate is active."""
    monkeypatch.setattr("helpers.config_helpers.needs_setup", lambda *a, **kw: False)


@pytest.fixture(autouse=True)
def _scan_states_table(app):
    """Create ``scan_states`` for the test DB.

    The test engine only creates ``tracks`` (see tests/conftest.py), and
    ``request_scan_stop``/``is_stop_requested`` are ORM queries against
    ``scan_states``. Without this every stop request 500s, which would be
    indistinguishable from the bug under test. Same pattern as
    tests/test_full_scan_startup_fix.py.

    Depends on ``app`` on purpose: importing the app disposes the shared
    in-memory engine (conftest documents this), so a table created BEFORE
    that import is thrown away. Requesting ``app`` here pins the order.
    """
    from sqlalchemy import text

    from db.engine import db_session

    with db_session() as session:
        session.execute(text(
            "CREATE TABLE IF NOT EXISTS scan_states ("
            "scan_type TEXT PRIMARY KEY, is_running BOOLEAN, status TEXT, "
            "stop_requested BOOLEAN, current_artist TEXT, "
            "last_scanned_artist TEXT, extra_data TEXT, updated_at TEXT)"
        ))
    yield


def _ensure_scan_states_table() -> None:
    """Idempotently create ``scan_states`` (safe to call from a test body)."""
    from sqlalchemy import text

    from db.engine import db_session

    with db_session() as session:
        session.execute(text(
            "CREATE TABLE IF NOT EXISTS scan_states ("
            "scan_type TEXT PRIMARY KEY, is_running BOOLEAN, status TEXT, "
            "stop_requested BOOLEAN, current_artist TEXT, "
            "last_scanned_artist TEXT, extra_data TEXT, updated_at TEXT)"
        ))


# ---------------------------------------------------------------------------
# 1. A JSON caller gets JSON, never a redirect to an HTML page
# ---------------------------------------------------------------------------


class TestJsonCallersReceiveJson:
    """A fetch() caller must never receive the dashboard/login HTML page."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("endpoint", STOP_ENDPOINTS)
    async def test_stop_returns_json_not_a_redirect(self, configured, client, endpoint):
        """THE REPORTED BUG: this used to be a 302 to an HTML page."""
        _ensure_scan_states_table()
        response = await client.post(
            endpoint,
            json={},
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 200, (
            f"{endpoint} must answer 200 with a JSON body, got "
            f"{response.status_code}"
        )
        assert response.status_code not in (301, 302), (
            f"{endpoint} redirected to a page — the client would follow it and "
            f"try to JSON.parse the HTML"
        )

        # The decisive assertion: the body must parse as JSON.
        data = await response.get_json()
        assert data is not None, (
            f"{endpoint} did not return a JSON body (content-type "
            f"{response.content_type!r})"
        )
        assert data.get("success") is True
        assert data.get("message")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("endpoint", STOP_ENDPOINTS)
    async def test_the_body_is_not_html(self, configured, client, endpoint):
        """Guard the literal failure: no HTML document in the response body."""
        _ensure_scan_states_table()
        response = await client.post(
            endpoint, json={}, headers={"Content-Type": "application/json"}
        )
        body = (await response.get_data()).decode("utf-8", "replace").strip()

        assert not body.startswith("<!DOCTYPE"), f"{endpoint} returned an HTML document"
        assert not body.startswith("<html"), f"{endpoint} returned an HTML document"
        assert body.startswith("{"), f"{endpoint} body is not a JSON object: {body[:80]}"


# ---------------------------------------------------------------------------
# 2. HTML form callers keep working (the original, load-bearing behaviour)
# ---------------------------------------------------------------------------


class TestFormCallersStillRedirect:
    """A genuine browser form post must still flash + redirect.

    The dashboard templates post these as forms in the legacy tree, and the
    flash banner is the user feedback. Content negotiation must not break that.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("endpoint", STOP_ENDPOINTS)
    async def test_form_post_redirects_to_the_dashboard(self, configured, client, endpoint):
        _ensure_scan_states_table()
        response = await client.post(
            endpoint,
            data={},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/html,application/xhtml+xml",
            },
        )

        assert response.status_code in (301, 302), (
            f"{endpoint} must still redirect an HTML form caller, got "
            f"{response.status_code}"
        )
        assert "/dashboard" in response.headers.get("Location", "")


# ---------------------------------------------------------------------------
# 3. An expired session must produce JSON, not the login page
# ---------------------------------------------------------------------------


class TestExpiredSessionReturnsJson:
    """The auth gate mistook /scan/stop-* for a page request."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("endpoint", ["/scan/stop-all", "/scan/stop-popularity"])
    async def test_unauthenticated_json_caller_gets_401_json(
        self, configured, unauthed_client, endpoint
    ):
        """THE 'auth/session redirect' HALF OF THE BUG.

        Without a session this used to 302 to /login, so the fetch caller
        parsed the login PAGE and reported the HTML-instead-of-JSON error.
        """
        _ensure_scan_states_table()
        response = await unauthed_client.post(
            endpoint, json={}, headers={"Content-Type": "application/json"}
        )

        assert response.status_code == 401, (
            f"{endpoint} must answer 401 for an unauthenticated JSON caller, "
            f"got {response.status_code}"
        )
        assert response.status_code not in (301, 302)
        data = await response.get_json()
        assert data is not None, "an auth failure must be JSON for a JSON caller"
        assert data.get("success") is False

    @pytest.mark.asyncio
    async def test_unauthenticated_page_request_still_redirects_to_login(
        self, configured, unauthed_client
    ):
        """Broadening JSON detection must NOT change page navigation behaviour."""
        response = await unauthed_client.get(
            "/dashboard", headers={"Accept": "text/html,application/xhtml+xml"}
        )

        assert response.status_code in (301, 302)
        assert "/login" in response.headers.get("Location", "")


# ---------------------------------------------------------------------------
# 4. The stop actually takes effect (the fix must not have broken the action)
# ---------------------------------------------------------------------------


class TestTheStopIsActuallyRecorded:
    """The JSON reply must reflect a REAL stop request, not just a 200."""

    @pytest.mark.asyncio
    async def test_stop_all_marks_every_scan_type_stopped(self, configured, client):
        _ensure_scan_states_table()
        from services.scanning.scan_state import (
            get_scan_progress_path,
            is_stop_requested,
        )

        response = await client.post(
            "/scan/stop-all", json={}, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 200

        for scan_type in ("popularity_scan", "singles_scan", "full_scan", "navidrome_scan"):
            assert is_stop_requested(get_scan_progress_path(scan_type)) is True, (
                f"{scan_type} was not marked stop_requested"
            )

    @pytest.mark.asyncio
    async def test_stop_popularity_also_stops_singles(self, configured, client):
        """The combined stop covers both, matching the route's documented intent."""
        _ensure_scan_states_table()
        from services.scanning.scan_state import (
            get_scan_progress_path,
            is_stop_requested,
        )

        await client.post(
            "/scan/stop-popularity", json={}, headers={"Content-Type": "application/json"}
        )

        assert is_stop_requested(get_scan_progress_path("popularity_scan")) is True
        assert is_stop_requested(get_scan_progress_path("singles_scan")) is True


# ---------------------------------------------------------------------------
# 5. The CLIENT must ask for JSON (both trees)
# ---------------------------------------------------------------------------


class TestBothDashboardsAskForJson:
    """Content negotiation only helps if the caller signals JSON.

    The legacy tree called the stop routes with a bare
    ``fetch(url, { method: "POST" })`` — no ``Content-Type``, no ``Accept`` —
    so the route could only answer with its flash+redirect HTML page, and the
    JSON parse failed. Both trees must now send the signal.

    The two trees signal it differently, so each is checked by its own
    mechanism rather than a shared literal:
      - the rebuilt tree delegates to ``api.postJson``, which sets
        ``Content-Type: application/json`` in utils/api.js
      - the legacy tree sends the headers inline in ``stopScanRequest``
    """

    def test_the_test_site_dashboard_delegates_to_the_json_helper(self):
        """The rebuilt dashboard must stop via api.postJson, not a bare fetch."""
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        src = (repo_root / "test_site/static/js/pages/dashboard.js").read_text(
            encoding="utf-8"
        )

        assert "global.api.postJson('/scan/stop-all'" in src, (
            "the rebuilt dashboard must use the JSON helper for stop-all"
        )
        # The helper itself is what carries the header.
        api_helper = (repo_root / "test_site/static/js/utils/api.js").read_text(
            encoding="utf-8"
        )
        assert "'Content-Type': 'application/json'" in api_helper, (
            "api.postJson must send a JSON content type, or the route cannot "
            "detect a JSON caller"
        )

    def test_the_legacy_dashboard_sends_the_json_signal_inline(self):
        """The legacy dashboard has no shared helper, so it must set them itself."""
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        src = (repo_root / "static/js/dashboard.js").read_text(encoding="utf-8")

        assert "stopScanRequest" in src, (
            "the legacy dashboard must route stop calls through the helper that "
            "checks the response"
        )
        assert '"Accept": "application/json"' in src, (
            "the legacy stop helper must ask for JSON"
        )
        assert '"Content-Type": "application/json"' in src

    def test_the_legacy_dashboard_surfaces_stop_failures(self):
        """A silent failure is how the bug went unnoticed for so long.

        The legacy tree's bare fetch ignored the response entirely, so an
        expired session (redirect to the login page) looked identical to a
        successful stop.
        """
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        src = (repo_root / "static/js/dashboard.js").read_text(encoding="utf-8")

        assert "Could not stop scans" in src, (
            "a failed stop must be reported to the operator, not swallowed"
        )

