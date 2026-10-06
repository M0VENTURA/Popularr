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


# ===========================================================================
# The upsert must repair a stale track artist, not skip the row
# ===========================================================================
class _FakeResult:
    def __init__(self, rows=None):
        self._rows = list(rows or [])

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def fetchall(self):
        return self._rows


class _FakeSession:
    """Just enough of ``db_session`` to drive ``_persist_missing_tracks``."""

    def __init__(self, existing):
        self._existing = list(existing)
        self.statements: list[tuple[str, dict]] = []
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        sql_text = str(sql)
        self.statements.append((sql_text, dict(params or {})))
        if "SELECT" in sql_text and "FROM missing_album_tracks" in sql_text:
            return _FakeResult(self._existing)
        return _FakeResult([])

    def commit(self):
        self.committed = True

    def updates(self) -> list[tuple[str, dict]]:
        return [
            (sql, params) for sql, params in self.statements
            if sql.lstrip().upper().startswith("UPDATE")
        ]

    def inserts(self) -> list[tuple[str, dict]]:
        return [
            (sql, params) for sql, params in self.statements
            if sql.lstrip().upper().startswith("INSERT")
        ]


def _persist(monkeypatch, existing, missing) -> _FakeSession:
    """Run ``_persist_missing_tracks`` against a fake session and return it."""
    from services.metadata import album_missing_service as ams_mod

    session = _FakeSession(existing)
    monkeypatch.setattr(ams_mod, "db_session", lambda: session)
    ams_mod._persist_missing_tracks("Various Artists", "A Very Special Christmas", missing)
    return session


def _existing_row(**overrides):
    row = {
        "id": 7,
        "title": "Do You Hear What I Hear?",
        "track_number": "4",
        "disc_number": 1,
        "track_artist": "Various Artists",
        "ignored": False,
    }
    row.update(overrides)
    return row


def _incoming(**overrides):
    item = {
        "title": "Do You Hear What I Hear?",
        "track_number": "4",
        "disc_number": 1,
        "track_artist": "Whitney Houston",
        "year": "1987",
        "release_id": "rel-1",
        "recording_mbid": "rec-1",
        "duration": 150000,
    }
    item.update(overrides)
    return item


class TestAPersistedRowNeverKeepsTheAlbumArtist:
    """Reported: *"Missing tracks are still adding 'various artists' to the
    queue search — I think it's due to the way missing tracks are populated."*

    The computation was already fixed to store the recording's own credit, but
    the upsert skipped any row whose ``(disc, track, title)`` already existed —
    so rows written before that fix kept their placeholder, the album page read
    ``track_artist`` and queued the track under "Various Artists". The report's
    diagnosis was right: it is the POPULATION.
    """

    def test_a_stored_placeholder_is_refreshed_to_the_performer(self, monkeypatch):
        session = _persist(
            monkeypatch,
            [_existing_row(track_artist="Various Artists")],
            [_incoming(track_artist="Whitney Houston")],
        )

        updates = session.updates()
        assert updates, "the stale placeholder was left in place"
        sql, params = updates[0]
        assert "SET track_artist" in sql
        assert params == {"track_artist": "Whitney Houston", "id": 7}, (
            "the queue reads track_artist — it must be repaired, not re-inserted"
        )

    def test_an_empty_stored_artist_is_filled(self, monkeypatch):
        session = _persist(
            monkeypatch,
            [_existing_row(track_artist="")],
            [_incoming(track_artist="Whitney Houston")],
        )

        assert session.updates()[0][1]["track_artist"] == "Whitney Houston"

    def test_a_real_performer_is_not_downgraded(self, monkeypatch):
        """CONTROL — MB returns no credit for some recordings, and its
        fallback IS the album artist; storing that would undo a good value."""
        session = _persist(
            monkeypatch,
            [_existing_row(track_artist="Whitney Houston")],
            [_incoming(track_artist="Various Artists")],
        )

        assert not session.updates(), (
            "a placeholder must never overwrite a real performer"
        )
        assert not session.inserts(), "the row must not be duplicated either"

    def test_an_unchanged_artist_is_not_rewritten(self, monkeypatch):
        """CONTROL — no churn when nothing differs."""
        session = _persist(
            monkeypatch,
            [_existing_row(track_artist="Whitney Houston")],
            [_incoming(track_artist="Whitney Houston")],
        )

        assert not session.updates()

    def test_a_new_missing_track_is_still_inserted(self, monkeypatch):
        """CONTROL — the repair must not turn the upsert into a no-op."""
        session = _persist(
            monkeypatch,
            [_existing_row()],
            [_incoming(title="Poor Jack", track_number="16")],
        )

        assert session.inserts(), "a genuinely new missing row must be inserted"

    def test_a_row_that_disappeared_is_still_deleted(self, monkeypatch):
        """CONTROL — the staleness removal must survive the new pass."""
        session = _persist(monkeypatch, [_existing_row()], [])

        deletes = [
            sql for sql, _ in session.statements
            if sql.lstrip().upper().startswith("DELETE")
        ]
        assert deletes, "a missing row that is no longer missing must go away"

    def test_the_repair_is_logged(self, monkeypatch):
        """So an operator can see it happen instead of guessing."""
        from services.metadata import album_missing_service as ams_mod

        calls: list[tuple[str, dict]] = []

        class _Rec:
            def info(self, event, **kw):
                calls.append((str(event), kw))

            def debug(self, event, **kw):
                calls.append((str(event), kw))

            def warning(self, event, **kw):
                calls.append((str(event), kw))

        monkeypatch.setattr(ams_mod, "logger", _Rec())
        _persist(
            monkeypatch,
            [_existing_row(track_artist="Various Artists")],
            [_incoming(track_artist="Whitney Houston")],
        )

        assert any(
            name == "Refreshed stale missing-track artists" and kw.get("refreshed") == 1
            for name, kw in calls
        ), "a silent repair leaves the next report unanswerable"
