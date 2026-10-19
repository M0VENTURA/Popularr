"""Automatic restart of a STOPPED download queue processor.

REQUESTED
---------
> ...when the queue processor fails, it shows it with the option to restart in
> both the scanner queue and the config page, but if it's failed, it should also
> show on the dashboard and a restart on failure should happen automatically.

WHAT THIS PINS
--------------
``services/queue/queue_watchdog_service.py`` reads the SAME verdict the card
draws (``queue_processor_status()``) and, when it says ``stopped``, makes the
next cycle happen now — re-arming the in-process scheduler job, or running one
cycle itself when there is no job to re-arm. That is exactly what the Restart
button does, which is the honest meaning of "restart" for a processor whose PID
this process does not own (see ``queue_heartbeat.py``).

FOUR PROPERTIES, EACH INDEPENDENTLY FALSIFIABLE
-----------------------------------------------
1. Only ``stopped`` triggers anything. A ``running`` or ``restarting`` processor
   must be left alone — re-kicking a cycle in flight would double up work.
2. The COOLDOWN bounds a restart loop when the environment itself is broken
   (database down, so every cycle raises), while never blocking the manual
   button.
3. ``enabled: false`` disables it completely — the operator's off switch.
4. The status payload carries the auto-restart state, because the dashboard has
   to say whether help is coming.

Hermetic like ``test_queue_processor_status.py``: a private heartbeat file, no
scheduler, and the restart itself monkeypatched so no cycle ever runs.
"""
from __future__ import annotations

import pathlib
import time

import pytest


@pytest.fixture
def no_scheduler(monkeypatch):
    """Take the APScheduler out of the verdict.

    A running scheduler with a registered queue job is INDEPENDENT evidence that
    the processor is alive, so it would mask the ``stopped`` verdict every test
    here depends on.
    """
    from services.scheduler import scheduler_service as sched

    monkeypatch.setattr(sched, "_SCHEDULER", None)


@pytest.fixture
def watchdog_env(no_scheduler, tmp_path, monkeypatch):
    """Private heartbeat + watchdog state files, so verdicts are hermetic."""
    monkeypatch.setenv(
        "QUEUE_PROCESSOR_HEALTH_FILE", str(tmp_path / "queue_processor_health.json")
    )
    monkeypatch.setenv(
        "QUEUE_PROCESSOR_WATCHDOG_FILE", str(tmp_path / "queue_processor_watchdog.json")
    )
    return tmp_path


@pytest.fixture
def restarts(monkeypatch):
    """Record restart calls instead of running a real queue cycle."""
    from services.queue import queue_diagnostics_service as diag

    calls: list[dict] = []

    def _restart():
        calls.append({"at": time.time()})
        return (
            {
                "success": True,
                "restarting": True,
                "message": "Queue processor restart requested — kicked a cycle",
            },
            200,
        )

    monkeypatch.setattr(diag, "queue_processor_restart", _restart)
    return calls


