"""Album-page "full" scans pass through the shared popularity-scan guard.

Reported log (2026-10-12)::

    [SCAN] section abandoned (timeout) section='album_track_phase'
    elapsed_s=900.001 budget_seconds=900.0
    ...
    Popularity Scan - no tracks processed: 1 album(s) exceeded their
    per-album time budget

for an album that had been running for only ~5 seconds. Two generations of
the SAME album scan were interleaved in the log (tracks processed at 16:10
re-processed at 16:26). Root cause: the album page's "full" scan launched
``run_album_pipeline`` with ``run_async`` and NO duplicate-run guard — the
only popularity-scan trigger in the app that skipped the guard. A second
trigger overlapped the first; both ran the popularity pipeline (Step 2/3 of
the album pipeline) against the shared 1 req/s MusicBrainz budget, the
skip-guards self-froze each other's rows, and everything crawled.

The fix routes BOTH the artist and album "full" branches through
``guard_and_launch_popularity_scan`` (extracted from
``_start_targeted_popularity_scan`` so every route-level trigger shares ONE
implementation with the dashboard API / scheduler semantics: in-process
runtime registry, cross-process ``is_popularity_scan_active()``, stale-row
self-heal, worker spawn + runtime registry write).
"""

from __future__ import annotations

import sys
import time
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

ROUTES_SOURCE = (REPO_ROOT / "routes" / "scan_routes" / "artist_album.py").read_text(encoding="utf-8")
COMMON_SOURCE = (REPO_ROOT / "routes" / "scan_routes" / "_common.py").read_text(encoding="utf-8")


def _function_body(source: str, func_name: str) -> str:
    """Slice ONE async route function's body by its decorator + def markers.

    Scoping a wiring assertion to the function body avoids false positives
    from OTHER call sites in the same file (e.g. the track-rescan route still
    legitimately calls ``run_async(run_artist_pipeline, ...)``).
    """
    start = source.index(f"async def {func_name}(")
    # Everything up to the NEXT route decorator or end of file.
    tail = source[start:]
    end_marker = tail.find("\n@scans_bp.route")
    if end_marker == -1:
        end_marker = len(tail)
    return tail[:end_marker]


# ---------------------------------------------------------------------------
# 1. Wiring — the "full" branches go through the shared guard
# ---------------------------------------------------------------------------

class TestTheAlbumFullBranchIsGuarded:
    def test_album_full_dispatch_calls_the_shared_guard(self):
        body = _function_body(ROUTES_SOURCE, "scan_album_custom")
        assert "guard_and_launch_popularity_scan(" in body, (
            "the album 'full' scan is a popularity pipeline and must pass "
            "through the same guard as every other popularity trigger"
        )

    def test_album_full_no_longer_uses_bare_run_async(self):
        body = _function_body(ROUTES_SOURCE, "scan_album_custom")
        assert "run_async(run_album_pipeline" not in body, (
            "the pre-fix trigger launched the album pipeline with NO guard — "
            "the reported overlap"
        )

    def test_album_full_passes_through_the_album_pipeline(self):
        body = _function_body(ROUTES_SOURCE, "scan_album_custom")
        assert "guard_and_launch_popularity_scan(\n            run_album_pipeline," in body, (
            "the guard must wrap the album pipeline, not a different runner"
        )

    def test_album_full_rejects_with_a_warning_when_busy(self):
        body = _function_body(ROUTES_SOURCE, "scan_album_custom")
        assert "A popularity scan is already running" in body, (
            "a rejected duplicate must surface to the user, not silently no-op"
        )


class TestTheArtistFullBranchIsGuarded:
    """The artist 'full' scan runs the SAME popularity pipeline, so it gets
    the SAME guard — the two pages must not disagree."""

    def test_artist_full_dispatch_calls_the_shared_guard(self):
        body = _function_body(ROUTES_SOURCE, "scan_artist_custom")
        assert "guard_and_launch_popularity_scan(" in body, (
            "the artist 'full' scan is a popularity pipeline and must pass "
            "through the same guard as every other popularity trigger"
        )

    def test_artist_full_no_longer_uses_bare_run_async(self):
        body = _function_body(ROUTES_SOURCE, "scan_artist_custom")
        assert "run_async(run_artist_pipeline" not in body, (
            "the pre-fix artist trigger launched the artist pipeline with NO guard"
        )


class TestTheGuardHasASingleDefinition:
    def test_the_helper_is_defined_once(self):
        assert COMMON_SOURCE.count("def guard_and_launch_popularity_scan(") == 1, (
            "the guard must have ONE definition — duplication is how the "
            "two paths drifted before"
        )

    def test_the_old_private_helper_is_gone(self):
        assert "_start_targeted_popularity_scan" not in ROUTES_SOURCE, (
            "the in-route private helper was extracted to _common; a stale "
            "copy would let the two call sites drift again"
        )
        assert "_start_targeted_popularity_scan" not in COMMON_SOURCE

    def test_the_targeted_branches_still_go_through_the_guard(self):
        # The popularity/metadata/singles/finalise branches must keep using
        # the shared guard (they used the private helper before).
        assert ROUTES_SOURCE.count(
            "guard_and_launch_popularity_scan("
        ) == 4, "artist full + album full + 2 targeted branches"


# ---------------------------------------------------------------------------
# 2. Behaviour — the guard's three outcomes + worker spawn
# ---------------------------------------------------------------------------

