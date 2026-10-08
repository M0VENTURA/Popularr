"""The queue cycle's maintenance pass must run under the cycle lock.

REPORTED
--------
> Fix Duplicate Logger Output — double `[QUEUE] Checking X completed
> download(s)` lines indicate a Python `logging` configuration issue where a
> handler is either registered twice, or child logs are incorrectly propagating
> to the root handler.

The logging config was already correct (`popularr.unified` has ONE handler and
`propagate: False` in dictConfig, re-asserted at emit time), so `propagate =
False` would have been a no-op. The real cause was **execution, not logging**:

* `services/queue/queue_lock.py` documents that the standalone ``queue_worker``
  process (started by ``entrypoint.sh`` and ``popularr-queue-worker.service``)
  and the APScheduler ``download_queue_processor`` thread **both dispatch queue
  items independently**.
* `process_cycle()` ran ``run_maintenance()`` **before** taking the lock, and
  only ``process_next_batch()`` took it. So BOTH drivers executed every
  maintenance hook — including ``check_completed_downloads``, whose
  ``[QUEUE] Checking N completed download(s)`` line therefore printed twice per
  interval.
* It is not merely cosmetic: that hook moves files and writes rows, so two
  processes could reconcile the same transfer concurrently — exactly the hazard
  the lock module exists for.

These tests pin the FIX: maintenance only ever runs while the lock is held, and
a contended lock skips the whole cycle.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _wire(monkeypatch, *, acquired: bool, calls: list[str]):
    """Replace the lock, the maintenance hooks and the batch with recorders."""
    import services.queue.queue_orchestrator as orch

    @contextmanager
    def _lock(**_kwargs):
        calls.append("lock")
        yield acquired

    def _maintenance():
        calls.append("maintenance")
        return {"hooks": 1, "failures": 0}, 200

    def _batch(*args, **kwargs):
        calls.append(f"batch:{kwargs.get('use_cycle_lock')!r}")
        return (
            {"total": 0, "processed": 0, "succeeded": 0, "skipped": 0, "failed": 0},
            200,
        )

    monkeypatch.setattr("services.queue.queue_lock.queue_cycle_lock", _lock)
    monkeypatch.setattr(orch, "run_maintenance", _maintenance)
    monkeypatch.setattr(orch, "process_next_batch", _batch)
    return orch


class TestMaintenanceRunsUnderTheCycleLock:
    def test_the_lock_is_taken_before_maintenance_runs(self, monkeypatch):
        calls: list[str] = []
        orch = _wire(monkeypatch, acquired=True, calls=calls)

        payload, status = orch.process_cycle()

        assert status == 200
        assert calls[0] == "lock", (
            "the lock must be taken first, or both drivers run maintenance "
            f"concurrently. Order: {calls}"
        )
        assert calls.index("maintenance") > calls.index("lock")
        assert payload["maintenance"] == {"hooks": 1, "failures": 0}, (
            "the maintenance payload must still reach the response"
        )

    def test_the_batch_does_not_take_the_lock_a_second_time(self, monkeypatch):
        """Taking it twice inside one cycle waits out the whole attempt budget
        and then reports the cycle as throttled — a silent self-DDoS."""
        calls: list[str] = []
        orch = _wire(monkeypatch, acquired=True, calls=calls)

        orch.process_cycle()

        assert "batch:False" in calls, (
            f"process_next_batch must be told not to re-acquire: {calls}"
        )

    def test_a_contended_lock_skips_maintenance(self, monkeypatch):
        """THE DUPLICATE-LINE GUARD.

        Another driver already owns the cycle; running maintenance here would
        print `[QUEUE] Checking N completed download(s)` a second time for the
        same interval.
        """
        calls: list[str] = []
        orch = _wire(monkeypatch, acquired=False, calls=calls)

        payload, status = orch.process_cycle()

        assert status == 200
        assert calls == ["lock"], (
            f"maintenance ran while another driver held the lock: {calls}"
        )
        assert payload.get("throttled") is True
        assert payload.get("processed") == 0

    def test_a_contended_lock_skips_the_batch_too(self, monkeypatch):
        """CONTROL — the contended path must not do half the work either."""
        calls: list[str] = []
        orch = _wire(monkeypatch, acquired=False, calls=calls)

        orch.process_cycle()

        assert not [c for c in calls if c.startswith("batch")], (
            f"the batch ran without the lock: {calls}"
        )

    def test_the_hooks_can_be_skipped_without_taking_the_lock(self, monkeypatch):
        """CONTROL — ``run_maintenance_hooks=False`` still runs the batch."""
        calls: list[str] = []
        orch = _wire(monkeypatch, acquired=True, calls=calls)

        payload, _status = orch.process_cycle(run_maintenance_hooks=False)

        assert "maintenance" not in calls
        assert [c for c in calls if c.startswith("batch")]
        assert payload["maintenance"] is None


class TestTheSuggestedFixWouldNotHaveWorked:
    """The advice's `logger.propagate = False` was already in place."""

    def test_the_unified_logger_already_propagates_nothing(self):
        source = (Path(__file__).resolve().parents[1]
                  / "helpers" / "logging_config.py").read_text(encoding="utf-8")
        # Anchor on the dictConfig ENTRY, not the first mention of the name —
        # `log_unified()` mentions it earlier and would give a window that
        # contains no dictConfig at all.
        assert '"popularr.unified": {' in source, "the dictConfig entry is missing"
        idx = source.index('"popularr.unified": {')
        window = source[idx: idx + 260]
        assert '"propagate": False' in window, (
            "popularr.unified is configured with propagate: False — the "
            "suggested fix would have been a no-op"
        )
        # and re-asserted at emit time, so a third-party reconfiguration
        # cannot switch it back on.
        assert "_lg.propagate = False" in source
