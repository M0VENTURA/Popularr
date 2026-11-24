"""Only ONE Navidrome rescan may run across the whole deployment.

REPORTED (from the log)
-----------------------
Repeated ``getScanStatus`` ``ReadTimeout`` warnings, "Navidrome rescan already
running — coalesced into follow-up" over and over, and scans completing with
``count=7737`` then ``count=7896``.

WHY
---
``navidrome_rescan_service`` coalesced with a module-level ``threading.Lock`` and
a ``_state`` dict — BOTH PER-PROCESS. The app runs 4 gunicorn workers by default
(``SPTNR_GUNICORN_WORKERS``), so each worker had its own "running" flag and its
own thread. Four workers could hold four concurrent full-library rescans against
one Navidrome, which is what starved ``getScanStatus`` and produced the timeouts
(the two different ``count=`` values are two workers' scans finishing).

The guard is now a PostgreSQL advisory lock on a dedicated connection: it is held
for the life of the CONNECTION and released automatically if the process dies,
which an in-memory flag cannot promise.
"""

from __future__ import annotations

import inspect

import pytest

from services.scanning import navidrome_rescan_service as svc


class TestTheGuardIsCrossProcess:
    def test_it_uses_a_postgres_advisory_lock(self):
        src = inspect.getsource(svc._claim_rescan_lock)
        assert "pg_try_advisory_lock" in src, (
            "the rescan guard is still in-process only, so each gunicorn worker "
            "can start its own scan"
        )

    def test_the_lock_name_is_scoped(self):
        assert svc._RESCAN_LOCK_NAME == "navidrome_rescan"

    def test_it_holds_a_dedicated_connection(self):
        """⚠️ A POOLED connection can be handed back mid-scan, which releases the
        lock. ``get_db_connection_raw`` is the house idiom for exactly this."""
        src = inspect.getsource(svc._claim_rescan_lock)
        assert "get_db_connection_raw" in src

    def test_it_releases_and_closes(self):
        src = inspect.getsource(svc._release_rescan_lock)
        assert "pg_advisory_unlock" in src
        assert "close" in src

    def test_the_worker_takes_it_before_scanning(self):
        src = inspect.getsource(svc._worker)
        lock_at = src.index("_claim_rescan_lock()")
        run_at = src.index("_run_once(reason)")
        assert lock_at < run_at, "the worker scans before claiming the lock"

    def test_the_worker_holds_it_across_the_whole_loop(self):
        """The rate-limit sleep must be INSIDE the lock, or a second process
        could start the deferred follow-up."""
        src = inspect.getsource(svc._worker)
        sleep_at = src.index("sleep(remaining)")
        release_at = src.index("_release_rescan_lock(_lock_conn)")
        assert sleep_at < release_at, (
            "the lock is released before the cooldown, so another worker can "
            "start a duplicate scan"
        )


class TestItDegradesSafely:
    def test_sqlite_still_scans(self, monkeypatch):
        """⚠️ A single-process test suite needs no cross-process guard, and
        refusing to scan there would be wrong."""
        class _Conn:
            def close(self):
                pass

        monkeypatch.setattr(
            "db.utils.get_db_connection_raw", lambda reason="": _Conn(), raising=False
        )
        monkeypatch.setattr(
            "db.utils.is_postgres_connection", lambda c: False, raising=False
        )
        may_scan, lock_conn = svc._claim_rescan_lock()
        assert may_scan is True
        assert lock_conn is None

    def test_a_held_lock_means_do_not_scan(self, monkeypatch):
        class _Cur:
            def execute(self, *a, **k):
                pass

            def fetchone(self):
                return (False,)

        class _Conn:
            def cursor(self):
                return _Cur()

            def commit(self):
                pass

            def close(self):
                pass

        monkeypatch.setattr(
            "db.utils.get_db_connection_raw", lambda reason="": _Conn(), raising=False
        )
        monkeypatch.setattr(
            "db.utils.is_postgres_connection", lambda c: True, raising=False
        )
        may_scan, lock_conn = svc._claim_rescan_lock()
        assert may_scan is False, (
            "another process holds the rescan lock but this worker decided to "
            "scan anyway"
        )
        assert lock_conn is None
