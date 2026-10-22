"""Which enable flag governs which scheduled job.

⚠️ THE BUG THIS PINS

``navidrome_import`` used to read its enable flag as
``_enabled("navidrome_import", "auto_import_enabled", True)`` — a fallback to the
Watcher card's **Auto Import** toggle. Unticking "Auto Import" therefore
DELETED the four-hourly Navidrome import job, which is close to the opposite of
what that setting advertises: the setting's own help text says "0 = never
(equivalent to unticking Auto Import)" about ``library_sync``, and the
``navidrome_import`` card says the job exists so ``tracks`` follows the library
"on its own".

Nothing reported the loss. The job was simply absent from
``scheduler.get_jobs()``, and the scheduler still described itself as healthy.

The rule now: a job's OWN ``scheduler.jobs.<id>.enabled`` wins; the Watcher
fallback survives only for the two jobs that toggle actually names
(``library_sync``, ``popularity_scan``).
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import patch

from apscheduler.schedulers.background import BackgroundScheduler

from services.scheduler.scheduler_service import _register_default_jobs


def _job_ids(cfg: dict, root_cfg: dict | None = None) -> set[str]:
    """Register jobs into an in-memory scheduler and return their ids."""
    scheduler = BackgroundScheduler(
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300}
    )
    with _patch_scan_config():
        _register_default_jobs(scheduler, cfg, root_cfg=root_cfg or {})
    return {job.id for job in scheduler.get_jobs()}


def _patch_scan_config():
    """Silence the two jobs that read the live config directly.

    ``missing_releases_scan`` reads ``get_config()`` and the ``upcoming_*`` jobs
    read ``get_feature()``; without this they register based on whatever the
    developer's ``config.yaml`` happens to say.
    """
    stack = ExitStack()
    stack.enter_context(
        patch(
            "helpers.config_helpers.get_config",
            return_value={"features": {"daily_musicbrainz_release_scan_enabled": False}},
        )
    )
    stack.enter_context(patch("helpers.config_helpers.get_feature", return_value=False))
    return stack


def _interval_seconds(job_id: str, cfg: dict) -> float | None:
    scheduler = BackgroundScheduler(
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300}
    )
    with _patch_scan_config():
        _register_default_jobs(scheduler, cfg)
    job = scheduler.get_job(job_id)
    return None if job is None else job.trigger.interval.total_seconds()


class TestNavidromeImportIsIndependentOfTheWatcherToggle:
    """The four-hourly import is governed by its own key, not Auto Import."""

    def test_registers_by_default(self):
        assert "navidrome_import" in _job_ids({})

    def test_registers_at_four_hours_by_default(self):
        assert _interval_seconds("navidrome_import", {}) == 4 * 60 * 60

    def test_survives_auto_import_being_unticked(self):
        """The regression: this used to delete the job.

        ``watcher.auto_import_enabled=False`` is the documented "turn Auto
        Import off" state. The four-hourly import must still be scheduled.
        """
        cfg = {"jobs": {}, "watcher": {"auto_import_enabled": False}}

        assert "navidrome_import" in _job_ids(cfg, root_cfg=cfg)
        assert _interval_seconds("navidrome_import", cfg) == 4 * 60 * 60

    def test_own_enabled_false_removes_it(self):
        assert "navidrome_import" not in _job_ids(
            {"jobs": {"navidrome_import": {"enabled": False}}}
        )

    def test_own_enabled_true_registers_it(self):
        cfg = {"jobs": {"navidrome_import": {"enabled": True}}}
        assert "navidrome_import" in _job_ids(cfg)

    def test_zero_interval_means_never(self):
        assert "navidrome_import" not in _job_ids(
            {"jobs": {"navidrome_import": {"interval_minutes": 0}}}
        )

    def test_custom_interval_is_honoured(self):
        cfg = {"jobs": {"navidrome_import": {"interval_minutes": 90}}}
        assert _interval_seconds("navidrome_import", cfg) == 90 * 60

    def test_own_enabled_beats_the_watcher_toggle(self):
        """Explicit config wins in BOTH directions."""
        cfg = {
            "jobs": {"navidrome_import": {"enabled": False}},
            "watcher": {"auto_import_enabled": True},
        }
        assert "navidrome_import" not in _job_ids(cfg, root_cfg=cfg)

        cfg2 = {
            "jobs": {"navidrome_import": {"enabled": True}},
            "watcher": {"auto_import_enabled": False},
        }
        assert "navidrome_import" in _job_ids(cfg2, root_cfg=cfg2)


class TestTheWatcherToggleStillGovernsTheScansItNames:
    """No regression: ``library_sync`` / ``popularity_scan`` keep the fallback."""

    def test_library_sync_follows_auto_import(self):
        cfg = {"jobs": {}, "watcher": {"auto_import_enabled": False}}
        assert "library_sync" not in _job_ids(cfg, root_cfg=cfg)

    def test_library_sync_registers_when_auto_import_is_on(self):
        cfg = {"jobs": {}, "watcher": {"auto_import_enabled": True}}
        assert "library_sync" in _job_ids(cfg, root_cfg=cfg)

    def test_popularity_scan_follows_auto_popularity_scan(self):
        cfg = {"jobs": {}, "watcher": {"auto_popularity_scan": False}}
        assert "popularity_scan" not in _job_ids(cfg, root_cfg=cfg)

    def test_popularity_scan_registers_when_enabled(self):
        cfg = {"jobs": {}, "watcher": {"auto_popularity_scan": True}}
        assert "popularity_scan" in _job_ids(cfg, root_cfg=cfg)

    def test_the_two_toggles_are_independent(self):
        """Unticking Auto Import must not also stop the popularity scan."""
        cfg = {
            "jobs": {},
            "watcher": {"auto_import_enabled": False, "auto_popularity_scan": True},
        }
        ids = _job_ids(cfg, root_cfg=cfg)

        assert "library_sync" not in ids
        assert "popularity_scan" in ids
        assert "navidrome_import" in ids
