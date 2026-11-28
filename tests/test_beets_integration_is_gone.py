"""The Beets integration is fully retired.

The 2026-08-14 changelog says `routes/beets_routes.py` was deleted. It was not:
the file was still on disk, 240 lines of `/api/beets/*` endpoints, with its
`beets_bp` blueprint registered in no bootstrap module and imported by nothing.
The same changelog said the `beets_*` DB columns were kept for the
metadata-compare page — but those columns exist in no schema, and that page was
retired too (its own route now redirects to /correcting).

A file nothing imports and nothing registers is dead weight that reads as a
live feature: it shows up in searches, in the route count, and in this project's
own docs. This suite pins the removal.

WHAT IS DELIBERATELY KEPT
-------------------------
* ``services/metadata/tag_names.py`` and ``tag_file_service.py`` still mention
  ``beets_mbid`` in their comments — those are FILE-TAG synonym notes (Beets and
  Picard write the same MUSICBRAINZ_TRACKID frame), which is a different thing
  from the integration and still worth documenting.
* The config page's and ``config_helpers``' prose tells users they can run an
  external tagger (Beets, Picard) with Popularr's tag writes switched off. That
  advice is about the read-only mode, not about an integration.
"""

from __future__ import annotations

import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
ROUTES = REPO_ROOT / "routes"


def _live_python() -> list[pathlib.Path]:
    return [
        p for p in REPO_ROOT.rglob("*.py")
        if not any(
            skip in str(p)
            for skip in ("old_system", "__pycache__", "node_modules", ".git")
        )
    ]


# ---------------------------------------------------------------------------
# The module and its blueprint are gone
# ---------------------------------------------------------------------------


def test_the_routes_module_is_deleted() -> None:
    assert not (ROUTES / "beets_routes.py").exists(), (
        "routes/beets_routes.py is back. It was never registered and never "
        "imported; keeping it means dead code that reads as a live feature."
    )


def test_no_module_defines_the_blueprint() -> None:
    offenders = [
        f"{p.relative_to(REPO_ROOT)}"
        for p in _live_python()
        if "beets_bp" in p.read_text(encoding="utf-8", errors="replace")
    ]
    assert not offenders, f"a beets blueprint is defined in: {offenders}"


def test_no_module_imports_it() -> None:
    needles = ("beets_routes", "routes.beets", "from routes import beets")
    offenders = []
    for path in _live_python():
        body = path.read_text(encoding="utf-8", errors="replace")
        if any(needle in body for needle in needles):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"modules still importing the retired blueprint: {offenders}"


# ---------------------------------------------------------------------------
# Nothing calls the retired API
# ---------------------------------------------------------------------------


def test_no_frontend_calls_the_beets_api() -> None:
    offenders = [
        f"{p.relative_to(REPO_ROOT)}"
        for p in (REPO_ROOT / "test_site").rglob("*")
        if p.suffix in {".js", ".html"}
        and "/api/beets" in p.read_text(encoding="utf-8", errors="replace")
    ]
    assert not offenders, f"/api/beets is called from: {offenders}"


# ---------------------------------------------------------------------------
# The retired columns are not read as if they existed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "column", ["beets_mbid", "beets_year", "beets_album_artist"]
)
def test_the_page_data_block_does_not_carry_retired_columns(column: str) -> None:
    """The track page's ``#page-data`` published all three, always null."""
    page = (REPO_ROOT / "test_site/templates/Pages/track_detail.html").read_text(
        encoding="utf-8"
    )
    payload = page[page.index('id="page-data"'): page.index("| tojson")]
    assert column not in payload, (
        f"#page-data still publishes {column}. It is in no schema, so the key is "
        "always null and no script reads it."
    )


# ---------------------------------------------------------------------------
# CONTROLS — the two deliberate keeps
# ---------------------------------------------------------------------------


def test_tag_synonym_notes_are_kept() -> None:
    """CONTROL: ``beets_mbid`` as a FILE-TAG synonym is a different thing.

    Beets and Picard write the same MUSICBRAINZ_TRACKID frame, so the
    standardiser must still know the spelling to clear stray frames.
    """
    names = (REPO_ROOT / "services/metadata/tag_names.py").read_text(encoding="utf-8")
    assert "beets_mbid" in names, (
        "the tag-synonym note was deleted along with the integration — they are "
        "unrelated, and losing it means a stray frame is never cleaned up"
    )


def test_the_read_only_advice_is_kept() -> None:
    """CONTROL: the config prose is about running *any* external tagger."""
    config = (REPO_ROOT / "test_site/templates/Pages/config.html").read_text(
        encoding="utf-8"
    )
    assert "Beets" in config, (
        "the read-only-mode advice names Beets as an example of an external "
        "tagger; that is still true and still useful"
    )
