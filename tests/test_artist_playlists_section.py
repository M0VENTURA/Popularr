"""Artist page: the "Included in Playlists" section.

Two things can break here, and they need different tests:

1. The TEMPLATE — the section markup, the counts, the per-playlist accordion.
   Verified by RENDERING the shipped templates, not by grepping them, because a
   grep cannot tell a rendered section from one inside a false branch.
2. The WIRING — that the route puts the data in the payload at all. A section
   whose variable is never supplied renders as an empty state, which looks
   exactly like "no playlists found".

Both templates are asserted because the route renders `artist_detail_v2.html`
and the loader resolves that name against the REBUILT tree first when the
cutover is on — so a change to one tree only is invisible to half the users.
"""

from __future__ import annotations

import pathlib

import jinja2
import pytest
from sqlalchemy import text

from db.engine import db_session
from db.repositories import playlists as repo
from services.playlists.playlist_index_service import playlists_for_artist

# The hermetic renderer. Its own Jinja Environment against a template root, so
# nothing another test does to the app can change what these renders produce —
# and unlike the app's environment it needs no app context and no event loop.
from tests._test_site_render import build_env

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: (templates root, template name relative to it) for BOTH trees.
#
# Both are asserted because the route renders `artist_detail_v2.html` and the
# loader resolves that name against the REBUILT tree first when the cutover is
# on — so a change to one tree only is invisible to half the users.
TREES = (
    ("test_site/templates", "Pages/artist_detail_v2.html"),
    ("templates", "pages/artist_detail_v2.html"),
)

TEMPLATE_PATHS = tuple(f"{root}/{name}" for root, name in TREES)

SECTION_MARKER = "artist-playlists-section"
EMPTY_TEXT = "None of this artist's tracks are in a playlist yet."


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_tables():
    """Clear tracks + the playlist index (the engine is shared across tests)."""
    with db_session() as session:
        session.execute(text("DELETE FROM playlist_entries"))
        session.execute(text("DELETE FROM playlists"))
        session.execute(text("DELETE FROM tracks"))
    yield


@pytest.fixture(autouse=True)
def _stub_scan_active(monkeypatch):
    """Stop the payload's scan probe from WIPING the test database.

    ⚠️ This is not tidiness — without it the route's payload cannot be called at
    all in tests, and the failure is deeply misleading.

    ``_build_artist_detail_payload`` opens with ``is_popularity_scan_active()``,
    which queries ``scan_states``/progress rows. The test harness never creates
    those tables, so the query raises ``OperationalError`` — and
    ``db.engine._is_transient_db_error`` returns True for EVERY
    ``OperationalError`` (including "no such table"), so the session manager
    DISPOSES the shared engine. For SQLite ``:memory:`` that closes the only
    connection and takes every table with it. The payload is caught by its own
    ``except Exception`` and carries on, then its next query fails with
    "no such table: tracks" — which reads like a broken query rather than a
    database that was erased two statements earlier.

    Patching it is the honest scope: this file tests the playlist section, not
    scan detection. The dependency is imported INSIDE the function, so patching
    the source module bites.
    """
    monkeypatch.setattr(
        "services.scanning.pipelines.popularity_pipeline.is_popularity_scan_active",
        lambda *a, **k: False,
    )
    yield


@pytest.fixture
def indexed(monkeypatch):
    """Seed two library tracks and one playlist holding both."""
    with db_session() as session:
        for track_id, title in (("ap1", "Come As You Are"), ("ap2", "Lithium")):
            session.execute(
                text(
                    "INSERT INTO tracks (id, artist, album_artist, album, title, stars) "
                    "VALUES (:id, :artist, :artist, 'Nevermind', :title, 5) "
                    "ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title"
                ),
                {"id": track_id, "artist": "Playlist Artist", "title": title},
            )

    repo.replace_playlist(
        playlist_id="pl-grunge",
        name="Grunge - Top Tracks",
        source="navidrome",
        playlist_type="regular",
        owner="admin",
        is_public=True,
        song_count=40,
        comment="",
        changed="c1",
        track_ids=["ap1", "ap2"],
    )
    return ["ap1", "ap2"]


def _playlist_payload(name: str) -> dict:
    """The payload keys this section reads, from the REAL service.

    Built here rather than by calling `_build_artist_detail_payload`.

    ⚠️ That builder cannot be exercised in this harness at all, and the reason is
    worth knowing: it queries several tables the test schema never creates
    (`scan_states` via `is_popularity_scan_active`, `artists`,
    `upcoming_releases`), and `db.engine._is_transient_db_error` returns True for
    EVERY `OperationalError` — including "no such table". Each miss therefore
    DISPOSES the shared engine, which for SQLite `:memory:` closes the only
    connection and takes every table with it. `tracks` is gone long before the
    builder reaches the playlist section, so the section reads empty and the
    test would report "the feature is broken" when the harness erased the data.

    ROUTE WIRING is asserted separately, on the source, by
    `test_the_route_supplies_the_playlists`.
    """
    return {
        "artist_name": name,
        "artist_playlists": playlists_for_artist(name),
    }


