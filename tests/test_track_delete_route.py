"""Regression tests for the album page's single-track delete.

Context — the bug these pin down
--------------------------------
The album page's per-row trash button did::

    window.location.href = `/track/${trackId}/delete`;

That is a browser GET to a URL **no route in this app has ever served**, so
every click landed on a 404.  Two separate defects were behind it:

1. There was no single-track delete endpoint at all — deletes existed only as
   ``POST /api/artist/corrections/delete-track`` (used by the artist page),
   ``POST /api/album/bulk-delete`` and ``POST /api/v1/albums/.../bulk-delete``.
2. ``routes/api_v1/albums.py`` was written and documented as part of the
   ``api_v1`` package but was **never imported** by ``__init__.py``, so none
   of its routes were registered.  Every ``/api/v1/albums/...`` call the album
   page made (``musicbrainz-compare``, ``bulk-delete``) 404'd while the file
   sat there looking correct.

The fix adds ``POST /api/v1/tracks/<track_id>/delete`` and the missing import.

Note on the verb: a destructive change must not be reachable by a GET, so the
route is POST-only and the browser-navigation pattern is gone.  Several tests
below assert that directly, because "someone re-adds a convenient GET alias" is
the most likely way this regresses.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: A browser navigation to a track view, capturing everything after ``/track/``.
#: Matches ``location.href = `/track/${id}/edit` `` and the ``/delete`` form it
#: replaced; ``${...}`` interpolations are stripped by the caller before the
#: remaining path is judged.
_TRACK_NAV_RE = re.compile(r"location\.href\s*=\s*[`\"']/track/([^`\"']*)[`\"']")


def _rule_strings(app) -> set[str]:
    """Every registered URL rule, as "/a/<b>/c" strings."""
    return {str(rule) for rule in app.url_map.iter_rules()}


def _methods_for(app, path: str) -> set[str]:
    """HTTP methods registered for a literal rule path (ignores converters)."""
    for rule in app.url_map.iter_rules():
        if str(rule) == path:
            return {m for m in rule.methods if m not in {"HEAD", "OPTIONS"}}
    return set()


# ---------------------------------------------------------------------------
# The new endpoint exists, and only accepts POST
# ---------------------------------------------------------------------------

def test_single_track_delete_route_is_registered(app):
    """POST /api/v1/tracks/<id>/delete must be registered."""
    assert "/api/v1/tracks/<track_id>/delete" in _rule_strings(app)


def test_single_track_delete_is_post_only(app):
    """A destructive route must not be reachable by GET.

    This is the core regression guard: the original bug was a GET navigation,
    and adding a GET alias "to make the old link work" would reintroduce the
    same class of defect (prefetch engines and crawlers issue GETs).
    """
    methods = _methods_for(app, "/api/v1/tracks/<track_id>/delete")
    assert methods == {"POST"}, f"expected POST-only, got {sorted(methods)}"


def test_no_get_route_at_the_old_broken_path(app):
    """The URL the JS used to navigate to must NOT resolve to anything.

    If someone ever adds it, this test should fail loudly and the JS should be
    pointed at the POST endpoint instead — the assertion is deliberately about
    the absence of a *GET*, since that is what the bug was.
    """
    for rule in app.url_map.iter_rules():
        if str(rule) == "/track/<track_id>/delete":
            methods = {m for m in rule.methods if m not in {"HEAD", "OPTIONS"}}
            assert "GET" not in methods, (
                "A GET route reappeared at /track/<id>/delete. The album page "
                "used to navigate here and it always 404'd; deletes are "
                "POST /api/... endpoints."
            )


# ---------------------------------------------------------------------------
# The api_v1 albums module is actually imported (the registration bug)
# ---------------------------------------------------------------------------

def test_api_v1_albums_routes_are_registered(app):
    """Importing api_v1 must register albums.py's routes.

    ``albums.py`` documented ``from . import albums`` in its own docstring but
    the import was missing, so the module was dead code and its endpoints
    404'd.  Both routes are asserted so deleting one is noticed.
    """
    rules = _rule_strings(app)
    assert "/api/v1/albums/<path:artist>/<path:album>/bulk-delete" in rules
    assert "/api/v1/albums/<path:artist>/<path:album>/musicbrainz-compare" in rules


def test_api_v1_albums_import_is_present():
    """The package must import ``albums``, not only ``tracks``/``artists``."""
    source = (REPO_ROOT / "routes" / "api_v1" / "__init__.py").read_text(
        encoding="utf-8"
    )
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith("from . import")
    ]
    assert any("albums" in line for line in import_lines), (
        "routes/api_v1/__init__.py no longer imports the albums sub-module; "
        "its routes will silently stop being registered."
    )


# ---------------------------------------------------------------------------
# Behaviour: missing track, and the payload contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_unknown_track_returns_404(client):
    """Deleting a track that does not exist is a 404, not a 500.

    ``artist_service.delete_track`` returns ``( {...}, 404 )`` for a missing
    row; the route must pass that status through rather than flattening it.
    """
    response = await client.post(
        "/api/v1/tracks/does-not-exist/delete", json={"delete_file": False}
    )
    assert response.status_code == 404
    data = await response.get_json()
    assert data["success"] is False
    assert data.get("error")


@pytest.mark.asyncio
async def test_delete_defaults_to_removing_the_file(client, app):
    """Omitting ``delete_file`` must not blow up and must still 404 cleanly.

    The route defaults ``delete_file`` to True (matching the artist page and
    the album page's own bulk flow, which treats file removal as the norm), so
    an empty body must be handled rather than raising on a missing key.
    """
    response = await client.post("/api/v1/tracks/does-not-exist/delete", json={})
    assert response.status_code == 404
    data = await response.get_json()
    assert data["success"] is False


@pytest.mark.asyncio
async def test_delete_accepts_a_bodyless_post(client):
    """No JSON at all must be tolerated (``silent=True``), still a clean 404."""
    response = await client.post("/api/v1/tracks/does-not-exist/delete")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# The front-end no longer navigates to the dead URL
# ---------------------------------------------------------------------------

def _strip_js_comments(source: str) -> str:
    """Remove ``//`` line comments and ``/* */`` blocks from JS.

    Both controllers document the bug they fix, and that documentation quotes
    the dead URL verbatim — so a naive ``in source`` check would match the
    explanatory comment and pass/fail on prose rather than on behaviour. The
    assertions below are about what the code DOES, so comments are removed
    first.

    Regex rather than a real parser: these files contain no regex or string
    literal that looks like a comment, and pulling in a JS tokenizer for two
    assertions would not be worth the dependency.
    """
    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", without_blocks)


@pytest.mark.parametrize(
    "rel_path",
    [
        "static/js/album_detail.js",
        "test_site/static/js/pages/album.js",
    ],
)
def test_album_js_does_not_navigate_to_the_dead_delete_url(rel_path: str):
    """No controller may navigate to ``/track/<id>/...`` — only ``/track/<id>``.

    Both trees carried the identical bug, so both are checked — fixing one and
    not the other is exactly the kind of half-fix that caused this.

    WHY THIS PARSES THE URL rather than searching for a fragment: the only
    registered track *view* route is ``/track/<track_id>``
    (``routes/ui_routes.py``), and the edit-modal fallback legitimately
    navigates there. So ``/delete`` and ``/edit`` are both wrong, and a bare
    ``"/delete" not in source`` would also match the explanatory comment (these
    functions document the bug they fix, quoting the dead URL). Each navigation
    is therefore extracted, its ``${...}`` interpolations removed, and the
    LEFTOVER — the extra path segment — must be empty.
    """
    code = _strip_js_comments((REPO_ROOT / rel_path).read_text(encoding="utf-8"))

    navigations = _TRACK_NAV_RE.findall(code)
    assert navigations, (
        f"{rel_path}: expected at least one /track/<id> navigation to check; "
        "if the fallbacks were removed, delete this test rather than leaving "
        "it vacuously passing."
    )

    for nav in navigations:
        remainder = re.sub(r"\$\{[^}]*\}", "", nav).strip().strip("/")
        assert remainder == "", (
            f"{rel_path}: navigates to /track/<id>/{remainder} — NO such route "
            "exists. The only registered track view is /track/<track_id>; "
            "deletes are POST /api/v1/tracks/<id>/delete."
        )


@pytest.mark.parametrize(
    "rel_path",
    [
        "static/js/album_detail.js",
        "test_site/static/js/pages/album.js",
    ],
)
def test_album_js_targets_the_real_endpoint(rel_path: str):
    """Both controllers must call the registered POST endpoint."""
    code = _strip_js_comments((REPO_ROOT / rel_path).read_text(encoding="utf-8"))
    assert "/tracks/${encodeURIComponent(trackId)}/delete" in code
    assert "POST" in code or "postJson" in code
