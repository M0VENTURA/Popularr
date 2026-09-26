"""Regression tests for the Essentia scan's DB write path.

THE REPORTED FAILURE
--------------------
A running Essentia scan logged, for successive tracks:

    Failed to update track track_id='34psJirWmggRRFyrhbjHGI' error='cursor already closed'
    Failed to update track track_id='JNKJDBVf25HI6seZjOLyhz' error='cursor already closed'
    Failed to update track track_id='75JpHXQP2JEPlLXDjstqEP' error='cursor already closed'

Note the shape: ONE message, repeated for DIFFERENT track ids. That is the
signature of a connection that died once and was never re-established — not of
N independent failures.

WHY IT HAPPENS
--------------
``run_essentia_mood_scan`` holds a SINGLE raw connection + cursor for the whole
run and calls ``cursor.execute(...)`` per track. Between two writes it runs the
Essentia subprocess, which can take up to ``per_file_timeout`` seconds, so the
connection sits idle for long stretches. If PostgreSQL drops it — a server
restart, a network blip, or ``idle_in_transaction_session_timeout`` — psycopg2
closes the CURSOR with the connection. The first write after that raises the
real cause ("server closed the connection unexpectedly"); from then on every
write on that same dead cursor raises only "cursor already closed".

The shipped handler caught the exception, logged it and ``continue``d, so a
single dropped connection silently skipped EVERY remaining track while the
progress counters still advanced.

WHAT THE FIX DOES
-----------------
``_update_with_reconnect`` retries the write ONCE on a fresh connection when the
error means the connection is gone, and returns the new (conn, cursor) for the
caller to rebind. One drop becomes one reconnect instead of N lost tracks.

These tests double the DBAPI objects rather than using a live database: the
defect is in the retry/rebind logic, and a genuine PostgreSQL drop cannot be
provoked on demand.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest

from services.scanning.pipelines import essentia_scanner as es

SCANNER = Path(es.__file__)


# ---------------------------------------------------------------------------
# Doubles mirroring psycopg2's observable behaviour
# ---------------------------------------------------------------------------

class CursorClosedError(Exception):
    """psycopg2.InterfaceError("cursor already closed")."""


class ConnLostError(Exception):
    """psycopg2.OperationalError("server closed the connection unexpectedly")."""


class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self.closed = False
        self.rowcount = 0
        self.statements: list[str] = []

    def execute(self, sql, params=None) -> None:
        if self.closed:
            raise CursorClosedError("cursor already closed")
        self._conn._record()
        self.statements.append(str(sql))
        self.rowcount = self._conn.rowcount_to_report

    def close(self) -> None:
        self.closed = True


class _FakeConn:
    """A connection that dies after N successful uses.

    Once dead, psycopg2 closes its cursors, so later uses of the SAME cursor
    raise "cursor already closed" rather than the original cause.
    """

    def __init__(self, dies_after: int | None = None) -> None:
        self.dies_after = dies_after
        self.uses = 0
        self.dead = False
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.rowcount_to_report = 1
        self._cursors: list[_FakeCursor] = []

    def cursor(self) -> _FakeCursor:
        c = _FakeCursor(self)
        self._cursors.append(c)
        return c

    def _record(self) -> None:
        if self.dead:
            raise CursorClosedError("cursor already closed")
        self.uses += 1
        if self.dies_after is not None and self.uses > self.dies_after:
            self.dead = True
            for c in self._cursors:
                c.closed = True
            raise ConnLostError("server closed the connection unexpectedly")

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True
        self.dead = True
        for c in self._cursors:
            c.closed = True


@pytest.fixture
def fresh_connections(monkeypatch):
    """Patch the reconnect factory so it hands out healthy connections."""
    created: list[_FakeConn] = []

    def _factory(reason: str = "") -> _FakeConn:
        conn = _FakeConn(dies_after=None)
        created.append(conn)
        return conn

    monkeypatch.setattr(es, "get_db_connection_raw", _factory)
    return created


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

class TestWhichErrorsCountAsALostConnection:
    def test_the_cascade_message_is_recognised(self) -> None:
        """'cursor already closed' must trigger recovery.

        This is the message the user actually saw. Treating only the ORIGINAL
        drop as recoverable would mean recovering nothing: the cursor is
        already dead by the time it surfaces.
        """
        assert es._is_lost_connection(CursorClosedError("cursor already closed"))

    def test_a_closed_connection_message_is_recognised(self) -> None:
        assert es._is_lost_connection(Exception("connection already closed"))

    def test_the_original_drop_is_recognised(self) -> None:
        assert es._is_lost_connection(
            ConnLostError("server closed the connection unexpectedly")
        )

    def test_an_unrelated_error_is_not_treated_as_a_lost_connection(self) -> None:
        """A NOT NULL violation must not trigger a pointless reconnect."""
        assert not es._is_lost_connection(
            Exception('null value in column "mood" violates not-null constraint')
        )


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

class TestTheWriteRecoversFromADroppedConnection:
    def test_a_lost_connection_is_retried_on_a_fresh_one(
        self, fresh_connections
    ) -> None:
        """The core regression: the write that follows a drop must succeed."""
        conn = _FakeConn(dies_after=0)      # dies on its first use
        cursor = conn.cursor()

        new_conn, new_cursor, rowcount = es._update_with_reconnect(
            conn, cursor, "UPDATE tracks SET mood = %s WHERE id = %s",
            ("happy", "t1"),
        )

        assert rowcount == 1
        assert new_conn is not conn, "must return the FRESH connection"
        assert not new_cursor.closed, "the returned cursor must be usable"
        assert len(fresh_connections) == 1, "exactly one reconnect"
        assert conn.closed, "the dead connection should be closed"

    def test_the_returned_cursor_works_for_the_NEXT_write(
        self, fresh_connections
    ) -> None:
        """This is what stops the cascade for every FOLLOWING track.

        The loop rebinds conn/cursor from the return value, so the next
        iteration must write successfully rather than raising
        "cursor already closed".
        """
        conn = _FakeConn(dies_after=0)
        cursor = conn.cursor()

        conn, cursor, _ = es._update_with_reconnect(
            conn, cursor, "UPDATE tracks SET mood = %s WHERE id = %s",
            ("happy", "t1"),
        )
        # A later, independent write on the rebound objects.
        conn2, cursor2, rowcount2 = es._update_with_reconnect(
            conn, cursor, "UPDATE tracks SET mood = %s WHERE id = %s",
            ("happy", "t2"),
        )

        assert rowcount2 == 1
        assert conn2 is conn, "no further reconnect should be needed"
        assert len(fresh_connections) == 1, "still exactly one reconnect"

    def test_a_healthy_write_never_reconnects(self, fresh_connections) -> None:
        conn = _FakeConn(dies_after=None)
        cursor = conn.cursor()

        same_conn, same_cursor, rowcount = es._update_with_reconnect(
            conn, cursor, "UPDATE tracks SET mood = %s WHERE id = %s",
            ("happy", "t1"),
        )

        assert rowcount == 1
        assert same_conn is conn
        assert same_cursor is cursor
        assert fresh_connections == [], "a working connection must be reused"
        assert conn.commits == 1

    def test_an_unrelated_error_is_NOT_retried(self, fresh_connections) -> None:
        """A real SQL error must propagate, not be masked by a reconnect."""
        conn = _FakeConn(dies_after=None)

        class _Rejecting(_FakeCursor):
            def execute(self, sql, params=None):
                raise ValueError("syntax error at or near SET")

        cursor = _Rejecting(conn)

        with pytest.raises(ValueError, match="syntax error"):
            es._update_with_reconnect(
                conn, cursor, "UPDATE tracks SET", ()
            )
        assert fresh_connections == [], "must not reconnect for a SQL error"

    def test_recovery_is_bounded(self, fresh_connections, monkeypatch) -> None:
        """If the FRESH connection also fails, give up after one retry.

        An unbounded retry would turn a genuinely broken database into an
        infinite loop inside the scan.

        Run on a DAEMON thread with ``join(timeout=)``: with the bound removed
        the call never returns, and a test that HANGS would wedge the whole
        suite (and any mutation run against it) instead of reporting a
        failure. A hung test is worse than a failing one.
        """
        conn = _FakeConn(dies_after=0)
        cursor = conn.cursor()

        def _also_broken(reason: str = "") -> _FakeConn:
            return _FakeConn(dies_after=0)   # dies immediately too

        monkeypatch.setattr(es, "get_db_connection_raw", _also_broken)

        outcome: dict[str, str] = {}

        def _call() -> None:
            try:
                es._update_with_reconnect(
                    conn, cursor, "UPDATE tracks SET mood = %s WHERE id = %s",
                    ("happy", "t1"),
                )
                outcome["result"] = "returned"
            except ConnLostError:
                outcome["result"] = "raised"
            except BaseException as exc:            # noqa: BLE001
                outcome["result"] = f"unexpected:{type(exc).__name__}"

        worker = threading.Thread(target=_call, daemon=True)
        worker.start()
        worker.join(timeout=5.0)

        assert not worker.is_alive(), (
            "recovery is NOT bounded - a permanently broken connection made "
            "the write retry forever instead of giving up"
        )
        assert outcome.get("result") == "raised", (
            f"expected the second failure to propagate, got {outcome!r}"
        )


# ---------------------------------------------------------------------------
# Wiring: the scan loop must actually USE the recovery, and count honestly
# ---------------------------------------------------------------------------

class TestTheScanLoopIsWiredToTheRecovery:
    def test_the_update_goes_through_the_reconnecting_helper(self) -> None:
        source = SCANNER.read_text(encoding="utf-8")
        assert "_update_with_reconnect(" in source, (
            "the scan's per-track UPDATE must run through the helper, or a "
            "dropped connection still loses every remaining track"
        )

    def test_the_loop_rebinds_conn_and_cursor(self) -> None:
        """The retry runs on a NEW connection; without rebinding, the next
        iteration (and the final commit/close) would use the dead one."""
        source = SCANNER.read_text(encoding="utf-8")
        assert re.search(
            r"conn,\s*cursor,\s*_rowcount\s*=\s*_update_with_reconnect\(",
            source,
        ), "the return value must be assigned back to conn/cursor"

    def test_updated_tracks_is_counted_after_the_write_not_before(self) -> None:
        """A failed save must not be reported as updated.

        The shipped code incremented before the attempt, so the progress file
        and the final summary claimed tracks were updated while nothing was
        written.
        """
        source = SCANNER.read_text(encoding="utf-8")
        call = source.index("_update_with_reconnect(")
        increment = source.index("updated_tracks += 1")
        assert increment > call, (
            "updated_tracks must be incremented only AFTER the write succeeds"
        )
        assert re.search(r"if _rowcount > 0:\s*\n\s*updated_tracks \+= 1", source), (
            "the increment must be gated on the write actually affecting a row"
        )
