"""The queue must never sit silently dead.

REPORTED
--------
After a restart the activity log showed only ``event='queued'`` entries — no
``searching`` attempts — while the queue worker was "started". Two drivers
(standalone worker + APScheduler job) share a NON-EXPIRING advisory lock, so a
hung scheduler cycle blocks the standalone worker for ever. And when every
in-flight slot is busy, the worker returned ``throttled`` at DEBUG level: the
UI/activity log looked like nothing was happening at all.
"""
from __future__ import annotations

import inspect
import time

import services.queue.queue_orchestrator as orch
import services.scheduler.scheduler_service as scheduler_service


class TestOnlyOneQueueDriverWhenTheEntrypointOwnsOne:
    def test_the_flag_is_recognised(self, monkeypatch):
        for value in ("1", "true", "YES", "on"):
            monkeypatch.setenv("SPTNR_QUEUE_PROCESSOR_MANAGED_EXTERNALLY", value)
            assert scheduler_service._queue_processor_managed_externally() is True

        monkeypatch.delenv("SPTNR_QUEUE_PROCESSOR_MANAGED_EXTERNALLY", raising=False)
        assert scheduler_service._queue_processor_managed_externally() is False

    def test_the_scheduler_job_is_disabled_in_that_mode(self):
        source = inspect.getsource(
            scheduler_service._register_default_jobs
        )
        assert "_queue_processor_managed_externally()" in source
        assert '_remove_job("download_queue_processor")' in source, (
            "the redundant scheduler driver must be removed when the "
            "standalone worker is managed externally"
        )


class TestAHungMaintenanceHookCannotHoldTheCycleLock:
    def test_a_hung_hook_is_skipped(self, monkeypatch):
        def _slow_hook():
            time.sleep(1.0)
            return {"success": True}

        monkeypatch.setattr(
            orch, "_iter_maintenance_hooks", lambda: iter([_slow_hook])
        )
        monkeypatch.setattr(orch, "_MAINTENANCE_HOOK_TIMEOUT_SECONDS", 0.05)

        started = time.monotonic()
        payload, _status = orch.run_maintenance()
        elapsed = time.monotonic() - started

        assert elapsed < 0.5, (
            f"run_maintenance waited for a hung hook ({elapsed:.2f}s) — it "
            "holds the cross-process queue cycle lock while doing so"
        )
        assert payload["failures"] == 1
        assert "timed out" in payload["results"][0]["error"]

        # The next cycle must NOT start another thread for the same hook.
        second_started = time.monotonic()
        second_payload, _ = orch.run_maintenance()
        assert time.monotonic() - second_started < 0.2
        assert "skipped" in second_payload["results"][0]["error"]


class TestSaturationIsVisible:
    def test_a_full_queue_reports_why_it_cannot_start(self, monkeypatch):
        orch._LAST_INFLIGHT_WARNING_AT = 0.0
        monkeypatch.setattr(
            orch.queue_repository,
            "get_queue_status_counts",
            lambda: {"searching": 2, "downloading": 3},
        )
        seen: list[tuple[str, str]] = []

        import services.queue.queue_diagnostics_service as diag

        monkeypatch.setattr(
            diag,
            "log_queue_event",
            lambda event_type, message, **kwargs: seen.append((event_type, message)),
        )

        orch._log_in_flight_saturation(max_in_flight=5, ready=4)

        assert seen, "a full in-flight queue with ready work logged nothing"
        event_type, message = seen[0]
        assert event_type == "throttled"
        assert "4 queue item(s) ready" in message
        assert "searching=2" in message
        assert "downloading=3" in message

    def test_the_warning_is_rate_limited(self, monkeypatch):
        orch._LAST_INFLIGHT_WARNING_AT = time.monotonic()
        monkeypatch.setattr(
            orch.queue_repository,
            "get_queue_status_counts",
            lambda: {"searching": 5, "downloading": 0},
        )
        seen: list[tuple[str, str]] = []

        import services.queue.queue_diagnostics_service as diag

        monkeypatch.setattr(
            diag,
            "log_queue_event",
            lambda event_type, message, **kwargs: seen.append((event_type, message)),
        )

        orch._log_in_flight_saturation(max_in_flight=5, ready=1)
        assert seen == [], "the saturation warning must not fire every cycle"