class TestGuardAndLaunchBehaviour:
    """Drive ``guard_and_launch_popularity_scan`` with a stubbed runtime.

    The helper imports its runtime symbols INSIDE the function body, so
    patching the module attributes (`services.scanning.runtime_state`,
    `...pipelines.popularity_pipeline`) is the effective seam.
    """

    @staticmethod
    def _patch(monkeypatch, *, runtime_running=False, scan_active=False,
               live_owner=None):
        import services.scanning.runtime_state as rt
        import services.scanning.pipelines.popularity_pipeline as pp

        calls = {"set_runtime": [], "cleared": [], "cleared_files": [], "get_runtime": []}

        monkeypatch.setattr(rt, "is_runtime_running", lambda name: runtime_running)
        monkeypatch.setattr(rt, "set_runtime", lambda *a, **k: calls["set_runtime"].append((a, k)))
        monkeypatch.setattr(rt, "clear_runtime", lambda *a, **k: None)
        monkeypatch.setattr(rt, "get_runtime", lambda name: live_owner if name == "popularity" else None)

        monkeypatch.setattr(pp, "is_popularity_scan_active", lambda: scan_active)

        import services.scanning.scan_state as ss
        monkeypatch.setattr(ss, "read_progress_file", lambda path: {"is_running": True})
        monkeypatch.setattr(ss, "get_scan_progress_path", lambda t: f"/p/{t}")
        monkeypatch.setattr(
            ss, "clear_progress_file",
            lambda path: calls["cleared_files"].append(path),
        )
        return calls

    def test_rejects_when_runtime_has_a_live_scan(self, monkeypatch):
        calls = self._patch(monkeypatch, runtime_running=True)
        ran: list[str] = []

        from routes.scan_routes._common import guard_and_launch_popularity_scan

        started = guard_and_launch_popularity_scan(
            lambda **kw: ran.append("runner"), scan_type="full"
        )

        assert started is False
        assert ran == [], "the runner must not start"
        assert calls["set_runtime"] == [], "nothing should be registered"
        assert calls["cleared_files"] == [], "no stale rows to heal"

    def test_rejects_when_a_live_owner_holds_the_shared_state(self, monkeypatch):
        # Cross-process: the shared scan-stated row says running AND a live
        # thread in THIS worker owns it → reject (never clear a live scan).
        live = {"thread": threading.current_thread()}
        calls = self._patch(monkeypatch, scan_active=True, live_owner=live)
        ran: list[str] = []

        from routes.scan_routes._common import guard_and_launch_popularity_scan

        started = guard_and_launch_popularity_scan(
            lambda **kw: ran.append("runner"), scan_type="full"
        )

        assert started is False
        assert ran == []
        assert calls["cleared_files"] == [], (
            "the self-heal must not clear a genuinely running scan"
        )

    def test_self_heals_a_stale_row_and_starts(self, monkeypatch):
        # No live owner anywhere → the shared row is a phantom; clear it and
        # start THIS scan.
        calls = self._patch(monkeypatch, scan_active=True, live_owner=None)
        ran: list[str] = []

        from routes.scan_routes._common import guard_and_launch_popularity_scan

        started = guard_and_launch_popularity_scan(
            lambda **kw: ran.append("runner"), scan_type="full"
        )

        assert started is True
        assert calls["cleared_files"], (
            "a stale 'running' row must be cleared so the scan is not blocked"
        )
        # The worker runs in a background thread — give it a moment.
        _wait_for(lambda: len(ran) > 0)
        assert ran == ["runner"]
        assert calls["set_runtime"], "the launched worker must be registered"

    def test_starts_directly_when_idle(self, monkeypatch):
        calls = self._patch(monkeypatch, scan_active=False)
        ran: list[str] = []

        from routes.scan_routes._common import guard_and_launch_popularity_scan

        started = guard_and_launch_popularity_scan(
            lambda **kw: ran.append("runner"), scan_type="full"
        )

        assert started is True
        assert calls["cleared_files"] == [], "nothing stale when idle"
        _wait_for(lambda: len(ran) > 0)
        assert ran == ["runner"]
        assert calls["set_runtime"]

    def test_worker_clears_the_runtime_registry_after_finishing(self, monkeypatch):
        # The helper's ``finally`` must clear the runtime registry so the
        # NEXT scan is not blocked by a phantom "running".
        import services.scanning.runtime_state as rt
        monkeypatch.setattr(rt, "is_runtime_running", lambda name: False)

        from services.scanning import runtime_state as real_rt
        cleared = []

        def _clear_runtime(name):
            cleared.append(name)

        monkeypatch.setattr(rt, "clear_runtime", _clear_runtime)
        monkeypatch.setattr(rt, "set_runtime", lambda *a, **k: None)
        monkeypatch.setattr(real_rt, "get_runtime", lambda name: None)

        import services.scanning.pipelines.popularity_pipeline as pp
        monkeypatch.setattr(pp, "is_popularity_scan_active", lambda: False)

        ran: list[str] = []

        from routes.scan_routes._common import guard_and_launch_popularity_scan

        started = guard_and_launch_popularity_scan(
            lambda **kw: ran.append("runner"), scan_type="full"
        )
        assert started is True
        _wait_for(lambda: len(ran) > 0)
        _wait_for(lambda: "popularity" in cleared)
        assert "popularity" in cleared, (
            "the worker must clear the runtime registry when it finishes"
        )


def _wait_for(condition, timeout: float = 5.0) -> None:
    """Poll a condition with a hard ceiling (tests spawn real threads)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError(f"condition not met within {timeout}s")