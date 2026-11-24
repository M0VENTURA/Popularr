"""The folder-derived repair: right albums, and it never guesses.

Backs the album page's "Repair from paths" action. A merge used to rewrite the
ALBUM TAG of every file of every source album, so a foreign rip kept its PATH
while its tag claimed the other album — and the tag that identified its true
album was overwritten, leaving the path as the only evidence.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from services.metadata.album_path_repair_service import (
    _album_from_folder,
    _artist_from_path,
    repair_album_from_paths,
    scan_album_path_mismatches,
)

ARTIST = "Various Artists"
ALBUM = "Spotify - Essential Alternative"


class TestReadingTheFolder:
    @pytest.mark.parametrize(
        "path,album,year",
        [
            ("/music/Various Artists/2026 - Spotify - Essential Alternative/02. Gorillaz - Clint Eastwood.mp3",
             "Spotify - Essential Alternative", "2026"),
            ("/music/Jimmy Eat World/2002 - Good To Go (EP)/01. Jimmy Eat World - The Middle (Acoustic).mp3",
             "Good To Go (EP)", "2002"),
            ("/music/Coldplay/2002 - A Rush of Blood to the Head/2 - Coldplay - In My Place.mp3",
             "A Rush of Blood to the Head", "2002"),
            ("/music/Killswitch Engage/2004 - The End of Heartache (Special Edition)/04 - X.mp3",
             "The End of Heartache (Special Edition)", "2004"),
        ],
    )
    def test_the_reported_paths(self, path, album, year):
        assert _album_from_folder(path) == (album, year)

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("/music/Various Artists/2026 - X/01. A.mp3", "Various Artists"),
            ("/music/Jimmy Eat World/2002 - Y/01. A.mp3", "Jimmy Eat World"),
            # A file directly under the library root has no artist folder.
            ("/music/Weezer/01. A.mp3", ""),
            ("/music/01. A.mp3", ""),
        ],
    )
    def test_the_artist_folder(self, path, expected):
        assert _artist_from_path(path) == expected

    def test_a_root_folder_is_never_an_album(self):
        assert _album_from_folder("/music/song.mp3") == ("", "")


_TRACKS_DDL = """
    CREATE TABLE IF NOT EXISTS tracks (
        id TEXT PRIMARY KEY, title TEXT, artist TEXT, album TEXT,
        album_artist TEXT, year TEXT, file_path TEXT,
        disc_number TEXT, track_number TEXT,
        local_tags_written_at TEXT, metadata_dirty_for_navidrome BOOLEAN,
        metadata_source TEXT
    )
"""


@pytest.fixture()
def album_rows(db_session):
    db_session.execute(text(_TRACKS_DDL))
    db_session.execute(
        text(
            "INSERT INTO tracks (id,title,artist,album,album_artist,year,file_path,"
            "disc_number,track_number) VALUES "
            "('own','Clint Eastwood','Gorillaz',:alb,:art,'2026',"
            "'/music/Various Artists/2026 - Spotify - Essential Alternative/02. Gorillaz - Clint Eastwood.mp3','1','2'),"
            "('foreign','The Middle (Acoustic)','Jimmy Eat World',:alb,:art,'2002',"
            "'/music/Jimmy Eat World/2002 - Good To Go (EP)/01. Jimmy Eat World - The Middle (Acoustic).mp3','1','1')"
        ),
        {"alb": ALBUM, "art": ARTIST},
    )
    db_session.commit()
    yield
    db_session.execute(text("DELETE FROM tracks"))
    db_session.commit()


def _rows(db_session):
    return {
        str(r[0]): dict(r._mapping)
        for r in db_session.execute(
            text("SELECT id, album, album_artist, year FROM tracks")
        ).fetchall()
    }


class TestTheScan:
    def test_it_finds_only_the_foreign_row(self, album_rows, db_session):
        result = scan_album_path_mismatches(ARTIST, ALBUM)
        assert result["scanned"] == 2
        assert result["changed_count"] == 1
        change = result["changes"][0]
        assert change["title"] == "The Middle (Acoustic)"
        assert change["proposed"]["album"] == "Good To Go (EP)"
        assert change["proposed"]["album_artist"] == "Jimmy Eat World"

    def test_it_proposes_the_folder_year(self, album_rows):
        change = scan_album_path_mismatches(ARTIST, ALBUM)["changes"][0]
        assert change["current"]["year"] == "2002"

    def test_it_writes_nothing(self, album_rows, db_session):
        """CONTROL — a scan must be read-only."""
        scan_album_path_mismatches(ARTIST, ALBUM)
        assert _rows(db_session)["foreign"]["album"] == ALBUM


class TestTheRepair:
    def test_dry_run_is_the_default(self, album_rows, db_session):
        result = repair_album_from_paths(ARTIST, ALBUM)
        assert result["dry_run"] is True
        assert result["repaired"] == 0
        assert _rows(db_session)["foreign"]["album"] == ALBUM, (
            "the default must never write — reading a folder name is a heuristic"
        )

    def test_it_repairs_only_the_mismatched_row(self, album_rows, db_session):
        result = repair_album_from_paths(ARTIST, ALBUM, dry_run=False)
        assert result["repaired"] == 1
        rows = _rows(db_session)
        assert rows["foreign"]["album"] == "Good To Go (EP)"
        assert rows["foreign"]["album_artist"] == "Jimmy Eat World"
        assert rows["own"]["album"] == ALBUM, "the correct row must be untouched"

    def test_file_tags_are_not_written_by_default(self, album_rows, db_session):
        result = repair_album_from_paths(ARTIST, ALBUM, dry_run=False)
        assert result["files_written"] == 0

    def test_the_repair_is_stamped_as_a_local_correction(self, album_rows, db_session):
        """Otherwise the next import re-applies the tag this repair just undid."""
        repair_album_from_paths(ARTIST, ALBUM, dry_run=False)
        row = db_session.execute(
            text("SELECT metadata_dirty_for_navidrome FROM tracks WHERE id = 'foreign'")
        ).fetchone()
        assert bool(row[0]) is True
