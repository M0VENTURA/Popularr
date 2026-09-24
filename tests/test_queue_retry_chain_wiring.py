"""Regression tests: failures return to the queue and the retry chain is wired.

Product rule: a track that fails to search or download must go back to a
*pending* state and return to the queue automatically — it must not be parked.
The audit that produced these tests confirmed the search/backoff half was
sound, so most of this file pins existing behaviour as a contract; the tests
that matter are the ones asserting items are actually *re-picked* after a
failure, which is the property a user observes.

The chain under test::

    failure → mark_failed / _schedule_search_retry   (writes status + window)
            → get_ready_for_processing               (picks it back up)
            → process_next_batch                     (hands it to a processor)

Two distinct failure classes exist and both must return to the queue:

* **Search failures** (``no_results`` / ``no_qualifying_result``) park in
  ``backed_off`` with an escalating window via ``_schedule_search_retry``.
* **Download failures** (``peer_no_free_slots`` / ``download_failed``) and
  exceptions call ``mark_failed``, which returns the row to ``queued`` (or
  preserves an existing ``backed_off`` / ``pending_release`` window).

``'failed'`` is NOT unreachable — ``cleanup_stuck_items`` parks items stuck in
``downloading`` for 6h there, and cancelling a download writes it directly.
``requeue_due_failed_items`` is what brings those back.
"""

from __future__ import annotations

import os
import tempfile

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from services.queue import queue_orchestrator


# ---------------------------------------------------------------------------
# Harness: a real SQLite download_queue
# ---------------------------------------------------------------------------

