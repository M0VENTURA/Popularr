"""/metadata-compare is retired into /correcting.

The page could not work, in three independent ways:

1. It queried ``beets_year``, which exists in neither ``db/schema.py``'s
   ``COLUMN_REGISTRY['tracks']``, ``db/models.py``, nor any migration — the
   Beets importer that wrote it lives in ``old_system/deprecated/``. Any real
   database raised UndefinedColumn, the handler caught it and redirected to the
   dashboard, so the page could never render.
2. Its "Beets" column was ``musicbrainz_genres``, compared against
   ``musicbrainz_genres`` — one value against itself.
3. Both write endpoints targeted columns that do not exist (``beets_year``,
   ``mb_override``, ``metadata_locked``), so every action 500'd.

/correcting already covers both halves with working actions:
``conflict_service.PROTECTED_FIELDS`` contains ``genres`` and ``year`` and
records provider disagreements in ``metadata_conflicts``; the MusicBrainz side
is the per-album lookup plus the metadata-proposal review.

This suite pins the retirement so the page cannot be half-restored.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
UI_ROUTES = REPO_ROOT / "routes" / "ui_routes.py"
SCHEMA = REPO_ROOT / "db" / "schema.py"

#: Files the retired page owned.
REMOVED = [
    "test_site/templates/Pages/metadata_compare.html",
    "test_site/static/js/pages/metadata-compare.js",
    "test_site/static/css/metadata-compare.css",
]

#: The three JSON endpoints its controller called. Nothing else called them.
REMOVED_ENDPOINTS = [
    "metadata_compare_search_mb",
    "metadata_compare_apply_mb",
    "metadata_compare_accept_navidrome",
]


def _functions() -> dict[str, str]:
    src = UI_ROUTES.read_text(encoding="utf-8")
    tree = ast.parse(src)
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            segment = ast.get_source_segment(src, node)
            if segment:
                out[node.name] = segment
    return out


# ---------------------------------------------------------------------------
# The route survives as a redirect
# ---------------------------------------------------------------------------


def test_the_route_still_exists_so_old_bookmarks_land_somewhere() -> None:
    assert "metadata_compare" in _functions(), (
        "the route was deleted outright — an existing bookmark would now 404. "
        "Keep it as a redirect to /correcting."
    )


def test_it_redirects_to_correcting() -> None:
    body = _functions()["metadata_compare"]
    assert 'redirect(url_for("ui.correcting"))' in body
    assert "render_template" not in body, "the page renders again"


# ---------------------------------------------------------------------------
# The three dead endpoints are gone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", REMOVED_ENDPOINTS)
def test_the_dead_endpoint_is_deleted(name: str) -> None:
    assert name not in _functions(), (
        f"{name} is back. Every one of these targeted a column that does not "
        "exist (beets_year / mb_override / metadata_locked), so it can only 500."
    )


def test_the_api_paths_are_not_registered_anywhere() -> None:
    src = UI_ROUTES.read_text(encoding="utf-8")
    for path in ("/api/metadata-compare/search-musicbrainz",
                 "/api/metadata-compare/apply-musicbrainz",
                 "/api/metadata-compare/accept-navidrome"):
        assert f'"{path}"' not in src, f"{path} is routed again"


# ---------------------------------------------------------------------------
# The reason it was retired is still true
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("column", ["beets_year", "metadata_locked", "mb_override"])
def test_the_page_was_querying_a_column_that_does_not_exist(column: str) -> None:
    """CONTROL for the diagnosis.

    If one of these columns is ever legitimately added, this test fails and the
    retirement's justification — recorded in the route's docstring — needs
    revisiting rather than being silently untrue.
    """
    schema = SCHEMA.read_text(encoding="utf-8")
    assert column not in schema, (
        f"db/schema.py now declares {column!r}. The /metadata-compare "
        "retirement was justified by its absence — recheck the docstring in "
        "routes/ui_routes.py::metadata_compare before trusting it."
    )


# ---------------------------------------------------------------------------
# Nothing is left behind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel", REMOVED)
def test_the_page_assets_are_deleted(rel: str) -> None:
    assert not (REPO_ROOT / rel).exists(), (
        f"{rel} is back — a template nobody renders and a controller nobody "
        "loads read as live code"
    )


def test_no_template_links_to_it() -> None:
    """It had no nav entry before (that was the original complaint), and must
    not gain one now that it is a redirect."""
    for tpl in (REPO_ROOT / "test_site" / "templates").rglob("*.html"):
        body = tpl.read_text(encoding="utf-8", errors="replace")
        assert "/metadata-compare" not in body, (
            f"{tpl.relative_to(REPO_ROOT)} links to the retired page"
        )
