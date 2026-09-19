"""Regression tests for full scans continuing through the whole library.

Covers the "full scan stops at the first letter group" symptom:

- A scheduled popularity scan must never overlap a manually started scan —
  both write to the SAME ``popularity_scan`` progress row, so whichever
  finishes first flips the shared state to complete while the other is still
  running, which makes a full scan look like it halted mid-letter.
- ``run_full_library_scan`` must clear a stale resume checkpoint (artist no
  longer in the index) instead of silently skipping every artist.
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


def _set_scan_running(scan_type: str = "popularity_scan", running: bool = True) -> None:
    with db_engine_mod.db_session() as session:
        session.execute(text("""
            INSERT INTO scan_states (scan_type, is_running, status)
            VALUES (:t, :r, :s)
            ON CONFLICT(scan_type) DO UPDATE SET
                is_running = :r, status = :s
        """), {"t": scan_type, "r": running, "s": "running" if running else "complete"})


class TestIsPopularityScanActive:
    def test_true_when_shared_state_running(self):
        _fresh_db()
        _set_scan_running("popularity_scan", running=True)

        from services.scanning.pipelines.popularity_pipeline import is_popularity_scan_active

        assert is_popularity_scan_active() is True

    def test_true_when_full_scan_shared_state_running(self):
        # The dashboard "All" scan runs under the "full_scan" progress row;
        # the guard must treat it as an active popularity-family scan too.
        _fresh_db()
        _set_scan_running("full_scan", running=True)

        from services.scanning.pipelines.popularity_pipeline import is_popularity_scan_active

        assert is_popularity_scan_active() is True

    def test_false_when_shared_state_complete(self):
        _fresh_db()
        _set_scan_running("popularity_scan", running=False)

        from services.scanning.pipelines.popularity_pipeline import is_popularity_scan_active

        assert is_popularity_scan_active() is False

    def test_false_when_no_state(self):
        _fresh_db()

        from services.scanning.pipelines.popularity_pipeline import is_popularity_scan_active

        assert is_popularity_scan_active() is False

    def test_true_when_runtime_running(self):
        _fresh_db()
        from services.scanning.runtime_state import set_runtime

        set_runtime("popularity", {"thread": threading.current_thread(), "type": "test"})

        from services.scanning.pipelines.popularity_pipeline import is_popularity_scan_active

        assert is_popularity_scan_active() is True


class TestScheduledPopularityScanGuard:
    def test_skips_when_scan_active(self):
        _fresh_db()
        _set_scan_running("popularity_scan", running=True)

        from services.scheduler import scheduler_service as sched
        from services.scanning.pipelines import popularity_pipeline as pipeline

        with (
            patch.object(pipeline, "run_popularity_mode", new=MagicMock()) as run_mock,
            patch.object(pipeline, "is_popularity_scan_active", return_value=True),
        ):
            sched._run_scheduled_popularity_scan()
            run_mock.assert_not_called()

    def test_runs_when_idle(self):
        _fresh_db()

        from services.scheduler import scheduler_service as sched
        from services.scanning.pipelines import popularity_pipeline as pipeline

        with patch.object(pipeline, "run_popularity_mode", new=MagicMock()) as run_mock:
            sched._run_scheduled_popularity_scan()
            run_mock.assert_called_once()

    def test_clears_runtime_after_run(self):
        _fresh_db()

        from services.scheduler import scheduler_service as sched
        from services.scanning.runtime_state import is_runtime_running
        from services.scanning.pipelines import popularity_pipeline as pipeline

        with patch.object(pipeline, "run_popularity_mode", new=MagicMock()):
            sched._run_scheduled_popularity_scan()

        assert is_runtime_running("popularity") is False


class TestValidatedResumeArtist:
    def test_stale_checkpoint_cleared(self):
        _fresh_db()
        # Persist a stale checkpoint artist that is NOT in the index.
        with db_engine_mod.db_session() as session:
            session.execute(text("""
                INSERT INTO scan_states (scan_type, is_running, status, last_scanned_artist)
                VALUES ('library', FALSE, 'idle', 'Gone Artist')
            """))

        from services.scanning.pipeline import _validated_resume_artist

        artists = [("A Day to Remember", {"id": "1"}), ("Muse", {"id": "2"})]
        resume = _validated_resume_artist(artists, "library", force=False)

        assert resume is None
        with db_engine_mod.db_session() as session:
            row = session.execute(
                text("SELECT last_scanned_artist FROM scan_states WHERE scan_type = 'library'")
            ).fetchone()
            assert row[0] is None

    def test_valid_checkpoint_kept(self):
        _fresh_db()
        with db_engine_mod.db_session() as session:
            session.execute(text("""
                INSERT INTO scan_states (scan_type, is_running, status, last_scanned_artist)
                VALUES ('library', FALSE, 'idle', 'Muse')
            """))

        from services.scanning.pipeline import _validated_resume_artist

        artists = [("A Day to Remember", {"id": "1"}), ("Muse", {"id": "2"})]
        resume = _validated_resume_artist(artists, "library", force=False)

        assert resume == "Muse"

    def test_force_resumes_from_checkpoint(self):
        """A FORCED scan (no restart) resumes from the last checkpoint in
        forced mode — it must NOT clear/ignore the resume point (previously
        force cleared the checkpoint and always restarted from the top)."""
        _fresh_db()
        with db_engine_mod.db_session() as session:
            session.execute(text("""
                INSERT INTO scan_states (scan_type, is_running, status, last_scanned_artist)
                VALUES ('library', FALSE, 'idle', 'Muse')
            """))

        from services.scanning.pipeline import _validated_resume_artist

        artists = [("A Day to Remember", {"id": "1"}), ("Muse", {"id": "2"})]
        resume = _validated_resume_artist(artists, "library", force=True)

        # Forced resumes from the checkpoint — only RESTART goes to the top.
        assert resume == "Muse"

    def test_restart_ignores_checkpoint(self):
        """A RESTART (with or without force) always starts from the top —
        the checkpoint is ignored so the whole library is revisited."""
        _fresh_db()
        with db_engine_mod.db_session() as session:
            session.execute(text("""
                INSERT INTO scan_states (scan_type, is_running, status, last_scanned_artist)
                VALUES ('library', FALSE, 'idle', 'Muse')
            """))

        from services.scanning.pipeline import _validated_resume_artist

        artists = [("A Day to Remember", {"id": "1"}), ("Muse", {"id": "2"})]
        assert _validated_resume_artist(artists, "library", force=False, restart=True) is None
        assert _validated_resume_artist(artists, "library", force=True, restart=True) is None

    def test_stale_checkpoint_cleared_on_forced_resume(self):
        """A stale checkpoint (artist gone) is cleared even in forced-resume
        mode so the forced scan does not stall in skip mode."""
        _fresh_db()
        with db_engine_mod.db_session() as session:
            session.execute(text("""
                INSERT INTO scan_states (scan_type, is_running, status, last_scanned_artist)
                VALUES ('library', FALSE, 'idle', 'Gone Artist')
            """))

        from services.scanning.pipeline import _validated_resume_artist

        artists = [("A Day to Remember", {"id": "1"}), ("Muse", {"id": "2"})]
        resume = _validated_resume_artist(artists, "library", force=True)

        assert resume is None
        with db_engine_mod.db_session() as session:
            row = session.execute(
                text("SELECT last_scanned_artist FROM scan_states WHERE scan_type = 'library'")
            ).fetchone()
            assert row[0] is None


class TestMissingReleasesSweepProbe:
    """The missing-releases sweep must be able to see a running popularity scan.

    Regression: the sweep's accessor probe listed four module/attribute
    candidates, none of which exist in this build, so it logged

        Cannot determine whether a popularity scan is active
        reason='no known accessor found; the missing-releases sweep cannot
        serialise itself against the popularity scan'

    and then always returned False. Because the two scans share one 1 req/s
    MusicBrainz budget, that let the sweep run concurrently with a popularity
    scan. The canonical accessor is
    ``services.scanning.pipelines.popularity_pipeline.is_popularity_scan_active``.
    """

    def _reset_warning_flag(self):
        import services.metadata.artist_scan_service as svc

        svc._popularity_probe_warned = False
        return svc

    def test_canonical_accessor_is_probed_first(self):
        """The one accessor that exists must be the first candidate tried."""
        from services.metadata.artist_scan_service import _POPULARITY_SCAN_PROBES

        assert _POPULARITY_SCAN_PROBES[0] == (
            "services.scanning.pipelines.popularity_pipeline",
            "is_popularity_scan_active",
        )

    def test_every_probed_attribute_exists(self):
        """No permanently-dead candidate may remain in the probe list.

        Each entry has to resolve to a real callable, or the entry is noise
        that only serves to hide the canonical accessor when it moves.
        """
        from services.metadata.artist_scan_service import _POPULARITY_SCAN_PROBES

        missing: list[str] = []
        for module_name, attribute in _POPULARITY_SCAN_PROBES:
            try:
                module = __import__(module_name, fromlist=[attribute])
            except Exception:
                missing.append(f"{module_name}.{attribute}")
                continue
            if not callable(getattr(module, attribute, None)):
                missing.append(f"{module_name}.{attribute}")

        assert missing == []

    def test_probe_false_when_idle(self):
        _fresh_db()
        from services.metadata.artist_scan_service import _popularity_scan_active

        assert _popularity_scan_active() is False

    def test_probe_true_when_shared_popularity_scan_running(self):
        _fresh_db()
        _set_scan_running("popularity_scan", running=True)

        from services.metadata.artist_scan_service import _popularity_scan_active

        assert _popularity_scan_active() is True

    def test_probe_true_when_shared_full_scan_running(self):
        """The dashboard "All" scan runs as ``full_scan`` and must also block."""
        _fresh_db()
        _set_scan_running("full_scan", running=True)

        from services.metadata.artist_scan_service import _popularity_scan_active

        assert _popularity_scan_active() is True

    def test_probe_true_when_runtime_registry_running(self):
        _fresh_db()
        from services.scanning.runtime_state import set_runtime

        set_runtime("popularity", {"thread": threading.current_thread(), "type": "test"})

        from services.metadata.artist_scan_service import _popularity_scan_active

        assert _popularity_scan_active() is True

    def test_no_warning_when_the_accessor_resolves(self):
        _fresh_db()
        svc = self._reset_warning_flag()

        with patch.object(svc.logger, "warning") as warn:
            assert svc._popularity_scan_active() is False

        assert [
            c for c in warn.call_args_list
            if c.args and c.args[0] == "Cannot determine whether a popularity scan is active"
        ] == []

    def test_warns_once_when_no_accessor_resolves(self):
        """Forward-compat: a moved accessor degrades to ONE warning, not a crash."""
        import services.metadata.artist_scan_service as svc

        svc._popularity_probe_warned = False
        dead_probes = (
            ("services.scanning.scan_state", "is_popularity_scan_running"),
            ("services.popularity.scan_state", "is_scan_running"),
        )

        with patch.object(svc, "_POPULARITY_SCAN_PROBES", dead_probes), \
                patch.object(svc.logger, "warning") as warn:
            assert svc._popularity_scan_active() is False
            assert svc._popularity_scan_active() is False

        scan_warnings = [
            c for c in warn.call_args_list
            if c.args and c.args[0] == "Cannot determine whether a popularity scan is active"
        ]
        assert len(scan_warnings) == 1
        assert scan_warnings[0].kwargs["probed"] == [
            "services.scanning.scan_state.is_popularity_scan_running",
            "services.popularity.scan_state.is_scan_running",
        ]