def _render(templates_root: str, template_name: str, payload: dict) -> str:
    """Render a SHIPPED template from its own tree.

    Uses the hermetic environment (`tests/_test_site_render.build_env`) with its
    loader re-rooted at the chosen tree, so `{% extends "base.html" %}` and the
    `_release_section.html` macro import both resolve inside THAT tree — which
    is the point of testing both.
    """
    from jinja2 import FileSystemLoader

    env = build_env("/artist/Someone")
    env.loader = FileSystemLoader(str(REPO_ROOT / templates_root))

    # Anything the payload does not carry is passed empty, so an unrelated
    # section cannot fail the render and mask this one.
    context = {
        "artist_name": payload.get("artist_name", ""),
        "albums": [],
        "stats": {},
        "top_tracks": [],
        "genre_sources": {},
        "genres": [],
        "albums_by_category": {},
        "album_categories": [],
        "appears_on_albums": [],
        "artist_bio": "",
        "artist_country": "",
        "artist_members": [],
        "artist_aliases": [],
        "artist_alias_canonical": "",
        "similar_artists": {},
        "scan_active": False,
        "slskd_config": {},
        "artist_playlists": [],
    }
    context.update(payload)
    return env.get_template(template_name).render(**context)


# ---------------------------------------------------------------------------
# The template parses
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("template", TEMPLATE_PATHS)
def test_the_template_parses(template):
    """A Jinja syntax error is a 500 on the artist page, and the parse is cheap."""
    jinja2.Environment().parse(
        (REPO_ROOT / template).read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# The section is present in both trees
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("template", TEMPLATE_PATHS)
def test_the_section_exists_in_both_trees(template):
    source = (REPO_ROOT / template).read_text(encoding="utf-8")
    assert SECTION_MARKER in source
    assert "Included in Playlists" in source
    assert "artist_playlists" in source


@pytest.mark.parametrize("template", TEMPLATE_PATHS)
def test_the_section_has_an_empty_state(template):
    """A hidden section reads as a broken page; an empty one reads as a fact."""
    source = (REPO_ROOT / template).read_text(encoding="utf-8")
    assert EMPTY_TEXT in source


# ---------------------------------------------------------------------------
# The route supplies the data
# ---------------------------------------------------------------------------

class TestRouteWiring:
    """The route must put the data in the PAYLOAD.

    Asserted on the source because the payload builder cannot run in this
    harness (see `_playlist_payload`). The invariant is a wiring one — "does the
    template's variable get supplied" — and a section whose variable is never
    supplied renders its empty state, which is indistinguishable from "no
    playlists found".
    """

    def _source(self) -> str:
        return (REPO_ROOT / "routes" / "ui_routes.py").read_text(encoding="utf-8")

    def test_the_payload_returns_the_key(self):
        assert '"artist_playlists": artist_playlists' in self._source()

    def test_it_is_filled_by_the_index_service(self):
        source = self._source()
        assert "from services.playlists.playlist_index_service import playlists_for_artist" in source
        assert "artist_playlists = playlists_for_artist(name)" in source

    def test_a_failure_leaves_it_empty_rather_than_raising(self):
        """The section is a convenience view — it must not cost the page."""
        source = self._source()
        start = source.index("artist_playlists: list[dict[str, Any]] = []")
        window = source[start:start + 500]
        assert "except Exception" in window
        assert "= []" in window


def test_the_query_groups_the_artists_tracks(indexed):
    """The service the route calls returns grouped, positioned rows."""
    result = playlists_for_artist("Playlist Artist")

    assert [p["name"] for p in result] == ["Grunge - Top Tracks"]
    entry = result[0]
    assert [t["title"] for t in entry["tracks"]] == ["Come As You Are", "Lithium"]
    assert [t["position"] for t in entry["tracks"]] == [1, 2]
    assert entry["song_count"] == 40, (
        "the playlist total must survive alongside the artist's own count"
    )


# ---------------------------------------------------------------------------
# It renders
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("templates_root,template_name", TREES)
def test_it_renders_the_playlist_and_its_tracks(templates_root, template_name, indexed):
    html = _render(templates_root, template_name, _playlist_payload("Playlist Artist"))

    assert "Included in Playlists" in html
    assert "Grunge - Top Tracks" in html
    assert "Come As You Are" in html
    assert "Lithium" in html


@pytest.mark.parametrize("templates_root,template_name", TREES)
def test_both_counts_are_rendered(templates_root, template_name, indexed):
    """`2 by this artist` and `40 in playlist` are different numbers.

    Showing only one would either hide that the artist is two tracks in a
    forty-track playlist, or imply the playlist holds nothing else.
    """
    html = _render(templates_root, template_name, _playlist_payload("Playlist Artist"))
    assert "by this artist" in html
    assert "in playlist" in html


@pytest.mark.parametrize("templates_root,template_name", TREES)
def test_it_renders_the_empty_state_when_there_are_no_playlists(templates_root, template_name):
    html = _render(templates_root, template_name, _playlist_payload("Nobody At All"))
    assert EMPTY_TEXT in html


@pytest.mark.parametrize("templates_root,template_name", TREES)
def test_every_playlist_gets_its_own_collapse_target(templates_root, template_name, indexed):
    """Two playlists must not share a collapse id.

    Bootstrap wires `data-bs-target` to an id, so a duplicated id makes every
    header open the SAME panel — one playlist's tracks would be unreachable and
    another's would appear under the wrong name.
    """
    with db_session() as session:
        session.execute(
            text(
                "INSERT INTO tracks (id, artist, album_artist, album, title, stars) "
                "VALUES ('ap3', 'Playlist Artist', 'Playlist Artist', 'Al', 'Third', 4)"
            )
        )
    repo.replace_playlist(
        playlist_id="pl-second", name="Rock - Top Tracks", source="navidrome",
        playlist_type="regular", owner=None, is_public=False, song_count=5,
        comment="", changed="c2", track_ids=["ap3"],
    )

    html = _render(templates_root, template_name, _playlist_payload("Playlist Artist"))

    assert html.count('id="artistPlaylist1"') == 1
    assert html.count('id="artistPlaylist2"') == 1
    assert 'data-bs-target="#artistPlaylist1"' in html
    assert 'data-bs-target="#artistPlaylist2"' in html
