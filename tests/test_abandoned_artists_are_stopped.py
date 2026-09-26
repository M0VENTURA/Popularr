"""Guard: an abandoned artist must be STOPPED, not left running.

REPORTED
--------
"Some artists skip due to timeout, but this doesn't happen when running an
artist scan" — the full-library scan abandons artists that a single-artist
scan handles fine, and the scan "seemed to jump without being finished".

TWO DEFECTS, both reproduced against the real code
--------------------------------------------------
1. THE CASCADE. ``_bounded_call_report`` runs each artist on a daemon thread
   and ``join``s it with a budget. On timeout it returns ``abandoned=True``
   and the loop moves on — **but the thread is still running**. Abandoned
   workers therefore accumulate, competing for the DB connection pool, the
   shared cross-thread HTTP rate limiter and CPU, which makes the NEXT artist
   likelier to time out: a self-reinforcing cascade. Measured with the real
   ``_bounded_call_report``: 8 artists needing 1.2s each against a 0.15s
   budget gave **8 concurrent pipelines with 7 still alive** when the loop
   ended.

2. THE BACKWARDS PROGRESS. The dashboard's per-artist callback closes over its
   own artist index (``_i=i``), so an orphaned thread keeps writing ITS index
   into the progress row while the main loop has moved on — ``percent_complete``
   goes BACKWARDS. Measured: 20 backwards jumps in a 5-artist run. That is the
   "it seemed to jump without being finished".

THE FIX
-------
  * ``services/popularity/scan_cancellation`` — a per-artist, per-scan-
    generation cancellation registry. Deliberately NOT the user Stop flag:
    reusing that would abort all 58 artists to stop one.
  * ``run_scan`` checks it at the album boundary (the same safe point the user
    stop uses), so an abandoned artist unwinds without leaving a half-written
    album.
  * ``_bounded_call_report`` now returns the worker ``thread``, so the caller
    can bound the pile-up.
  * The full-scan loop cancels the abandoned artist and waits a BOUNDED grace
    period for the live count to fall back under a cap. Bounded is essential:
    a worker stuck in a blocked syscall never observes cancellation, and
    waiting for it forever would reintroduce the frozen scan the budget
    exists to prevent.
"""

from __future__ import annotations

import threading
import time

import pytest

from services.popularity import scan_cancellation as cancel


@pytest.fixture(autouse=True)
def _reset_cancellation():
    """Cancellation state is process-wide by design, so isolate every test."""
    cancel.reset()
    yield
    cancel.reset()


# ---------------------------------------------------------------------------
# The cancellation registry
# ---------------------------------------------------------------------------


class TestCancellationRegistry:

    def test_an_artist_starts_out_not_cancelled(self):
        assert cancel.is_cancelled("Warrel Dane") is False

    def test_request_cancel_marks_that_artist(self):
        assert cancel.request_cancel("Warrel Dane") is True
        assert cancel.is_cancelled("Warrel Dane") is True

    def test_cancelling_one_artist_does_not_touch_another(self):
        """THE POINT OF A SEPARATE REGISTRY: one artist, not the whole scan.

        Reusing the user Stop flag would have halted all 58 artists.
        """
        cancel.request_cancel("Warrel Dane")
        assert cancel.is_cancelled("Warrel Dane") is True
        assert cancel.is_cancelled("Wayne Static") is False
        assert cancel.is_cancelled("We Are DOMI") is False

    def test_matching_is_case_and_whitespace_insensitive(self):
        cancel.request_cancel("  warrel dane ")
        assert cancel.is_cancelled("Warrel Dane") is True

    def test_falsy_artist_is_never_cancelled(self):
        """An empty artist must not become a wildcard."""
        cancel.request_cancel("")
        assert cancel.is_cancelled("") is False
        assert cancel.is_cancelled("Anyone") is False

    def test_a_stale_epoch_cannot_cancel_a_newer_scan(self):
        """Artist names repeat across runs; a late straggler must not leak.

        An abandoned worker from scan #1 could otherwise cancel the artist of
        the same name in scan #2, which it knows nothing about.
        """
        old_epoch = cancel.begin_scan()
        new_epoch = cancel.begin_scan()

        assert cancel.request_cancel("Warrel Dane", epoch=new_epoch) is True
        assert cancel.request_cancel("Wayne Static", epoch=old_epoch) is False
        assert cancel.is_cancelled("Wayne Static") is False

    def test_begin_scan_clears_previous_cancellations(self):
        cancel.request_cancel("Warrel Dane")
        cancel.begin_scan()
        assert cancel.is_cancelled("Warrel Dane") is False

    def test_clear_artist_forgets_only_that_artist(self):
        cancel.request_cancel("Warrel Dane")
        cancel.request_cancel("Wayne Static")
        cancel.clear_artist("Warrel Dane")
        assert cancel.is_cancelled("Warrel Dane") is False
        assert cancel.is_cancelled("Wayne Static") is True


