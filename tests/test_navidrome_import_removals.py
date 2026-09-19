"""Tests for Navidrome import stale-track removal (diff mode).

Regression: removing songs (or whole albums) from the Navidrome library never
deleted the corresponding rows from the Popularr database during a change
scan:

1. ``should_skip_cached_album`` skipped any album whose DB row count was
   >= the Navidrome count — exactly the removal case — so the per-album
   stale-track cleanup never ran for albums with removed songs.
2. Albums removed entirely from Navidrome never entered the album loop
   (and diff mode skips the artist-level stale cleanup), so their tracks
   stayed in the DB forever.
"""

from __future__ import annotations

from db.engine import db_session
from services.scanning.filters import should_skip_cached_album
from services.scanning.navidrome_import import compute_artist_album_diff, scan_artist_to_db
from sqlalchemy import text


# ---------------------------------------------------------------------------
# should_skip_cached_album
# ---------------------------------------------------------------------------


def _skip(tracks, cached, **overrides):
    kwargs = dict(
        artist_name="A",
        album_name="X",
        tracks=tracks,
        cached_ids_for_album=cached,
        force=False,
        album_needs_reimport=False,
        verbose=False,
    )
    kwargs.update(overrides)
    return should_skip_cached_album(**kwargs)


def test_skip_cached_album_removals_not_skipped():
    """DB holds MORE ids than Navidrome (songs removed) → must process."""
    assert _skip(tracks=[{"id": "n1"}, {"id": "n2"}], cached={"d1", "d2", "d3"}) is False


def test_skip_cached_album_equal_ids_skipped():
    """Unchanged album: same count, same ids → skip."""
    assert _skip(tracks=[{"id": "n1"}, {"id": "n2"}], cached={"n1", "n2"}) is True


def test_skip_cached_album_equal_count_different_ids_processed():
    """Same count but different ids (tracks replaced) → process + cleanup."""
    assert _skip(tracks=[{"id": "n1"}, {"id": "n2"}], cached={"d1", "d2"}) is False


def test_skip_cached_album_additions_not_skipped():
    """Navidrome has MORE ids than the DB (new songs) → process."""
    assert _skip(tracks=[{"id": "n1"}, {"id": "n2"}], cached={"n1"}) is False


def test_skip_cached_album_empty_nav_tracks_processed():
    """Album emptied in Navidrome → process so the cleanup can delete rows."""
    assert _skip(tracks=[], cached={"d1"}) is False


# ---------------------------------------------------------------------------
# compute_artist_album_diff — removed-album detection
# ---------------------------------------------------------------------------


class _FakeDiffClient:
    def __init__(self, albums):
        self.albums = albums

    def fetch_artist_albums(self, artist_id):
        return self.albums


def test_artist_diff_reports_removed_albums():
    with db_session() as session:
        session.execute(
            text(
                "INSERT INTO tracks (id, artist, album, album_artist) "
                "VALUES (:id, :artist, :album, :album_artist)"
            ),
            [
                {"id": "g1", "artist": "X", "album": "Gone Album", "album_artist": "X"},
                {"id": "g2", "artist": "X", "album": "Gone Album", "album_artist": "X"},
                {"id": "k1", "artist": "X", "album": "Keep Album", "album_artist": "X"},
            ],
        )

    client = _FakeDiffClient([{"id": "al-keep", "name": "Keep Album", "songCount": 1}])

    skip, changed, removed = compute_artist_album_diff("X", client.albums)

    assert skip is False
    assert "Gone Album" in changed
    assert removed == {"Gone Album"}


# ---------------------------------------------------------------------------
# scan_artist_to_db (diff mode) end-to-end removals
# ---------------------------------------------------------------------------


class _FakeImportClient:
    def __init__(self, albums, album_tracks):
        self.albums = albums
        self.album_tracks = album_tracks

    def fetch_artist_albums(self, artist_id):
        return self.albums

    def fetch_album_tracks(self, album_id):
        tracks = self.album_tracks.get(album_id, [])
        return {"tracks": tracks, "artist": "", "artistId": "", "name": "", "id": album_id}

    def get_song(self, song_id):
        return {}


def _album_track(track_id, title, artist):
    return {"id": track_id, "title": title, "artist": artist, "path": f"{artist}/{title}.mp3"}


