"""A timed-out maintenance hook must not be started a second time.

REPORTED
--------
``check_completed_downloads`` and ``cleanup_stale_downloads`` timed out at
120 s on every cycle while ``downloading=15`` stayed saturated. The old wrapper
created a NEW executor per hook and shut it down with ``wait=False`` on timeout;
a running hook cannot be cancelled, so its thread kept scanning while the next
cycle could submit the same hook again.
"""

from __future__ import annotations

import time

from services.queue import queue_orchestrator as orch


def test_a_timed_out_hook_is_not_restarted_while_it_is_running(monkeypatch):
    starts: list[float] = []

    def _slow_hook():
        starts.append(time.monotonic())
        time.sleep(0.3)
        return {"success": True}

    monkeypatch.setattr(orch, "_iter_maintenance_hooks", lambda: [_slow_hook])
    monkeypatch.setattr(orch, "_MAINTENANCE_HOOK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(orch, "_MAINTENANCE_HOOK_RETRY_SECONDS", 0.0)
    orch._RUNNING_MAINTENANCE_HOOKS.clear()
    orch._STUCK_MAINTENANCE_HOOKS.clear()

    try:
        first, _ = orch.run_maintenance()
        assert len(starts) == 1
        assert first["failures"] == 1

        # The first attempt is still sleeping. It must NOT be submitted again.
        second, _ = orch.run_maintenance()
        assert len(starts) == 1, (
            "a still-running timed-out hook was started again — scans overlap"
        )
        assert "still active" in second["results"][0]["error"]

        # Let the background worker finish so the shared executor is idle.
        for future in list(orch._RUNNING_MAINTENANCE_HOOKS.values()):
            future.result(timeout=2)
    finally:
        orch._RUNNING_MAINTENANCE_HOOKS.clear()
        orch._STUCK_MAINTENANCE_HOOKS.clear()
