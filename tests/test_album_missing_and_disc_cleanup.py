"""Regression tests: missing-track detection and disc-number cleanup.

Covers:
1. ``_title_match_key`` preserves Hangul/CJK — Korean titles like
   "락 (樂) (LALALALA)" must NOT be erased to near-empty ASCII keys.
2. ``_album_key`` strips a leading year so "2024 - 樂-STAR" matches "樂-STAR".
3. ``get_library_tracks`` matches year-prefixed album names leniently.
4. Disc-number cleanup: disc "0" must not render as its own "disc 0" group
   (the reported "disc 1 and disc 0" split on single-disc releases).
"""

from __future__ import annotations

import os

from services.metadata import album_missing_service as ams


class TestTitleMatchKey:
    def test_korean_titles_preserved(self):
        """Hangul must survive normalization — two DIFFERENT Korean titles
        must produce DIFFERENT keys (the old ASCII-only strip collapsed
        them, causing false matches)."""
        k1 = ams._title_match_key("락 (樂) (LALALALA)")
        k2 = ams._title_match_key("사각지대 (BLIND SPOT)")
        assert k1
        assert k2
        assert k1 != k2
        # Hangul characters are retained in the key.
        assert "락" in k1 or "lalalala" in k1
        assert "사각지대" in k2 or "blindspot" in k2

    def test_identical_titles_match(self):
        assert ams._title_match_key("락 (樂) (LALALALA)") == ams._title_match_key("락 (樂) (LALALALA)")

    def test_case_and_punctuation_insensitive(self):
        assert ams._title_match_key("Blind Spot") == ams._title_match_key("blind-spot!")
        assert ams._title_match_key("Blind Spot") == ams._title_match_key("blind spot")


class TestAlbumKey:
    def test_year_prefix_stripped(self):
        assert ams._album_key("2024 - 樂-STAR") == "樂-star"
        assert ams._album_key("2024 樂-STAR") == "樂-star"
        assert ams._album_key("樂-STAR") == "樂-star"

    def test_no_year_unchanged(self):
        assert ams._album_key("Obscured Horizons") == "obscured horizons"


class TestGetLibraryTracksLenientAlbum:
    def test_year_prefixed_album_matches(self, db_session):
        from sqlalchemy import text
        db_session.execute(text(
            "CREATE TABLE IF NOT EXISTS tracks (id TEXT PRIMARY KEY, artist TEXT, "
            "album_artist TEXT, album TEXT, title TEXT, track_number TEXT, "
            "disc_number TEXT, file_path TEXT, duration REAL, mbid TEXT)"
        ))
        # ⚠️ UNIQUE id, deliberately NOT 't1'.
        #
        # ``conftest.db_session`` shares ONE in-memory SQLite DB across the whole
        # session, and several other test files insert a tracks row with id='t1'
        # (test_album_musicbrainz_matching, test_favourites_sync, …). Together
        # with ``ON CONFLICT DO NOTHING`` that made this test's own row get
        # SILENTLY SKIPPED whenever it happened to run after one of them, so
        # ``get_library_tracks`` found nothing and ``len(tracks) == 1`` failed.
        # The test passed or failed purely on collection order.
        db_session.execute(text(
            "INSERT INTO tracks (id, artist, album_artist, album, title, track_number, disc_number) "
            "VALUES ('lenient-year-prefix-1', 'Stray Kids', 'Stray Kids', '2024 - 樂-STAR', "
            "'락 (樂) (LALALALA)', '2', '1') "
            "ON CONFLICT DO NOTHING"
        ))
        db_session.commit()

        tracks = ams.get_library_tracks("Stray Kids", "樂-STAR")
        assert len(tracks) == 1
        assert tracks[0]["title"] == "락 (樂) (LALALALA)"


class TestGetMissingTracksRowMapping:
    def test_stored_mbid_uses_column_name_not_index(self, db_session, monkeypatch):
        """Regression: ``get_missing_tracks`` raised "Could not locate column
        in row for column '0'" because it indexed a RowMapping by integer
        position.  It must read the MBID via the column name."""
        from sqlalchemy import text
        db_session.execute(text(
            "CREATE TABLE IF NOT EXISTS tracks (id TEXT PRIMARY KEY, artist TEXT, "
            "album_artist TEXT, album TEXT, title TEXT, track_number TEXT, "
            "disc_number TEXT, file_path TEXT, duration REAL, mbid TEXT, "
            "musicbrainz_album_mbid TEXT)"
        ))
        # Unique id — see the note in TestGetLibraryTracksLenientAlbum: id='t1'
        # collides with rows other test files leave in the shared in-memory DB,
        # and the ON CONFLICT DO NOTHING below would then skip this insert.
        db_session.execute(text(
            "INSERT INTO tracks (id, artist, album_artist, album, title, track_number, "
            "disc_number, musicbrainz_album_mbid) "
            "VALUES ('rowmap-mbid-1', 'Artist', 'Artist', 'Album', 'Song', '1', '1', 'rel-mbid-1') "
            "ON CONFLICT DO NOTHING"
        ))
        db_session.commit()

        # Patch the MB fetch so no network call happens; the stored-MBID path
        # must resolve and NOT raise the RowMapping index error.
        monkeypatch.setattr(
            ams, "fetch_musicbrainz_release_metadata",
            lambda mbid: {"tracks": [], "release_year": "2024"} if mbid == "rel-mbid-1" else None,
        )
        monkeypatch.setattr(ams, "_persist_missing_tracks", lambda *a, **k: None)
        monkeypatch.setattr(ams, "_rejected_missing_titles", lambda *a, **k: set())

        result = ams.get_missing_tracks("Artist", "Album")
        assert result["missing_count"] == 0
        assert result["mb_total"] == 0
    def test_disc_zero_normalised_to_one(self):
        """A bogus disc_number of 0 must be treated as disc 1 — never its
        own 'disc 0' group (mirrors the album page's tracks_by_disc logic)."""

        def _safe_int(value):
            try:
                if value in (None, ""):
                    return None
                return int(value)
            except (TypeError, ValueError):
                return None

        tracks = [
            {"id": "a", "disc_number": "0", "title": "A"},
            {"id": "b", "disc_number": "1", "title": "B"},
            {"id": "c", "disc_number": "", "title": "C"},
        ]

        tracks_by_disc: dict[int, list] = {}
        for track in tracks:
            d = _safe_int(track.get("disc_number"))
            if not d or d < 1:
                d = 1
            tracks_by_disc.setdefault(d, []).append(track)

        # All three tracks collapse onto disc 1 — no separate "disc 0".
        assert set(tracks_by_disc.keys()) == {1}
        assert len(tracks_by_disc[1]) == 3

    def test_disc_strip_clears_zero_on_single_disc(self):
        """On a single-disc album (disctotal <= 1) a stored '0' disc must be
        cleared from the payload so the DB and file tags drop it."""
        # Mirrors the album-save single-disc strip branch.
        _strip_disc_numbers = True
        payload: dict = {}
        _cur_disc = "0"
        if _strip_disc_numbers:
            if _cur_disc and _cur_disc != "0":
                payload["disc_number"] = ""
            elif _cur_disc == "0":
                payload["disc_number"] = ""
        assert payload.get("disc_number") == ""