def _seed_artist(artist):
    with db_session() as session:
        session.execute(
            text(
                "INSERT INTO tracks (id, artist, album, album_artist) "
                "VALUES (:id, :artist, :album, :album_artist)"
            ),
            [
                {"id": "g1", "artist": artist, "album": "Gone Album", "album_artist": artist},
                {"id": "g2", "artist": artist, "album": "Gone Album", "album_artist": artist},
                {"id": "k1", "artist": artist, "album": "Keep Album", "album_artist": artist},
                {"id": "k2", "artist": artist, "album": "Keep Album", "album_artist": artist},
            ],
        )


def _db_track_ids():
    with db_session() as session:
        rows = session.execute(text("SELECT id FROM tracks ORDER BY id")).fetchall()
        return {str(r[0]) for r in rows}


def test_scan_artist_to_db_diff_mode_removes_removed_album_tracks():
    """A whole album removed from Navidrome disappears from the DB."""
    artist = "Removal Artist"
    _seed_artist(artist)

    client = _FakeImportClient(
        albums=[{"id": "al-keep", "name": "Keep Album", "songCount": 2}],
        album_tracks={
            "al-keep": [
                _album_track("k1", "K One", artist),
                _album_track("k2", "K Two", artist),
            ],
        },
    )

    result = scan_artist_to_db(artist, "ar-1", diff_mode=True, client=client)

    assert isinstance(result, dict)
    assert result.get("changed") is True
    assert _db_track_ids() == {"k1", "k2"}  # Gone Album's rows deleted


def test_scan_artist_to_db_diff_mode_removes_song_from_existing_album():
    """A song removed from a still-existing album is deleted from the DB."""
    artist = "Trim Artist"
    _seed_artist(artist)

    client = _FakeImportClient(
        albums=[
            {"id": "al-keep", "name": "Keep Album", "songCount": 1},  # k2 removed
            {"id": "al-gone", "name": "Gone Album", "songCount": 2},  # unchanged
        ],
        album_tracks={
            "al-keep": [_album_track("k1", "K One", artist)],  # k2 no longer in Navidrome
            "al-gone": [
                _album_track("g1", "G One", artist),
                _album_track("g2", "G Two", artist),
            ],
        },
    )

    result = scan_artist_to_db(artist, "ar-2", diff_mode=True, client=client)

    assert isinstance(result, dict)
    assert result.get("changed") is True
    assert _db_track_ids() == {"k1", "g1", "g2"}  # k2 deleted, everything else survives


def test_scan_artist_to_db_diff_mode_keeps_duplicate_album_tracks():
    """Same album name listed twice (two MBIDs) — no sibling-track deletion.

    The DB caches tracks by album NAME only, so two Navidrome entries with
    the same name share one cached id set.  The per-album stale cleanup must
    NOT run for duplicated names — ``cached - entry_tracks`` would otherwise
    delete the sibling duplicate's rows (each entry sees the other's tracks
    as "stale").
    """
    artist = "Dup Artist"
    with db_session() as session:
        session.execute(
            text(
                "INSERT INTO tracks (id, artist, album, album_artist) "
                "VALUES (:id, :artist, :album, :album_artist)"
            ),
            [
                {"id": "k1", "artist": artist, "album": "Keep Album", "album_artist": artist},
                {"id": "k2", "artist": artist, "album": "Keep Album", "album_artist": artist},
                {"id": "k3", "artist": artist, "album": "Keep Album", "album_artist": artist},
                {"id": "k4", "artist": artist, "album": "Keep Album", "album_artist": artist},
            ],
        )

    client = _FakeImportClient(
        albums=[
            {"id": "al-dup-x", "name": "Keep Album", "songCount": 2},
            {"id": "al-dup-y", "name": "Keep Album", "songCount": 2},
        ],
        album_tracks={
            "al-dup-x": [
                _album_track("k1", "K One", artist),
                _album_track("k2", "K Two", artist),
            ],
            "al-dup-y": [
                _album_track("k3", "K Three", artist),
                _album_track("k4", "K Four", artist),
            ],
        },
    )

    result = scan_artist_to_db(artist, "ar-3", diff_mode=True, client=client)

    assert isinstance(result, dict)
    assert result.get("changed") is True
    # Both duplicates' tracks survive — no sibling deletion.
    assert _db_track_ids() == {"k1", "k2", "k3", "k4"}
