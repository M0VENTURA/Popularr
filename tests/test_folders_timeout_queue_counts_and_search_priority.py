"""Three reported faults:

1. "Matched & Unmatched Folders: Could not load folders: Request timed out
   after 30s".
2. The processor card said "64 queued" while the queue pills said "145 Queued".
3. "Search failed: Soulseek search slot did not free up. Try again shortly." —
   and, correctly, "It used to wait until it freed up."
"""

from __future__ import annotations

import ast
import pathlib
import re
import time

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
FOLDER_SVC = REPO_ROOT / "services" / "downloads" / "download_folder_service.py"
DOWNLOADS_ROUTE = REPO_ROOT / "routes" / "downloads.py"
MONITOR_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "monitor.js"
SLSKD_JS = REPO_ROOT / "test_site" / "static" / "js" / "services" / "slskd.js"
DIAG_SVC = REPO_ROOT / "services" / "queue" / "queue_diagnostics_service.py"
QUEUE_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "download-queue.js"
PRIORITY = REPO_ROOT / "services" / "downloads" / "manual_search_priority.py"
PIPELINE = REPO_ROOT / "services" / "downloads" / "download_pipeline_service.py"
SEARCH_ROUTES = REPO_ROOT / "routes" / "download_search_routes.py"


def _src(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _func(source: str, name: str) -> str:
    lines = source.splitlines()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1: node.end_lineno])
    raise AssertionError(f"no function {name}")


# ---------------------------------------------------------------------------
# 1. The folders endpoint must always answer
# ---------------------------------------------------------------------------

class TestTheFoldersScanIsBounded:
    def test_a_wall_time_budget_exists(self):
        src = _src(FOLDER_SVC)
        assert "_FOLDERS_SCAN_BUDGET_SECONDS" in src
        budget = float(re.search(
            r"_FOLDERS_SCAN_BUDGET_SECONDS = ([\d.]+)", src).group(1))
        assert 5.0 <= budget <= 25.0, (
            "the budget must fit inside the client's timeout"
        )

    def test_the_walk_receives_the_deadline(self):
        body = _func(_src(FOLDER_SVC), "get_unmatched_folders")
        assert "deadline=_scan_deadline" in body, (
            "the directory walk was the unbounded half of this endpoint"
        )

    def test_the_candidate_walk_honours_a_deadline_inside_recursion(self):
        body = _func(_src(FOLDER_SVC), "_collect_album_folders")
        assert "deadline" in body.split("def _recurse")[1].split("def ")[0] or (
            "time.monotonic() >= deadline" in body
        ), "a deep tree must stop, not run to max_depth regardless"

    def test_a_candidate_cap_exists(self):
        src = _src(FOLDER_SVC)
        assert "_MAX_FOLDER_CANDIDATES" in src
        assert "len(candidates) >= max_candidates" in _func(
            src, "_iter_matched_folder_candidates")

    def test_truncation_is_reported(self):
        body = _func(_src(FOLDER_SVC), "get_unmatched_folders")
        assert '"truncated": _scan_truncated' in body, (
            "a partial list must not read as the complete set of folders"
        )

    def test_the_extensions_are_checked_before_stat(self):
        """An isfile() per artwork file was a network round trip each."""
        src = _src(FOLDER_SVC)
        body = _func(src, "_collect_album_folders")
        direct = body.split("def _has_direct_audio")[1].split("def _has_disc_audio")[0]
        # Ignore the explanatory comment: compare the CODE order.
        code = "\n".join(
            line for line in direct.splitlines()
            if not line.strip().startswith("#")
        )
        assert code.index("SUPPORTED_AUDIO_FORMATS") < code.index("os.path.isfile")

    def test_the_route_is_offloaded(self):
        body = _func(_src(DOWNLOADS_ROUTE), "api_get_unmatched_folders")
        assert "async def" in body
        assert "asyncio.to_thread" in body

    def test_the_client_gives_the_server_room(self):
        src = _src(MONITOR_JS)
        timeout = int(re.search(r"UNMATCHED_TIMEOUT_MS = (\d+)", src).group(1))
        assert timeout > 20000, "the default 30 s sat at the server's own limit"
        assert "timeoutMs: UNMATCHED_TIMEOUT_MS" in src

    def test_the_poll_interval_exceeds_the_timeout(self):
        src = _src(MONITOR_JS)
        refresh = int(re.search(r"UNMATCHED_REFRESH_MS = (\d+)", src).group(1))
        timeout = int(re.search(r"UNMATCHED_TIMEOUT_MS = (\d+)", src).group(1))
        assert refresh > timeout, (
            "an interval equal to the timeout re-issues the scan the instant it "
            "is abandoned, so the endpoint is never idle"
        )

    def test_the_one_query_is_not_duplicated(self):
        body = _func(_src(FOLDER_SVC), "get_unmatched_folders")
        code = "\n".join(
            line for line in body.splitlines()
            if not line.strip().startswith("#")
        )
        assert code.count("get_active_releases_with_progress()") == 1, (
            "the same aggregate was fetched twice per request"
        )


