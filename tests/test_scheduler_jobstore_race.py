"""Regression tests for the multi-worker APScheduler duplicate-key race.

Each hypercorn worker boots its own scheduler over the SAME DB-backed job
store.  Two workers can both pass the existence check and both attempt the
INSERT for a job id — the loser hits the ``apscheduler_jobs_pkey`` unique
constraint (``duplicate key value violates unique constraint
apscheduler_jobs_pkey``) or the store's own ``ConflictingIdError``.
``ResilientSQLAlchemyJobStore.add_job`` must treat that as an upsert
(fall back to ``update_job``) rather than propagating the error, because
``_register_default_jobs`` always registers with ``replace_existing=True``.

Additionally, ``_register_default_jobs`` must NOT re-register a job that
already exists in the persisted store.  While the scheduler is stopped
(registration runs inside ``get_scheduler``, before ``start()``),
``scheduler.get_job`` only inspects in-memory pending jobs — a job persisted
by the preflight import, a previous boot, or a sibling worker would be
invisible to it, so every worker would re-add the same default jobs and the
loser would log a duplicate-key warning.  The store-aware existence lookup
(``_existing_job``) closes that gap.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from apscheduler.jobstores.base import ConflictingIdError
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from sqlalchemy.exc import IntegrityError

from services.scheduler.scheduler_service import (
    ResilientSQLAlchemyJobStore,
    _register_default_jobs,
)


def _make_store():
    store = ResilientSQLAlchemyJobStore.__new__(ResilientSQLAlchemyJobStore)
    store.engine = MagicMock()
    store._execute = lambda fn, *a, **kw: fn(*a, **kw)
    return store


def _make_job(job_id: str = "popularity_scan"):
    job = MagicMock()
    job.id = job_id
    return job


class TestJobStoreDuplicateKeyRace:
    def test_add_job_falls_back_to_update_on_conflicting_id(self):
        store = _make_store()
        job = _make_job()
        with (
            patch.object(
                SQLAlchemyJobStore, "add_job",
                side_effect=ConflictingIdError(job.id),
            ),
            patch.object(SQLAlchemyJobStore, "update_job") as update_mock,
        ):
            store.add_job(job)
        update_mock.assert_called_once_with(job)

    def test_add_job_falls_back_to_update_on_integrity_error(self):
        store = _make_store()
        job = _make_job()
        with (
            patch.object(
                SQLAlchemyJobStore, "add_job",
                side_effect=IntegrityError("stmt", {}, Exception("duplicate key value")),
            ),
            patch.object(SQLAlchemyJobStore, "update_job") as update_mock,
        ):
            store.add_job(job)
        update_mock.assert_called_once_with(job)

    def test_add_job_happy_path_does_not_update(self):
        store = _make_store()
        job = _make_job()
        with (
            patch.object(SQLAlchemyJobStore, "add_job", return_value="added") as add_mock,
            patch.object(SQLAlchemyJobStore, "update_job") as update_mock,
        ):
            result = store.add_job(job)
        assert result == "added"
        add_mock.assert_called_once_with(job)
        update_mock.assert_not_called()

    def test_add_job_propagates_other_errors(self):
        store = _make_store()
        job = _make_job()
        with (
            patch.object(
                SQLAlchemyJobStore, "add_job", side_effect=RuntimeError("boom"),
            ),
            patch.object(SQLAlchemyJobStore, "update_job") as update_mock,
        ):
            try:
                store.add_job(job)
            except RuntimeError:
                pass
            else:
                raise AssertionError("expected RuntimeError to propagate")


class TestStoreAwareRegistration:
    """``_register_default_jobs`` must see jobs already persisted in the store.

    While the scheduler is stopped, ``scheduler.get_job`` only inspects
    in-memory pending jobs.  Without the store-aware lookup every worker would
    re-register the same default jobs, and the loser would hit the duplicate
    key on ``start()`` (the noisy "already exists in jobstore" warning).  These
    tests pin the store-aware path.
    """

    # Config that disables every other built-in job so only the job under
    # test is registered (missing_releases_scan / upcoming_* jobs read the
    # live config through config_helpers, patched below).
    #
    # ⚠️ ``navidrome_import`` is disabled BY ITS OWN KEY, not via the Watcher
    # toggle. It used to fall back to ``watcher.auto_import_enabled``, so this
    # fixture — which sets that toggle to True while disabling everything else —
    # left ``navidrome_import`` registering during a test whose whole point is
    # "no other job is registered", and ``test_existing_job_in_store_is_not_
    # re_registered`` failed on a call it was not expecting. Naming the job
    # explicitly keeps the fixture honest and pins the new contract: the
    # Watcher's Auto Import toggle does NOT govern the four-hourly import.
    _DISABLED_CFG = {
        "jobs": {
            "navidrome_import": {"enabled": False},
        },
        "watcher": {
            "auto_import_enabled": True,
            "auto_popularity_scan": False,
            "downloads_watcher_enabled": False,
        },
    }

    def _make_scheduler(self, existing_job_ids: tuple[str, ...] = ()):
        scheduler = MagicMock()
        scheduler.get_job.return_value = None  # scheduler stopped → pending only
        store = MagicMock()

        def _lookup(job_id):
            if job_id in existing_job_ids:
                job = MagicMock()
                job.func_ref = None  # unchanged callable
                job.trigger = MagicMock()
                return job
            return None

        store.lookup_job.side_effect = _lookup
        scheduler._jobstores = {"default": store}
        return scheduler, store

    def _patch_config_disabled(self):
        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(
            patch(
                "helpers.config_helpers.get_config",
                return_value={"features": {"daily_musicbrainz_release_scan_enabled": False}},
            )
        )
        stack.enter_context(patch("helpers.config_helpers.get_feature", return_value=False))
        return stack

    def test_existing_job_in_store_is_not_re_registered(self):
        scheduler, store = self._make_scheduler(existing_job_ids=("library_sync",))
        with self._patch_config_disabled():
            with patch(
                "services.scheduler.scheduler_service._same_trigger",
                return_value=True,
            ):
                _register_default_jobs(scheduler, self._DISABLED_CFG)

        # The pre-existing job was found in the store and must NOT be re-added.
        scheduler.add_job.assert_not_called()
        # Same trigger + same callable → untouched, so nothing removed either.
        store.remove_job.assert_not_called()

    def test_missing_job_in_store_is_registered(self):
        scheduler, store = self._make_scheduler()
        with self._patch_config_disabled():
            _register_default_jobs(scheduler, self._DISABLED_CFG)

        store.lookup_job.assert_called()
        scheduler.add_job.assert_called()

    def test_existing_job_with_changed_trigger_is_replaced(self):
        scheduler, store = self._make_scheduler(existing_job_ids=("library_sync",))
        with self._patch_config_disabled():
            with patch(
                "services.scheduler.scheduler_service._same_trigger",
                return_value=False,
            ):
                _register_default_jobs(scheduler, self._DISABLED_CFG)

        # Trigger changed → persisted row dropped, then re-registered.
        store.remove_job.assert_called()
        scheduler.add_job.assert_called()