@pytest.fixture()
def queue_env(monkeypatch):
    """Fresh SQLite DB with the columns the retry queries touch."""
    tmp = tempfile.mkdtemp()
    engine = create_engine(f"sqlite:///{os.path.join(tmp, 'test.db')}")
    sess_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE download_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                artist TEXT,
                title TEXT,
                album TEXT,
                status TEXT DEFAULT 'queued',
                source TEXT DEFAULT 'soulseek',
                priority INTEGER DEFAULT 5,
                file_path TEXT,
                music_file_path TEXT,
                retry_count INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 5,
                retry_delay_minutes INTEGER DEFAULT 30,
                failure_reason TEXT,
                next_retry_at TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """))

    class _Session:
        def __init__(self, session):
            self._session = session

        def execute(self, *args, **kwargs):
            return self._session.execute(*args, **kwargs)

        def commit(self):
            self._session.commit()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, *exc):
            if exc_type is None:
                self._session.commit()
            self._session.close()
            return False

    monkeypatch.setattr(
        "db.repositories.queue.db_session",
        lambda *a, **kw: _Session(sess_factory()),
    )
    # ``queue_processing_service`` binds ``db_session`` at module import, so it
    # needs its own patch — otherwise its helpers hit the real engine.
    monkeypatch.setattr(
        "services.queue.queue_processing_service.db_session",
        lambda *a, **kw: _Session(sess_factory()),
    )
    return engine


class _RecordingSession:
    """Captures statements instead of executing them.

    Required for ``mark_failed``, whose UPDATE uses Postgres-only
    ``GREATEST(...)`` and ``INTERVAL '1 minute'`` (the repo is Postgres-only by
    design), so it cannot run against the SQLite test engine. The existing
    retry tests assert on the statement text for the same reason.
    """

    def __init__(self):
        self.statements: list[tuple[str, dict]] = []

    def execute(self, statement, params=None):
        self.statements.append((str(statement), dict(params or {})))
        return _EmptyResult()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _EmptyResult:
    def fetchone(self):
        return None

    def fetchall(self):
        return []


@pytest.fixture()
def recording_session(monkeypatch):
    """Point every ``mark_failed`` db_session at a recorder."""
    recorder = _RecordingSession()
    monkeypatch.setattr(
        "db.repositories.queue.db_session", lambda *a, **kw: recorder
    )
    return recorder


def _insert(engine, **row):
    columns = ", ".join(row)
    placeholders = ", ".join(f":{c}" for c in row)
    with engine.begin() as conn:
        conn.execute(
            text(f"INSERT INTO download_queue ({columns}) VALUES ({placeholders})"),
            row,
        )


def _get(engine, queue_id: int):
    with engine.begin() as conn:
        return conn.execute(
            text("SELECT * FROM download_queue WHERE id = :id"), {"id": queue_id}
        ).fetchone()


# ---------------------------------------------------------------------------
# 1. A download failure returns the item to the queue
# ---------------------------------------------------------------------------

class TestDownloadFailureReturnsToQueue:

    def test_mark_failed_requeues_the_row(self, recording_session):
        """Asserted on the statement, because the UPDATE is Postgres-specific.

        The load-bearing part is the ``status`` CASE: an unscheduled row goes
        back to ``'queued'`` rather than staying put, and the statement never
        targets the terminal ``'failed'`` status.
        """
        from db.repositories.queue import mark_failed

        result = mark_failed(1, "peer_no_free_slots")

        assert result == {"success": True, "id": 1}
        sql, params = recording_session.statements[0]
        assert "'queued'" in sql, "a failed download must return to the queue"
        assert "'failed'" not in sql, "a transient failure must not terminate the item"
        assert "backed_off" in sql
        assert "pending_release" in sql
        assert "next_retry_at" in sql, "a retry window must be scheduled"
        assert "failure_reason = :reason" in sql
        assert params["qid"] == 1
        assert params["reason"] == "peer_no_free_slots"

    def test_mark_failed_preserves_an_existing_backoff(self, queue_env):
        """The CASE must keep an existing scheduled status, and ``GREATEST``
        must keep the later window — a 24h search backoff must not be clobbered
        to the short default delay by a later failure."""
        from db.repositories.queue import mark_failed
        from db.repositories.queue import _queue_retry_defaults

        sql = None
        delay = _queue_retry_defaults()[0]
        assert delay >= 1  # the default window is a real positive interval

        # Capture without executing (Postgres-only syntax).
        captured = {}

        class _Rec:
            def execute(self, statement, params=None):
                captured["sql"] = str(statement)
                captured["params"] = dict(params or {})
                return _EmptyResult()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        import db.repositories.queue as queue_repo

        original = queue_repo.db_session
        queue_repo.db_session = lambda *a, **kw: _Rec()
        try:
            mark_failed(1, "peer_no_free_slots")
        finally:
            queue_repo.db_session = original

        sql = captured["sql"]
        assert "WHEN status IN ('backed_off', 'pending_release') THEN status" in sql, (
            "a scheduled status must survive a later failure"
        )
        # The window only ever moves forward.
        assert "GREATEST" in sql
        assert "COALESCE(next_retry_at, CURRENT_TIMESTAMP)" in sql

    def test_mark_failed_increments_retry_count(self, recording_session):
        from db.repositories.queue import mark_failed

        mark_failed(1, "download_failed")

        sql = recording_session.statements[0][0]
        assert "retry_count = retry_count + 1" in sql

    def test_mark_failed_never_writes_the_terminal_status(self, recording_session):
        """The only routes that park an item as 'failed' are the cleanup and
        cancel paths — a transient failure must not terminate the item."""
        from db.repositories.queue import mark_failed

        mark_failed(1, "download_failed")

        sql = recording_session.statements[0][0]
        assert "'failed'" not in sql
        # And the reason is recorded, so the UI can explain the retry.
        assert "failure_reason" in sql


# ---------------------------------------------------------------------------
# 2. Pending items are genuinely re-picked
# ---------------------------------------------------------------------------

class TestPendingItemsAreRePicked:

    def test_a_requeued_row_is_selected_again(self, queue_env):
        """The whole retry feature reduces to this: the exact row state
        ``mark_failed`` produces (``status='queued'``, no file yet) must be
        visible to the worker again.

        ``mark_failed`` itself cannot be executed here (Postgres-only syntax),
        so the state it writes is reproduced directly — the assertion is on the
        pick-up gate, which is the half that decides whether a retry happens.
        """
        from db.repositories.queue import get_ready_for_processing

        # What mark_failed writes for an unscheduled failure.
        _insert(
            queue_env,
            id=1,
            artist="A",
            title="T",
            status="queued",
            file_path=None,
            retry_count=1,
            next_retry_at="2000-01-01 00:00:00",
        )

        ready = get_ready_for_processing(limit=10)

        assert [r["id"] for r in ready] == [1]

    def test_a_backed_off_row_waits_for_its_window(self, queue_env):
        """Backed off into the future, the row must NOT be picked early."""
        from db.repositories.queue import get_ready_for_processing

        _insert(
            queue_env,
            id=1,
            artist="A",
            title="T",
            status="backed_off",
            next_retry_at="2099-01-01 00:00:00",
        )

        assert get_ready_for_processing(limit=10) == []

    def test_a_backed_off_row_is_picked_once_due(self, queue_env):
        from db.repositories.queue import get_ready_for_processing

        _insert(
            queue_env,
            id=1,
            artist="A",
            title="T",
            status="backed_off",
            next_retry_at="2000-01-01 00:00:00",  # long past
        )

        assert [r["id"] for r in get_ready_for_processing(limit=10)] == [1]

    def test_a_pending_release_row_is_picked_once_due(self, queue_env):
        from db.repositories.queue import get_ready_for_processing

        _insert(
            queue_env,
            id=1,
            artist="A",
            title="T",
            status="pending_release",
            next_retry_at="2000-01-01 00:00:00",
        )

        assert [r["id"] for r in get_ready_for_processing(limit=10)] == [1]

    def test_rows_that_already_have_a_file_are_not_researched(self, queue_env):
        """An item with a downloaded file belongs to the completion path, not
        the search path."""
        from db.repositories.queue import get_ready_for_processing

        _insert(
            queue_env,
            id=1,
            artist="A",
            title="T",
            status="queued",
            file_path="/downloads/album/01.flac",
        )

        assert get_ready_for_processing(limit=10) == []

    def test_local_source_rows_are_not_researched(self, queue_env):
        """Disk-discovered rows are matched locally, never searched on Soulseek."""
        from db.repositories.queue import get_ready_for_processing

        _insert(queue_env, id=1, artist="A", title="T", status="queued", source="discovered")

        assert get_ready_for_processing(limit=10) == []


# ---------------------------------------------------------------------------
# 3. Permanently-parked items come back too
# ---------------------------------------------------------------------------

class TestParkedItemsAreRecovered:
    """``cleanup_stuck_items`` parks items stuck in 'downloading' for 6h as
    'failed'; cancelling a download writes 'failed' directly. Both must be
    recoverable, otherwise the 'failed' status is a black hole."""

    def test_a_due_failed_row_is_requeued(self, queue_env):
        from db.repositories.queue import requeue_due_failed_items

        _insert(
            queue_env,
            id=1,
            artist="A",
            title="T",
            status="failed",
            retry_count=0,
            next_retry_at=None,
        )

        requeued = requeue_due_failed_items(limit=50)

        assert [r["id"] for r in requeued] == [1]
        row = _get(queue_env, 1)
        assert row.status == "queued"
        assert row.retry_count == 1

    def test_a_failed_row_with_a_future_window_is_left_alone(self, queue_env):
        from db.repositories.queue import requeue_due_failed_items

        _insert(
            queue_env,
            id=1,
            artist="A",
            title="T",
            status="failed",
            next_retry_at="2099-01-01 00:00:00",
        )

        assert requeue_due_failed_items(limit=50) == []
        assert _get(queue_env, 1).status == "failed"

    def test_requeue_all_failed_returns_them_to_the_queue(self, queue_env):
        """The UI's "Retry all failed" must move every failed row."""
        from services.queue.queue_processing_service import queue_retry_all_failed

        _insert(queue_env, id=1, artist="A", title="T", status="failed")
        _insert(queue_env, id=2, artist="B", title="U", status="failed")
        _insert(queue_env, id=3, artist="C", title="V", status="completed")

        result = queue_retry_all_failed()

        assert result["success"] is True
        assert result["updated_count"] == 2
        assert _get(queue_env, 1).status == "queued"
        assert _get(queue_env, 2).status == "queued"
        assert _get(queue_env, 3).status == "completed", "completed rows are untouched"

    def test_manual_requeue_clears_the_backoff_window(self, queue_env):
        """A user clicking Requeue wants it to run NOW, not in 24h."""
        from db.repositories.queue import requeue_queue_item

        _insert(
            queue_env,
            id=1,
            artist="A",
            title="T",
            status="failed",
            retry_count=4,
            next_retry_at="2099-01-01 00:00:00",
        )

        requeue_queue_item(1)

        row = _get(queue_env, 1)
        assert row.status == "queued"
        assert row.retry_count == 0
        assert row.next_retry_at is None, "a manual requeue must clear the window"

    def test_manual_requeue_does_not_research_a_row_with_a_file(self, queue_env):
        from db.repositories.queue import requeue_queue_item

        _insert(
            queue_env,
            id=1,
            artist="A",
            title="T",
            status="failed",
            file_path="/downloads/album/01.flac",
        )

        requeue_queue_item(1)

        assert _get(queue_env, 1).status == "unmatched"

    def test_a_manually_requeued_row_is_immediately_selectable(self, queue_env):
        """End-to-end for the manual path: requeue → visible to the worker."""
        from db.repositories.queue import get_ready_for_processing, requeue_queue_item

        _insert(
            queue_env,
            id=1,
            artist="A",
            title="T",
            status="failed",
            next_retry_at="2099-01-01 00:00:00",
        )
        assert get_ready_for_processing(limit=10) == []

        requeue_queue_item(1)

        assert [r["id"] for r in get_ready_for_processing(limit=10)] == [1]