def _set_auto_restart(monkeypatch, *, enabled=None, cooldown=None):
    """Point ``get_config()`` at a config carrying ``queue.auto_restart``."""
    from helpers import config_helpers
    from services.queue import queue_watchdog_service as watchdog

    section: dict = {}
    if enabled is not None:
        section["enabled"] = enabled
    if cooldown is not None:
        section["cooldown_seconds"] = cooldown

    monkeypatch.setattr(
        config_helpers,
        "get_config",
        lambda: {"queue": {"auto_restart": section}},
    )
    # ``get_auto_restart_config`` imports ``get_config`` INSIDE the function, so
    # patching the module attribute is enough — no stale binding to worry about.
    assert watchdog.get_auto_restart_config() is not None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class TestTheConfigSection:
    def test_defaults_are_usable_when_the_section_is_absent(self, watchdog_env, monkeypatch):
        """A config.yaml predating this feature must behave like the defaults."""
        from helpers import config_helpers
        from services.queue import queue_watchdog_service as watchdog

        monkeypatch.setattr(config_helpers, "get_config", lambda: {"queue": {}})
        cfg = watchdog.get_auto_restart_config()

        assert cfg["enabled"] is True
        assert cfg["cooldown_seconds"] == 300.0
        assert cfg["check_interval_seconds"] >= 60.0

    def test_a_string_false_is_not_truthy(self, watchdog_env, monkeypatch):
        """YAML gives bools, a form POST gives strings."""
        from helpers import config_helpers
        from services.queue import queue_watchdog_service as watchdog

        monkeypatch.setattr(
            config_helpers, "get_config", lambda: {"queue": {"auto_restart": {"enabled": "false"}}}
        )
        assert watchdog.get_auto_restart_config()["enabled"] is False

    def test_the_cooldown_cannot_be_configured_into_a_storm(self, watchdog_env, monkeypatch):
        from helpers import config_helpers
        from services.queue import queue_watchdog_service as watchdog

        monkeypatch.setattr(
            config_helpers,
            "get_config",
            lambda: {"queue": {"auto_restart": {"cooldown_seconds": 1}}},
        )
        assert watchdog.get_auto_restart_config()["cooldown_seconds"] == 30.0

    def test_an_unreadable_config_does_not_break_the_watchdog(self, watchdog_env, monkeypatch):
        """Fail OPEN: a watchdog that cannot read config must still watch."""
        from helpers import config_helpers
        from services.queue import queue_watchdog_service as watchdog

        def _boom():
            raise RuntimeError("config is on fire")

        monkeypatch.setattr(config_helpers, "get_config", _boom)
        cfg = watchdog.get_auto_restart_config()

        assert cfg["enabled"] is True, "an unreadable config must not silently disable it"


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------
class TestOnlyAStoppedProcessorIsRestarted:
    def test_a_stopped_processor_is_restarted(self, watchdog_env, restarts):
        from services.queue.queue_watchdog_service import auto_restart_queue_processor

        result = auto_restart_queue_processor()

        assert result["restarted"] is True
        assert len(restarts) == 1, "a stopped processor must be restarted exactly once"

    def test_a_running_processor_is_left_alone(self, watchdog_env, restarts, monkeypatch):
        """CONTROL: the cooldown must not be the only thing preventing restarts."""
        from services.queue import queue_diagnostics_service as diag
        from services.queue.queue_watchdog_service import auto_restart_queue_processor

        monkeypatch.setattr(
            diag,
            "queue_processor_status",
            lambda: ({"status": "running", "message": "all good"}, 200),
        )
        result = auto_restart_queue_processor()

        assert result["restarted"] is False
        assert result["attempted"] is False
        assert restarts == [], "a running processor must never be restarted"

    def test_a_restarting_processor_is_left_alone(self, watchdog_env, restarts, monkeypatch):
        """A cycle was just requested; kicking another would double up work."""
        from services.queue import queue_diagnostics_service as diag
        from services.queue.queue_watchdog_service import auto_restart_queue_processor

        monkeypatch.setattr(
            diag,
            "queue_processor_status",
            lambda: ({"status": "restarting", "message": "requested"}, 200),
        )
        result = auto_restart_queue_processor()

        assert result["restarted"] is False
        assert restarts == []

    def test_an_unknown_status_is_not_treated_as_stopped(self, watchdog_env, restarts, monkeypatch):
        from services.queue import queue_diagnostics_service as diag
        from services.queue.queue_watchdog_service import auto_restart_queue_processor

        monkeypatch.setattr(
            diag, "queue_processor_status", lambda: ({"message": "?"}, 200)
        )
        result = auto_restart_queue_processor()

        assert result["restarted"] is False
        assert restarts == []

    def test_the_verdict_is_re_read_every_tick(self, watchdog_env, restarts, monkeypatch):
        """A heartbeat arriving between ticks must CANCEL the restart, not race it."""
        from services.queue import queue_diagnostics_service as diag
        from services.queue.queue_watchdog_service import auto_restart_queue_processor

        verdicts = [("stopped", 1), ("running", 0)]

        def _status():
            state, _ = verdicts.pop(0)
            return ({"status": state, "message": state}, 200)

        monkeypatch.setattr(diag, "queue_processor_status", _status)

        assert auto_restart_queue_processor()["restarted"] is True
        assert auto_restart_queue_processor()["restarted"] is False


