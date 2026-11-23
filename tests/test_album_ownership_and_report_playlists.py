"""Album ownership must be per-ALBUM, not per-album-NAME, and the export must show it.

Reported twice in one message:

1. "Can you fix the playlists so they are shown on the report?"
2. "I also found many albums somehow got merged into the Various Artists albums
   such as Spotify - Essential Alternative."
"""

from __future__ import annotations

import csv
import io
import pathlib

import pytest
from sqlalchemy import text

ROOT = pathlib.Path(__file__).resolve().parent.parent


class TestAlbumOwnershipIsScopedToTheAlbum:
    """The cross-artist poisoning: one VA row claimed every same-titled album."""

    def _resolve(self, *, artist, album_id, existing):
        from services.scanning.navidrome_import import album_artist_from_sources

        return album_artist_from_sources(
            api_album_artist=artist,
            album_artist_from_artist_view=artist,
            canonical_artist_name=artist,
            album_name="Essential Alternative",
            existing_album_artists=existing,
            album_id=album_id,
        )

    def test_another_albums_va_row_does_not_claim_this_one(self):
        poisoned = {"id:nav-111": "Various Artists"}
        assert self._resolve(
            artist="Weezer", album_id="nav-222", existing=poisoned
        ) == "Weezer", (
            "a VA row for a DIFFERENT album that happens to share a title must "
            "not decide this album's owner — that is the reported merge"
        )

    def test_the_albums_own_va_identity_is_still_honoured(self):
        assert self._resolve(
            artist="HIM", album_id="nav-111",
            existing={"id:nav-111": "Various Artists"},
        ) == "Various Artists"

    def test_a_va_source_still_resolves_to_various_artists(self):
        assert self._resolve(
            artist="Various Artists", album_id="nav-333", existing={}
        ) == "Various Artists"

    def test_keying(self):
        from services.scanning.navidrome_import import _album_scope_key

        assert _album_scope_key("nav-1", "X") == "id:nav-1"
        assert _album_scope_key("", "Some Album") == "name:Some Album"
        # Two different albums with one title must not share a key.
        assert _album_scope_key("nav-1", "X") != _album_scope_key("nav-2", "X")


class TestTheReadAndWriteVocabulariesAgree:
    """A value the reader calls VA must be a value the writer accepts."""

    @pytest.mark.parametrize(
        "value",
        ["Various Artists", "various artists", "Original Soundtrack",
         "Soundtrack", "Compilation"],
    )
    def test_consistent(self, value):
        import services.scanning.navidrome_import as ni

        assert ni._has_various_artist_part(value) is True
        assert value.casefold() in ni.VA_ALBUM_ARTIST_VARIANTS

    def test_virgin_va_slash_is_write_only(self):
        """``V/A`` is an accepted album-artist VALUE but is not a component the
        multi-artist reader recognises — ``_has_various_artist_part`` splits on
        ``/``, so "V/A" becomes "V" + "A". Pre-existing and left alone: widening
        the reader would change which existing rows count as VA."""
        import services.scanning.navidrome_import as ni

        assert "v/a" in ni.VA_ALBUM_ARTIST_VARIANTS
        assert ni._has_various_artist_part("V/A") is False

    def test_an_ordinary_artist_is_not_va(self):
        import services.scanning.navidrome_import as ni

        for value in ("Evanescence", "Gorillaz", "Spotify", "The Killers"):
            assert ni._has_various_artist_part(value) is False
            assert value.casefold() not in ni.VA_ALBUM_ARTIST_VARIANTS