# ---------------------------------------------------------------------------
# 4. Backoff never abandons the item
# ---------------------------------------------------------------------------

class TestSearchBackoffNeverAbandons:

    def test_backoff_caps_at_the_last_tier_and_never_returns_none(self):
        from services.downloads.download_pipeline_service import _backoff_hours_for

        # Even absurd retry counts keep producing a window.
        for count in (0, 1, 2, 3, 10, 100, 10_000):
            hours = _backoff_hours_for(count)
            assert isinstance(hours, int)
            assert hours > 0

    def test_backoff_is_monotonic_then_flat(self):
        from services.downloads.download_pipeline_service import _backoff_hours_for

        hours = [_backoff_hours_for(i) for i in range(6)]
        assert hours == sorted(hours), "backoff must not shrink as attempts grow"
        assert hours[-1] == hours[-2] == hours[-3], "and must settle at a cap"

    def test_scheduling_a_retry_never_uses_the_terminal_status(self, monkeypatch):
        """After many search misses the item still returns to the queue."""
        from services.downloads import download_pipeline_service as dps

        scheduled = {}
        monkeypatch.setattr(
            "db.repositories.queue.schedule_queue_retry",
            lambda qid, status, next_retry_at, reason="": scheduled.update(
                {"qid": qid, "status": status, "reason": reason}
            ),
        )
        monkeypatch.setattr(dps, "_resolve_item_release_date", lambda item: None)
        monkeypatch.setattr(dps, "log_unified", lambda *a, **k: None)
        monkeypatch.setattr(dps, "_log_queue_event", lambda *a, **k: None)

        dps._schedule_search_retry(5, {"artist": "A", "title": "T", "retry_count": 99}, "no_results")

        assert scheduled["status"] not in ("failed", "cancelled", "removed")
        assert scheduled["status"] == "backed_off"


