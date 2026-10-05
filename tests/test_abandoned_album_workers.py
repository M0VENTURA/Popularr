"""An album that hits its budget must not leave an untracked worker behind.

``_bounded_call_report`` gives up at the per-album budget and returns
``abandoned`` — but the thread it spawned is a daemon that is *still running*,
holding a DB-pool connection and a rate-limiter slot. The artist-level path
always handed those to ``_drain_abandoned_workers``; the album path only
propagated the sentinel, so album stragglers accumulated unchecked and the
next album waited on a pool the previous one had not released.

Covers three things:

1. the straggler is REGISTERED (and the caller-visible sentinel never leaks it);
2. the shared drain is actually CALLED, with the configured cap, and a failure
   in the bookkeeping cannot become a new way for an already-abandoned phase to
   raise;
3. the artist-level and album-level drains are ONE implementation — a second
   copy would drift the moment either side changed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from services.popularity import scan_stage_runner as ssr
from services.scanning import abandoned_workers as aw

REPO = Path(__file__).resolve().parents[1]


class _FakeWorker:
    """A thread stand-in whose liveness the drain can ask about."""

    def __init__(self, alive: bool = True, name: str = "album-phase"):
        self._alive = alive
        self.name = name

    def is_alive(self) -> bool:
        return self._alive


@pytest.fixture(autouse=True)
def _clean_registry():
    """The registry is module-level BY DESIGN, so it must be reset per test."""
    ssr._abandoned_album_workers.clear()
    yield
    ssr._abandoned_album_workers.clear()


@pytest.fixture
def reports(monkeypatch):
    """Replace ``_bounded_call_report`` and return a recorder of what was asked."""
    calls: list[dict] = []
    state: dict = {"report": None}

    def _fake(func, *args, seconds=None, section=None, log_context=None, **kwargs):
        calls.append({"seconds": seconds, "section": section})
        return state["report"]

    monkeypatch.setattr(ssr, "_bounded_call_report", _fake)
    return {"calls": calls, "set": lambda report: state.__setitem__("report", report)}


def _run(seconds=1.0):
    return ssr._bounded_album_phase(
        lambda: "raw",
        seconds=seconds,
        section="enrich_album",
    )


def _abandoned_report(worker: _FakeWorker | None = None) -> dict:
    return {
        "ok": False,
        "result": None,
        "abandoned": True,
        "reason": "exceeded 900.0s budget",
        "budget_seconds": 900.0,
        "thread": worker if worker is not None else _FakeWorker(),
    }


# ---------------------------------------------------------------------------
# 1. Registration
# ---------------------------------------------------------------------------
class TestTheStragglerIsRegistered:
    def test_an_abandoned_phase_hands_over_its_worker(self, reports):
        worker = _FakeWorker()
        reports["set"](_abandoned_report(worker))

        _run()

        assert ssr._abandoned_album_workers == [worker], (
            "the worker the budget stopped WAITING for must be tracked, or it "
            "keeps holding a DB connection and a rate-limiter slot unseen"
        )

    def test_the_caller_never_sees_the_thread_handle(self, reports):
        """The sentinel's shape is a contract with every album call site."""
        reports["set"](_abandoned_report())

        result = _run()

        assert result == {
            "ok": False,
            "abandoned": True,
            "reason": "exceeded 900.0s budget",
            "budget_seconds": 900.0,
        }
        assert "thread" not in result, (
            "the handle is for the drain, not for callers that only test "
            "result.get('abandoned')"
        )

    def test_a_completed_phase_registers_nothing(self, reports):
        reports["set"]({
            "ok": True,
            "result": [{"track_id": "t1"}],
            "abandoned": False,
            "reason": None,
        })

        result = _run()

        assert result == [{"track_id": "t1"}], "a finished phase unwraps to its result"
        assert ssr._abandoned_album_workers == []

    def test_the_budget_disabled_path_registers_nothing(self, reports):
        """``seconds is None`` returns the raw value and never builds a report."""
        reports["set"]({"some": "raw payload"})

        result = _run(seconds=None)

        assert result == {"some": "raw payload"}
        assert ssr._abandoned_album_workers == []

    def test_the_registry_is_module_level_so_the_cap_is_per_run(self):
        """Two scopes must see ONE list — a per-call copy would reset the cap."""
        first = ssr._abandoned_album_workers
        first.append(_FakeWorker())

        assert ssr._abandoned_album_workers is first
        assert ssr._abandoned_album_workers == [first[0]]


