"""Tests for the Download Queue Processor status card contract.

REPORTED
--------
> Under active queue and monitor, the download processor is stuck on loading
> with a "checking processor status" that never changes, and the restart is
> greyed out.

Two independent defects, both left behind by the old_system port:

1. ``queue_processor_status()`` was a hardcoded stub — ``running=False,
   message="Queue processor status not implemented"`` — so the endpoint could
   never report "running" no matter what the UI did with it.
2. ``updateProcessorStatusCard()`` was never ported out of
   ``old_system/templates/downloads.html``, so NOTHING wrote
   ``#processorStatusBadge`` / ``#processorStatusContent`` and
   ``#restartProcessorBtn`` — which ships ``disabled`` in the markup — was
   never enabled by anything.

The two halves have to land together: a wired card reading a stub still says
"not running", and a truthful endpoint with no renderer still shows
"Checking processor status…" for ever. These tests pin both sides, plus the
heartbeat that makes the verdict real.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

LIVE_JS = ROOT / "static" / "js" / "downloads.js"
TEST_JS = ROOT / "test_site" / "static" / "js" / "pages" / "download-queue.js"
LIVE_PARTIAL = ROOT / "templates" / "components" / "search" / "_queue_status.html"
TEST_PARTIAL = (
    ROOT / "test_site" / "templates" / "components" / "search" / "_queue_status.html"
)

CARD_IDS = ("processorStatusBadge", "processorStatusContent", "restartProcessorBtn")


@pytest.fixture
def no_scheduler(monkeypatch):
    """Take the APScheduler out of the verdict.

    A running scheduler (which the app fixture may well have started) is
    INDEPENDENT evidence of a live processor, so it would mask whichever
    signal a given test is trying to observe — and in the restart tests it
    would let a button press fire a real queue cycle inside pytest.
    """
    from services.scheduler import scheduler_service as sched

    monkeypatch.setattr(sched, "_SCHEDULER", None)


@pytest.fixture
def health(no_scheduler, tmp_path, monkeypatch):
    """Give the heartbeat a private file so verdicts are hermetic."""
    path = tmp_path / "queue_processor_health.json"
    monkeypatch.setenv("QUEUE_PROCESSOR_HEALTH_FILE", str(path))
    return path


@pytest.fixture
def restart_probe(monkeypatch):
    """Record what Restart asked for instead of running a real cycle/signal."""
    from services.queue import queue_diagnostics_service as diag
    from services.queue import queue_signal

    probe = {"kicks": 0, "signals": 0}

    def _kick():
        probe["kicks"] += 1
        return {
            "kicked": True,
            "reason": "a queue cycle was started in the background",
        }

    def _signal(count=1):
        probe["signals"] += count

    monkeypatch.setattr(diag, "_kick_queue_cycle", _kick)
    monkeypatch.setattr(queue_signal, "signal_new_item", _signal)
    return probe


# ---------------------------------------------------------------------------
# The endpoint must no longer be a stub
# ---------------------------------------------------------------------------
class TestTheEndpointIsNoLongerAStub:
    def test_it_returns_a_real_payload(self, health):
        from services.queue.queue_diagnostics_service import queue_processor_status

        payload, status = queue_processor_status()

        assert status == 200
        for key in (
            "running",
            "processor_running",
            "status",
            "detail",
            "queue_stats",
            "schedule",
            "stale_after_seconds",
            "message",
        ):
            assert key in payload, f"missing {key!r} — the card renders it"

    def test_the_placeholder_message_is_gone(self, health):
        from services.queue.queue_diagnostics_service import queue_processor_status

        payload, _status = queue_processor_status()

        assert "Queue processor status not implemented" not in payload["message"]
        assert payload["status"] in {"running", "restarting", "stopped"}

    def test_the_verdict_is_never_left_ambiguous(self, health):
        """Every branch must explain itself — the card body shows this text."""
        from services.queue.queue_diagnostics_service import queue_processor_status

        payload, _status = queue_processor_status()

        assert payload["detail"], "detail must carry the evidence behind the verdict"
        assert "NOT running" in payload["message"] or "is running" in payload["message"]


# ---------------------------------------------------------------------------
# The verdict must come from evidence, not optimism
# ---------------------------------------------------------------------------
class TestTheVerdictComesFromEvidence:
    def test_a_fresh_heartbeat_reads_running(self, health):
        from services.queue.queue_diagnostics_service import queue_processor_status
        from services.queue.queue_heartbeat import record_queue_processor_cycle

        record_queue_processor_cycle("standalone-worker")

        payload, _status = queue_processor_status()

        assert payload["running"] is True
        assert payload["status"] == "running"
        assert payload["driver"] == "standalone-worker"
        assert "last queue cycle" in payload["message"]

    def test_a_stale_heartbeat_reads_stopped(self, health):
        """A worker that died 100k seconds ago must not be reported as alive."""
        from services.queue.queue_diagnostics_service import queue_processor_status
        from services.queue.queue_heartbeat import record_queue_processor_cycle

        record_queue_processor_cycle("standalone-worker")
        data = json.loads(health.read_text(encoding="utf-8"))
        data["ts"] = time.time() - 100_000
        health.write_text(json.dumps(data), encoding="utf-8")

        payload, _status = queue_processor_status()

        assert payload["running"] is False
        assert payload["status"] == "stopped"
        assert "Click Restart Processor" in payload["message"]

    def test_no_heartbeat_at_all_reads_stopped(self, health):
        from services.queue.queue_diagnostics_service import queue_processor_status

        assert not health.exists()
        payload, _status = queue_processor_status()

        assert payload["running"] is False
        assert payload["status"] == "stopped"
        assert "no queue cycle has completed" in payload["message"]

    def test_an_armed_scheduler_reads_running_before_its_first_tick(
        self, health, monkeypatch
    ):
        """The fresh-boot case: the job exists, it just has not fired yet."""
        from services.queue.queue_diagnostics_service import queue_processor_status
        from services.scheduler import scheduler_service as sched

        class _Job:
            next_run_time = None

        class _Scheduler:
            running = True

            def get_job(self, job_id):
                return _Job() if job_id == "download_queue_processor" else None

        monkeypatch.setattr(sched, "_SCHEDULER", _Scheduler())

        payload, _status = queue_processor_status()

        assert payload["running"] is True
        assert payload["status"] == "running"
        assert "scheduler job armed" in payload["message"]

    def test_a_stopped_scheduler_is_not_evidence_of_life(self, health, monkeypatch):
        """``_SCHEDULER`` can exist with its thread stopped — armed-but-dead
        must not read as Running."""
        from services.queue.queue_diagnostics_service import queue_processor_status
        from services.scheduler import scheduler_service as sched

        class _Job:
            next_run_time = None

        class _Scheduler:
            running = False

            def get_job(self, job_id):
                return _Job()

        monkeypatch.setattr(sched, "_SCHEDULER", _Scheduler())

        payload, _status = queue_processor_status()

        assert payload["running"] is False
        assert payload["status"] == "stopped"
        assert "scheduler is not running" in payload["message"]

    def test_the_staleness_window_follows_the_configured_interval(
        self, health, monkeypatch
    ):
        """A shorter configured cycle must shorten the detection window."""
        from helpers import config_helpers
        from services.queue.queue_heartbeat import stale_after_seconds

        monkeypatch.setattr(
            config_helpers, "get_queue_worker_config", lambda: {"interval_seconds": 10}
        )
        # Floored so one slow cycle can never be mistaken for death.
        assert stale_after_seconds() == 60.0

        monkeypatch.setattr(
            config_helpers, "get_queue_worker_config", lambda: {"interval_seconds": 120}
        )
        assert stale_after_seconds() == 360.0


# ---------------------------------------------------------------------------
# Restart must actually do something
# ---------------------------------------------------------------------------
class TestRestartDoesSomethingReal:
    def test_it_kicks_a_cycle_instead_of_deleting_a_file(self, health, restart_probe):
        from services.queue.queue_diagnostics_service import queue_processor_restart

        payload, status = queue_processor_restart()

        assert status == 200
        assert payload["success"] is True
        assert "Queue processor restart requested" in payload["message"]
        assert restart_probe["kicks"] == 1, "restart must start a cycle"
        # The old implementation's entire body was
        # ``os.remove(os.environ["QUEUE_PROCESSOR_HEALTH_FILE"])`` — a no-op
        # because nothing wrote that file. It must now say what it did.
        assert payload["restart"]["actions"], "restart must report its actions"
        assert any("cycle" in action for action in payload["restart"]["actions"])

    def test_it_wakes_an_in_process_worker(self, health, restart_probe):
        from services.queue.queue_diagnostics_service import queue_processor_restart

        queue_processor_restart()

        assert restart_probe["signals"] == 1, "restart must signal the worker"

    def test_it_reports_restarting_until_a_cycle_finishes(self, health, restart_probe):
        """Clicking Restart must change the badge straight away."""
        from services.queue.queue_diagnostics_service import (
            queue_processor_restart,
            queue_processor_status,
        )

        payload, _status = queue_processor_restart()

        assert payload["restarting"] is True
        assert payload["status"] == "restarting"

        # …and the status endpoint must agree with what the button just did.
        follow_up, _status = queue_processor_status()
        assert follow_up["status"] == "restarting"
        assert follow_up["driver"] == "restart-requested"

    def test_a_finished_kick_turns_into_running(self, health):
        from services.queue.queue_diagnostics_service import queue_processor_status
        from services.queue.queue_heartbeat import record_queue_processor_cycle

        record_queue_processor_cycle("restart-requested", outcome="requested")
        record_queue_processor_cycle("restart-kick")

        payload, _status = queue_processor_status()

        assert payload["status"] == "running"
        assert payload["driver"] == "restart-kick"


# ---------------------------------------------------------------------------
# Both drivers must stamp the heartbeat — and only on success
# ---------------------------------------------------------------------------
class TestBothDriversStampTheHeartbeat:
    def test_the_standalone_worker_stamps_after_its_cycle(self):
        source = (ROOT / "services" / "queue" / "queue_worker.py").read_text(
            encoding="utf-8"
        )
        assert 'record_queue_processor_cycle("standalone-worker")' in source

    def test_the_scheduler_tick_stamps_after_its_cycle(self):
        source = (ROOT / "services" / "scheduler" / "scheduler_service.py").read_text(
            encoding="utf-8"
        )
        assert 'record_queue_processor_cycle("scheduler")' in source

    def test_a_cycle_that_raises_leaves_no_stamp(self, health, monkeypatch):
        """Otherwise a permanently crashing processor would read as Running."""
        from services.queue import queue_diagnostics_service as diag
        from services.queue import queue_heartbeat

        attempted = threading.Event()

        def _boom():
            attempted.set()
            raise RuntimeError("cycle exploded")

        monkeypatch.setattr("services.queue.queue_orchestrator.process_cycle", _boom)
        diag._kick_queue_cycle()

        assert attempted.wait(5.0), "the kicked cycle never ran"
        time.sleep(0.2)  # let the failure handler finish

        assert not health.exists()
        assert queue_heartbeat.read_queue_processor_health() is None


# ---------------------------------------------------------------------------
# The UI must render the payload and unlock the button
# ---------------------------------------------------------------------------
class TestTheFrontendWritesTheCard:
    @pytest.mark.parametrize("js_file", [LIVE_JS, TEST_JS], ids=["live", "test_site"])
    def test_the_card_renderer_exists(self, js_file):
        source = js_file.read_text(encoding="utf-8")
        assert "function updateProcessorStatusCard" in source, (
            f"{js_file.name} never writes the card — that is the reported bug"
        )
        assert "async function loadQueueProcessorStatus" in source

    @pytest.mark.parametrize("js_file", [LIVE_JS, TEST_JS], ids=["live", "test_site"])
    def test_it_writes_all_three_card_ids(self, js_file):
        source = js_file.read_text(encoding="utf-8")
        for element_id in CARD_IDS:
            assert f"'{element_id}'" in source, (
                f"{js_file.name} does not touch #{element_id}"
            )

    @pytest.mark.parametrize("js_file", [LIVE_JS, TEST_JS], ids=["live", "test_site"])
    def test_it_enables_the_restart_button(self, js_file):
        source = js_file.read_text(encoding="utf-8")
        assert "restartBtn.disabled = false;" in source, (
            "the button ships `disabled` in the markup; nothing else enables it"
        )

    @pytest.mark.parametrize("js_file", [LIVE_JS, TEST_JS], ids=["live", "test_site"])
    def test_every_queue_poll_refreshes_the_card(self, js_file):
        source = js_file.read_text(encoding="utf-8")
        assert "await loadQueueProcessorStatus();" in source, (
            "loadQueueStatus must refresh the card, or it stays on Loading…"
        )

    @pytest.mark.parametrize("js_file", [LIVE_JS, TEST_JS], ids=["live", "test_site"])
    def test_all_three_states_are_rendered(self, js_file):
        source = js_file.read_text(encoding="utf-8")
        for state in (
            "'bg-success', 'Running'",
            "'bg-danger', 'Stopped'",
            "'bg-warning text-dark', 'Restarting'",
        ):
            assert state in source, f"{js_file.name} does not render {state}"

    def test_the_test_site_exports_the_refresh_helper(self):
        source = TEST_JS.read_text(encoding="utf-8")
        assert "global.loadQueueProcessorStatus = loadQueueProcessorStatus;" in source


class TestTheTemplateContract:
    @pytest.mark.parametrize(
        "partial", [LIVE_PARTIAL, TEST_PARTIAL], ids=["live", "test_site"]
    )
    def test_the_card_ids_are_present(self, partial):
        source = partial.read_text(encoding="utf-8")
        for element_id in CARD_IDS:
            assert f'id="{element_id}"' in source, f"{partial} is missing #{element_id}"

    @pytest.mark.parametrize(
        "partial", [LIVE_PARTIAL, TEST_PARTIAL], ids=["live", "test_site"]
    )
    def test_the_button_ships_disabled_and_the_badge_ships_loading(self, partial):
        """The initial state must be neutral — only the renderer may change it."""
        source = partial.read_text(encoding="utf-8")
        assert "disabled" in source
        assert "Loading..." in source


# ---------------------------------------------------------------------------
# The route must serve it
# ---------------------------------------------------------------------------
class TestTheRoute:
    async def test_status_endpoint_serves_a_real_verdict(self, client, health):
        response = await client.get("/api/queue-processor/status")

        assert response.status_code == 200
        data = await response.get_json()
        assert data["status"] in {"running", "restarting", "stopped"}
        assert "Queue processor status not implemented" not in data["message"]
