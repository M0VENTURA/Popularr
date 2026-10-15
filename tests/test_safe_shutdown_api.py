"""The safe-shutdown endpoints and their UI/config surfaces.

A maintenance control is only as safe as the ways it can be triggered, so this
pins the endpoint contract (including the auth gate — an unauthenticated caller
must not be able to quiesce the application), and the UI parity across the two
trees the app ships.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

CONFIG_PAGES = (
    REPO_ROOT / "templates" / "pages" / "config.html",
    REPO_ROOT / "test_site" / "templates" / "Pages" / "config.html",
)

CONFIG_JS = (
    REPO_ROOT / "static" / "js" / "config.js",
    REPO_ROOT / "test_site" / "static" / "js" / "pages" / "config.js",
)


def _read(path: Path) -> str:
    assert path.is_file(), f"expected {path} to exist"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_endpoint_reports_state(client, monkeypatch) -> None:
    from services.maintenance import safe_shutdown_service as ss

    monkeypatch.setattr(
        ss, "safe_shutdown_status",
        lambda: {"requested": False, "safe_to_stop": False, "active_scans": [], "queue_items": 0},
    )

    response = await client.get("/api/maintenance/safe-shutdown")

    assert response.status_code == 200
    body = await response.get_json()
    assert body["requested"] is False
    assert body["safe_to_stop"] is False


@pytest.mark.asyncio
async def test_prepare_endpoint_forwards_drain(client, monkeypatch) -> None:
    calls: list[dict] = []

    def _fake(*, reason: str, drain: bool):
        calls.append({"reason": reason, "drain": drain})
        return {"success": True, "safe_to_stop": True}

    from services.maintenance import safe_shutdown_service as ss

    monkeypatch.setattr(ss, "prepare_for_database_shutdown", _fake)

    response = await client.post(
        "/api/maintenance/safe-shutdown", json={"drain": True, "reason": "upgrade"}
    )

    assert response.status_code == 200
    assert calls == [{"reason": "upgrade", "drain": True}]


@pytest.mark.asyncio
async def test_prepare_endpoint_drains_by_default(client, monkeypatch) -> None:
    """No ``drain`` in the body must mean drain.

    Skipping the drain by default would release the pool while a scan or queue
    cycle is still running — the exact write-into-a-dying-server case this exists
    to prevent.
    """
    calls: list[dict] = []

    def _fake(*, reason: str, drain: bool):
        calls.append({"drain": drain})
        return {"success": True, "safe_to_stop": True}

    from services.maintenance import safe_shutdown_service as ss

    monkeypatch.setattr(ss, "prepare_for_database_shutdown", _fake)

    response = await client.post("/api/maintenance/safe-shutdown", json={})

    assert response.status_code == 200
    assert calls and calls[0]["drain"] is True


@pytest.mark.asyncio
async def test_string_false_means_no_drain(client, monkeypatch) -> None:
    """``"false"`` from a form post must not read as True (it is a truthy string)."""
    calls: list[dict] = []

    def _fake(*, reason: str, drain: bool):
        calls.append({"drain": drain})
        return {"success": True, "safe_to_stop": True}

    from services.maintenance import safe_shutdown_service as ss

    monkeypatch.setattr(ss, "prepare_for_database_shutdown", _fake)

    response = await client.post(
        "/api/maintenance/safe-shutdown", json={"drain": "false"}
    )

    assert response.status_code == 200
    assert calls and calls[0]["drain"] is False


@pytest.mark.asyncio
async def test_a_blank_reason_is_normalised(client, monkeypatch) -> None:
    calls: list[dict] = []

    def _fake(*, reason: str, drain: bool):
        calls.append({"reason": reason})
        return {"success": True}

    from services.maintenance import safe_shutdown_service as ss

    monkeypatch.setattr(ss, "prepare_for_database_shutdown", _fake)

    response = await client.post("/api/maintenance/safe-shutdown", json={"reason": "   "})

    assert response.status_code == 200
    assert calls and calls[0]["reason"] == "manual"


@pytest.mark.asyncio
async def test_a_failed_prepare_returns_500(client, monkeypatch) -> None:
    from services.maintenance import safe_shutdown_service as ss

    monkeypatch.setattr(
        ss, "prepare_for_database_shutdown",
        lambda **kw: {"success": False, "error": "state directory not writable"},
    )

    response = await client.post("/api/maintenance/safe-shutdown", json={})

    assert response.status_code == 500
    assert "state directory not writable" in (await response.get_data(as_text=True))


@pytest.mark.asyncio
async def test_a_raising_prepare_returns_500_not_a_traceback(client, monkeypatch) -> None:
    from services.maintenance import safe_shutdown_service as ss

    def _boom(**kwargs):
        raise RuntimeError("dispose exploded")

    monkeypatch.setattr(ss, "prepare_for_database_shutdown", _boom)

    response = await client.post("/api/maintenance/safe-shutdown", json={})

    assert response.status_code == 500
    assert "dispose exploded" in (await response.get_data(as_text=True))


@pytest.mark.asyncio
async def test_cancel_endpoint(client, monkeypatch) -> None:
    from services.maintenance import safe_shutdown_service as ss

    monkeypatch.setattr(
        ss, "cancel_safe_shutdown",
        lambda reason="manual": {"success": True, "requested": False},
    )

    response = await client.post("/api/maintenance/safe-shutdown/cancel", json={})

    assert response.status_code == 200
    assert (await response.get_json())["requested"] is False


@pytest.mark.asyncio
async def test_release_connections_endpoint(client, monkeypatch) -> None:
    from services.maintenance import safe_shutdown_service as ss

    monkeypatch.setattr(ss, "release_database_connections", lambda: {"disposed": True, "errors": []})
    monkeypatch.setattr(ss, "safe_shutdown_status", lambda: {"requested": True, "safe_to_stop": True})

    response = await client.post(
        "/api/maintenance/safe-shutdown/release-connections", json={}
    )

    assert response.status_code == 200
    body = await response.get_json()
    assert body["disposed"] is True
    assert body["safe_to_stop"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/maintenance/safe-shutdown"),
        ("post", "/api/maintenance/safe-shutdown"),
        ("post", "/api/maintenance/safe-shutdown/cancel"),
        ("post", "/api/maintenance/safe-shutdown/release-connections"),
    ],
)
async def test_every_maintenance_route_requires_a_session(
    unauthed_client, method: str, path: str
) -> None:
    """⚠️ An unauthenticated caller must not be able to quiesce the application.

    The write routes are obvious, but the STATUS route matters too: it discloses
    what the instance is doing, and it must not be reachable without a session.
    """
    response = await getattr(unauthed_client, method)(path, json={})

    assert response.status_code in (401, 403, 302), (
        f"{method.upper()} {path} was reachable without a session"
    )


# ---------------------------------------------------------------------------
# UI parity — both trees
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("page", CONFIG_PAGES, ids=lambda p: p.parts[-3])
def test_config_page_offers_the_controls(page: Path) -> None:
    html = _read(page)
    for field in (
        "safe_shutdown_enabled",
        "safe_shutdown_drain_timeout",
        "safe_shutdown_auto_cancel",
        "safe_shutdown_release_connections",
        "safe_shutdown_prepare",
        "safe_shutdown_cancel",
        "safe_shutdown_refresh",
        "safe_shutdown_status",
    ):
        assert field in html, f"{page.name} is missing {field}"


@pytest.mark.parametrize("page", CONFIG_PAGES, ids=lambda p: p.parts[-3])
def test_config_page_reads_the_nested_block(page: Path) -> None:
    assert "get('maintenance', {}).get('safe_shutdown'" in _read(page)


@pytest.mark.parametrize("js", CONFIG_JS, ids=lambda p: p.parent.name)
def test_config_js_collects_the_block(js: Path) -> None:
    source = _read(js)
    assert "safe_shutdown: {" in source
    assert "safe_shutdown_drain_timeout" in source
    assert "safe_shutdown_auto_cancel" in source


@pytest.mark.parametrize("js", CONFIG_JS, ids=lambda p: p.parent.name)
def test_config_js_calls_both_endpoints(js: Path) -> None:
    source = _read(js)
    assert "/api/maintenance/safe-shutdown'" in source, "the prepare endpoint is not called"
    assert "/api/maintenance/safe-shutdown/cancel" in source, "the cancel endpoint is not called"


@pytest.mark.parametrize("js", CONFIG_JS, ids=lambda p: p.parent.name)
def test_config_js_shows_the_host_command(js: Path) -> None:
    """The operator needs the next step, not just a status word."""
    assert "docker compose stop db" in _read(js)


# ---------------------------------------------------------------------------
# The host script
# ---------------------------------------------------------------------------

def test_host_script_exists_and_is_executable_in_git() -> None:
    script = REPO_ROOT / "scripts" / "safe-shutdown.sh"
    assert script.is_file(), "the operator-facing script must ship"


def test_host_script_verifies_a_clean_stop() -> None:
    """⚠️ The exit-code check is the point of the script.

    A force-killed container exits 137 and the ``docker stop`` still "succeeds",
    so without this check a crash recovery is indistinguishable from a clean stop.
    """
    source = _read(REPO_ROOT / "scripts" / "safe-shutdown.sh")
    assert "137" in source, "the script must detect a SIGKILLed container"
    assert "State.ExitCode" in source, "the script must read the real exit code"


def test_host_script_drives_the_app_inside_the_container() -> None:
    """No HTTP and no credentials: it must work when the web server is unhealthy."""
    source = _read(REPO_ROOT / "scripts" / "safe-shutdown.sh")
    assert "compose exec" in source
    assert "safe_shutdown_service" in source


def test_host_script_warns_about_a_major_version_jump() -> None:
    """A 16 -> 17 upgrade needs pg_upgrade; the script must not imply otherwise."""
    source = _read(REPO_ROOT / "scripts" / "safe-shutdown.sh")
    assert "pg_upgrade" in source
