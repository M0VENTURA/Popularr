"""Regression tests for the download-queue "moving" recovery fixes.

Covers:
- ``_apply_stored_metadata`` writes the MusicBrainz-matched fields onto a file
  before it is copied into /music (name + info from the MB match are preserved)
  and never writes empty fields.
- ``_move_and_import`` resets a claimed item back to ``downloading`` when an
  unexpected error occurs after the ``downloading -> moving`` claim, instead of
  leaving it stuck as ``moving`` forever.
- ``_reconcile_stale_moving`` promotes a stale ``moving`` row to ``imported``
  when the target file is already present in /music, and resets it to
  ``downloading`` when it is not.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from services.downloads import download_completion_service as dcs
from services.downloads.download_organize_helpers import _build_target_path


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Row:
    def __init__(self, mapping: dict):
        self._mapping = mapping


def _install_fake_db(monkeypatch, rows: list[dict] | None = None, rowcount: int = 1):
    """Install a fake module-level ``db_session``.

    SELECT queries return ``rows`` (converted to ``_mapping`` rows); every
    other statement reports ``rowcount``.
    """
    class _Result:
        def __init__(self):
            self._rows = [_Row(r) for r in (rows or [])]
            self.rowcount = rowcount

        def fetchall(self):
            return self._rows

    class _Session:
        def __init__(self):
            self.calls: list = []

        def execute(self, stmt, params=None):
            self.calls.append((str(stmt), params))
            return _Result()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _Fake:
        def __init__(self):
            self.session = _Session()

        def __call__(self):
            return self.session

    fake = _Fake()
    monkeypatch.setattr(dcs, "db_session", fake)
    return fake.session


def _install_update_recorder(monkeypatch):
    calls: list[tuple[int, dict]] = []

    def fake_update(queue_id, **kwargs):
        calls.append((queue_id, kwargs))
        return {"id": queue_id, **kwargs}

    monkeypatch.setattr("db.repositories.queue.update_queue_item", fake_update)
    return calls


# ---------------------------------------------------------------------------
# _apply_stored_metadata
# ---------------------------------------------------------------------------

class TestApplyStoredMetadata:
    def test_writes_matched_metadata_including_mbids(self, monkeypatch):
        written = {}

        def fake_update_file_metadata(path, meta):
            written["path"] = path
            written["meta"] = meta
            return True

        monkeypatch.setattr("services.metadata.tag_file_service.update_file_metadata", fake_update_file_metadata)

        item = {
            "id": 1,
            "artist": "Herzblut",
            "title": "You're Just a Ghost (feat. Melissa Bonny)",
            "album": "Ghost",
            "album_artist": "Herzblut",
            "year": "2023",
            "track_number": "3",
            "disc_number": "1",
            "recording_mbid": "rec-mbid-123",
            "release_mbid": "rel-mbid-456",
        }
        dcs._apply_stored_metadata(item, "/downloads/song.mp3")

        assert written["path"] == "/downloads/song.mp3"
        meta = written["meta"]
        assert meta["artist"] == "Herzblut"
        assert meta["title"] == "You're Just a Ghost (feat. Melissa Bonny)"
        assert meta["album"] == "Ghost"
        assert meta["year"] == "2023"
        assert meta["track_number"] == "3"
        assert meta["recording_mbid"] == "rec-mbid-123"
        assert meta["release_mbid"] == "rel-mbid-456"

    def test_skips_empty_fields_and_never_wipes_tags(self, monkeypatch):
        written = {}

        def fake_update_file_metadata(path, meta):
            written["meta"] = meta
            return True

        monkeypatch.setattr("services.metadata.tag_file_service.update_file_metadata", fake_update_file_metadata)

        # Track_number/disc_number missing, no MBIDs.
        item = {"id": 2, "artist": "Muse", "title": "Hysteria", "album": "Absolution"}
        dcs._apply_stored_metadata(item, "/downloads/x.mp3")

        meta = written["meta"]
        assert "track_number" not in meta
        # Single-disc (disc < 2) files get the disc field CLEARED on the
        # transferred file so Navidrome doesn't show a spurious "Disc 1" —
        # the empty value is the deliberate clear signal.
        assert meta.get("disc_number") == ""
        assert "recording_mbid" not in meta
        assert "release_mbid" not in meta
        assert meta["artist"] == "Muse"
        assert meta["title"] == "Hysteria"

    def test_release_id_falls_back_when_release_mbid_missing(self, monkeypatch):
        written = {}

        def fake_update_file_metadata(path, meta):
            written["meta"] = meta
            return True

        monkeypatch.setattr("services.metadata.tag_file_service.update_file_metadata", fake_update_file_metadata)

        item = {"id": 3, "artist": "A", "title": "T", "release_id": "rel-abc"}
        dcs._apply_stored_metadata(item, "/downloads/x.mp3")
        assert written["meta"]["release_mbid"] == "rel-abc"

    def test_persists_mb_enrichment_to_track_row_when_file_known(self, monkeypatch):
        """The reported bug: imported tracks lost the MusicBrainz metadata
        they were matched with.  ``_apply_stored_metadata`` must ALSO write
        the stored writer/cover/genre/work MBID enrichment to the matching
        tracks row (not just the file tags), so the library shows it without
        waiting for a Navidrome rescan."""
        written_file = {}

        def fake_update_file_metadata(path, meta):
            written_file["meta"] = meta
            return True

        monkeypatch.setattr("services.metadata.tag_file_service.update_file_metadata", fake_update_file_metadata)

        saved_rows: list[dict] = []
        import db.repositories.tracks as track_repo

        def fake_insert_or_update_track(track_id, data):
            saved_rows.append({"id": track_id, **data})

        monkeypatch.setattr(track_repo, "insert_or_update_track", fake_insert_or_update_track)
        # The imported file already exists as a tracks row (found by path).
        monkeypatch.setattr(
            dcs,
            "_resolve_track_id_for_import",
            lambda **kw: "track-abc",
        )

        item = {
            "id": 9,
            "artist": "Aephanemer",
            "title": "Contrepoint",
            "album": "Utopie",
            "album_artist": "Aephanemer",
            "year": "2025",
            "track_number": "15",
            "disc_number": "1",
            "recording_mbid": "rec-abc",
            "release_mbid": "rel-xyz",
            "metadata": '{"writer": "M. Goyat", "is_cover": true, '
                        '"original_cover_artist": "Original Artist", '
                        '"musicbrainz_genres": "melodic death metal", '
                        '"work_mbid": "work-123"}',
        }
        dcs._apply_stored_metadata(item, "/music/Aephanemer/2025 - Utopie/15 - Contrepoint.flac")

        assert saved_rows, "expected a tracks-row write"
        row = saved_rows[0]
        assert row["id"] == "track-abc"
        assert row["writer"] == "M. Goyat"
        assert row["is_cover"] == 1
        assert row["original_cover_artist"] == "Original Artist"
        assert row["musicbrainz_genres"] == "melodic death metal"
        assert row["musicbrainz_workid"] == "work-123"
        assert row["recording_mbid"] == "rec-abc"
        assert row["release_mbid"] == "rel-xyz"

    def test_no_track_row_no_db_write(self, monkeypatch):
        """When no tracks row exists for the imported file, the file tags are
        still written but the DB persist is skipped (the next Navidrome scan
        creates the row)."""
        written_file = {}

        def fake_update_file_metadata(path, meta):
            written_file["meta"] = meta
            return True

        monkeypatch.setattr("services.metadata.tag_file_service.update_file_metadata", fake_update_file_metadata)

        import db.repositories.tracks as track_repo
        saved_rows: list[dict] = []

        def fake_insert_or_update_track(track_id, data):
            saved_rows.append({"id": track_id, **data})

        monkeypatch.setattr(track_repo, "insert_or_update_track", fake_insert_or_update_track)
        monkeypatch.setattr(dcs, "_resolve_track_id_for_import", lambda **kw: None)

        item = {
            "id": 10,
            "artist": "A",
            "title": "T",
            "album": "Al",
            "recording_mbid": "rec-1",
            "release_mbid": "rel-2",
        }
        dcs._apply_stored_metadata(item, "/music/A/2000 - Al/01 - T.flac")

        assert saved_rows == []
        assert written_file["meta"]["recording_mbid"] == "rec-1"


# ---------------------------------------------------------------------------
# _move_and_import
# ---------------------------------------------------------------------------

class TestMoveAndImport:
    def test_claim_failure_returns_skipped(self, monkeypatch):
        _install_fake_db(monkeypatch, rowcount=0)
        result = dcs._move_and_import({"id": 42}, "/tmp/src.mp3", "metadata")
        assert result == {"success": False, "error": "already_claimed"}

    def test_unhandled_move_error_resets_to_downloading(self, monkeypatch):
        _install_fake_db(monkeypatch, rowcount=1)
        updates = _install_update_recorder(monkeypatch)

        def boom(*a, **k):
            raise RuntimeError("move exploded")

        monkeypatch.setattr(dcs, "_apply_stored_metadata", lambda *a, **k: None)
        monkeypatch.setattr(
            "services.downloads.download_organize_helpers.move_track_to_library",
            boom,
        )

        result = dcs._move_and_import(
            {"id": 42, "artist": "A", "title": "T"}, "/tmp/src.mp3", "metadata"
        )
        assert result["success"] is False
        assert any(
            qid == 42 and kwargs.get("status") == "downloading"
            for qid, kwargs in updates
        )

    def test_move_failure_resets_to_downloading(self, monkeypatch):
        _install_fake_db(monkeypatch, rowcount=1)
        updates = _install_update_recorder(monkeypatch)

        monkeypatch.setattr(dcs, "_apply_stored_metadata", lambda *a, **k: None)
        monkeypatch.setattr(
            "services.downloads.download_organize_helpers.move_track_to_library",
            lambda *a, **k: {"success": False, "error": "disk full"},
        )

        result = dcs._move_and_import(
            {"id": 42, "artist": "A", "title": "T"}, "/tmp/src.mp3", "metadata"
        )
        assert result["success"] is False
        assert any(
            qid == 42 and kwargs.get("status") == "downloading"
            for qid, kwargs in updates
        )


# ---------------------------------------------------------------------------
# _reconcile_stale_moving
# ---------------------------------------------------------------------------

class TestReconcileStaleMoving:
    def _row(self, queue_id: int, **overrides) -> dict:
        return {
            "id": queue_id,
            "status": "moving",
            "artist": "Herzblut",
            "title": "Ghost",
            "album": "Ghost",
            "album_artist": "Herzblut",
            "year": "2023",
            "track_number": "3",
            "file_path": "/downloads/src.mp3",
            "music_file_path": None,
            **overrides,
        }

    def test_promotes_to_imported_when_target_present(self, monkeypatch, tmp_path):
        _install_fake_db(monkeypatch, rows=[self._row(10)])
        updates = _install_update_recorder(monkeypatch)

        monkeypatch.setattr(dcs, "_MUSIC_ROOT", str(tmp_path))
        target = _build_target_path(
            str(tmp_path),
            "Herzblut", "2023", "Ghost", "Herzblut", "Ghost", "3", "/downloads/src.mp3",
        )
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as fh:
            fh.write("audio")

        stats = dcs._reconcile_stale_moving(stale_minutes=0)
        assert stats["imported"] == 1
        assert stats["reset"] == 0
        imported = [kwargs for qid, kwargs in updates if qid == 10]
        assert imported and imported[0]["status"] == "imported"
        assert imported[0]["music_file_path"] == str(target)

    def test_resets_to_downloading_when_target_missing(self, monkeypatch, tmp_path):
        _install_fake_db(monkeypatch, rows=[self._row(11)])
        updates = _install_update_recorder(monkeypatch)
        monkeypatch.setattr(dcs, "_MUSIC_ROOT", str(tmp_path))

        stats = dcs._reconcile_stale_moving(stale_minutes=0)
        assert stats["imported"] == 0
        assert stats["reset"] == 1
        assert any(
            qid == 11 and kwargs.get("status") == "downloading"
            for qid, kwargs in updates
        )

    def test_uses_music_file_path_directly(self, monkeypatch, tmp_path):
        existing = tmp_path / "already-moved.mp3"
        existing.write_bytes(b"audio")
        _install_fake_db(
            monkeypatch,
            rows=[self._row(12, music_file_path=str(existing), album="", year="", track_number="")],
        )
        updates = _install_update_recorder(monkeypatch)
        monkeypatch.setattr(dcs, "_MUSIC_ROOT", str(tmp_path))

        stats = dcs._reconcile_stale_moving(stale_minutes=0)
        assert stats["imported"] == 1
        imported = [kwargs for qid, kwargs in updates if qid == 12]
        assert imported and imported[0]["status"] == "imported"
        assert imported[0]["music_file_path"] == str(existing)

    def test_staleness_uses_db_clock_not_python_cutoff(self, monkeypatch, tmp_path):
        """The recovery must evaluate staleness against the DB's own clock.

        ``updated_at`` is a naive TIMESTAMP written from Postgres
        ``CURRENT_TIMESTAMP``.  A Python-side ``datetime.utcnow()`` cutoff is
        wrong by the timezone offset whenever the DB session timezone is not
        UTC, so the SQL must subtract the interval from ``CURRENT_TIMESTAMP``
        (the legacy recovery did exactly this).
        """
        session = _install_fake_db(monkeypatch, rows=[self._row(20)])
        _install_update_recorder(monkeypatch)
        monkeypatch.setattr(dcs, "_MUSIC_ROOT", str(tmp_path))

        dcs._reconcile_stale_moving(stale_minutes=7)

        sqls = [c[0] for c in session.calls]
        select_sql = next(s for s in sqls if "SELECT * FROM download_queue" in s)
        assert "CURRENT_TIMESTAMP" in select_sql
        assert "make_interval(mins => :stale_minutes)" in select_sql
        # No Python-computed ISO cutoff string may be bound to the query.
        params = [c[1] for c in session.calls if c[1] and "stale_minutes" in (c[1] or {})]
        assert params and all(isinstance(p["stale_minutes"], int) for p in params)


# ---------------------------------------------------------------------------
# DB-clock staleness helpers
# ---------------------------------------------------------------------------

class TestDbClockStaleness:
    def test_is_stale_with_db_clock_now(self):
        # updated_at is stored as a naive wall-clock in the DB session tz
        # (e.g. AEST); the DB clock is the same wall-clock 12 minutes later.
        item = {"updated_at": "2026-08-06 20:16:13"}
        now = datetime(2026, 8, 6, 20, 28, 13)
        assert dcs._is_stale_queue_item(item, stale_minutes=10, now=now) is True

    def test_not_stale_when_recent_under_db_clock(self):
        item = {"updated_at": "2026-08-06 20:20:13"}
        now = datetime(2026, 8, 6, 20, 28, 13)
        assert dcs._is_stale_queue_item(item, stale_minutes=10, now=now) is False

    def test_db_now_naive_strips_session_offset(self, monkeypatch):
        # psycopg2 returns timestamptz in the session timezone; the helper must
        # drop the offset (keeping the wall-clock) so it is comparable with the
        # naive updated_at column — even for a non-UTC session timezone.
        aware = datetime(2026, 8, 6, 20, 16, 13, tzinfo=timezone(timedelta(hours=10)))

        class _Result:
            def scalar(self):
                return aware

        class _Session:
            def execute(self, stmt, params=None):
                return _Result()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(dcs, "db_session", lambda: _Session())
        assert dcs._db_now_naive() == datetime(2026, 8, 6, 20, 16, 13)

    def test_db_now_naive_parses_sqlite_string(self, monkeypatch):
        class _Result:
            def scalar(self):
                return "2026-08-06 20:16:13"

        class _Session:
            def execute(self, stmt, params=None):
                return _Result()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(dcs, "db_session", lambda: _Session())
        assert dcs._db_now_naive() == datetime(2026, 8, 6, 20, 16, 13)


# ---------------------------------------------------------------------------
# cleanup_stuck_items ('moving' recovery in the maintenance hook)
# ---------------------------------------------------------------------------

class TestCleanupStuckMoving:
    def test_resets_stale_moving_to_downloading(self, monkeypatch):
        from services.queue import queue_cleanup_service as qcs

        executed: list[str] = []

        class _Result:
            rowcount = 1

        class _Session:
            def execute(self, stmt, params=None):
                executed.append(str(stmt))
                return _Result()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(qcs, "db_session", lambda: _Session())

        result = qcs.cleanup_stuck_items()
        assert result["moving_reset"] == 1

        # The cleanup now SELECTs the affected rows first (so the log can name
        # them) and then UPDATEs. Assert on the UPDATE, not the pre-read.
        moving_sql = next(
            s for s in executed
            if "status = 'moving'" in s and "UPDATE" in s
        )
        assert "status = 'downloading'" in moving_sql
        assert "CURRENT_TIMESTAMP" in moving_sql
        assert "make_interval(mins => :minutes)" in moving_sql

    def test_searching_cutoffs_are_db_clock(self, monkeypatch):
        from services.queue import queue_cleanup_service as qcs

        executed: list[str] = []

        class _Result:
            rowcount = 0

        class _Session:
            def execute(self, stmt, params=None):
                executed.append(str(stmt))
                return _Result()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(qcs, "db_session", lambda: _Session())

        qcs.cleanup_stuck_items()
        searching_sql = next(s for s in executed if "status = 'searching'" in s)
        assert "CURRENT_TIMESTAMP - make_interval(secs => :seconds)" in searching_sql
        assert ":cutoff" not in searching_sql