# ---------------------------------------------------------------------------
# The abandoned worker must be reachable, so the pile-up can be bounded
# ---------------------------------------------------------------------------


class TestBoundedCallReportExposesTheWorker:

    def test_an_abandoned_call_returns_its_thread(self):
        """Without the handle the caller cannot stop or even count stragglers.

        Returning only a boolean is what let abandoned artists accumulate
        silently.
        """
        from services.popularity.scan_stage_runner import _bounded_call_report

        def _hang():
            time.sleep(3)

        report = _bounded_call_report(_hang, seconds=0.2, label="hang")
        assert report["abandoned"] is True

        worker = report.get("thread")
        assert worker is not None, (
            "an abandoned call must expose its worker thread so the caller can "
            "bound how many abandoned artists pile up"
        )
        assert worker.is_alive() is True, (
            "the whole point is that the worker is still running"
        )

    def test_a_completed_call_is_not_abandoned(self):
        from services.popularity.scan_stage_runner import _bounded_call_report

        report = _bounded_call_report(lambda: None, seconds=10, label="x")
        assert report.get("ok") is True
        assert report.get("abandoned") is False

    def test_a_raised_exception_is_a_failure_not_an_abandonment(self):
        """A crashing artist is a different condition from a slow one."""
        from services.popularity.scan_stage_runner import _bounded_call_report

        def _boom():
            raise RuntimeError("disk full")

        report = _bounded_call_report(_boom, seconds=10, label="x")
        assert report["ok"] is False
        assert report.get("abandoned") is False
        assert "disk full" in report["reason"]


# ---------------------------------------------------------------------------
# The drain helper
# ---------------------------------------------------------------------------


class _FakeWorker:
    def __init__(self, name: str, alive: bool = True) -> None:
        self.name = name
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


