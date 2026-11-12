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

import time

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


# ---------------------------------------------------------------------------
# 2. The reported hot retry loop
# ---------------------------------------------------------------------------
# REPORTED
# --------
# A PostgreSQL restart produced HUNDREDS of identical failures per SECOND for a
# single artist (``processed=0``), every one saying ``retry in 0s``:
#
#   scan_artist_to_db failed artist='Electric Callboy' ... DatabaseUnavailable:
#   database unavailable (recovering from a restart); retry in 0s
#
# The breaker refused the caller (so it did not connect), yet the retry value it
# reported was 0 — which made ``is_db_outage_active()`` read False and
# ``wait_for_db_available()`` return True INSTANTLY. Every caller's pause became
# a no-op, so the artist was re-entered immediately, forever.
#
# ``_claim_db_probe`` refuses for three reasons; the reported wait only honoured
# the first. THE INVARIANT PINNED HERE: whenever a caller is refused, the wait it
# is told to observe must be positive.


def test_retry_wait_is_positive_while_another_caller_is_probing():
    """The outage deadline can pass while a probe is still in flight.

    ``_claim_db_probe`` still refuses everyone else (``PROBE_IN_FLIGHT``), so a
    0-second wait here is what produced the spin.
    """
    _open_breaker()
    # The outage window has elapsed, but the probe that grabbed the slot has not
    # finished. This is the exact state the log storm was raised from.
    db_engine._DB_OUTAGE_UNTIL = time.monotonic() - 1.0
    db_engine._DB_OUTAGE_PROBE_AT = time.monotonic() - 1.0
    db_engine._DB_OUTAGE_PROBE_IN_FLIGHT = True

    assert db_engine.db_outage_retry_after() > 0, (
        "a caller is still refused while a probe is in flight, so the reported "
        "wait must be positive — 0 is the hot-loop bug"
    )
    assert db_engine.is_db_outage_active() is True
    assert db_engine.wait_for_db_available(timeout_seconds=0) is False


def test_retry_wait_is_positive_during_the_probe_cooldown():
    """Second gate: the cooldown between probes must also be observable."""
    _open_breaker()
    db_engine._DB_OUTAGE_UNTIL = time.monotonic() - 1.0
    db_engine._DB_OUTAGE_PROBE_IN_FLIGHT = False
    db_engine._DB_OUTAGE_PROBE_AT = time.monotonic() + 5.0

    assert db_engine.db_outage_retry_after() > 0
    assert db_engine.is_db_outage_active() is True


def test_a_refused_caller_always_gets_a_positive_wait(monkeypatch):
    """THE INVARIANT: refusal (claim False) ⇒ retry_after > 0.

    Walked over the gate states the breaker can actually be in, so a future
    change to either ``_claim_db_probe`` or the wait cannot silently reintroduce
    the mismatch.
    """
    probes_and_states = [
        # (probe_in_flight, outage_remaining, cooldown_remaining)
        (True, 10.0, 5.0),
        (True, -1.0, -1.0),   # reported: outage elapsed, probe still running
        (False, 10.0, 5.0),
        (False, 10.0, -1.0),
        (False, -1.0, 5.0),
    ]
    now = time.monotonic()
    for in_flight, outage_remaining, cooldown_remaining in probes_and_states:
        db_engine.reset_db_outage()
        # Force the breaker open so the gates are meaningful.
        db_engine._DB_OUTAGE_UNTIL = now + max(outage_remaining, 0.001)
        db_engine._DB_OUTAGE_PROBE_IN_FLIGHT = in_flight
        db_engine._DB_OUTAGE_PROBE_AT = now + cooldown_remaining

        claimed = db_engine._claim_db_probe()
        try:
            retry_after = db_engine.db_outage_retry_after()
            if not claimed:
                assert retry_after > 0, (
                    f"caller refused with in_flight={in_flight}, "
                    f"outage={outage_remaining}, cooldown={cooldown_remaining} "
                    f"but was told to wait {retry_after}s — that is the spin"
                )
            else:
                # A granted probe may legitimately see 0: it is allowed to try.
                assert retry_after >= 0
        finally:
            db_engine._release_db_probe()