class TestTheExportShowsAlbumOwnership:
    """The reported symptom was undiagnosable from the file."""

    def test_album_artist_is_a_column(self):
        from services.popularity.popularity_report_service import (
            POPULARITY_REPORT_COLUMNS,
            _POPULARITY_REPORT_SELECT,
        )

        assert "album_artist" in POPULARITY_REPORT_COLUMNS
        assert "album_artist" in _POPULARITY_REPORT_SELECT
        # It belongs next to the per-track artist it is easily confused with.
        cols = list(POPULARITY_REPORT_COLUMNS)
        assert cols.index("album_artist") == cols.index("artist") + 1

    def test_a_compilation_and_a_merged_album_are_distinguishable(self, db_session):
        db_session.execute(text(
            "CREATE TABLE IF NOT EXISTS tracks (id TEXT PRIMARY KEY, title TEXT, "
            "artist TEXT, album_artist TEXT, album TEXT, disc_number INTEGER, "
            "track_number TEXT, year TEXT, listenbrainz_genres TEXT, "
            "musicbrainz_genres TEXT, lastfm_tags TEXT, discogs_genres TEXT, "
            "genres TEXT, raw_score REAL, final_score REAL, popularity REAL, "
            "stars INTEGER, duration REAL, lastfm_listeners INTEGER, "
            "lastfm_playcount INTEGER, lastfm_playcount_available INTEGER, "
            "lastfm_score REAL, listenbrainz_listens INTEGER, "
            "listenbrainz_users INTEGER, listenbrainz_score REAL, "
            "exclude_from_stats BOOLEAN, is_single BOOLEAN, single_confidence TEXT, "
            "single_confidence_score REAL, is_live INTEGER, popularity_frozen BOOLEAN, "
            "lastfm_last_updated TEXT, listenbrainz_last_updated TEXT)"))
        db_session.execute(text(
            "INSERT INTO tracks (id,title,artist,album_artist,album,final_score,stars) "
            "VALUES ('va','Clint Eastwood','Gorillaz','Various Artists',"
            "'Spotify - Essential Alternative',90.0,5),"
            "('own','Fallen','Evanescence','Evanescence','Fallen',80.0,5)"))
        db_session.commit()

        from services.popularity.popularity_report_service import (
            build_popularity_report,
        )

        rows = {r["id"] if "id" in r else r["title"]: r
                for r in csv.DictReader(io.StringIO(build_popularity_report()))}
        assert rows["Clint Eastwood"]["album_artist"] == "Various Artists"
        assert rows["Fallen"]["album_artist"] == "Evanescence"


class TestThePlaylistColumnIsPopulated:
    """The export now syncs the index itself instead of waiting for a full scan."""

    def test_the_column_renders_from_the_index(self, db_session):
        db_session.execute(text(
            "CREATE TABLE IF NOT EXISTS tracks (id TEXT PRIMARY KEY, title TEXT, "
            "artist TEXT, album_artist TEXT, album TEXT, disc_number INTEGER, "
            "track_number TEXT, year TEXT, listenbrainz_genres TEXT, "
            "musicbrainz_genres TEXT, lastfm_tags TEXT, discogs_genres TEXT, "
            "genres TEXT, raw_score REAL, final_score REAL, popularity REAL, "
            "stars INTEGER, duration REAL, lastfm_listeners INTEGER, "
            "lastfm_playcount INTEGER, lastfm_playcount_available INTEGER, "
            "lastfm_score REAL, listenbrainz_listens INTEGER, "
            "listenbrainz_users INTEGER, listenbrainz_score REAL, "
            "exclude_from_stats BOOLEAN, is_single BOOLEAN, single_confidence TEXT, "
            "single_confidence_score REAL, is_live INTEGER, popularity_frozen BOOLEAN, "
            "lastfm_last_updated TEXT, listenbrainz_last_updated TEXT)"))
        db_session.execute(text(
            "CREATE TABLE IF NOT EXISTS playlists (playlist_id TEXT PRIMARY KEY, "
            "name TEXT, changed TEXT, managed_type TEXT, source TEXT)"))
        db_session.execute(text(
            "CREATE TABLE IF NOT EXISTS playlist_entries (playlist_id TEXT, "
            "position INTEGER, track_id TEXT)"))
        db_session.execute(text(
            "INSERT INTO tracks (id,title,artist,album_artist,album,final_score,stars) "
            "VALUES ('t1','Song A','Artist','Artist','Album',80.0,5),"
            "       ('t2','Song B','Other','Other','Album2',40.0,3)"))
        db_session.execute(text(
            "INSERT INTO playlists (playlist_id,name,source) "
            "VALUES ('p1','Rock - Top Tracks','navidrome')"))
        db_session.execute(text(
            "INSERT INTO playlist_entries (playlist_id,position,track_id) "
            "VALUES ('p1',1,'t1')"))
        db_session.commit()

        from services.popularity.popularity_report_service import (
            build_popularity_report,
        )

        rows = {r["title"]: r for r in csv.DictReader(
            io.StringIO(build_popularity_report()))}
        assert rows["Song A"]["playlists_included_in"] == "Rock - Top Tracks"
        assert rows["Song B"]["playlists_included_in"] == ""

    def test_every_export_scope_syncs_the_index(self):
        """One definition in the service; all three routes call it."""
        service = (ROOT / "services" / "popularity" /
                   "popularity_report_service.py").read_text(encoding="utf-8")
        assert "def ensure_playlist_index(" in service

        for rel in ("routes/misc_routes.py", "routes/artist_routes.py",
                    "routes/album_routes.py"):
            src = (ROOT / rel).read_text(encoding="utf-8")
            assert "ensure_playlist_index" in src, (
                f"{rel} would export a blank playlists column"
            )
