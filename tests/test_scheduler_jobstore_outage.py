"""The APScheduler main loop must survive a transient jobstore failure.

Reported traceback ("Exception in thread APScheduler"), from a Postgres restart:

    File ".../scheduler_service.py", line 155, in get_next_run_time
        return self._execute(super().get_next_run_time)
    ...
    sqlalchemy.exc.OperationalError: (psycopg2.OperationalError) connection to
    server at "popularr-postgres" ... failed: FATAL:  the database system is in
    recovery mode

APScheduler's ``BlockingScheduler._main_loop`` calls
``jobstore.get_next_run_time()`` with NO surrounding try/except (unlike
``get_due_jobs``, which it does guard). An exception there ends the scheduler
thread, and every scheduled job stops with it.
"""

from __future__ import annotations

import inspect

from sqlalchemy.exc import OperationalError

from services.scheduler.scheduler_service import ResilientSQLAlchemyJobStore


def _operational_error() -> OperationalError:
    """A real ``OperationalError`` carrying the driver's own error type.

    ``_is_transient`` looks at ``exc.orig.__class__.__name__`` as well as the
    SQLAlchemy type, so the ``orig`` must be an actual psycopg2-shaped error —
    not a renamed ``Exception`` (which cannot be renamed; it is immutable).
    """
    try:
        import psycopg2

        orig: Exception = psycopg2.OperationalError(
            'connection to server at "popularr-postgres" failed: '
            "FATAL:  the database system is in recovery mode"
        )
    except ImportError:
        class _FakeOperationalError(Exception):
            pass

        orig = _FakeOperationalError(
            "FATAL:  the database system is in recovery mode"
        )
    return OperationalError("SELECT 1", {}, orig)


class TestGetNextRunTimeNeverRaises:
    def _store(self) -> ResilientSQLAlchemyJobStore:
        # The constructor opens no connection; the reads below are what matter.
        store = ResilientSQLAlchemyJobStore.__new__(ResilientSQLAlchemyJobStore)
        store._last_next_run_time = None
        return store

    def test_a_transient_failure_is_swallowed(self, monkeypatch):
        store = self._store()

        def _boom():
            raise _operational_error()

        monkeypatch.setattr(ResilientSQLAlchemyJobStore, "_execute", lambda *a, **k: _boom())
        # Must return, not propagate.
        assert store.get_next_run_time() is None

    def test_the_last_known_value_is_served(self, monkeypatch):
        """A transient outage must not silently make every job unscheduled."""
        store = self._store()
        store._last_next_run_time = "2026-11-23T22:00:00"

        def _boom():
            raise _operational_error()

        monkeypatch.setattr(ResilientSQLAlchemyJobStore, "_execute", lambda *a, **k: _boom())
        assert store.get_next_run_time() == "2026-11-23T22:00:00"

    def test_a_non_transient_failure_still_propagates(self, monkeypatch):
        """A schema/permission fault is REAL — it must not be papered over."""
        store = self._store()

        def _boom():
            raise ValueError("no such column: next_run_time")

        monkeypatch.setattr(ResilientSQLAlchemyJobStore, "_execute", lambda *a, **k: _boom())
        try:
            store.get_next_run_time()
        except ValueError:
            return
        raise AssertionError("a non-transient error must still surface")

    def test_a_successful_read_is_cached(self, monkeypatch):
        store = self._store()
        monkeypatch.setattr(
            ResilientSQLAlchemyJobStore, "_execute", lambda *a, **k: "2026-11-24T01:00:00"
        )
        assert store.get_next_run_time() == "2026-11-24T01:00:00"
        assert store._last_next_run_time == "2026-11-24T01:00:00"


class TestTheReasonThisMattersIsRecorded:
    def test_the_docstring_names_the_failure_mode(self):
        doc = inspect.getdoc(ResilientSQLAlchemyJobStore.get_next_run_time) or ""
        # The guard against someone "simplifying" this back to one line.
        assert "Exception in thread APScheduler" in doc
        assert "recovery mode" in doc

    def test_a_transient_error_is_recognised(self):
        assert ResilientSQLAlchemyJobStore._is_transient(_operational_error()) is True
