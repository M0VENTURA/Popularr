"""A PostgreSQL restart must not turn the scan into a connection storm.

REPORTED
--------
The postgres log shows a backend exiting with code 2 (PANIC), the postmaster
reinitializing, and then the scan continuing artist by artist for 1,000+
artists. Every artist made several connection attempts into "the database
system is in recovery mode", which lengthens recovery and floods the logs.

The breaker opens on the first connection-level failure. Other callers fail
fast with ``DatabaseUnavailable`` without opening a socket; one probe every
few seconds checks for recovery.
"""

from __future__ import annotations

import pytest

from db import engine as db_engine


@pytest.fixture(autouse=True)
def _reset_breaker():
    db_engine.reset_db_outage()
    yield
    db_engine.reset_db_outage()


def _open_breaker():
    db_engine._note_db_outage(
        RuntimeError("FATAL: the database system is in recovery mode")
    )


def test_a_recovery_error_opens_the_breaker():
    _open_breaker()

    assert db_engine.is_db_outage_active() is True
    assert db_engine.db_outage_retry_after() > 0
    assert db_engine.wait_for_db_available(timeout_seconds=0) is False


def test_a_lock_or_query_error_does_not_open_the_breaker():
    db_engine._note_db_outage(RuntimeError("deadlock detected"))

    assert db_engine.is_db_outage_active() is False


def test_non_probe_callers_fail_fast_without_connecting(monkeypatch):
    _open_breaker()

    def _must_not_connect():
        raise AssertionError("db_session opened a connection during recovery")

    monkeypatch.setattr(db_engine, "get_session_factory", _must_not_connect)

    with pytest.raises(db_engine.DatabaseUnavailable):
        with db_engine.db_session():
            pass


def test_a_successful_probe_closes_the_breaker(monkeypatch):
    _open_breaker()
    # Let this caller become the probe immediately.
    db_engine._DB_OUTAGE_PROBE_AT = 0.0
    db_engine._DB_OUTAGE_PROBE_IN_FLIGHT = False

    class _Session:
        committed = False
        closed = False

        def commit(self):
            self.committed = True

        def rollback(self):
            pass

        def close(self):
            self.closed = True

    sessions: list[_Session] = []

    def _factory():
        session = _Session()
        sessions.append(session)
        return session

    monkeypatch.setattr(db_engine, "get_session_factory", lambda: _factory)

    with db_engine.db_session() as session:
        assert isinstance(session, _Session)

    assert sessions and sessions[0].committed and sessions[0].closed
    assert db_engine.is_db_outage_active() is False


def test_nested_sessions_on_the_probe_thread_are_allowed(monkeypatch):
    _open_breaker()
    db_engine._DB_OUTAGE_PROBE_AT = 0.0
    db_engine._DB_OUTAGE_PROBE_IN_FLIGHT = False

    created: list[object] = []

    class _Session:
        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    def _factory():
        session = _Session()
        created.append(session)
        return session

    monkeypatch.setattr(db_engine, "get_session_factory", lambda: _factory)

    with db_engine.db_session():
        with db_engine.db_session():
            pass

    assert len(created) == 2
