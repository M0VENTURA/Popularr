"""The API budget belongs to the DEPLOYMENT, not to the process.

The report: a scan was hitting MusicBrainz far faster than the documented
1 request/second, MusicBrainz answered ``503``, and the circuit breaker opened
mid-scan.

Two independent multipliers, both pinned here:

1. **Per-process rate limiting.** ``APIRateLimiter.state`` lives in memory and
   was only persisted every 30s, so every PROCESS enforced the interval on its
   own.  hypercorn serves 4 workers plus the standalone queue worker, so a
   "1 req/s" budget was really ~5 req/s.  The reservation now happens as a
   read-modify-write of the shared state file under an exclusive ``flock``.

2. **One scheduler per worker.** ``initialize_app_services`` only consulted
   ``ENABLE_BACKGROUND_WORKERS``, which defaults to ``"true"`` and is identical
   in every worker of a container — so a 4-worker deployment started FOUR
   schedulers, each running every periodic job and each with its own limiter.
   Leadership is now an actual cross-process election (``helpers/leader_lock``).

NOTE on the rejected theory: a race in ``get_shared_mb_client()`` cannot be the
cause — that singleton is already guarded by ``_INIT_LOCK`` and the throttle in
``api_clients/musicbrainz_http.py`` is module-level, so extra client OBJECTS
never multiply the budget.  Only extra PROCESSES do, which is what these tests
simulate (two limiter instances sharing one state file = two processes).
"""

from __future__ import annotations

import json
import pathlib
import sys
import threading
import time

import pytest

from services.infrastructure.api_rate_limiter import (
    MUSICBRAINZ_MIN_INTERVAL,
    APIRateLimiter,
)


class _SleepRecorder:
    """Replace ``time.sleep`` so the reservation can be asserted without waiting."""

    def __init__(self, monkeypatch) -> None:
        self.calls: list[float] = []
        monkeypatch.setattr(time, "sleep", self._record)

    def _record(self, seconds: float) -> None:
        self.calls.append(float(seconds))

    @property
    def total(self) -> float:
        return sum(self.calls)


@pytest.fixture()
def state_file(tmp_path: pathlib.Path) -> str:
    return str(tmp_path / "api_rate_limiter_state.json")


# ---------------------------------------------------------------------------
# 1. The budget is shared across processes
# ---------------------------------------------------------------------------

def test_a_second_process_cannot_use_the_same_slot(state_file, monkeypatch) -> None:
    """THE regression test.

    Two limiters created BEFORE either has reserved anything is exactly the
    multi-process case: each holds the same stale in-memory state, so with the
    old in-memory reservation both would send immediately.  The shared file must
    make the second one wait a full interval.
    """
    first = APIRateLimiter(state_file=state_file)
    second = APIRateLimiter(state_file=state_file)

    recorder = _SleepRecorder(monkeypatch)

    first.throttle_musicbrainz()
    assert recorder.total == pytest.approx(0.0, abs=0.05), "the first request should not wait"

    second.throttle_musicbrainz()
    assert recorder.total >= MUSICBRAINZ_MIN_INTERVAL * 0.9, (
        "the second process sent its request without waiting — the 1 req/s "
        "budget is being enforced per process, which is how a scan ends up "
        "several times over the MusicBrainz limit"
    )


def test_the_reservation_is_persisted_to_the_shared_file(state_file, monkeypatch) -> None:
    _SleepRecorder(monkeypatch)
    limiter = APIRateLimiter(state_file=state_file)
    limiter.throttle_musicbrainz()

    with open(state_file, "r", encoding="utf-8") as handle:
        persisted = json.load(handle)

    assert persisted["musicbrainz_last_request"] > time.time() - 1.0
    assert persisted["musicbrainz_daily_count"] == 1


def test_daily_counters_accumulate_across_processes(state_file, monkeypatch) -> None:
    """The counter used to be written at most every 30s → last-writer-wins."""
    _SleepRecorder(monkeypatch)
    for _ in range(3):
        APIRateLimiter(state_file=state_file).throttle_musicbrainz()

    with open(state_file, "r", encoding="utf-8") as handle:
        persisted = json.load(handle)
    assert persisted["musicbrainz_daily_count"] == 3


def test_each_provider_has_its_own_budget(state_file, monkeypatch) -> None:
    """MusicBrainz must never be throttled by a Last.fm request."""
    _SleepRecorder(monkeypatch)
    limiter = APIRateLimiter(state_file=state_file)

    limiter.throttle_musicbrainz()
    limiter.throttle_lastfm()  # different budget → no extra wait

    with open(state_file, "r", encoding="utf-8") as handle:
        persisted = json.load(handle)
    assert persisted["musicbrainz_last_request"] > 0
    assert persisted["lastfm_last_request"] > 0


def test_lastfm_wait_helper_respects_the_shared_slot(state_file, monkeypatch) -> None:
    _SleepRecorder(monkeypatch)
    first = APIRateLimiter(state_file=state_file)
    second = APIRateLimiter(state_file=state_file)

    assert first.wait_if_needed_lastfm(max_wait_seconds=5.0) in (True, False)
    # The second process now sees a reserved slot; it must take it into account.
    second.wait_if_needed_lastfm(max_wait_seconds=5.0)
    with open(state_file, "r", encoding="utf-8") as handle:
        persisted = json.load(handle)
    assert persisted["lastfm_daily_count"] == 2


