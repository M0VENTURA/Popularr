"""Per-album "do not search album release" flag + CSV tracklist import.

Feature (test_site): the Edit Album tab gains "Do not search album release"
and, when checked, an "Upload a CSV tracklist" control. For a custom album
that is not on MusicBrainz/Discogs:

* every track of the album is marked ``album_lookup_skip = '1'`` (album save
  and CSV upload both write it album-wide);
* the scan then skips the album-release lookups — the MusicBrainz album-type
  lookup, the Discogs release ID, online album art (CAA/AudioDB/Discogs), and
  the per-album missing-track refresh against a MusicBrainz release (which
  would otherwise DELETE the CSV-persisted rows);
* the uploaded CSV stands in for the release: tracks the library doesn't have
  are persisted to ``missing_album_tracks`` and flow through the album page's
  existing missing list + Download Missing Tracks.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.metadata import album_lookup_skip_service as alss  # noqa: E402
from services.metadata import album_missing_service as ams  # noqa: E402
from services.popularity.stages import album_stage  # noqa: E402


def _seed_track(session, track_id: str, *, title: str, artist: str = "Test Artist",
                album: str = "Custom Album", album_lookup_skip: str | None = None) -> None:
    session.execute(
        text(
            "INSERT INTO tracks (id, title, artist, album_artist, album, album_lookup_skip) "
            "VALUES (:id, :title, :artist, '', :album, :skip) "
            "ON CONFLICT DO NOTHING"
        ),
        {
            "id": track_id,
            "title": title,
            "artist": artist,
            "album": album,
            "skip": album_lookup_skip,
        },
    )


# ---------------------------------------------------------------------------
# 1. The helper service — album-wide read + fan-out write
# ---------------------------------------------------------------------------

class TestAlbumLookupSkipHelper:

    def test_unflagged_album_reads_false(self, db_session):
        _seed_track(db_session, "skip-helper-unflagged-1", title="One", album="Helper A")
        db_session.commit()
        assert alss.album_lookup_skip("Test Artist", "Helper A") is False

    def test_flagged_album_reads_true(self, db_session):
        _seed_track(db_session, "skip-helper-flagged-1", title="One", album="Helper B", album_lookup_skip="1")
        db_session.commit()
        assert alss.album_lookup_skip("Test Artist", "Helper B") is True

    def test_set_writes_every_track_fan_out(self, db_session):
        _seed_track(db_session, "skip-helper-fanout-1", title="One", album="Helper C")
        _seed_track(db_session, "skip-helper-fanout-2", title="Two", album="Helper C")
        db_session.commit()

        updated = alss.set_album_lookup_skip("Test Artist", "Helper C", True)
        assert updated == 2, "the flag must be written to EVERY track of the album"

        assert alss.album_lookup_skip("Test Artist", "Helper C") is True

        # Clearing fans out too (back to the search-allowed default).
        updated2 = alss.set_album_lookup_skip("Test Artist", "Helper C", False)
        assert updated2 == 2
        assert alss.album_lookup_skip("Test Artist", "Helper C") is False

    def test_other_albums_are_untouched(self, db_session):
        _seed_track(db_session, "skip-helper-other-1", title="One", album="Helper D")
        _seed_track(db_session, "skip-helper-other-2", title="Two", album="Helper E", album_lookup_skip="1")
        db_session.commit()

        assert alss.album_lookup_skip("Test Artist", "Helper E") is True
        updated = alss.set_album_lookup_skip("Test Artist", "Helper D", False)
        assert updated == 1
        assert alss.album_lookup_skip("Test Artist", "Helper E") is True


# ---------------------------------------------------------------------------
# 2. CSV parsing (pure)
# ---------------------------------------------------------------------------

class TestParseCsvTracklist:

    def test_header_form(self):
        csv_text = textwrap.dedent("""\
            Title,Artist,Duration
            Track One,Someone,3:45
            Two,Someone Else,4:00
        """)
        rows, errors = ams._parse_csv_tracklist(csv_text)
        assert errors == []
        assert len(rows) == 2
        assert rows[0]["title"] == "Track One"
        assert rows[0]["artist"] == "Someone"
        assert rows[1]["title"] == "Two"
        assert rows[0]["duration"] == 225, "3:45 parses to seconds"

    def test_positional_title_only(self):
        rows, errors = ams._parse_csv_tracklist("Track One\nTwo\nThree")
        assert rows[0]["title"] == "Track One"
        assert rows[1]["title"] == "Two"
        assert len(rows) == 3

    def test_positional_number_title(self):
        rows, _ = ams._parse_csv_tracklist("1,Intro\n2,Second Song\n")
        assert rows[0]["track_number"] == "1"
        assert rows[0]["title"] == "Intro"
        assert rows[1]["track_number"] == "2"
        assert rows[1]["title"] == "Second Song"

    def test_positional_disc_number_title(self):
        rows, _ = ams._parse_csv_tracklist("1,1,Opening\n1,2,Second\n2,1,Disc Two Open\n")
        assert rows[0]["disc_number"] == 1 and rows[0]["track_number"] == "1"
        assert rows[2]["disc_number"] == 2 and rows[2]["title"] == "Disc Two Open"

    def test_exportify_hash_prefix(self):
        rows, _ = ams._parse_csv_tracklist("#5 - Song Five\n#6 - Song Six\n")
        assert rows[0]["track_number"] == "5"
        assert rows[0]["title"] == "Song Five"

    def test_bare_empty_returns_no_rows(self):
        rows, errors = ams._parse_csv_tracklist("")
        assert rows == [] and errors == []


# ---------------------------------------------------------------------------
# 3. import_csv_tracklist — persists the missing set + sets the flag
# ---------------------------------------------------------------------------

class TestImportCsvTracklist:

    @pytest.fixture(autouse=True)
    def _missing_table(self):
        # The harness only pre-creates ``tracks``; nothing pre-creates
        # ``missing_album_tracks``. It may however exist as the ORM's BIGINT-
        # id shell (which does NOT autoincrement on SQLite, so the service's
        # raw INSERT without an id fails). No other suite writes to this
        # table (they patch ``_persist_missing_tracks`` out), so DROP and
        # recreate in the working INTEGER-PK form. ``download_queue`` se does
        # not need dropping: the CSV import only reads columns that exist in
        # both forms, and db_session DISPOSES the shared engine on a
        # transient error — so the table must exist to avoid a mid-test wipe.
        from db.engine import get_engine
        from sqlalchemy import text as _text
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(_text("DROP TABLE IF EXISTS missing_album_tracks"))
            conn.execute(_text(
                "CREATE TABLE missing_album_tracks ("
                " id INTEGER PRIMARY KEY, artist_name TEXT NOT NULL, "
                " album_name TEXT NOT NULL, title TEXT NOT NULL, "
                " track_number TEXT, track_artist TEXT, year TEXT, "
                " release_id TEXT, recording_mbid TEXT, duration INTEGER, "
                " disc_number INTEGER DEFAULT 1, ignored BOOLEAN DEFAULT 0, "
                " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
                " CONSTRAINT uq_missing_album_tracks "
                "  UNIQUE (artist_name, album_name, title, disc_number))"
            ))
            conn.execute(_text(
                "CREATE TABLE IF NOT EXISTS download_queue ("
                " id INTEGER PRIMARY KEY, artist TEXT, album_artist TEXT, "
                " title TEXT, album TEXT, track_number TEXT, disc_number TEXT, "
                " status TEXT DEFAULT 'queued')"
            ))
        yield

    def test_missing_rows_are_persisted_and_flag_set(self, db_session):
        _seed_track(db_session, "csv-import-present-1", title="Owned Song", album="CSV Album One")
        db_session.commit()

        result = ams.import_csv_tracklist(
            "Test Artist", "CSV Album One",
            "Owned Song\nMissing Song One\nMissing Song Two\n",
        )

        assert result["success"] is True
        assert result["missing_count"] == 2, "the two unowned tracks are missing"
        assert result["library_count"] == 1
        titles = {t["title"] for t in result["missing_tracks"]}
        assert titles == {"Missing Song One", "Missing Song Two"}

        # Persisted to the missing list the album page renders.
        from_db = ams.get_missing_tracks_from_db("Test Artist", "CSV Album One")
        assert from_db["missing_count"] == 2
        assert {t["title"] for t in from_db["missing_tracks"]} == titles

        # And the flag is set so the scan stops searching the release.
        assert alss.album_lookup_skip("Test Artist", "CSV Album One") is True

    def test_an_owned_title_is_not_missing(self, db_session):
        _seed_track(db_session, "csv-import-owned-1", title="Have It", album="CSV Album Two")
        db_session.commit()
        result = ams.import_csv_tracklist("Test Artist", "CSV Album Two", "Have It\n")
        assert result["missing_count"] == 0

    def test_empty_csv_still_sets_the_flag(self, db_session):
        _seed_track(db_session, "csv-import-empty-1", title="Song", album="CSV Album Three")
        db_session.commit()
        result = ams.import_csv_tracklist("Test Artist", "CSV Album Three", "")
        assert result["rows_read"] == 0
        assert alss.album_lookup_skip("Test Artist", "CSV Album Three") is True, (
            "uploading an empty CSV must still stop the scan's MB refresh from "
            "wiping the album's existing missing list"
        )


# ---------------------------------------------------------------------------
# 4. Scan gates — the album-release lookups are skipped when flagged
# ---------------------------------------------------------------------------

class TestScanGatesSkipFlaggedAlbums:

    def test_resolve_album_type_skips_the_mb_lookup(self, db_session, monkeypatch):
        _seed_track(db_session, "gate-type-1", title="One", album="Gate Type A", album_lookup_skip="1")
        db_session.commit()

        def _boom(*a, **k):
            pytest.fail("MusicBrainz album-type lookup must be skipped for a flagged album")

        monkeypatch.setattr(album_stage, "_lookup_musicbrainz_album_type", _boom)
        detected, mb_type, rg_mbid, raw = album_stage._resolve_album_type(
            "Test Artist", "Gate Type A", "Test Artist", "album", []
        )
        assert mb_type is None and rg_mbid is None and raw is None
        # Local heuristic still answered (a plain unclassified album).
        assert detected

    def test_resolve_album_type_still_looks_up_unflagged(self, db_session, monkeypatch):
        _seed_track(db_session, "gate-type-2", title="One", album="Gate Type B")
        db_session.commit()
        called = {"yes": False}

        def _fake(artist, album, album_artist, tracks):
            called["yes"] = True
            return "single", "rg-1"

        monkeypatch.setattr(album_stage, "_lookup_musicbrainz_album_type", _fake)
        album_stage._resolve_album_type("Test Artist", "Gate Type B", "Test Artist", "album", [])
        assert called["yes"] is True, "an unflagged album still searches MusicBrainz"

    def test_resolve_discogs_album_id_returns_none_when_flagged(self, db_session, monkeypatch):
        _seed_track(db_session, "gate-discogs-1", title="One", album="Gate Discogs A", album_lookup_skip="1")
        db_session.commit()

        import services.enrichment.discogs_service as dsvc

        def _boom(*a, **k):
            pytest.fail("Discogs lookup must be skipped for a flagged album")

        monkeypatch.setattr(dsvc, "lookup_discogs_album", _boom)
        assert album_stage._resolve_discogs_album_id("Test Artist", "Gate Discogs A") is None

    def test_art_fallback_skips_online_providers_when_flagged(self, db_session, monkeypatch):
        _seed_track(db_session, "gate-art-1", title="One", album="Gate Art A", album_lookup_skip="1")
        db_session.commit()

        import services.enrichment.album_art_service as aas
        import api_clients.audiodb as audiodb

        monkeypatch.setattr(
            album_stage, "fetch_album_art_from_musicbrainz",
            lambda *a, **k: pytest.fail("CAA must be skipped"),
        )
        monkeypatch.setattr(
            audiodb, "get_album_artwork",
            lambda *a, **k: pytest.fail("AudioDB must be skipped"),
        )
        # No cached art, no Navidrome art → returns None without touching providers.
        monkeypatch.setattr(aas, "fetch_album_art_from_navidrome", lambda *a, **k: None)

        result = album_stage._fetch_album_art_with_fallback("Test Artist", "Gate Art A", "token")
        assert result is None

    def test_art_fallback_still_searches_unflagged(self, db_session, monkeypatch):
        _seed_track(db_session, "gate-art-2", title="One", album="Gate Art B")
        db_session.commit()

        import services.enrichment.album_art_service as aas

        monkeypatch.setattr(aas, "fetch_album_art_from_navidrome", lambda *a, **k: None)
        monkeypatch.setattr(
            album_stage, "fetch_album_art_from_musicbrainz", lambda *a, **k: "url"
        )
        assert album_stage._fetch_album_art_with_fallback("Test Artist", "Gate Art B", None) == "musicbrainz"


# ---------------------------------------------------------------------------
# 5. Wiring — the runner refresh is gated, the save/render carry the flag
# ---------------------------------------------------------------------------

class TestWiring:

    def test_runner_missing_refresh_is_gated(self):
        src = (REPO_ROOT / "services" / "popularity" / "scan_stage_runner.py").read_text(encoding="utf-8")
        idx = src.index("if not options.get(\"popularity_only\") and not options.get(\"singles_detection_only\") and not _mode_finalise:")
        window = src[idx: idx + 1600]
        assert "album_lookup_skip" in window, (
            "the per-album missing-track refresh must skip albums marked "
            "do-not-search (their list is CSV-sourced)"
        )
        assert "get_missing_tracks(artist=artist, album=album)" in window

    def test_save_writes_the_flag_album_wide(self):
        src = (REPO_ROOT / "routes" / "ui_routes.py").read_text(encoding="utf-8")
        assert "set_album_lookup_skip(" in src, "the album save must write the flag"
        assert "album_lookup_skip" in src.split("album_data = {")[1].split("}")[0], (
            "album_data must carry the flag so the template renders the checkbox checked"
        )

    def test_endpoint_imports_the_csv_service(self):
        src = (REPO_ROOT / "routes" / "album_routes.py").read_text(encoding="utf-8")
        assert '"/tracklist-csv"' in src
        assert "import_csv_tracklist" in src

    def test_test_site_template_and_js_have_the_controls(self):
        html = (REPO_ROOT / "test_site" / "templates" / "Pages" / "album_detail.html").read_text(encoding="utf-8")
        assert 'name="album_lookup_skip"' in html
        assert "albumLookupSkipCsvCard" in html
        js = (REPO_ROOT / "test_site" / "static" / "js" / "pages" / "album.js").read_text(encoding="utf-8")
        assert "uploadAlbumCsvTracklist" in js
        assert "toggleAlbumLookupSkip" in js
        assert "/api/album/tracklist-csv" in js
        assert "global.toggleAlbumLookupSkip" in js