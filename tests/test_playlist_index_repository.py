"""Playlist index repository — the SQL, run against a real engine.

Why these are behavioural, not source assertions
------------------------------------------------
The two tables are declared in ``db/schema.py`` only (there is no ORM model),
and EVERY read in the repository is guarded by ``_tables_exist`` so that one
stray query can never dispose the shared in-memory engine. That guard makes a
MISSING table indistinguishable from an EMPTY one: the function returns [] or
{} and does not raise. A test asserting "no exception" would therefore pass with
the tables absent, the DDL wrong, or the JOIN broken.

So every test here SEEDS rows and asserts on values that only a working query
can produce.

⚠️ ``tracks`` rows persist across tests (the engine is shared and
``_recreate_test_schema`` only ever runs ``checkfirst`` creates), so each test
uses its OWN track ids rather than assuming an empty table.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import text

from db.engine import db_session
from db.repositories import playlists as repo

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_SOURCE = REPO_ROOT / "db" / "repositories" / "playlists.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_index():
    """Clear the index tables before each test.

    The in-memory engine is shared for the whole session and the schema is
    re-created with ``checkfirst=True``, so rows written by one test are still
    there in the next. Without this, "artist_playlist_tracks returns 2 rows"
    becomes order-dependent.
    """
    with db_session() as session:
        session.execute(text("DELETE FROM playlist_entries"))
        session.execute(text("DELETE FROM playlists"))
    yield


def _seed_tracks(rows: list[dict]) -> None:
    """Insert library rows for the JOIN to find."""
    with db_session() as session:
        for row in rows:
            session.execute(
                text(
                    "INSERT INTO tracks (id, artist, album, title, stars) "
                    "VALUES (:id, :artist, :album, :title, :stars) "
                    "ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title"
                ),
                row,
            )


def _add(
    playlist_id: str,
    *,
    name: str | None = None,
    track_ids: list[str] | None = None,
    source: str = "navidrome",
    playlist_type: str | None = "playlist",
    owner: str | None = "admin",
    is_public: bool | None = True,
    song_count: int | None = None,
    comment: str | None = None,
    changed: str | None = None,
) -> dict[str, int]:
    return repo.replace_playlist(
        playlist_id=playlist_id,
        name=name or playlist_id,
        source=source,
        playlist_type=playlist_type,
        owner=owner,
        is_public=is_public,
        song_count=song_count if song_count is not None else len(track_ids or []),
        comment=comment,
        changed=changed,
        track_ids=track_ids or [],
    )


# ---------------------------------------------------------------------------
# replace_playlist
# ---------------------------------------------------------------------------

class TestReplacePlaylist:
    def test_it_writes_the_header_and_the_entries(self):
        _seed_tracks([
            {"id": "t1", "artist": "A", "album": "Al", "title": "One", "stars": 5},
            {"id": "t2", "artist": "A", "album": "Al", "title": "Two", "stars": 4},
        ])
        result = _add("pl1", name="Road Trip", track_ids=["t1", "t2"], changed="111")

        assert result == {"entries": 2}
        rows = repo.artist_playlist_tracks(["t1", "t2"])
        assert {r["name"] for r in rows} == {"Road Trip"}
        assert len(rows) == 2

    def test_entries_are_positioned_from_one(self):
        _seed_tracks([
            {"id": "p1", "artist": "A", "album": "Al", "title": "One", "stars": 5},
            {"id": "p2", "artist": "A", "album": "Al", "title": "Two", "stars": 4},
            {"id": "p3", "artist": "A", "album": "Al", "title": "Three", "stars": 3},
        ])
        _add("pl2", track_ids=["p1", "p2", "p3"])

        rows = repo.artist_playlist_tracks(["p1", "p2", "p3"])
        # Ordered by position, and the first position is 1 — not 0, because the
        # position is what the artist page reports and a 0-based display would
        # read as "track 0".
        assert [r["position"] for r in rows] == [1, 2, 3]
        assert [r["track_id"] for r in rows] == ["p1", "p2", "p3"]

    def test_a_playlist_may_hold_the_same_track_twice(self):
        """The PK is (playlist_id, position) — NOT the track id.

        A playlist legitimately repeats a track (a reprise, or a user who added
        it twice). Keying on the track would collapse those to one row and
        silently report a shorter playlist than the source has.
        """
        _seed_tracks([
            {"id": "dup", "artist": "A", "album": "Al", "title": "Song", "stars": 5},
        ])
        result = _add("pl3", track_ids=["dup", "dup", "dup"])

        assert result == {"entries": 3}
        rows = repo.artist_playlist_tracks(["dup"])
        assert [r["position"] for r in rows] == [1, 2, 3]

    def test_replacing_a_playlist_replaces_its_entries(self):
        """A shorter playlist must not leave the old tail behind."""
        _seed_tracks([
            {"id": "r1", "artist": "A", "album": "Al", "title": "One", "stars": 5},
            {"id": "r2", "artist": "A", "album": "Al", "title": "Two", "stars": 4},
            {"id": "r3", "artist": "A", "album": "Al", "title": "Three", "stars": 3},
        ])
        _add("pl4", track_ids=["r1", "r2", "r3"])
        _add("pl4", track_ids=["r1"])

        assert repo.index_sizes()["entries"] == 1
        assert [r["track_id"] for r in repo.artist_playlist_tracks(["r1", "r2", "r3"])] == ["r1"]

    def test_replacing_updates_the_stamp_and_metadata(self):
        _add("pl5", name="Old", changed="100")
        _add("pl5", name="New", changed="200", song_count=9)

        stamps = repo.stored_changed_map()
        assert stamps == {"pl5": "200"}
        with db_session() as session:
            name = session.execute(
                text("SELECT name FROM playlists WHERE playlist_id = 'pl5'")
            ).scalar()
        assert name == "New"

    def test_blank_track_ids_are_skipped_not_stored(self):
        """An empty id would create a row that can never join to a track."""
        result = _add("pl6", track_ids=["ok", "", "   ", None])  # type: ignore[list-item]
        assert repo.index_sizes()["entries"] == 1


# ---------------------------------------------------------------------------
# stored_changed_map — the sync's fast path
# ---------------------------------------------------------------------------

class TestStoredChangedMap:
    def test_it_returns_the_stamp_per_playlist(self):
        _add("a", changed="aaa")
        _add("b", changed="bbb")
        assert repo.stored_changed_map() == {"a": "aaa", "b": "bbb"}

    def test_a_missing_stamp_reads_as_an_empty_string_not_none(self):
        """The caller compares values; None and '' must not be two answers."""
        _add("c", changed=None)
        assert repo.stored_changed_map() == {"c": ""}


# ---------------------------------------------------------------------------
# artist_playlist_tracks — the artist page's query
# ---------------------------------------------------------------------------

class TestArtistPlaylistTracks:
    def test_it_joins_the_library_row_for_display_fields(self):
        """Title/album/artist come from THIS library's row, not the index.

        The index stores only ids, so a track renamed since the last sync still
        displays correctly, and a track deleted from the library drops out of
        the section without waiting for a re-sync.
        """
        _seed_tracks([
            {"id": "j1", "artist": "Nirvana", "album": "Nevermind",
             "title": "Come As You Are", "stars": 5},
        ])
        _add("pj", name="Grunge", track_ids=["j1"])

        rows = repo.artist_playlist_tracks(["j1"])
        assert len(rows) == 1
        assert rows[0]["title"] == "Come As You Are"
        assert rows[0]["album"] == "Nevermind"
        assert rows[0]["artist"] == "Nirvana"

    def test_a_track_absent_from_the_library_is_dropped(self):
        """The INNER JOIN is the filter: an id with no library row is invisible."""
        _seed_tracks([
            {"id": "k1", "artist": "A", "album": "Al", "title": "Kept", "stars": 5},
        ])
        _add("pk", track_ids=["k1", "k-ghost"])

        rows = repo.artist_playlist_tracks(["k1", "k-ghost"])
        assert [r["track_id"] for r in rows] == ["k1"]

    def test_order_is_by_playlist_name_then_position(self):
        """The caller groups without re-sorting."""
        _seed_tracks([
            {"id": "o1", "artist": "A", "album": "Al", "title": "One", "stars": 5},
            {"id": "o2", "artist": "A", "album": "Al", "title": "Two", "stars": 4},
        ])
        _add("pz", name="Zebra", track_ids=["o1"])
        _add("pa", name="Alpha", track_ids=["o2", "o1"])

        rows = repo.artist_playlist_tracks(["o1", "o2"])
        assert [(r["name"], r["position"]) for r in rows] == [
            ("Alpha", 1),
            ("Alpha", 2),
            ("Zebra", 1),
        ]

    def test_name_ordering_is_case_insensitive(self):
        """`LOWER(name)` — otherwise "apple" files after "Zebra"."""
        _seed_tracks([
            {"id": "z1", "artist": "A", "album": "Al", "title": "One", "stars": 5},
            {"id": "z2", "artist": "A", "album": "Al", "title": "Two", "stars": 4},
        ])
        _add("pz2", name="Zebra", track_ids=["z1"])
        _add("pa2", name="apple", track_ids=["z2"])

        rows = repo.artist_playlist_tracks(["z1", "z2"])
        assert [r["name"] for r in rows] == ["apple", "Zebra"]

    def test_no_track_ids_is_empty_without_querying(self):
        """An empty IN list is invalid SQL on every dialect, so it short-circuits."""
        assert repo.artist_playlist_tracks([]) == []
        assert repo.artist_playlist_tracks([None, "", "  "]) == []  # type: ignore[list-item]

    def test_unknown_ids_return_no_rows(self):
        assert repo.artist_playlist_tracks(["nothing-indexed-here"]) == []


# ---------------------------------------------------------------------------
# delete_playlists_not_in — the sync's prune
# ---------------------------------------------------------------------------

class TestDeletePlaylistsNotIn:
    def test_it_keeps_the_listed_playlists_and_drops_the_rest(self):
        _add("keep1", changed="1")
        _add("keep2", changed="2")
        _add("gone1", changed="3")
        _add("gone2", changed="4")

        assert repo.delete_playlists_not_in(["keep1", "keep2"]) == 2
        assert set(repo.stored_changed_map()) == {"keep1", "keep2"}

    def test_it_removes_the_entries_of_deleted_playlists(self):
        """Otherwise a deleted playlist's rows keep surfacing on artist pages."""
        _seed_tracks([
            {"id": "d1", "artist": "A", "album": "Al", "title": "One", "stars": 5},
            {"id": "d2", "artist": "A", "album": "Al", "title": "Two", "stars": 4},
        ])
        _add("keep", track_ids=["d1"])
        _add("gone", track_ids=["d1", "d2"])

        repo.delete_playlists_not_in(["keep"])

        rows = repo.artist_playlist_tracks(["d1", "d2"])
        assert {r["playlist_id"] for r in rows} == {"keep"}
        assert repo.index_sizes()["entries"] == 1

    def test_nothing_kept_clears_everything(self):
        """The documented meaning of an empty keep-list.

        Exercised as its own case because `NOT IN ()` is not valid SQL — this
        path is handled explicitly rather than left to the driver's empty-list
        rendering.
        """
        _add("x", track_ids=["e1"])
        _add("y", track_ids=["e2"])

        assert repo.delete_playlists_not_in([]) == 2
        assert repo.index_sizes() == {"playlists": 0, "entries": 0}

    def test_blank_entries_in_the_keep_list_are_ignored(self):
        """A stray "" must not become a playlist that is "kept"."""
        _add("solo")
        assert repo.delete_playlists_not_in(["", "  ", None]) == 1  # type: ignore[list-item]
        assert repo.index_sizes()["playlists"] == 0