# ---------------------------------------------------------------------------
# 2. Degradation — a limiter must never break the app
# ---------------------------------------------------------------------------

def test_an_unwritable_state_file_does_not_raise(monkeypatch) -> None:
    # A directory can never be written as a file → both the state and the lock
    # degrade to the in-process path.
    limiter = APIRateLimiter(state_file="/proc/definitely/not/writable/state.json")
    recorder = _SleepRecorder(monkeypatch)

    limiter.throttle_musicbrainz()  # must not raise
    limiter.throttle_musicbrainz()
    assert recorder.total >= 0.0


# ---------------------------------------------------------------------------
# 3. One limiter object per process
# ---------------------------------------------------------------------------

def test_get_rate_limiter_returns_one_instance_under_threads() -> None:
    """A double-checked `is None` without a lock builds two limiters."""
    import services.infrastructure.api_rate_limiter as module

    created: list[APIRateLimiter] = []
    original = module.APIRateLimiter

    class _SlowInit(original):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs) -> None:  # pragma: no cover - timing
            time.sleep(0.01)
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(module, "APIRateLimiter", _SlowInit)
    monkeypatch.setattr(module, "_rate_limiter", None, raising=False)

    results: list[object] = []
    threads = [
        threading.Thread(target=lambda: results.append(module.get_rate_limiter()))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    monkeypatch.undo()

    assert len({id(r) for r in results}) == 1, "get_rate_limiter() handed out more than one instance"
    assert len(created) == 1


# ---------------------------------------------------------------------------
# 4. One scheduler per deployment
# ---------------------------------------------------------------------------

def test_background_workers_are_elected_not_assumed(tmp_path, monkeypatch) -> None:
    """``ENABLE_BACKGROUND_WORKERS`` defaulted to true for EVERY worker."""
    import helpers.leader_lock as leader

    monkeypatch.setenv("SCAN_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ENABLE_BACKGROUND_WORKERS", raising=False)
    monkeypatch.setattr(leader, "_LEADER_STATE", None, raising=False)
    monkeypatch.setattr(leader, "_LEADER_HANDLE", None, raising=False)

    assert leader.acquire_background_worker_leadership() is True
    # Idempotent: a later call in the SAME process must not flip the verdict.
    assert leader.acquire_background_worker_leadership() is True
    leader.release_background_worker_leadership()


def test_background_workers_can_be_disabled(tmp_path, monkeypatch) -> None:
    import helpers.leader_lock as leader

    monkeypatch.setenv("SCAN_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ENABLE_BACKGROUND_WORKERS", "false")
    monkeypatch.setattr(leader, "_LEADER_STATE", None, raising=False)

    assert leader.acquire_background_worker_leadership() is False


def test_leadership_lock_records_the_owning_pid(tmp_path, monkeypatch) -> None:
    import helpers.leader_lock as leader

    monkeypatch.setenv("SCAN_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ENABLE_BACKGROUND_WORKERS", raising=False)
    monkeypatch.setattr(leader, "_LEADER_STATE", None, raising=False)
    monkeypatch.setattr(leader, "_LEADER_HANDLE", None, raising=False)

    assert leader.acquire_background_worker_leadership() is True
    # Read AFTER releasing: on Windows the locked region cannot be read, and the
    # PID is written once at acquisition either way.
    leader.release_background_worker_leadership()

    contents = (tmp_path / "background_workers.lock").read_text(encoding="utf-8").strip()
    assert contents == str(__import__("os").getpid())


@pytest.mark.skipif(sys.platform.startswith("win"), reason="flock semantics differ on Windows")
def test_a_second_holder_cannot_take_leadership(tmp_path) -> None:
    """The kernel enforces exclusivity — this is what makes it an election."""
    import helpers.leader_lock as leader

    lock = tmp_path / "l.lock"
    first = leader._try_take_lock(str(lock))
    assert first is not None, "the first holder should win"
    try:
        assert leader._try_take_lock(str(lock)) is None, (
            "a second process took the lock while it was held — duplicate "
            "schedulers would be started"
        )
    finally:
        first.close()


def test_initialize_app_services_skips_non_leaders(monkeypatch) -> None:
    """A non-leader must not start the scheduler at all."""
    import helpers.task_manager as task_manager
    import helpers.leader_lock as leader

    started: list[object] = []
    monkeypatch.setattr(
        "services.scheduler.scheduler_service.start_scheduler",
        lambda *a, **k: started.append(True),
    )
    monkeypatch.setattr(leader, "acquire_background_worker_leadership", lambda: False)

    task_manager.initialize_app_services(app=None)

    assert started == [], "a non-leader worker started the scheduler"


def test_initialize_app_services_starts_the_leader_scheduler(monkeypatch) -> None:
    import helpers.task_manager as task_manager
    import helpers.leader_lock as leader
    import services.scheduler.scheduler_service as scheduler_service

    started: list[object] = []
    monkeypatch.setattr(scheduler_service, "start_scheduler", lambda *a, **k: started.append(True))
    monkeypatch.setattr(leader, "acquire_background_worker_leadership", lambda: True)
    # The retry-scheduler sync on boot must not reach for the real config.
    monkeypatch.setattr(
        "services.downloads.download_scheduler_service.start_scheduler", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "services.downloads.download_retry_service._retry_scheduler_enabled", lambda: False
    )

    task_manager.initialize_app_services(app=None)

    assert started == [True], "the elected leader did not start the scheduler"
