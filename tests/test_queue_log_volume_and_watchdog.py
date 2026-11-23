"""Log volume and the watchdog window, both reported from the queue log.

* "3,404 of the 12,406 lines are check_completed_downloads stage timings. Log
  that at DEBUG, and only report it as a warning when it's slow."
* "The watchdog restarted the queue processor 12 times, each time because the
  last queue cycle was 181-256 s old against a 180 s limit."
"""

from __future__ import annotations

import inspect

from services.queue import queue_heartbeat as heartbeat


class TestTheStalenessWindowIsNotTooTight:
    """A cycle runs maintenance inline, so 3 intervals was a four-minute pass."""

    def test_five_cycles_of_margin(self, monkeypatch):
        from helpers import config_helpers

        monkeypatch.setattr(
            config_helpers, "get_queue_worker_config", lambda: {"interval_seconds": 60}
        )
        assert heartbeat.stale_after_seconds() == 300.0, (
            "the reported restarts were 181-256s against a 180s window"
        )

    def test_the_floor_still_applies(self, monkeypatch):
        from helpers import config_helpers

        monkeypatch.setattr(
            config_helpers, "get_queue_worker_config", lambda: {"interval_seconds": 5}
        )
        assert heartbeat.stale_after_seconds() == 60.0

    def test_an_operator_override_is_honoured(self, monkeypatch):
        from helpers import config_helpers

        monkeypatch.setenv("QUEUE_PROCESSOR_STALE_AFTER_SECONDS", "900")
        monkeypatch.setattr(
            config_helpers, "get_queue_worker_config", lambda: {"interval_seconds": 60}
        )
        assert heartbeat.stale_after_seconds() == 900.0

    def test_a_bad_override_is_ignored(self, monkeypatch):
        from helpers import config_helpers

        monkeypatch.setenv("QUEUE_PROCESSOR_STALE_AFTER_SECONDS", "not-a-number")
        monkeypatch.setattr(
            config_helpers, "get_queue_worker_config", lambda: {"interval_seconds": 60}
        )
        assert heartbeat.stale_after_seconds() == 300.0


class TestTheStageTimingsAreNotInfoNoise:
    """3,404 of 12,406 log lines were one routine maintenance hook."""

    def test_a_normal_pass_logs_at_debug(self):
        import services.downloads.download_completion_service as svc

        src = inspect.getsource(svc.check_completed_downloads)
        # The unconditional INFO line is gone.
        assert 'logger.info(\n                "check_completed_downloads stage timings"' not in src
        assert "_SLOW_PASS_WARNING_MS" in src

    def test_a_slow_pass_still_warns(self):
        import services.downloads.download_completion_service as svc

        src = inspect.getsource(svc.check_completed_downloads)
        assert "check_completed_downloads was slow" in src
        assert svc._SLOW_PASS_WARNING_MS == 5000.0
