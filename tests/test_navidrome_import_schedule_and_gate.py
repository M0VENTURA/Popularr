"""The Navidrome import runs every four hours, alongside other scans.

REQUESTED
---------
> Can we have the Navidrome import run on a schedule every four hours so that the
> database is always up to date. The Navidrome import should be able to run
> alongside the other scans. It will just need to make sure that the other scans
> stop excess polling of Navidrome during the Navidrome import scan running.

THREE SEPARABLE CLAIMS
----------------------
1. **Schedule.** A ``navidrome_import`` job exists, defaults to 240 minutes, can
   be disabled, and refuses to start when an import is already running.
2. **Concurrency.** Nothing in the import's guard blocks a non-import scan: it
   checks only ``is_runtime_running("navidrome")`` (its own type) and the
   import gate.
3. **Polling suppression.** While an import runs, OTHER clients defer the
   incidental endpoints — rating pushes, playlist reads/writes, cover art —
   while the import's OWN client (``is_import=True``) is never deferred, because
   a self-deferring import would silently import nothing.

⭐ THE GATE IS FILE-BASED ON PURPOSE. The app runs 4 hypercorn workers plus a
standalone queue worker; an in-process flag would be true in exactly one of them.
The state is the progress file the import already writes, so there is no second
source of truth to keep in sync.
"""
from __future__ import annotations

import json
import os

import pytest

from services.navidrome import import_gate as gate


@pytest.fixture(autouse=True)
def _clean_gate():
    gate.reset_cache()
    yield
    gate.reset_cache()


@pytest.fixture
def progress_file(tmp_path, monkeypatch):
    """Point the gate at a private progress file."""
    path = tmp_path / "navidrome_scan_progress.json"
    monkeypatch.setenv("NAVIDROME_PROGRESS_FILE", str(path))
    gate.reset_cache()
    return path


def _set_running(path, running: bool) -> None:
    path.write_text(json.dumps({"scan_type": "navidrome_scan", "is_running": running}), encoding="utf-8")
    gate.reset_cache()


# ===========================================================================
# 1. The gate
# ===========================================================================
class TestTheImportGate:
    def test_idle_when_the_file_is_absent(self, progress_file):
        assert gate.is_import_running() is False

    def test_idle_when_the_file_says_not_running(self, progress_file):
        _set_running(progress_file, False)
        assert gate.is_import_running() is False

    def test_active_when_the_file_says_running(self, progress_file):
        _set_running(progress_file, True)
        assert gate.is_import_running() is True

    def test_a_corrupt_file_fails_open(self, progress_file):
        """⚠️ FAIL OPEN. A broken gate must not disable rating sync, playlist
        sync and cover art for ever — it may only make the deferral miss."""
        progress_file.write_text("{not json", encoding="utf-8")
        gate.reset_cache()

        assert gate.is_import_running() is False

    def test_an_unreadable_path_fails_open(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "NAVIDROME_PROGRESS_FILE", str(tmp_path / "nope" / "missing.json")
        )
        gate.reset_cache()

        assert gate.is_import_running() is False

    def test_the_verdict_is_cached_briefly(self, progress_file, monkeypatch):
        """A rating push per track must not re-read the file per track."""
        _set_running(progress_file, True)
        assert gate.is_import_running() is True

        # Flip the file WITHOUT resetting the cache: the cached True must stand.
        progress_file.write_text(json.dumps({"is_running": False}), encoding="utf-8")
        assert gate.is_import_running() is True, "the cache did not hold the verdict"

        # ...and the uncached read must see the truth.
        assert gate.is_import_running_uncached() is False


