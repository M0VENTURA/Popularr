"""Clearing an album's release must release the missing tracks it produced.

Reported: "I had an album that was previously linked to an MBID so it filled in
the missing releases, I removed the MBID and selected 'Do not search album
release', but the missing releases are still showing."

Why the badge could never clear on its own
------------------------------------------
Two halves are written by different paths:

* the missing badges read ``missing_album_tracks``;
* the row that DELETES a stale badge is the scan's per-album missing-track
  refresh (``get_missing_tracks`` -> ``_persist_missing_tracks``).

That refresh is SKIPPED for a "do not search album release" album — correctly,
because it would resolve the wrong release and delete a CSV tracklist. So the
moment the flag went on, nothing could remove a row a previous MBID-linked scan
had written. The list was frozen exactly as reported.

Discriminator: a release-derived row carries ``release_id``; a CSV-imported row
is written with ``release_id = NULL`` (``import_csv_tracklist``), and a row the
user rejected has ``ignored = TRUE``. Both of those must survive.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

ARTIST = "Various Artists"
ALBUM = "Christmas Rock Santa's Secret"
RELEASE = "3c8d8b38-ac3b-480c-87f8-5048a6198ff5"

_TRACKS_DDL = """
    CREATE TABLE IF NOT EXISTS tracks (
        id TEXT PRIMARY KEY, title TEXT, artist TEXT, album TEXT,
        album_artist TEXT, musicbrainz_album_mbid TEXT,
        musicbrainz_albumid TEXT, album_lookup_skip TEXT
    )
"""

_BADGES_DDL = """
    CREATE TABLE IF NOT EXISTS missing_album_tracks (
        id INTEGER PRIMARY KEY, artist_name TEXT, album_name TEXT, title TEXT,
        track_number TEXT, disc_number TEXT, track_artist TEXT, year TEXT,
        release_id TEXT, recording_mbid TEXT, duration REAL, ignored BOOLEAN,
        is_bonus BOOLEAN
    )
"""


@pytest.fixture()
def album_with_badges(db_session):
    db_session.execute(text(_TRACKS_DDL))
    db_session.execute(text(_BADGES_DDL))
    db_session.execute(
        text(
            "INSERT INTO tracks (id, title, artist, album, album_artist, "
            "musicbrainz_album_mbid) VALUES ('t1', 'Track', :ar, :al, :ar, :re)"
        ),
        {"ar": ARTIST, "al": ALBUM, "re": RELEASE},
    )
    db_session.execute(
        text(
            "INSERT INTO missing_album_tracks (artist_name, album_name, title, "
            "track_number, disc_number, release_id, ignored) VALUES "
            "(:ar, :al, 'MB Missing 1', '3', '1', :re, FALSE), "
            "(:ar, :al, 'MB Missing 2', '6', '1', :re, FALSE), "
            "(:ar, :al, 'CSV Row', '7', '1', NULL, FALSE), "
            "(:ar, :al, 'User Rejected', '8', '1', :re, TRUE)"
        ),
        {"ar": ARTIST, "al": ALBUM, "re": RELEASE},
    )
    db_session.commit()
    yield
    db_session.execute(text("DELETE FROM missing_album_tracks"))
    db_session.execute(text("DELETE FROM tracks"))
    db_session.commit()


def _badges(db_session) -> set[str]:
    return {
        str(r[0]) for r in db_session.execute(
            text(
                "SELECT title FROM missing_album_tracks "
                "WHERE LOWER(artist_name) = LOWER(:ar) AND LOWER(album_name) = LOWER(:al)"
            ),
            {"ar": ARTIST, "al": ALBUM},
        ).fetchall()
    }


class TestClearingTheAlbumMbid:
    def test_the_release_derived_rows_go(self, album_with_badges, db_session):
        from services.metadata.album_lookup_skip_service import (
            release_missing_tracks_for_cleared_mbid,
        )

        removed = release_missing_tracks_for_cleared_mbid(ARTIST, ALBUM)
        assert removed == 2
        assert _badges(db_session) == {"CSV Row", "User Rejected"}

    def test_a_csv_tracklist_is_preserved(self, album_with_badges, db_session):
        from services.metadata.album_lookup_skip_service import (
            release_missing_tracks_for_cleared_mbid,
        )

        release_missing_tracks_for_cleared_mbid(ARTIST, ALBUM)
        assert "CSV Row" in _badges(db_session), (
            "a user's own tracklist must survive — it has no release_id"
        )

    def test_a_rejected_track_is_preserved(self, album_with_badges, db_session):
        from services.metadata.album_lookup_skip_service import (
            release_missing_tracks_for_cleared_mbid,
        )

        release_missing_tracks_for_cleared_mbid(ARTIST, ALBUM)
        assert "User Rejected" in _badges(db_session), (
            "ignored = TRUE is never cleared, as everywhere else"
        )


class TestTurningTheSkipFlagBackOff:
    def test_it_releases_the_same_rows(self, album_with_badges, db_session):
        from services.metadata.album_lookup_skip_service import set_album_lookup_skip

        set_album_lookup_skip(ARTIST, ALBUM, True)
        assert len(_badges(db_session)) == 4, "turning it ON must not touch the list"

        set_album_lookup_skip(ARTIST, ALBUM, False)
        assert _badges(db_session) == {"CSV Row", "User Rejected"}

    def test_turning_it_on_alone_changes_nothing(self, album_with_badges, db_session):
        """CONTROL — the CSV-import path sets this flag deliberately and its rows
        must survive, so setting it must never delete."""
        from services.metadata.album_lookup_skip_service import set_album_lookup_skip

        set_album_lookup_skip(ARTIST, ALBUM, True)
        assert _badges(db_session) == {
            "MB Missing 1", "MB Missing 2", "CSV Row", "User Rejected",
        }


class TestTheRefreshStaysGated:
    def test_the_scan_skips_the_refresh_for_a_skipped_album(self):
        """The forward behaviour is correct and must remain: the refresh would
        resolve the wrong release and delete a CSV list."""
        import inspect

        from services.popularity import scan_stage_runner

        src = inspect.getsource(scan_stage_runner)
        assert "_skip_missing_refresh" in src
        assert "if not _skip_missing_refresh:" in src
