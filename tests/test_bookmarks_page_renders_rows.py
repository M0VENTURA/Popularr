"""/bookmarks actually renders the rows it reads.

REPORTED (via the UI audit): the page is always empty.

The route was:

    return await render_template("pages/bookmarks.html")

— no context at all. The template reads ``bookmarks`` and ``filter_type``, so
Jinja's ``Undefined`` made the list branch unreachable: the empty state rendered
unconditionally and all four filter links rendered as "All" whatever ``?filter=``
said, while both nav entries point at ``/bookmarks?filter=artist``.

These tests drive the REAL route against a real table, so they fail if the
context is dropped again, if the filter stops applying, or if a track favourite
stops being enriched from ``tracks``.

The page must also cope with the ``bookmarks`` table having TWO column sets in
the wild — the current ``(bookmark_type, entity_id, name, artist_name,
album_name, title)`` and the older app's ``(type, artist, album, track_id,
url)``. ``CREATE TABLE IF NOT EXISTS`` means a long-lived database keeps
whichever shape it was born with, so the read is ``SELECT *`` and the normaliser
picks up both. That half is tested directly (``TestBothColumnSets``), because
the test schema here can only be one shape at a time.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text


@pytest.fixture
def bookmarks_table(app):
    """Create ``bookmarks`` from the ORM model, which the test schema omits.

    ``conftest`` only creates ``tracks``; everything else is expected to exist
    already. Creating from ``db.models`` rather than a hand-written DDL keeps
    this in step with the real column set.
    """
    from db.engine import get_engine
    from db.models import Bookmark

    engine = get_engine()
    Bookmark.__table__.create(engine, checkfirst=True)
    return Bookmark


async def _set_rows(db_session, rows: list[dict]) -> None:
    """Replace the table's contents with ``rows`` (shared, not per-test)."""
    db_session.execute(text("DELETE FROM bookmarks"))
    for index, row in enumerate(rows, start=1):
        columns = {"id": index, **row}
        names = ", ".join(columns)
        values = ", ".join(f":{name}" for name in columns)
        db_session.execute(
            text(f"INSERT INTO bookmarks ({names}) VALUES ({values})"), columns
        )
    db_session.commit()


# ---------------------------------------------------------------------------
# The context is passed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_page_renders_a_stored_artist_favourite(
    client, db_session, bookmarks_table
):
    """The regression: with no context this rendered only the empty state."""
    await _set_rows(db_session, [{"type": "artist_favourite", "name": "Northlane"}])

    resp = await client.get("/bookmarks")
    assert resp.status_code == 200
    body = await resp.get_data(as_text=True)

    assert "Northlane" in body, (
        "the stored favourite is not on the page — the route is passing no "
        "context again, so the template's list branch never runs"
    )
    assert "You don't have any bookmarks yet" not in body, (
        "the empty state rendered despite a real row"
    )


@pytest.mark.asyncio
async def test_the_filter_actually_filters(client, db_session, bookmarks_table):
    """All four buttons were dead links because ``filter_type`` was never set."""
    await _set_rows(
        db_session,
        [
            {"type": "artist_favourite", "name": "Northlane"},
            {"type": "track_favourite", "name": "track-abc"},
            {"type": "album_favourite", "album_name": "Obsidian"},
        ],
    )

    artists = await (await client.get("/bookmarks?filter=artist")).get_data(as_text=True)
    assert "Northlane" in artists
    assert "Obsidian" not in artists, "?filter=artist still rendered an album"

    albums = await (await client.get("/bookmarks?filter=album")).get_data(as_text=True)
    assert "Obsidian" in albums
    assert "Northlane" not in albums, "?filter=album still rendered an artist"


@pytest.mark.asyncio
async def test_an_unknown_filter_is_treated_as_no_filter(
    client, db_session, bookmarks_table
):
    """A junk ``?filter=`` must not blank the page."""
    await _set_rows(db_session, [{"type": "artist_favourite", "name": "Northlane"}])

    body = await (await client.get("/bookmarks?filter=nonsense")).get_data(as_text=True)
    assert "Northlane" in body