class TestWhichEndpointsAreDeferred:
    @pytest.mark.parametrize(
        "endpoint",
        ["setRating", "star", "unstar", "getPlaylists", "getPlaylist",
         "createPlaylist", "updatePlaylist", "deletePlaylist", "getCoverArt"],
    )
    def test_incidental_polling_is_deferred_while_importing(
        self, progress_file, endpoint: str
    ):
        _set_running(progress_file, True)
        assert gate.should_defer(endpoint) is True

    @pytest.mark.parametrize(
        "endpoint",
        ["getArtists", "getIndexes", "getAlbum", "getArtist", "getAlbumList2",
         "startScan", "getScanStatus", "ping"],
    )
    def test_import_endpoints_are_NEVER_deferred(self, progress_file, endpoint: str):
        """⚠️ Deferring these would stall the import against itself.

        They are absent from the allow-list by design — a deny-list would
        silently catch any new import endpoint someone adds later.
        """
        _set_running(progress_file, True)
        assert gate.should_defer(endpoint) is False

    def test_nothing_is_deferred_when_idle(self, progress_file):
        _set_running(progress_file, False)
        assert gate.should_defer("setRating") is False
        assert gate.should_defer("getCoverArt") is False

    def test_an_unknown_endpoint_is_not_deferred(self, progress_file):
        _set_running(progress_file, True)
        assert gate.should_defer("someFutureEndpoint") is False


class TestTheImportClientIsExempt:
    """⭐ The distinction that keeps the import working."""

    def test_a_scan_client_defers_incidental_requests(self, progress_file):
        from api_clients.navidrome import NavidromeClient

        _set_running(progress_file, True)
        client = NavidromeClient("http://127.0.0.1:1", "u", "p")

        assert client._defer_for_import("setRating") is True

    def test_the_import_client_defers_NOTHING(self, progress_file):
        from api_clients.navidrome import NavidromeClient

        _set_running(progress_file, True)
        client = NavidromeClient("http://127.0.0.1:1", "u", "p", is_import=True)

        for endpoint in ("setRating", "getCoverArt", "getPlaylists"):
            assert client._defer_for_import(endpoint) is False, (
                f"the import's own client deferred {endpoint} — it would import nothing"
            )

    def test_the_default_is_not_an_import_client(self):
        """The flag must be opt-in, or every caller would escape the gate."""
        from api_clients.navidrome import NavidromeClient

        assert NavidromeClient("http://x:1", "u", "p").is_import is False


# ===========================================================================
# 2. The scheduled job
# ===========================================================================
class _FakeJob:
    def __init__(self, job_id, func, trigger):
        self.id = job_id
        self.func = func
        self.trigger = trigger
        self.func_ref = f"{func.__module__}:{func.__qualname__}"


class _FakeScheduler:
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


def _register(monkeypatch, jobs_cfg: dict, *, seed_job: bool = False) -> _FakeScheduler:
    from services.scheduler import scheduler_service as sched

    monkeypatch.setattr(
        sched, "_enabled", lambda job_id, watcher_key, default=True: True, raising=False
    )
    scheduler = _FakeScheduler()
    if seed_job:
        # ``_remove_job`` is a NO-OP when nothing carries that id — it only
        # removes a job that exists. Seeding is therefore the only way to observe
        # the removal path: "switched this off" has to take an ALREADY-REGISTERED
        # job away, not merely decline to add one.
        scheduler.jobs["navidrome_import"] = _FakeJob(
            "navidrome_import", sched._run_navidrome_import_job, None
        )
    sched._register_default_jobs(scheduler, {"jobs": jobs_cfg}, root_cfg={})
    return scheduler