# ---------------------------------------------------------------------------
# 5. The worker actually runs the retry hooks
# ---------------------------------------------------------------------------

class TestWorkerRunsTheRetryHooks:

    def test_retry_hook_is_registered(self):
        """``retry_due_items`` must be in the maintenance candidates, otherwise
        parked 'failed' rows are never recovered by the background worker."""
        names = {
            (candidate.module, candidate.function)
            for candidate in queue_orchestrator.MAINTENANCE_CANDIDATES
        }
        assert (
            "services.downloads.download_retry_service",
            "retry_due_items",
        ) in names

    def test_completion_hook_is_registered(self):
        """``check_completed_downloads`` is what turns a finished transfer into
        an imported track — it must be a maintenance hook."""
        names = {
            (candidate.module, candidate.function)
            for candidate in queue_orchestrator.MAINTENANCE_CANDIDATES
        }
        assert (
            "services.downloads.download_scan_service",
            "check_completed_downloads",
        ) in names

    def test_process_cycle_runs_maintenance_by_default(self, monkeypatch):
        """The scheduled tick calls ``process_cycle()`` with no arguments, so
        the default must run the hooks."""
        called = {"maintenance": False, "batches": 0}

        monkeypatch.setattr(
            queue_orchestrator,
            "run_maintenance",
            lambda: (called.__setitem__("maintenance", True) or {"success": True}, 200),
        )
        monkeypatch.setattr(
            queue_orchestrator,
            "process_next_batch",
            lambda *a, **k: (called.__setitem__("batches", called["batches"] + 1) or {"success": True}, 200),
        )

        queue_orchestrator.process_cycle()

        assert called["maintenance"] is True, "retries never run without the hooks"
        assert called["batches"] == 1

    def test_the_processor_resolves(self):
        """A missing processor would make every batch a no-op, so the chain's
        first link must exist."""
        processor = queue_orchestrator._resolve_processor()
        assert callable(processor), "no queue processor could be resolved"

    def test_process_next_batch_reports_a_missing_processor(self, monkeypatch):
        """Fail loudly rather than silently processing nothing."""
        monkeypatch.setattr(queue_orchestrator, "_resolve_processor", lambda: None)

        payload, status = queue_orchestrator.process_next_batch(use_cycle_lock=False)

        assert status >= 400
        assert payload.get("success") is False