class TestTheCooldownBoundsARestartLoop:
    def test_a_second_restart_inside_the_window_is_refused(
        self, watchdog_env, restarts, monkeypatch
    ):
        """A broken environment would otherwise restart on every tick."""
        from services.queue.queue_watchdog_service import auto_restart_queue_processor

        _set_auto_restart(monkeypatch, enabled=True, cooldown=600)

        assert auto_restart_queue_processor()["restarted"] is True
        second = auto_restart_queue_processor()

        assert second["restarted"] is False
        assert second["attempted"] is False
        assert "cooldown" in second["reason"]
        assert second["seconds_until_eligible"] > 0
        assert len(restarts) == 1

    def test_the_cooldown_expires(self, watchdog_env, restarts, monkeypatch):
        from services.queue import queue_watchdog_service as watchdog

        _set_auto_restart(monkeypatch, enabled=True, cooldown=60)

        assert watchdog.auto_restart_queue_processor()["restarted"] is True

        # Age the recorded stamp past the window rather than sleeping.
        state = watchdog.read_watchdog_state()
        state["last_restart_at"] = time.time() - 61
        watchdog._write_watchdog_state(state)

        assert watchdog.auto_restart_queue_processor()["restarted"] is True
        assert len(restarts) == 2

    def test_the_count_only_advances_on_a_real_restart(
        self, watchdog_env, restarts, monkeypatch
    ):
        from services.queue.queue_watchdog_service import (
            auto_restart_snapshot,
            auto_restart_queue_processor,
        )

        _set_auto_restart(monkeypatch, enabled=True, cooldown=600)

        assert auto_restart_snapshot()["restart_count"] == 0
        auto_restart_queue_processor()
        assert auto_restart_snapshot()["restart_count"] == 1
        auto_restart_queue_processor()  # refused by the cooldown
        assert auto_restart_snapshot()["restart_count"] == 1


class TestDisabledMeansDisabled:
    def test_it_does_nothing_when_switched_off(self, watchdog_env, restarts, monkeypatch):
        from services.queue.queue_watchdog_service import auto_restart_queue_processor

        _set_auto_restart(monkeypatch, enabled=False)

        result = auto_restart_queue_processor()

        assert result["restarted"] is False
        assert result["attempted"] is False
        assert restarts == [], "a disabled watchdog must not touch the queue"
        assert "disabled" in result["reason"]


# ---------------------------------------------------------------------------
# The tick, and its failure containment
# ---------------------------------------------------------------------------
class TestTheScheduledTick:
    def test_the_tick_swallows_its_own_failures(self, watchdog_env, monkeypatch):
        """A raising watchdog would stop ITSELF from running the next cycle.

        A watchdog has no business taking the scheduler thread down with it.
        """
        from services.queue import queue_diagnostics_service as diag
        from services.queue import queue_watchdog_service as watchdog

        def _boom():
            raise RuntimeError("status endpoint exploded")

        monkeypatch.setattr(diag, "queue_processor_status", _boom)

        watchdog.queue_processor_watchdog_tick()  # must not raise

    def test_a_corrupt_state_file_does_not_stop_the_watchdog(
        self, watchdog_env, restarts, monkeypatch
    ):
        """'We lost track of the last restart' beats 'the queue never restarts'."""
        from services.queue import queue_watchdog_service as watchdog

        pathlib.Path(watchdog.watchdog_file_path()).write_text("{not json", encoding="utf-8")

        assert watchdog.read_watchdog_state() == {}
        assert watchdog.auto_restart_queue_processor()["restarted"] is True


# ---------------------------------------------------------------------------
# The payload the dashboard reads
# ---------------------------------------------------------------------------
class TestTheStatusPayloadReportsAutoRestart:
    def test_the_state_is_exposed(self, watchdog_env):
        """The dashboard must be able to say whether help is on the way."""
        from services.queue.queue_diagnostics_service import queue_processor_status

        payload, _status = queue_processor_status()

        assert "auto_restart" in payload, "the dashboard reads payload['auto_restart']"
        auto = payload["auto_restart"]
        for key in ("enabled", "cooldown_seconds", "restart_count", "last_restart_iso"):
            assert key in auto, f"auto_restart is missing {key!r}"

    def test_it_reflects_the_configuration(self, watchdog_env, monkeypatch):
        from services.queue.queue_diagnostics_service import queue_processor_status

        _set_auto_restart(monkeypatch, enabled=False, cooldown=120)

        payload, _status = queue_processor_status()

        assert payload["auto_restart"]["enabled"] is False
        assert payload["auto_restart"]["cooldown_seconds"] == 120.0

    def test_a_last_restart_is_surfaced(self, watchdog_env, restarts, monkeypatch):
        from services.queue import queue_watchdog_service as watchdog
        from services.queue.queue_diagnostics_service import queue_processor_status

        _set_auto_restart(monkeypatch, enabled=True, cooldown=600)
        watchdog.auto_restart_queue_processor()

        payload, _status = queue_processor_status()
        auto = payload["auto_restart"]

        assert auto["restart_count"] == 1
        assert auto["last_restart_iso"], "the dashboard renders a timestamp from this"
        assert auto["last_reason"]


# ---------------------------------------------------------------------------
# The scheduler registration
# ---------------------------------------------------------------------------
class _FakeJob:
    def __init__(self, job_id, func, trigger):
        self.id = job_id
        self.func = func
        self.trigger = trigger
        self.func_ref = f"{getattr(func, '__module__', '')}:{getattr(func, '__qualname__', '')}"