# ---------------------------------------------------------------------------
# 2. The drain actually runs — and cannot break an already-abandoned phase
# ---------------------------------------------------------------------------
class TestTheDrainIsCalled:
    def test_the_shared_drain_sees_the_registered_worker(self, reports, monkeypatch):
        seen: dict = {}
        worker = _FakeWorker()

        def _spy(workers, position, max_live, **kwargs):
            seen["workers"] = list(workers)
            seen["max_live"] = max_live
            seen["label"] = kwargs.get("label")
            return {"live": 0, "finished": 0, "forced": 0}

        monkeypatch.setattr(aw, "drain_abandoned_workers", _spy)
        reports["set"](_abandoned_report(worker))

        _run()

        assert seen["workers"] == [worker], "the drain must receive the straggler"
        assert seen["max_live"] == aw.resolve_max_live_abandoned(), (
            "the album path must honour the same configured cap as the artist "
            "path (features.full_scan_max_abandoned_workers)"
        )

    def test_the_label_says_scan_not_full_scan(self, reports, monkeypatch):
        """Album phases run in every popularity scan, not just full ones."""
        seen: dict = {}
        monkeypatch.setattr(
            aw,
            "drain_abandoned_workers",
            lambda *a, **k: seen.update(k) or {"live": 0, "finished": 0, "forced": 0},
        )
        reports["set"](_abandoned_report())

        _run()

        assert seen.get("label") == "[SCAN]"

    def test_a_drain_failure_cannot_break_an_abandoned_phase(self, reports, monkeypatch):
        """Bookkeeping must never become a new way for the phase to raise."""
        def _boom(*args, **kwargs):
            raise RuntimeError("drain exploded")

        monkeypatch.setattr(aw, "drain_abandoned_workers", _boom)
        reports["set"](_abandoned_report())

        result = _run()

        assert result.get("abandoned") is True, (
            "the caller still has to skip this album even if the drain failed"
        )

    def test_the_worker_is_kept_when_the_drain_fails(self, reports, monkeypatch):
        """A failed drain must not silently FORGET the straggler it couldn't drain."""
        def _boom(*args, **kwargs):
            raise RuntimeError("drain exploded")

        monkeypatch.setattr(aw, "drain_abandoned_workers", _boom)
        worker = _FakeWorker()
        reports["set"](_abandoned_report(worker))

        _run()

        assert ssr._abandoned_album_workers == [worker], (
            "dropping it here would hide the very workers the cap exists to bound"
        )


# ---------------------------------------------------------------------------
# 3. One implementation, not two
# ---------------------------------------------------------------------------
class TestTheArtistAndAlbumPathsShareOneImplementation:
    def test_the_pipeline_reexports_the_shared_functions(self):
        from services.scanning.pipelines import popularity_pipeline as pp

        assert pp._drain_abandoned_workers is aw.drain_abandoned_workers
        assert pp._live_abandoned_workers is aw.live_abandoned_workers
        assert pp._effective_grace_seconds is aw.effective_grace_seconds
        assert pp._resolve_max_live_abandoned is aw.resolve_max_live_abandoned
        assert pp._ABANDONED_GRACE_SECONDS == aw.ABANDONED_GRACE_SECONDS
        assert pp._ABANDONED_MAX_WAIT_SECONDS == aw.ABANDONED_MAX_WAIT_SECONDS
        assert pp._DEFAULT_MAX_LIVE_ABANDONED == aw.DEFAULT_MAX_LIVE_ABANDONED

    def test_a_second_copy_of_the_drain_would_be_a_drift_risk(self):
        """Guards the extraction itself: exactly one ``def drain_abandoned_workers``."""
        defs = [
            path
            for path in (REPO / "services").rglob("*.py")
            if "def drain_abandoned_workers(" in path.read_text(
                encoding="utf-8", errors="replace"
            )
        ]
        assert defs == [REPO / "services" / "scanning" / "abandoned_workers.py"], (
            f"expected a single definition, found {defs}"
        )