class TestTheImportIsScheduledEveryFourHours:
    def test_the_default_is_240_minutes(self, monkeypatch):
        """THE REQUEST: every four hours."""
        scheduler = _register(monkeypatch, {})

        job = scheduler.jobs.get("navidrome_import")
        assert job is not None, "the Navidrome import is not scheduled at all"
        assert job.trigger.interval.total_seconds() == 240 * 60, (
            f"expected a 4-hour interval, got {job.trigger.interval}"
        )

    def test_the_interval_is_configurable(self, monkeypatch):
        scheduler = _register(monkeypatch, {"navidrome_import": {"interval_minutes": 90}})

        assert scheduler.jobs["navidrome_import"].trigger.interval.total_seconds() == 90 * 60

    def test_zero_disables_it(self, monkeypatch):
        """0 = never, matching every other interval on that card."""
        scheduler = _register(
            monkeypatch, {"navidrome_import": {"interval_minutes": 0}}, seed_job=True
        )

        assert "navidrome_import" not in scheduler.jobs
        assert "navidrome_import" in scheduler.removed, (
            "an already-registered job must be REMOVED when the interval is set to 0"
        )

    def test_enabled_false_disables_it(self, monkeypatch):
        scheduler = _register(
            monkeypatch,
            {"navidrome_import": {"enabled": False, "interval_minutes": 240}},
            seed_job=True,
        )

        assert "navidrome_import" not in scheduler.jobs
        assert "navidrome_import" in scheduler.removed

    def test_it_runs_the_same_pipeline_as_the_button(self):
        """A scheduled path that diverged from the manual one would be a second
        importer nobody tested."""
        import inspect

        from services.scheduler import scheduler_service as sched

        source = inspect.getsource(sched._run_navidrome_import_job)
        assert "run_navidrome_import_scan" in source, (
            "the job does not call the import pipeline"
        )
        assert 'mode="all"' in source, (
            'mode="all" is the INCREMENTAL import; a forced full rescan every four '
            "hours is not what was asked for"
        )

    def test_the_tick_is_guarded_by_the_shared_lock(self):
        """Two hypercorn workers must not both import."""
        import inspect

        from services.scheduler import scheduler_service as sched

        assert '_job_tick_lock("navidrome_import")' in inspect.getsource(
            sched._run_navidrome_import_job
        )

    def test_it_skips_when_an_import_is_already_running(self, progress_file, monkeypatch):
        """Covers a manual import, a boot import, or this job's own last tick."""
        import inspect

        from services.scheduler import scheduler_service as sched

        source = inspect.getsource(sched._run_navidrome_import_job)
        assert "is_import_running()" in source, (
            "the job does not check the cross-process gate, so a scheduled tick "
            "could start a second import while a manual one runs"
        )


# ===========================================================================
# 3. Concurrency — the import must not block, or be blocked by, other scans
# ===========================================================================
class TestTheImportRunsAlongsideOtherScans:
    def test_the_import_guard_checks_only_its_own_scan_type(self):
        """The endpoint refuses a duplicate NAVidrome import and nothing else.

        If it also consulted the popularity gate, the requirement "should be
        able to run alongside the other scans" would be broken — and a 4-hourly
        job would then be silently skipped whenever a scan happened to be
        running.
        """
        import inspect

        from routes.scan_routes import api as api_module

        source = inspect.getsource(api_module.api_navidrome_import)
        assert 'is_runtime_running("navidrome")' in source
        for other in ("popularity", "singles", "essentia", "library"):
            assert f'is_runtime_running("{other}")' not in source, (
                f"the import is blocked by a {other} scan; it must run alongside"
            )

    def test_the_scheduled_job_does_not_wait_for_other_scans(self):
        """Asserted on CODE, not on the docstring that explains the absence.

        ⚠️ The first version of this test searched the raw source and matched the
        job's own docstring ("A popularity/album scan running at the same time is
        fine") — the comment-matching trap, in the very test guarding against it.
        Comments and the docstring are stripped before the check.
        """
        import inspect
        import re

        from services.scheduler import scheduler_service as sched

        source = inspect.getsource(sched._run_navidrome_import_job)
        # Drop the docstring and any comment, then look at what the code CALLS.
        body = re.sub(r'"""[\s\S]*?"""', "", source)
        body = re.sub(r"#[^\n]*", "", body)

        for other in ("popularity", "is_popularity_scan_active", "singles"):
            assert other not in body, (
                f"the import job consults {other}; it must not defer to other scans"
            )
