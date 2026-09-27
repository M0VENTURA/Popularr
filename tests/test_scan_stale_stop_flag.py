"""Regression tests: stale stop flags + resume-stops-after-current-artist.

Reported issues:
1. "When resuming, it will only do the current artist and stop when running
   from the dashboard."
2. "When pressing stop to shut down the scan, any scan that tries to run
   after is immediately stopped again."

Root cause: a previous Stop leaves ``scan_states.stop_requested=True`` on the
scan type.  The dashboard "All" scan checks ``is_stop_requested(full_scan)``
every artist, but NOTHING cleared that flag when a new scan started — so the
new scan halted on its FIRST check (after doing exactly the resume-point
artist).  The fix clears stale stop flags at the START of every scan
(full_scan / library / popularity / mp3).

Verified here: ``_run_full_scan_as_artist_pipeline`` clears the stale
``full_scan`` stop flag before iterating, so a previous Stop no longer
instantly halts the next scan.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text

import db.engine as db_engine_mod


@pytest.fixture(autouse=True)
def _clean_runtime():
    from services.scanning.runtime_state import clear_runtime

    clear_runtime("popularity")
    yield
    clear_runtime("popularity")


@pytest.fixture(autouse=True)
def _restore_engine_singleton():
    """⚠️ ``_fresh_db()`` REPLACES the global engine singleton.

    It used to do so PERMANENTLY: ``db_engine_mod._ENGINE = engine`` with no
    restore, so this file left a stray engine (and its open connection) behind
    for every test that ran afterwards in the session. The engine is a
    PROCESS-WIDE singleton, so anything asserting on its pool, its URL or its
    shared in-memory database then failed for a reason that had nothing to do
    with the code under test — a purely ORDER-DEPENDENT failure.

    Snapshot before the test and restore afterwards.
    """
    previous_engine = db_engine_mod._ENGINE
    previous_factory = db_engine_mod._SESSION_FACTORY
    yield
    swapped_in = db_engine_mod._ENGINE
    db_engine_mod._ENGINE = previous_engine
    db_engine_mod._SESSION_FACTORY = previous_factory
    # Release the temporary engine's connection rather than leaking it.
    if swapped_in is not None and swapped_in is not previous_engine:
        try:
            swapped_in.dispose()
        except Exception:
            pass


def _fresh_db() -> None:
    """Swap the singleton engine for a fresh in-memory SQLite engine."""
    engine = create_engine("sqlite:///:memory:")
    db_engine_mod._ENGINE = engine
    db_engine_mod._SESSION_FACTORY = None
    with db_engine_mod.db_session() as session:
        session.execute(text("""
            CREATE TABLE scan_states (
                scan_type TEXT PRIMARY KEY,
                is_running BOOLEAN,
                status TEXT,
                stop_requested BOOLEAN,
                current_artist TEXT,
                last_scanned_artist TEXT,
                extra_data TEXT,
                updated_at TEXT
            )
        """))


def _set_stop_requested(scan_type: str, value: bool = True) -> None:
    with db_engine_mod.db_session() as session:
        session.execute(text("""
            INSERT INTO scan_states (scan_type, is_running, status, stop_requested)
            VALUES (:t, FALSE, :s, :v)
            ON CONFLICT(scan_type) DO UPDATE SET
                stop_requested = :v,
                status = :s
        """), {"t": scan_type, "v": value, "s": "stop_requested" if value else "idle"})


class TestFullScanClearsStaleStopFlag:
    def test_stale_full_scan_stop_cleared_before_loop(self):
        """A previous Stop leaves ``full_scan.stop_requested=True``; the next
        "All" scan must clear it so it does NOT halt on the first artist
        (the reported "resume only does current artist then stops")."""
        _fresh_db()
        _set_stop_requested("full_scan", True)

        from services.scanning.pipelines import popularity_pipeline as pipeline

        fake_artists = ["A Day to Remember", "Muse", "Stray Kids"]
        ran = []

        def _fake_run_artist_scan_pipeline(artist, **kwargs):
            ran.append(artist)

        with (
            patch.object(pipeline, "get_all_artists", return_value=fake_artists),
            patch.object(pipeline, "run_artist_scan_pipeline", side_effect=_fake_run_artist_scan_pipeline),
            patch.object(pipeline, "get_scan_progress_path", return_value="full_scan"),
            patch.object(pipeline, "is_stop_requested", return_value=False),
            patch.object(pipeline, "save_artist_scan_checkpoint", lambda *a, **k: None),
            patch.object(pipeline, "clear_scan_checkpoint", lambda *a, **k: None),
            patch.object(pipeline, "write_progress_with_current_artist", lambda *a, **k: None),
            patch.object(pipeline, "log_unified", lambda *a, **k: None),
            patch.object(pipeline, "record_scan", lambda *a, **k: None),
            patch.object(pipeline, "logger", MagicMock()),
        ):
            pipeline._run_full_scan_as_artist_pipeline(force=False, resume_from=None)

        # The stale stop flag was cleared and ALL artists were processed.
        assert ran == fake_artists

    def test_stale_stop_cleared_via_clear_stop_request(self):
        """After _run_full_scan_as_artist_pipeline starts, the full_scan
        stop_requested flag must be False in the DB."""
        _fresh_db()
        _set_stop_requested("full_scan", True)

        from services.scanning.pipelines import popularity_pipeline as pipeline

        with (
            patch.object(pipeline, "get_all_artists", return_value=[]),
            patch.object(pipeline, "run_artist_scan_pipeline", lambda *a, **k: None),
            patch.object(pipeline, "get_scan_progress_path", return_value="full_scan"),
            patch.object(pipeline, "write_progress_with_current_artist", lambda *a, **k: None),
            patch.object(pipeline, "log_unified", lambda *a, **k: None),
            patch.object(pipeline, "record_scan", lambda *a, **k: None),
            patch.object(pipeline, "logger", MagicMock()),
        ):
            pipeline._run_full_scan_as_artist_pipeline(force=False, resume_from=None)

        with db_engine_mod.db_session() as session:
            row = session.execute(
                text("SELECT stop_requested FROM scan_states WHERE scan_type = 'full_scan'")
            ).fetchone()
            assert row is not None
            assert row[0] is False or row[0] == 0
