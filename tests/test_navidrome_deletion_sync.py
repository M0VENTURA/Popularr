"""Regression tests: files deleted in Navidrome must disappear from the DB.

Three separate defects made deletions silently no-op during a normal
(change/diff) scan:

1. ``prefetch_artist_state`` and ``compute_artist_album_diff`` matched the
   artist with a CASE-SENSITIVE ``=`` while every other part of the app
   matches artist names case-insensitively.  ``clean_artist_name_for_storage``
   title-cases names, so a stored ``album_artist`` can differ in case from the
   name the scanner passes in.  When it does, the prefetch returns ZERO rows,
   ``existing_album_tracks`` is empty, and *every* stale-track cleanup becomes a
   no-op — deleted files stay in the database forever.

2. The per-album stale cleanup was skipped completely for album names that
   collide once edition markers are stripped (two entries named
   "Album" / "Album (Deluxe)").  The skip existed to protect duplicate
   *siblings* from deleting each other, but it also stranded genuinely
   deleted tracks.  The cleanup must run on the UNION of the colliding
   entries' track ids instead of being skipped.

3. ``delete_tracks_by_id`` swallowed every exception, returned 0, and
   reported ``len(track_ids)`` as the number deleted even when the DELETE
   matched no rows — so a failed removal was indistinguishable from a
   successful one.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from db.engine import db_session
from services.scanning.navidrome_import import (
    compute_artist_album_diff,
    prefetch_artist_state,
    scan_artist_to_db,
)


@pytest.fixture(autouse=True)
def _isolated_tracks():
    """Empty the shared in-memory ``tracks`` table around every test."""
    def _wipe() -> None:
        with db_session() as session:
            session.execute(text("DELETE FROM tracks"))

    _wipe()
    yield
    _wipe()


class _FakeClient:
    def __init__(self, albums, album_tracks):
        self.albums = albums
        self.album_tracks = album_tracks

    def fetch_artist_albums(self, artist_id):
        return self.albums

    def fetch_album_tracks(self, album_id):
        return {
            "tracks": self.album_tracks.get(album_id, []),
            "artist": "",
            "artistId": "",
            "name": "",
            "id": album_id,
        }

    def get_song(self, song_id):
        return {}


def _track(track_id, title, artist):
    return {
        "id": track_id,
        "title": title,
        "artist": artist,
        "path": f"{artist}/{title}.mp3",
    }


def _seed(rows):
    with db_session() as session:
        session.execute(
            text(
                "INSERT INTO tracks (id, artist, album, album_artist) "
                "VALUES (:id, :artist, :album, :album_artist)"
            ),
            rows,
        )


def _db_track_ids():
    with db_session() as session:
        rows = session.execute(text("SELECT id FROM tracks ORDER BY id")).fetchall()
        return {str(r[0]) for r in rows}


def _four_tracks(artist, album_artist):
    return [
        {"id": "g1", "artist": artist, "album": "Gone Album", "album_artist": album_artist},
        {"id": "g2", "artist": artist, "album": "Gone Album", "album_artist": album_artist},
        {"id": "k1", "artist": artist, "album": "Keep Album", "album_artist": album_artist},
        {"id": "k2", "artist": artist, "album": "Keep Album", "album_artist": album_artist},
    ]


# ---------------------------------------------------------------------------
# Defect 1 — artist matching must be case-insensitive
# ---------------------------------------------------------------------------


def test_prefetch_artist_state_matches_artist_case_insensitively():
    """Stored ``album_artist`` differing only in case must still be found."""
    _seed(_four_tracks("ACDC", "ACDC"))

    state = prefetch_artist_state(canonical_artist_name="Acdc")

    assert state["existing_track_ids"] == {"g1", "g2", "k1", "k2"}
    assert {k: len(v) for k, v in state["existing_album_tracks"].items()} == {
        "Gone Album": 2,
        "Keep Album": 2,
    }


def test_artist_diff_matches_artist_case_insensitively():
    """The removed album must be reported even when the case differs."""
    _seed(_four_tracks("ACDC", "ACDC"))

    skip, changed, removed = compute_artist_album_diff(
        "Acdc",
        [{"id": "al-keep", "name": "Keep Album", "songCount": 2}],
    )

    assert skip is False
    assert removed == {"Gone Album"}


def test_diff_scan_deletes_removed_album_when_artist_case_differs():
    """End-to-end: a deleted album is purged despite artist-case drift.

    This is the reported bug — the rows survived every scan because the
    prefetch could not see them, so no cleanup was ever attempted.
    """
    _seed(_four_tracks("ACDC", "ACDC"))
    client = _FakeClient(
        albums=[{"id": "al-keep", "name": "Keep Album", "songCount": 2}],
        album_tracks={
            "al-keep": [
                _track("k1", "K One", "Acdc"),
                _track("k2", "K Two", "Acdc"),
            ],
        },
    )

    scan_artist_to_db("Acdc", "ar-case", diff_mode=True, client=client)

    assert _db_track_ids() == {"k1", "k2"}


def test_full_scan_deletes_removed_song_when_artist_case_differs():
    """The full-scan (non-diff) path must also purge removed songs."""
    _seed(_four_tracks("ACDC", "ACDC"))
    client = _FakeClient(
        albums=[
            {"id": "al-keep", "name": "Keep Album", "songCount": 1},
            {"id": "al-gone", "name": "Gone Album", "songCount": 0},
        ],
        album_tracks={"al-keep": [_track("k1", "K One", "Acdc")], "al-gone": []},
    )

    scan_artist_to_db("Acdc", "ar-case-full", diff_mode=False, force=True, client=client)

    assert _db_track_ids() == {"k1"}


# ---------------------------------------------------------------------------
# Defect 2 — duplicate (colliding) album names must still be cleaned up
# ---------------------------------------------------------------------------


def _seed_colliding_albums(artist):
    _seed([
        {"id": "d1", "artist": artist, "album": "Album", "album_artist": artist},
        {"id": "d2", "artist": artist, "album": "Album", "album_artist": artist},
        {"id": "x1", "artist": artist, "album": "Album (Deluxe)", "album_artist": artist},
    ])


def test_diff_scan_removes_stale_track_when_album_names_collide():
    """A track deleted from one of two colliding albums is still purged."""
    artist = "Collide Artist"
    _seed_colliding_albums(artist)

    client = _FakeClient(
        albums=[
            {"id": "al-a", "name": "Album", "songCount": 1},
            {"id": "al-b", "name": "Album (Deluxe)", "songCount": 1},
        ],
        album_tracks={
            "al-a": [_track("d2", "Two", artist)],
            "al-b": [_track("x1", "Deluxe One", artist)],
        },
    )

    scan_artist_to_db(artist, "ar-collide", diff_mode=True, client=client)

    assert _db_track_ids() == {"d2", "x1"}


def test_diff_scan_keeps_sibling_tracks_when_album_names_collide():
    """The union fix must not delete a colliding sibling's tracks."""
    artist = "Sibling Artist"
    _seed([
        {"id": "k1", "artist": artist, "album": "Keep Album", "album_artist": artist},
        {"id": "k2", "artist": artist, "album": "Keep Album", "album_artist": artist},
        {"id": "k3", "artist": artist, "album": "Keep Album", "album_artist": artist},
        {"id": "k4", "artist": artist, "album": "Keep Album", "album_artist": artist},
    ])

    client = _FakeClient(
        albums=[
            {"id": "al-dup-x", "name": "Keep Album", "songCount": 2},
            {"id": "al-dup-y", "name": "Keep Album", "songCount": 2},
        ],
        album_tracks={
            "al-dup-x": [_track("k1", "One", artist), _track("k2", "Two", artist)],
            "al-dup-y": [_track("k3", "Three", artist), _track("k4", "Four", artist)],
        },
    )

    scan_artist_to_db(artist, "ar-sibling", diff_mode=True, client=client)

    assert _db_track_ids() == {"k1", "k2", "k3", "k4"}