# ---------------------------------------------------------------------------
# 2. The card and the pills must agree
# ---------------------------------------------------------------------------

class TestTheQueueCountsAgree:
    def _stats(self, histogram):
        from services.queue import queue_diagnostics_service as qds

        original = qds.get_queue_status_counts
        qds.get_queue_status_counts = lambda: dict(histogram)
        try:
            return qds._queue_processor_stats()
        finally:
            qds.get_queue_status_counts = original

    HIST = {
        "queued": 64, "searching": 20, "processing": 10, "queried": 5,
        "copy_recommended": 3, "backed_off": 7, "pending_release": 30,
        "matched": 2, "pending_match": 3, "discovered": 1, "downloading": 1,
        "completed": 750, "unmatched": 5, "possible_duplicate": 2,
        "failed": 29, "imported": 3050,
    }

    def test_queued_matches_the_pill(self):
        from services.queue.queue_constraints import ACTIVE_SECTION

        expected = max(
            0,
            sum(self.HIST.get(s, 0) for s in ACTIVE_SECTION) - self.HIST["downloading"],
        )
        stats = self._stats(self.HIST)
        assert stats["queued"] == expected == 145

    def test_ready_matches_the_pill(self):
        from services.queue.queue_constraints import READY_SECTION

        expected = sum(self.HIST.get(s, 0) for s in READY_SECTION)
        assert self._stats(self.HIST)["ready"] == expected

    def test_failed_matches_the_pill(self):
        from services.queue.queue_constraints import FAILED_SECTION

        expected = sum(self.HIST.get(s, 0) for s in FAILED_SECTION)
        assert self._stats(self.HIST)["failed"] == expected

    def test_the_narrow_count_is_still_reported(self):
        assert self._stats(self.HIST)["queued_raw"] == 64

    def test_imported_is_never_counted_as_queued(self):
        stats = self._stats(self.HIST)
        assert stats["queued"] < self.HIST["imported"]


# ---------------------------------------------------------------------------
# 3. A manual search gets the slot
# ---------------------------------------------------------------------------

class TestTheManualSearchGetsTheSlot:
    def test_a_priority_module_exists(self):
        assert PRIORITY.is_file()

    def test_a_claim_expires(self, tmp_path, monkeypatch):
        target = tmp_path / "priority.json"
        monkeypatch.setenv("MANUAL_SEARCH_PRIORITY_FILE", str(target))
        from services.downloads import manual_search_priority as msp

        msp.claim_priority("Muse - Hysteria")
        assert msp.is_priority_claimed() is True

        # Rewrite the claim in the past: a closed tab must not park the queue.
        import json

        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["expires_at"] = time.time() - 1
        target.write_text(json.dumps(payload), encoding="utf-8")
        assert msp.is_priority_claimed() is False

    def test_no_file_means_no_claim(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MANUAL_SEARCH_PRIORITY_FILE", str(tmp_path / "absent.json"))
        from services.downloads import manual_search_priority as msp

        assert msp.is_priority_claimed() is False

    def test_release_clears_the_claim(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MANUAL_SEARCH_PRIORITY_FILE", str(tmp_path / "p.json"))
        from services.downloads import manual_search_priority as msp

        msp.claim_priority("x")
        msp.release_priority()
        assert msp.is_priority_claimed() is False

    def test_the_search_route_claims_it(self):
        body = _func(_src(SEARCH_ROUTES), "slskd_search")
        assert "claim_priority" in body

    def test_the_worker_yields_before_searching(self):
        src = _src(PIPELINE)
        assert "_wait_for_manual_search_priority" in src
        search_block = src.split("slskd.search_and_filter(")[0]
        assert "_wait_for_manual_search_priority()" in search_block.rsplit(
            "for idx, q in enumerate", 1)[-1], (
            "the worker must check for a waiting human before taking the slot"
        )

    def test_the_worker_wait_is_bounded(self):
        body = _func(_src(PIPELINE), "_wait_for_manual_search_priority")
        assert "PRIORITY_WINDOW_SECONDS" in body
        assert "min(" in body, "an unbounded sleep could park the queue"

    def test_the_client_waits_longer_than_one_queue_search(self):
        src = _src(SLSKD_JS)
        retries = int(re.search(r"SLOT_MAX_RETRIES = (\d+)", src).group(1))
        interval = int(re.search(r"SLOT_RETRY_INTERVAL_MS = (\d+)", src).group(1))
        total = retries * interval / 1000.0
        assert total >= 150, (
            f"a single queue search can run 145 s, so {total:.0f} s times out by "
            "arithmetic rather than because the slot is stuck"
        )