# ---------------------------------------------------------------------------
# 6. The 'failed' status is reachable and its retry route matches
# ---------------------------------------------------------------------------

class TestFailedStatusIsReachable:
    """D4 in the audit report claimed 'failed' was a black hole. It is not:
    the cleanup hook and the cancel path both write it, and the retry
    surfaces query exactly that status. These tests pin the join."""

    def test_cleanup_parks_long_stuck_downloads_as_failed(self):
        import inspect

        from services.queue import queue_cleanup_service as qcs

        source = inspect.getsource(qcs.cleanup_stuck_items)
        assert "SET status = 'failed'" in source
        assert "'Stuck in downloading state'" in source

    def test_the_failed_retry_surfaces_query_the_failed_status(self):
        import inspect

        from db.repositories import queue as queue_repo
        from services.queue import queue_processing_service as qps

        assert "status = 'failed'" in inspect.getsource(queue_repo.requeue_due_failed_items)
        assert "status = 'failed'" in inspect.getsource(queue_repo.get_failed_queue)
        assert "status = 'failed'" in inspect.getsource(qps.queue_retry_all_failed)

    def test_cancelling_a_download_parks_it_as_failed(self):
        import inspect

        from services.queue import queue_processing_service as qps

        assert 'status="failed"' in inspect.getsource(qps.queue_cancel)