def test_colliding_album_cleanup_skipped_when_an_entry_was_never_fetched():
    """Data-loss guard: an unfetched colliding entry must not lose its tracks.

    Two Navidrome entries share the name "Keep Album" but only one carries an
    id, so the other is never fetched and contributes no live ids.  Running the
    union cleanup anyway would treat the id-less entry's still-present tracks
    (k3/k4) as stale and delete them.
    """
    artist = "Unfetched Artist"
    _seed([
        {"id": "k1", "artist": artist, "album": "Keep Album", "album_artist": artist},
        {"id": "k2", "artist": artist, "album": "Keep Album", "album_artist": artist},
        {"id": "k3", "artist": artist, "album": "Keep Album", "album_artist": artist},
        {"id": "k4", "artist": artist, "album": "Keep Album", "album_artist": artist},
    ])

    client = _FakeClient(
        albums=[
            {"id": "al-dup-x", "name": "Keep Album", "songCount": 2},
            {"name": "Keep Album", "songCount": 2},  # no id -> never fetched
        ],
        album_tracks={
            "al-dup-x": [_track("k1", "One", artist), _track("k2", "Two", artist)],
        },
    )

    scan_artist_to_db(artist, "ar-unfetched", diff_mode=True, client=client)

    # The un-fetched sibling's tracks must survive.
    assert _db_track_ids() == {"k1", "k2", "k3", "k4"}


# ---------------------------------------------------------------------------
# Defect 3 — delete_tracks_by_id must report real failures
# ---------------------------------------------------------------------------


def test_delete_tracks_by_id_reports_rows_actually_deleted():
    """The return value must be the real row count, not ``len(track_ids)``."""
    from db.repositories.tracks import delete_tracks_by_id

    _seed([
        {"id": "p1", "artist": "A", "album": "Al", "album_artist": "A"},
    ])

    # One existing id + one that is not in the table.
    deleted = delete_tracks_by_id({"p1", "does-not-exist"}, context="probe")

    assert deleted == 1
    assert _db_track_ids() == set()


def test_delete_tracks_by_id_raises_instead_of_swallowing_errors():
    """A failing DELETE must surface, not silently report success."""
    from db.repositories.tracks import delete_tracks_by_id

    class _Boom:
        def execute(self, *_args, **_kwargs):
            raise RuntimeError("connection reset")

    with pytest.raises(RuntimeError):
        delete_tracks_by_id({"z1"}, context="probe", session=_Boom())