class _FakeScheduler:
    """Just enough scheduler for ``_register_default_jobs``.

    ⚠️ ``_put`` and ``_remove_job`` are LOCALS inside ``_register_default_jobs``,
    so they cannot be monkeypatched from outside. The scheduler object is the
    only seam — which is the right one anyway, since this asserts what would
    really be handed to APScheduler.
    """

    def __init__(self):
        self.jobs: dict[str, _FakeJob] = {}
        self.removed: list[str] = []
        self._jobstores: dict = {}

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def add_job(self, func, trigger=None, id=None, name=None, replace_existing=True, **kwargs):
        self.jobs[id] = _FakeJob(id, func, trigger)

    def remove_job(self, job_id):
        self.removed.append(job_id)
        self.jobs.pop(job_id, None)


class TestTheWatchdogIsScheduled:
    """Registered from ``_register_default_jobs`` — no daemon of its own.

    ⚠️ Driving the real registration rather than reading the live scheduler:
    building that singleton opens its SQLAlchemy job store, which a test must not
    do as a side effect.
    """

    @staticmethod
    def _register(
        monkeypatch, *, auto_enabled: bool, queue_enabled: bool, seed_job: bool = False
    ) -> _FakeScheduler:
        from services.queue import queue_watchdog_service as watchdog
        from services.scheduler import scheduler_service as sched

        monkeypatch.setattr(
            watchdog,
            "get_auto_restart_config",
            lambda: {
                "enabled": auto_enabled,
                "cooldown_seconds": 300.0,
                "check_interval_seconds": 90.0,
            },
        )

        scheduler = _FakeScheduler()
        if seed_job:
            # ``_remove_job`` is a NO-OP when nothing carries that id — it only
            # removes a job that exists. Seeding is therefore the only way to
            # observe the removal path at all: "switched this off" has to take
            # an ALREADY-REGISTERED watchdog away, not merely stop adding one.
            scheduler.jobs[watchdog.WATCHDOG_JOB_ID] = _FakeJob(
                watchdog.WATCHDOG_JOB_ID, sched._run_queue_processor_watchdog_job, None
            )

        sched._register_default_jobs(
            scheduler,
            {"jobs": {"download_queue_processor": {"enabled": queue_enabled}}},
            root_cfg={},
        )
        return scheduler

    def test_it_is_registered_when_both_the_watchdog_and_the_queue_are_on(self, monkeypatch):
        from services.queue import queue_watchdog_service as watchdog

        scheduler = self._register(monkeypatch, auto_enabled=True, queue_enabled=True)

        job = scheduler.jobs.get(watchdog.WATCHDOG_JOB_ID)
        assert job is not None, (
            "the auto-restart watchdog was never registered, so 'a restart on failure "
            "should happen automatically' would never happen"
        )
        assert job.func.__name__ == "_run_queue_processor_watchdog_job"

    def test_it_is_removed_when_switched_off(self, monkeypatch):
        from services.queue import queue_watchdog_service as watchdog

        scheduler = self._register(
            monkeypatch, auto_enabled=False, queue_enabled=True, seed_job=True
        )

        assert watchdog.WATCHDOG_JOB_ID not in scheduler.jobs
        assert watchdog.WATCHDOG_JOB_ID in scheduler.removed, (
            "an existing watchdog job must be removed when the option is switched off, "
            "or turning it off would not actually stop it"
        )

    def test_it_is_not_registered_when_the_queue_itself_is_off(self, monkeypatch):
        """A deployment that turned the download queue off must not have cycles
        kicked into it every minute."""
        from services.queue import queue_watchdog_service as watchdog

        scheduler = self._register(
            monkeypatch, auto_enabled=True, queue_enabled=False, seed_job=True
        )

        assert watchdog.WATCHDOG_JOB_ID not in scheduler.jobs
        assert watchdog.WATCHDOG_JOB_ID in scheduler.removed

    def test_the_tick_is_guarded_by_the_same_lock_as_every_other_job(self):
        """Two web workers must never both decide the queue is down and kick it."""
        src = (
            pathlib.Path(__file__).resolve().parent.parent
            / "services"
            / "scheduler"
            / "scheduler_service.py"
        ).read_text(encoding="utf-8")

        assert '_job_tick_lock("queue_processor_watchdog")' in src, (
            "the watchdog tick is not guarded, so a multi-worker deployment could "
            "double-kick the queue"
        )
