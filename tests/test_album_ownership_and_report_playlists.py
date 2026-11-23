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


class TestAlbumArtistResolution:
    """VA must survive a re-import, and must not be invented.

    ⚠️ An earlier version of this file asserted a per-album-ID key. That was
    WRONG: the prefetch is already scoped by the artist being imported, so it
    cannot observe another artist's album, and the ID key broke the one thing the
    map is FOR — preserving a stored "Various Artists" on re-import.
    """

    def _resolve(self, *, artist, existing=None, from_state="", **kw):
        from services.scanning.navidrome_import import album_artist_from_sources

        return album_artist_from_sources(
            api_album_artist=kw.get("api", artist),
            album_artist_from_artist_view=kw.get("view", ""),
            canonical_artist_name=artist,
            album_name="Essential Alternative",
            existing_album_artists=existing or {},
            album_artist_from_state=from_state,
        )

    @pytest.mark.parametrize(
        "stored", ["Various Artists", "various artists", "Original Soundtrack"]
    )
    def test_a_stored_va_is_preserved(self, stored):
        assert self._resolve(
            artist="Evanescence", existing={"Evanescence": stored}
        ) == "Various Artists", "the whole point of the stored value"

    def test_an_ordinary_artist_is_not_forced_to_va(self):
        assert self._resolve(artist="Evanescence") == "Evanescence"

    def test_another_artists_row_cannot_claim_this_one(self):
        """The map is artist-scoped, so a different key must not be consulted."""
        assert self._resolve(
            artist="Evanescence", existing={"Gorillaz": "Various Artists"}
        ) == "Evanescence"

    @pytest.mark.parametrize("kw", [
        {"api": "Various Artists"},
        {"view": "Various Artists"},
    ])
    def test_a_va_source_still_resolves_to_various_artists(self, kw):
        assert self._resolve(artist="Evanescence", **kw) == "Various Artists"

    def test_an_explicit_state_value_wins(self):
        assert self._resolve(
            artist="Evanescence",
            existing={"Evanescence": "Evanescence"},
            from_state="Various Artists",
        ) == "Various Artists"


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


class TestABadTitleRegexIsNotSilent:
    """The reported config had an unclosed group and the failure was swallowed."""

    @staticmethod
    def _exclude(title: str, pattern: str) -> bool:
        from services.catalog.album_classification_service import (
            should_exclude_track_from_stats,
        )

        return should_exclude_track_from_stats(
            title=title, exclude_title_regex=pattern
        )

    def test_an_invalid_pattern_does_not_raise(self):
        bad = r"(?i)(^intro$|\bintro$|interlude|\[intro\]|\(intro\|[untitled]))"
        assert isinstance(self._exclude("Intro", bad), bool)

    def test_the_warning_fires_once(self, monkeypatch):
        import services.catalog.album_classification_service as acs

        calls = []
        monkeypatch.setattr(acs.logger, "warning",
                            lambda *a, **k: calls.append(k.get("pattern")))
        bad = "(unclosed"
        acs._WARNED_BAD_REGEX.discard(bad)
        for _ in range(50):
            acs.should_exclude_track_from_stats(
                title="Intro", exclude_title_regex=bad
            )
        assert calls == [bad], (
            "a bad regex is tested once per title; warning per track would be "
            "hundreds of thousands of lines"
        )

    def test_a_valid_pattern_still_excludes(self):
        good = r"(?i)(^intro$|\bintro$|interlude|\(intro\))"
        assert self._exclude("Intro", good) is True
        assert self._exclude("Golden Ratio (Interlude)", good) is True
        assert self._exclude("A Real Song", good) is False
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
