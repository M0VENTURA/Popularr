"""The metadata-heal endpoint, its config plumbing, and the two Config pages.

Three separate things are pinned here because a heal is only as safe as the ways
it can be triggered:

* the ENDPOINT must default to a dry run, so a stray or replayed call cannot
  rewrite a library, and must reject malformed input rather than coercing it;
* the CONFIG must round-trip — a nested block the Config page does not carry in
  its sanitizer gets silently coerced to ``{}`` on save, which is how
  ``playlists.genre_exclusions`` stopped applying;
* both CONFIG PAGES and both JS collectors must offer the settings, because the
  app ships two UI trees (``templates/`` live and ``test_site/`` rebuilt) and a
  setting added to only one is invisible to whichever tree is being served.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers.config_helpers import get_metadata_heal_config
from routes.ui_routes import _sanitize_config_sections

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
# Endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_endpoint_defaults_to_a_dry_run(client, monkeypatch) -> None:
    """No ``dry_run`` in the body must mean REPORT ONLY.

    The destructive form has to be asked for explicitly — a default of apply
    would make any stray call a library-wide rewrite.
    """
    calls: list[dict] = []

    def _fake(**kwargs):
        calls.append(kwargs)
        return {"dry_run": kwargs.get("dry_run"), "healed": 0, "albums": []}

    from services.downloads import download_metadata_heal_service as heal_mod

    monkeypatch.setattr(heal_mod, "heal_queue_metadata", _fake)

    response = await client.post("/api/downloads/metadata-heal", json={})

    assert response.status_code == 200
    assert calls and calls[0]["dry_run"] is True


@pytest.mark.asyncio
async def test_endpoint_applies_when_dry_run_is_false(client, monkeypatch) -> None:
    calls: list[dict] = []

    def _fake(**kwargs):
        calls.append(kwargs)
        return {"dry_run": kwargs.get("dry_run"), "healed": 1, "albums": []}

    from services.downloads import download_metadata_heal_service as heal_mod

    monkeypatch.setattr(heal_mod, "heal_queue_metadata", _fake)

    response = await client.post("/api/downloads/metadata-heal", json={"dry_run": False})

    assert response.status_code == 200
    assert calls and calls[0]["dry_run"] is False


@pytest.mark.asyncio
async def test_string_false_means_apply(client, monkeypatch) -> None:
    """``"false"`` from a form post must not read as True.

    A truthiness test would make every string — including ``"false"`` — mean
    apply, which is the opposite of the intended default.
    """
    calls: list[dict] = []

    def _fake(**kwargs):
        calls.append(kwargs)
        return {"dry_run": kwargs.get("dry_run"), "healed": 0, "albums": []}

    from services.downloads import download_metadata_heal_service as heal_mod

    monkeypatch.setattr(heal_mod, "heal_queue_metadata", _fake)

    response = await client.post(
        "/api/downloads/metadata-heal", json={"dry_run": "false"}
    )

    assert response.status_code == 200
    assert calls and calls[0]["dry_run"] is False


@pytest.mark.asyncio
async def test_zero_limit_means_no_cap(client, monkeypatch) -> None:
    """``0`` is the Config page's documented "no cap", not "examine nothing"."""
    calls: list[dict] = []

    def _fake(**kwargs):
        calls.append(kwargs)
        return {"healed": 0, "albums": []}

    from services.downloads import download_metadata_heal_service as heal_mod

    monkeypatch.setattr(heal_mod, "heal_queue_metadata", _fake)

    response = await client.post("/api/downloads/metadata-heal", json={"limit": 0})

    assert response.status_code == 200
    assert calls and calls[0]["limit"] is None


@pytest.mark.asyncio
async def test_positive_limit_is_forwarded(client, monkeypatch) -> None:
    calls: list[dict] = []

    def _fake(**kwargs):
        calls.append(kwargs)
        return {"healed": 0, "albums": []}

    from services.downloads import download_metadata_heal_service as heal_mod

    monkeypatch.setattr(heal_mod, "heal_queue_metadata", _fake)

    response = await client.post("/api/downloads/metadata-heal", json={"limit": 25})

    assert response.status_code == 200
    assert calls and calls[0]["limit"] == 25


