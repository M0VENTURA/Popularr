"""Deadlock retry: the unit of work is retried, not one statement.

REPORTED (PostgreSQL)::

    ERROR: deadlock detected
    Process 1413: INSERT INTO tracks (…) … ON CONFLICT (id) DO UPDATE …
    Process 1378: UPDATE tracks SET musicbrainz_album_mbid = …
                  WHERE … album_artist = 'Adema' AND album = 'Planets'

Several writers legitimately target "every row of one album" by predicate, and
take row locks in whatever order the planner scans. Two such writers can cycle.
Postgres resolves it by killing ONE session — so an occasional deadlock in the
server log is expected; what matters is whether the application RETRIES the
victim. Three of the writers did not.
"""

from __future__ import annotations

import pytest

from db.engine import _is_deadlock_error, run_with_deadlock_retry


class _FakeDeadlock(Exception):
    def __init__(self):
        super().__init__("deadlock detected")


class _FakeWrap:
    """Mimics SQLAlchemy's ``exc.orig`` chain with a DBAPI ``pgcode``."""

    def __init__(self, pgcode):
        self.pgcode = pgcode
        self.__cause__ = None

    def __str__(self):
        return "some driver error"


class _Wrapped(Exception):
    def __init__(self, pgcode):
        super().__init__("(raised) ")
        self.orig = _FakeWrap(pgcode)


class TestDetection:
    def test_a_deadlock_message_is_recognised(self):
        assert _is_deadlock_error(_FakeDeadlock()) is True

    def test_a_pgcode_40P01_is_recognised(self):
        assert _is_deadlock_error(_Wrapped("40P01")) is True

    def test_a_plain_error_is_not_a_deadlock(self):
        assert _is_deadlock_error(ValueError("nope")) is False

    def test_a_serialisation_failure_pgcode_is_not(self):
        # 40001 is retryable differently; this helper must not catch it.
        assert _is_deadlock_error(_Wrapped("40001")) is False


class TestRetry:
    def test_a_deadlock_is_retried_and_the_work_succeeds(self):
        calls = {"n": 0}

        def _work():
            calls["n"] += 1
            if calls["n"] < 3:
                raise _FakeDeadlock()
            return "ok"

        assert run_with_deadlock_retry(_work, attempts=4) == "ok"
        assert calls["n"] == 3

    def test_a_persistent_deadlock_raises_after_the_attempts(self):
        calls = {"n": 0}

        def _work():
            calls["n"] += 1
            raise _FakeDeadlock()

        with pytest.raises(_FakeDeadlock):
            run_with_deadlock_retry(_work, attempts=3)
        assert calls["n"] == 3, "it must not loop forever"

    def test_a_non_deadlock_is_not_retried(self):
        calls = {"n": 0}

        def _work():
            calls["n"] += 1
            raise ValueError("real bug")

        with pytest.raises(ValueError):
            run_with_deadlock_retry(_work, attempts=4)
        assert calls["n"] == 1, "only deadlocks are retried"