def _drain_with_timeout(
    workers: list,
    artist_position: int,
    max_live: int,
    *,
    grace_seconds: float = 5.0,
    timeout: float = 10.0,
) -> dict:
    """Call the drain on a worker thread and FAIL if it does not return.

    ⚠️ Every drain assertion goes through here on purpose. The property under
    test is "this returns promptly"; if a regression removes the bound, calling
    the helper inline would HANG the test session rather than fail it — and a
    hanging suite is worse than a failing one (it also wedges mutation testing,
    because the mutated file never gets restored). Joining with a timeout turns
    "never returns" into a clean assertion failure.
    """
    from services.scanning.pipelines.popularity_pipeline import _drain_abandoned_workers

    box: dict[str, object] = {}

    def _run() -> None:
        try:
            box["stats"] = _drain_abandoned_workers(
                workers, artist_position, max_live, grace_seconds=grace_seconds
            )
        except BaseException as exc:  # pragma: no cover - defensive
            box["error"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    assert not thread.is_alive(), (
        f"the drain did not return within {timeout}s — the wait is UNBOUNDED, "
        "so a worker that ignores cancellation could freeze the whole scan"
    )
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["stats"]  # type: ignore[return-value]


class TestDrainAbandonedWorkers:

    def test_finished_workers_are_dropped_from_the_list(self):
        workers = [_FakeWorker("done-1", alive=False), _FakeWorker("done-2", alive=False)]
        stats = _drain_with_timeout(workers, 1, max_live=1)

        assert workers == [], "finished workers must not be retained"
        assert stats["live"] == 0

    def test_a_straggler_over_the_cap_is_reported_loudly(self):
        """This is the case where the cascade could resume, so it must be visible."""
        workers = [_FakeWorker("stuck-1"), _FakeWorker("stuck-2")]
        stats = _drain_with_timeout(workers, 1, max_live=1, grace_seconds=0.1)

        assert stats["live"] == 2
        assert stats["forced"] == 1, "the worker above the cap must be counted"

    def test_a_cap_of_zero_never_waits(self):
        """0 means 'never wait' — the pre-fix behaviour, available on purpose."""
        workers = [_FakeWorker("stuck-1")]
        start = time.monotonic()
        stats = _drain_with_timeout(workers, 1, max_live=0, grace_seconds=5.0)
        elapsed = time.monotonic() - start

        assert elapsed < 1.0, "a cap of 0 must not wait at all"
        assert stats["live"] == 1
        assert stats["forced"] == 1

    def test_the_wait_is_bounded_even_when_nothing_finishes(self):
        """THE CRITICAL PROPERTY: a permanently-stuck worker cannot stall the scan.

        Waiting for every abandoned artist would reintroduce the frozen scan
        the budget exists to prevent.
        """
        workers = [_FakeWorker("stuck-1"), _FakeWorker("stuck-2"), _FakeWorker("stuck-3")]
        start = time.monotonic()
        stats = _drain_with_timeout(workers, 1, max_live=1, grace_seconds=0.3)
        elapsed = time.monotonic() - start

        assert elapsed < 5.0, (
            f"drain took {elapsed:.2f}s for a 0.3s grace period — the wait is "
            "not clamped to the requested grace"
        )
        assert stats["live"] == 3

    def test_a_hard_ceiling_caps_an_oversized_grace_period(self):
        """A caller-supplied grace period cannot exceed the hard ceiling.

        The ceiling is not configurable on purpose: the drain must never be a
        vector for stalling the scan, whatever a caller passes. Asserted on the
        clamp directly — verifying it by TIMING a 1-hour wait would make the
        suite take an hour.
        """
        from services.scanning.pipelines import popularity_pipeline as pp

        assert pp._effective_grace_seconds(3600.0) == pp._ABANDONED_MAX_WAIT_SECONDS, (
            "a 1-hour grace period was not clamped by the hard ceiling — the "
            "drain could be made to stall the scan"
        )
        assert pp._effective_grace_seconds(1.0) == 1.0, (
            "a sane grace period must pass through unchanged"
        )
        assert pp._effective_grace_seconds(-5.0) == 0.0, (
            "a negative grace period must clamp to 0, not become a wait"
        )
        assert pp._effective_grace_seconds("garbage") == pp._ABANDONED_GRACE_SECONDS, (
            "an unparsable grace period must fall back to the default"
        )

    def test_a_worker_at_the_cap_is_not_waited_for(self):
        """Waiting is only needed when OVER the cap, not merely at it.

        One abandoned artist still winding down is harmless, so the drain must
        return immediately rather than burn the grace period every time.
        """
        workers = [_FakeWorker("still-winding-down")]
        start = time.monotonic()
        stats = _drain_with_timeout(workers, 1, max_live=1, grace_seconds=5.0)
        elapsed = time.monotonic() - start

        assert elapsed < 0.5, (
            f"waited {elapsed:.2f}s with the live count already at the cap"
        )
        assert stats["live"] == 1
        assert stats["forced"] == 0, "at the cap, nothing is 'over' it"

    def test_a_worker_that_unwinds_within_the_grace_period_is_not_forced(self):
        """The good path: cancellation works and the straggler disappears."""
        settles = _FakeWorker("unwinds")
        stuck = _FakeWorker("stuck")
        threading.Timer(0.15, lambda: setattr(settles, "_alive", False)).start()

        # Two live workers against a cap of 1 puts us OVER the cap, so the
        # helper actually waits and can observe the unwind.
        stats = _drain_with_timeout(
            [settles, stuck], 1, max_live=1, grace_seconds=2.0, timeout=10.0
        )

        assert stats["live"] == 1, (
            "the worker that unwound should be gone, leaving only the stuck one"
        )
        assert stats["forced"] == 0, (
            "the cap was met within the grace period, so nothing was forced"
        )


class TestMaxLiveAbandonedConfig:

    def test_default_is_one(self, monkeypatch):
        from services.scanning.pipelines import popularity_pipeline as pp

        monkeypatch.setattr(
            "helpers.config_helpers.get_feature",
            lambda key, default=None: default,
        )
        assert pp._resolve_max_live_abandoned() == pp._DEFAULT_MAX_LIVE_ABANDONED == 1

    def test_configured_value_is_honoured(self, monkeypatch):
        from services.scanning.pipelines import popularity_pipeline as pp

        monkeypatch.setattr(
            "helpers.config_helpers.get_feature",
            lambda key, default=None: 4,
        )
        assert pp._resolve_max_live_abandoned() == 4

    def test_a_bad_value_falls_back_to_the_default(self, monkeypatch):
        from services.scanning.pipelines import popularity_pipeline as pp

        monkeypatch.setattr(
            "helpers.config_helpers.get_feature",
            lambda key, default=None: "not-a-number",
        )
        assert pp._resolve_max_live_abandoned() == pp._DEFAULT_MAX_LIVE_ABANDONED

    def test_values_are_clamped(self, monkeypatch):
        from services.scanning.pipelines import popularity_pipeline as pp

        monkeypatch.setattr(
            "helpers.config_helpers.get_feature",
            lambda key, default=None: 9999,
        )
        assert pp._resolve_max_live_abandoned() == 16

        monkeypatch.setattr(
            "helpers.config_helpers.get_feature",
            lambda key, default=None: -5,
        )
        assert pp._resolve_max_live_abandoned() == 0


class TestArtistTimeoutConfig:
    """The per-artist budget is now settable, including 'disabled'."""

    @staticmethod
    def _with_config(monkeypatch, cfg):
        import helpers.config_helpers as ch

        monkeypatch.setattr(ch, "get_config", lambda: cfg)
        return ch

    def test_default_is_1800(self, monkeypatch):
        ch = self._with_config(monkeypatch, {})
        assert ch.get_artist_timeout_seconds() == 1800

    def test_a_configured_value_is_honoured(self, monkeypatch):
        ch = self._with_config(monkeypatch, {"popularity": {"artist_timeout_seconds": 900}})
        assert ch.get_artist_timeout_seconds() == 900

    def test_zero_disables_the_budget(self, monkeypatch):
        """0 must mean 'never abandon an artist', not 'abandon immediately'."""
        ch = self._with_config(monkeypatch, {"popularity": {"artist_timeout_seconds": 0}})
        assert ch.get_artist_timeout_seconds() == 0

        ch = self._with_config(monkeypatch, {"popularity": {"artist_timeout_seconds": -1}})
        assert ch.get_artist_timeout_seconds() == 0

    def test_a_positive_value_is_clamped_to_a_sane_band(self, monkeypatch):
        ch = self._with_config(monkeypatch, {"popularity": {"artist_timeout_seconds": 10}})
        assert ch.get_artist_timeout_seconds() == 60, "too small must clamp up"

        ch = self._with_config(monkeypatch, {"popularity": {"artist_timeout_seconds": 999999}})
        assert ch.get_artist_timeout_seconds() == 21600, "absurd must clamp down"

    def test_the_legacy_location_still_works(self, monkeypatch):
        """An existing config.yaml written before the move must keep working."""
        ch = self._with_config(
            monkeypatch, {"features": {"full_scan_artist_timeout_seconds": 1200}}
        )
        assert ch.get_artist_timeout_seconds() == 1200

    def test_the_new_location_wins_over_the_legacy_one(self, monkeypatch):
        ch = self._with_config(
            monkeypatch,
            {
                "popularity": {"artist_timeout_seconds": 400},
                "features": {"full_scan_artist_timeout_seconds": 1200},
            },
        )
        assert ch.get_artist_timeout_seconds() == 400

    def test_a_bad_value_falls_back_to_the_default(self, monkeypatch):
        ch = self._with_config(monkeypatch, {"popularity": {"artist_timeout_seconds": "junk"}})
        assert ch.get_artist_timeout_seconds() == 1800


class TestThePipelineUsesTheConfigurableBudget:
    """The full-scan loop must READ the configured budget, and 0 must disable it.

    ``_run_full_scan_as_artist_pipeline`` imports its collaborators INSIDE the
    function (``from db.repositories.library import get_all_artists`` etc.), so
    each is patched on its SOURCE module rather than on the pipeline module.
    """

    @staticmethod
    def _stub_scan(monkeypatch, captured: dict, budget: int) -> None:
        import services.scanning.pipelines.popularity_pipeline as pp
        import db.repositories.library as library_repo
        import services.scanning.pipeline as scan_pipeline
        import services.scanning.scan_state as scan_state

        def _fake_bounded(func, *args, **kwargs):
            captured["seconds"] = kwargs.get("seconds")
            return {"ok": True}

        monkeypatch.setattr(
            "services.popularity.scan_stage_runner._bounded_call_report",
            _fake_bounded,
        )
        monkeypatch.setattr(
            "helpers.config_helpers.get_artist_timeout_seconds", lambda: budget
        )
        # Function-local imports: patch the source modules.
        monkeypatch.setattr(library_repo, "get_all_artists", lambda: ["Artist A"])
        monkeypatch.setattr(scan_pipeline, "run_artist_scan_pipeline", lambda *a, **k: None)
        monkeypatch.setattr(scan_state, "clear_stop_request", lambda *a, **k: None)
        monkeypatch.setattr(scan_state, "is_stop_requested", lambda *a, **k: False)
        monkeypatch.setattr(scan_state, "save_artist_scan_checkpoint", lambda *a, **k: None)
        monkeypatch.setattr(scan_state, "clear_scan_checkpoint", lambda *a, **k: None)
        # The pipeline touches the scan_states TABLE through several paths
        # (module-level imports AND a re-import inside the function). None of
        # this test is about progress persistence, and there is no DB in the
        # unit environment, so every one is stubbed.
        monkeypatch.setattr(scan_state, "write_progress_with_current_artist", lambda *a, **k: None)
        monkeypatch.setattr(scan_state, "read_progress_file", lambda *a, **k: {})
        monkeypatch.setattr(pp, "write_progress_with_current_artist", lambda *a, **k: None)
        # ``is_stop_requested`` is imported at module level INTO the pipeline, so
        # patching only the source module still leaves the pipeline's own bound
        # reference hitting the DB. Both are patched.
        monkeypatch.setattr(pp, "is_stop_requested", lambda *a, **k: False)
        # Module-level names on the pipeline itself.
        monkeypatch.setattr(pp, "record_scan", lambda *a, **k: None)
        monkeypatch.setattr(pp, "log_unified", lambda *a, **k: None)
        monkeypatch.setattr(pp, "get_scan_progress_path", lambda *a, **k: "test_progress")

    def test_zero_budget_means_no_timeout_is_applied(self, monkeypatch):
        """With the budget disabled, ``seconds`` must be None.

        ``_bounded_call_report(seconds=None)`` runs the call directly with no
        join budget at all, which is exactly what "never abandon" requires.
        """
        import services.scanning.pipelines.popularity_pipeline as pp

        captured: dict[str, object] = {}
        self._stub_scan(monkeypatch, captured, budget=0)

        pp._run_full_scan_as_artist_pipeline(force=False)

        assert captured.get("seconds") is None, (
            "artist_timeout_seconds=0 must DISABLE the budget (pass seconds=None), "
            f"but the pipeline applied {captured.get('seconds')!r}"
        )

    def test_a_configured_budget_is_passed_through(self, monkeypatch):
        import services.scanning.pipelines.popularity_pipeline as pp

        captured: dict[str, object] = {}
        self._stub_scan(monkeypatch, captured, budget=900)

        pp._run_full_scan_as_artist_pipeline(force=False)

        assert captured.get("seconds") == 900.0, (
            "the configured artist budget must reach _bounded_call_report"
        )


class TestAbandoningAnArtistCancelsItsWorker:
    """THE CASCADE FIX: when an artist is abandoned it must be cancelled.

    Mutation-testing found this gap: every other test exercised the
    cancellation REGISTRY and the drain helper directly, so deleting the
    pipeline's ``request_cancel`` call was SURVIVED — the code could have
    stopped asking workers to unwind without any test noticing.
    """

    @staticmethod
    def _stub_pipeline(monkeypatch, budget_result: dict) -> None:
        """Make the single artist look abandoned, and record the side effects."""
        import services.scanning.pipelines.popularity_pipeline as pp
        import db.repositories.library as library_repo
        import services.scanning.pipeline as scan_pipeline
        import services.scanning.scan_state as scan_state

        monkeypatch.setattr(
            "services.popularity.scan_stage_runner._bounded_call_report",
            lambda func, *a, **k: dict(budget_result),
        )
        monkeypatch.setattr(
            "helpers.config_helpers.get_artist_timeout_seconds", lambda: 900
        )
        monkeypatch.setattr(library_repo, "get_all_artists", lambda: ["Warrel Dane"])
        monkeypatch.setattr(scan_pipeline, "run_artist_scan_pipeline", lambda *a, **k: None)
        monkeypatch.setattr(scan_state, "clear_stop_request", lambda *a, **k: None)
        monkeypatch.setattr(scan_state, "save_artist_scan_checkpoint", lambda *a, **k: None)
        monkeypatch.setattr(scan_state, "clear_scan_checkpoint", lambda *a, **k: None)
        monkeypatch.setattr(scan_state, "write_progress_with_current_artist", lambda *a, **k: None)
        monkeypatch.setattr(pp, "write_progress_with_current_artist", lambda *a, **k: None)
        monkeypatch.setattr(pp, "is_stop_requested", lambda *a, **k: False)
        monkeypatch.setattr(pp, "record_scan", lambda *a, **k: None)
        monkeypatch.setattr(pp, "log_unified", lambda *a, **k: None)
        monkeypatch.setattr(pp, "get_scan_progress_path", lambda *a, **k: "test_progress")

    def test_an_abandoned_artist_is_cancelled(self, monkeypatch):
        """Without this the orphan keeps running: the cascade."""
        import services.scanning.pipelines.popularity_pipeline as pp

        self._stub_pipeline(
            monkeypatch,
            {
                "ok": False,
                "abandoned": True,
                "reason": "exceeded 900s budget",
                "budget_seconds": 900,
                "thread": _FakeWorker("abandoned-worker"),
            },
        )

        pp._run_full_scan_as_artist_pipeline(force=False)

        assert cancel.is_cancelled("Warrel Dane") is True, (
            "an abandoned artist was NOT cancelled — its worker keeps running, "
            "holding DB/rate-limiter resources and making the next artist "
            "likelier to time out (the reported cascade)"
        )

    def test_a_completed_artist_is_not_cancelled(self, monkeypatch):
        """Only abandoned artists are cancelled, never successful ones."""
        import services.scanning.pipelines.popularity_pipeline as pp

        self._stub_pipeline(
            monkeypatch,
            {"ok": True, "abandoned": False, "thread": _FakeWorker("finished")},
        )

        pp._run_full_scan_as_artist_pipeline(force=False)

        assert cancel.is_cancelled("Warrel Dane") is False, (
            "a successful artist must never be cancelled"
        )

    def test_the_abandoned_worker_is_handed_to_the_drain(self, monkeypatch):
        """The worker must be tracked, or the pile-up cannot be bounded."""
        import services.scanning.pipelines.popularity_pipeline as pp

        worker = _FakeWorker("abandoned-worker")
        self._stub_pipeline(
            monkeypatch,
            {
                "ok": False,
                "abandoned": True,
                "reason": "exceeded 900s budget",
                "budget_seconds": 900,
                "thread": worker,
            },
        )

        seen: list[list] = []
        real_drain = pp._drain_abandoned_workers

        def _spy_drain(workers, position, max_live, **kwargs):
            seen.append(list(workers))
            return real_drain(workers, position, max_live, **kwargs)

        monkeypatch.setattr(pp, "_drain_abandoned_workers", _spy_drain)

        pp._run_full_scan_as_artist_pipeline(force=False)

        assert seen, "the drain was never called for an abandoned artist"
        assert worker in seen[0], (
            "the abandoned worker was not handed to the drain, so the pile-up "
            "of stragglers cannot be bounded"
        )