@pytest.mark.asyncio
async def test_bad_limit_is_rejected(client) -> None:
    response = await client.post("/api/downloads/metadata-heal", json={"limit": "soon"})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_bad_queue_ids_are_rejected(client) -> None:
    response = await client.post(
        "/api/downloads/metadata-heal", json={"queue_ids": ["a", "b"]}
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_queue_ids_must_be_a_list(client) -> None:
    response = await client.post("/api/downloads/metadata-heal", json={"queue_ids": 7})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_a_failing_heal_returns_500_not_a_traceback(client, monkeypatch) -> None:
    def _boom(**kwargs):
        raise RuntimeError("musicbrainz exploded")

    from services.downloads import download_metadata_heal_service as heal_mod

    monkeypatch.setattr(heal_mod, "heal_queue_metadata", _boom)

    response = await client.post("/api/downloads/metadata-heal", json={"dry_run": True})

    assert response.status_code == 500
    assert "musicbrainz exploded" in (await response.get_data(as_text=True))


@pytest.mark.asyncio
async def test_the_endpoint_requires_a_session(unauthed_client) -> None:
    """A write-capable endpoint must never be reachable without a session."""
    response = await unauthed_client.post("/api/downloads/metadata-heal", json={})
    assert response.status_code in (401, 403, 302), (
        "an unauthenticated caller could otherwise rewrite the library"
    )


# ---------------------------------------------------------------------------
# Config plumbing
# ---------------------------------------------------------------------------

def test_config_defaults_are_inert() -> None:
    """The heal ships DISABLED.

    It rewrites files and rows, so enabling it is a deliberate act rather than
    something that starts happening on the next deploy.
    """
    cfg = get_metadata_heal_config()
    assert cfg["enabled"] is False
    assert cfg["run_on_startup"] is True
    assert cfg["limit"] == 200


def test_config_reads_the_saved_block(monkeypatch) -> None:
    monkeypatch.setattr(
        "helpers.config_helpers.get_config",
        lambda: {"downloads": {"metadata_heal": {"enabled": True, "limit": 5}}},
    )
    cfg = get_metadata_heal_config()
    assert cfg["enabled"] is True
    assert cfg["limit"] == 5


def test_config_tolerates_a_junk_limit(monkeypatch) -> None:
    """A hand-edited YAML must not crash the worker at boot."""
    monkeypatch.setattr(
        "helpers.config_helpers.get_config",
        lambda: {"downloads": {"metadata_heal": {"limit": "not-a-number"}}},
    )
    assert get_metadata_heal_config()["limit"] == 200


def test_config_clamps_a_negative_limit(monkeypatch) -> None:
    monkeypatch.setattr(
        "helpers.config_helpers.get_config",
        lambda: {"downloads": {"metadata_heal": {"limit": -10}}},
    )
    assert get_metadata_heal_config()["limit"] == 0


def test_sanitizer_keeps_metadata_heal_a_dict() -> None:
    """Without the nested entry the Config page save coerces the block to ``{}``."""
    cleaned = _sanitize_config_sections(
        {"downloads": {"metadata_heal": {"enabled": True, "limit": 5}}}
    )
    assert cleaned["downloads"]["metadata_heal"] == {"enabled": True, "limit": 5}


def test_sanitizer_coerces_a_scalar_block_to_a_dict() -> None:
    cleaned = _sanitize_config_sections({"downloads": {"metadata_heal": "yes"}})
    assert cleaned["downloads"]["metadata_heal"] == {}


# ---------------------------------------------------------------------------
# UI parity — both trees
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("page", CONFIG_PAGES, ids=lambda p: p.parts[-3])
def test_config_page_offers_the_settings(page: Path) -> None:
    html = _read(page)
    for field in (
        "downloads_heal_enabled",
        "downloads_heal_on_startup",
        "downloads_heal_limit",
        "downloads_heal_preview",
        "downloads_heal_run",
        "downloads_heal_result",
    ):
        assert field in html, f"{page.name} is missing {field}"


@pytest.mark.parametrize("page", CONFIG_PAGES, ids=lambda p: p.parts[-3])
def test_config_page_reads_the_nested_block(page: Path) -> None:
    assert "get('downloads', {}).get('metadata_heal'" in _read(page)


@pytest.mark.parametrize("js", CONFIG_JS, ids=lambda p: p.parent.name)
def test_config_js_collects_the_block(js: Path) -> None:
    """The page must actually SAVE the settings, or they silently reset."""
    source = _read(js)
    assert "metadata_heal:" in source
    assert "downloads_heal_enabled" in source
    assert "downloads_heal_limit" in source


@pytest.mark.parametrize("js", CONFIG_JS, ids=lambda p: p.parent.name)
def test_config_js_calls_the_endpoint(js: Path) -> None:
    source = _read(js)
    assert "/api/downloads/metadata-heal" in source
    # Preview must be a dry run, and the destructive call must be separate.
    assert "dry_run: dryRun" in source or "dry_run: true" in source