# ---------------------------------------------------------------------------
# index_sizes
# ---------------------------------------------------------------------------

class TestIndexSizes:
    def test_it_counts_both_tables(self):
        _seed_tracks([
            {"id": "s1", "artist": "A", "album": "Al", "title": "One", "stars": 5},
            {"id": "s2", "artist": "A", "album": "Al", "title": "Two", "stars": 4},
        ])
        _add("s1pl", track_ids=["s1", "s2"])
        _add("s2pl", track_ids=["s1"])

        assert repo.index_sizes() == {"playlists": 2, "entries": 3}


# ---------------------------------------------------------------------------
# Dialect safety
# ---------------------------------------------------------------------------

class TestDialectSafety:
    def test_in_lists_do_not_use_sqlite_only_json_functions(self):
        """`json_each` is SQLite's array deconstructor, not PostgreSQL's.

        Production is PostgreSQL-only; SQLite is the TEST dialect. PostgreSQL
        does have `json_each`, but it deconstructs an object and raises
        "cannot deconstruct an array as an object" for the array this module
        would pass — so an IN-list written with it passes every test here and
        fails in production. The house pattern is `bindparam(expanding=True)`.

        ⚠️ Asserted on the CALL FORM, not the bare name. The module docstring
        deliberately explains this trap by NAME, and a bare `"json_each" not in
        source` check would be satisfied by — or broken by — that prose rather
        than by the code. (`"FROM json_each"` and `"json_each("` cannot appear
        in the explanation.)
        """
        source = REPO_SOURCE.read_text(encoding="utf-8")
        code = re.sub(r"#.*", "", source)  # drop line comments
        assert "FROM json_each" not in code
        assert "json_each(" not in code

    def test_in_lists_use_an_expanding_bindparam(self):
        """The positive half: the idiom is actually present, not merely absent."""
        source = REPO_SOURCE.read_text(encoding="utf-8")
        assert source.count("expanding=True") >= 2