@pytest.mark.asyncio
async def test_an_empty_table_still_shows_the_empty_state(
    client, db_session, bookmarks_table
):
    """CONTROL: the empty state must survive — it is correct when there is nothing."""
    await _set_rows(db_session, [])

    body = await (await client.get("/bookmarks")).get_data(as_text=True)
    assert "You don't have any bookmarks yet" in body


@pytest.mark.asyncio
async def test_a_track_favourite_is_enriched_from_tracks(
    client, db_session, bookmarks_table
):
    """A ``track_favourite`` row stores only the id, so the page fills the title.

    Without the enrichment the Tracks table listed a bare UUID.
    """
    from db.engine import get_engine
    from db.models import Track

    Track.__table__.create(get_engine(), checkfirst=True)
    db_session.execute(text("DELETE FROM tracks"))
    db_session.execute(
        text(
            "INSERT INTO tracks (id, title, artist, album) VALUES "
            "(:id, :title, :artist, :album)"
        ),
        {
            "id": "track-xyz",
            "title": "Bloodline",
            "artist": "Northlane",
            "album": "Obsidian",
        },
    )
    await _set_rows(db_session, [{"type": "track_favourite", "name": "track-xyz"}])

    body = await (await client.get("/bookmarks?filter=track")).get_data(as_text=True)
    assert "Bloodline" in body, (
        "the track favourite was not enriched from the tracks table — the row "
        "would render as a raw id"
    )


# ---------------------------------------------------------------------------
# Both column sets (normaliser only — the table can only be one shape here)
# ---------------------------------------------------------------------------


class TestBothColumnSets:
    """``_normalise_bookmark_row`` must read both shapes.

    The current schema uses ``bookmark_type``/``artist_name``/``album_name``/
    ``entity_id``; databases created by the older app carry ``type``/``artist``/
    ``album``/``track_id``. A live database keeps whichever it was born with.
    """

    def _normalise(self, row: dict) -> dict:
        from routes.ui_routes import _normalise_bookmark_row

        return _normalise_bookmark_row(row)

    def test_the_current_column_names(self):
        row = self._normalise(
            {
                "id": 1,
                "bookmark_type": "album",
                "artist_name": "Northlane",
                "album_name": "Obsidian",
                "title": "Obsidian",
            }
        )
        assert row["type"] == "album"
        assert row["artist"] == "Northlane"
        assert row["album"] == "Obsidian"

    def test_the_legacy_column_names(self):
        row = self._normalise(
            {"id": 2, "type": "album", "artist": "Northlane", "album": "Obsidian"}
        )
        assert row["type"] == "album"
        assert row["artist"] == "Northlane"
        assert row["album"] == "Obsidian"

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("artist_favourite", "artist"),
            ("album_favourite", "album"),
            ("track_favourite", "track"),
        ],
    )
    def test_the_favourite_spellings_collapse(self, raw, expected):
        row = self._normalise({"id": 3, "type": raw, "name": "X"})
        assert row["type"] == expected, (
            f"{raw} did not collapse onto the kind the page groups"
        )

    def test_an_artist_favourite_takes_its_name_from_name(self):
        """Artist rows store the artist in ``name`` only."""
        row = self._normalise({"id": 4, "type": "artist_favourite", "name": "Northlane"})
        assert row["artist"] == "Northlane"

    def test_a_track_favourite_reads_the_id_from_name(self):
        """``track_favourite`` rows store the TRACK ID in ``name``.

        See routes/track_routes.py's writes — there is no ``track_id`` column on
        the current schema, so without this the row has no id to link to.
        """
        row = self._normalise({"id": 5, "type": "track_favourite", "name": "abc-123"})
        assert row["track_id"] == "abc-123"
