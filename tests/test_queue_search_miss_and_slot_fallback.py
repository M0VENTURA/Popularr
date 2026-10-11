"""Queue log: one warning per search run, and a slot-less peer is skipped — not fatal.

Three defects read straight off the 2026-10-10 queue.log, all pre-existing:

1. **Per-query WARNING spam.** A search run fans out over fallback queries
   and bitrate tiers (the log shows ``queries=18``…``21``); every fruitless
   query emitted "No result met min_score" at WARNING. One doomed item
   produced ~20 identical lines every ~15s — while holding the queue cycle
   lock. That was most of queue.log's 2.6MB/day and it buried the real
   signals. Now the run collects the misses and warns ONCE with the closest
   attempt.

2. **A slot-less BEST candidate failed the whole item.** The Netherwilds
   "Peasant Pit" case in the log: ``Selected download candidate … candidates=2``
   then ``Best match peer has 0 free slots — skipping download`` then the
   item FAILED — while a second qualifying candidate sat untried. A retry
   ~25min later succeeded from the other peer. The candidate loop already
   knew how to skip slot-less peers; the early return never let it run for
   the best candidate.

3. **The dual-driver lock skip logged at INFO** — "Another queue cycle holds
   the lock — skipping cycle" ~4×/minute forever (expected contention between
   the standalone worker and the scheduler thread). Thousands of identical
   lines a day; now DEBUG.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _strip_python(src: str) -> str:
    src = re.sub(r'""".*?"""', "", src, flags=re.S)
    src = re.sub(r"'''.*?'''", "", src, flags=re.S)
    return "\n".join(line.split("#", 1)[0] for line in src.splitlines())


class _LoggerRecorder:
    """Stands in for the structlog logger; records (method, message, kwargs)."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def __getattr__(self, name):
        def _rec(*args, **kwargs):
            self.calls.append((name, str(args[0]) if args else "", kwargs))

        return _rec

    def of(self, method: str, contains: str = "") -> list[tuple[str, str, dict]]:
        return [
            c for c in self.calls
            if c[0] == method and contains in c[1]
        ]


@pytest.fixture
def dps(monkeypatch):
    import services.downloads.download_pipeline_service as mod

    rec = _LoggerRecorder()
    monkeypatch.setattr(mod, "logger", rec)
    mod._blocked_peers.clear()
    return mod, rec


def _result(filename="peer\\song.flac", username="peer", score_bitrate=320, **extra):
    r = {
        "filename": filename,
        "username": username,
        "size": 10_000_000,
        "bitrate": score_bitrate,
        "extension": "flac",
        "is_lossless": True,
        "has_free_upload_slot": True,
        "length_seconds": 200,
    }
    r.update(extra)
    return r


# ---------------------------------------------------------------------------
# 1. ONE min_score warning per run — the miss sink
# ---------------------------------------------------------------------------


class TestMissSinkConsolidatesTheWarning:
    def test_with_a_sink_no_warning_and_one_miss_recorded(self, dps):
        mod, rec = dps
        misses: list[dict] = []
        best = mod._select_best_result(
            [_result(filename="junk.flac", username="junkpeer")],
            expected_artist="Comeback Kid",
            expected_title="Underneath the Pressure",
            miss_sink=misses,
        )
        assert best is None
        assert len(misses) == 1
        assert misses[0]["candidates"] == 1
        assert rec.of("warning", "No result met min_score") == [], (
            "the per-query warning is exactly the spam this fix removes"
        )
        assert rec.of("debug", "Query yielded no qualifying candidate"), (
            "the per-query miss must still be visible at DEBUG"
        )

    def test_without_a_sink_the_warning_still_fires(self, dps):
        # Back-compat control: the literal contract stays for any caller that
        # does not pass a sink.
        mod, rec = dps
        best = mod._select_best_result(
            [_result(filename="junk.flac", username="junkpeer")],
            expected_artist="Comeback Kid",
            expected_title="Underneath the Pressure",
        )
        assert best is None
        assert rec.of("warning", "No result met min_score")

    def test_the_consolidated_warning_names_the_closest_miss_once(self, dps):
        mod, rec = dps
        misses = [
            {"min_score": 45.0, "top_score": 0.0, "candidates": 0,
             "rejected": {}, "top_candidate": ""},
            {"min_score": 45.0, "top_score": 22.0, "candidates": 9,
             "rejected": {"no_artist_evidence": 9}, "top_candidate": "a.flac"},
            {"min_score": 45.0, "top_score": 40.1, "candidates": 5,
             "rejected": {"year_mismatch": 5}, "top_candidate": "best.flac"},
        ]
        mod._warn_closest_search_miss(
            misses,
            artist="Comeback Kid",
            title="Underneath the Pressure",
            album="Chosen Hell",
            queries_tried=21,
        )
        warnings = rec.of("warning", "No result met min_score")
        assert len(warnings) == 1, "exactly ONE warning per search run"
        kwargs = warnings[0][2]
        # Most candidates wins the pick when counts differ; here 9 > 5, and
        # the pick must carry THAT attempt's reject breakdown.
        assert kwargs["candidates"] == 9
        assert kwargs["rejected"] == {"no_artist_evidence": 9}
        assert kwargs["queries_tried"] == 21
        assert kwargs["artist"] == "Comeback Kid"

    def test_ties_go_to_the_highest_top_score(self, dps):
        mod, rec = dps
        misses = [
            {"min_score": 45.0, "top_score": 12.0, "candidates": 4,
             "rejected": {}, "top_candidate": "low.flac"},
            {"min_score": 45.0, "top_score": 41.0, "candidates": 4,
             "rejected": {}, "top_candidate": "high.flac"},
        ]
        mod._warn_closest_search_miss(
            misses, artist="A", title="T", album=None, queries_tried=4
        )
        warnings = rec.of("warning", "No result met min_score")
        assert len(warnings) == 1
        assert warnings[0][2]["top_candidate"] == "high.flac"

    def test_all_queries_returning_nothing_logs_no_warning(self, dps):
        # The no_results search event already says it — no extra line.
        mod, rec = dps
        misses = [
            {"min_score": 45.0, "top_score": 0.0, "candidates": 0,
             "rejected": {}, "top_candidate": ""},
        ] * 5
        mod._warn_closest_search_miss(
            misses, artist="A", title="T", album=None, queries_tried=5
        )
        assert rec.of("warning") == []

    def test_empty_misses_log_nothing(self, dps):
        mod, rec = dps
        mod._warn_closest_search_miss(
            [], artist="A", title="T", album=None, queries_tried=0
        )
        assert rec.calls == []


# ---------------------------------------------------------------------------
# 2. A slot-less BEST candidate falls through to the next candidate
# ---------------------------------------------------------------------------


class _FakeSlskd:
    def __init__(self, accept: set[str] | None = None):
        self.accept = accept or set()
        self.calls: list[tuple[str, str]] = []

    def download_file(self, username, filename, size=None):
        self.calls.append((username, filename))
        return (username, filename) in self.accept or not self.accept


class TestSlotlessBestCandidateFallsThrough:
    def _patch_exists(self, monkeypatch, mod, exists=True):
        monkeypatch.setattr(mod, "_queue_item_exists", lambda qid: exists)

    def test_the_second_candidate_is_tried_when_the_best_peer_is_full(self, dps, monkeypatch):
        # THE Netherwilds case: candidates=2, best peer has 0 free slots.
        mod, rec = dps
        self._patch_exists(monkeypatch, mod)
        best = _result(filename="full-peer.flac", username="ccrackkk",
                       has_free_upload_slot=False)
        second = _result(filename="open-peer.flac", username="danishKing")
        slskd = _FakeSlskd(accept={("danishKing", "open-peer.flac")})

        success, chosen, fail_reason = mod._attempt_candidate_downloads(
            slskd, [best, second], queue_id=1,
            expected_artist="Netherwilds", expected_title="Peasant Pit",
        )
        assert success is True
        assert chosen["username"] == "danishKing"
        # fail_reason is only meaningful when success is False — the caller
        # reads it solely on the failure path.
        # The slot-less peer is still remembered so its file is not re-offered.
        assert mod._is_peer_blocked("ccrackkk", "full-peer.flac")

    def test_all_slotless_still_fails_with_the_contract_reason(self, dps, monkeypatch):
        mod, rec = dps
        self._patch_exists(monkeypatch, mod)
        c1 = _result(filename="f1.flac", username="p1", has_free_upload_slot=False)
        c2 = _result(filename="f2.flac", username="p2", has_free_upload_slot=False)
        slskd = _FakeSlskd()

        success, chosen, fail_reason = mod._attempt_candidate_downloads(
            slskd, [c1, c2], queue_id=1, expected_artist="A", expected_title="T",
        )
        assert success is False
        assert chosen is None
        # The mark_failed("peer_no_free_slots") contract used by the retry
        # chain is preserved through the generic failure path.
        assert fail_reason == "peer_no_free_slots"
        assert slskd.calls == [], "a slot-less peer must never be asked to download"

    def test_a_failed_download_falls_through_and_reports_its_own_reason(self, dps, monkeypatch):
        mod, rec = dps
        self._patch_exists(monkeypatch, mod)
        slotless = _result(filename="f0.flac", username="p0", has_free_upload_slot=False)
        broken = _result(filename="f1.flac", username="p1")
        ok = _result(filename="f2.flac", username="p2")
        slskd = _FakeSlskd(accept={("p2", "f2.flac")})

        success, chosen, fail_reason = mod._attempt_candidate_downloads(
            slskd, [slotless, broken, ok], queue_id=1,
            expected_artist="A", expected_title="T",
        )
        assert success is True
        assert chosen["username"] == "p2"
        assert mod._is_peer_blocked("p1", "f1.flac"), "failed download is remembered"

    def test_item_removed_mid_attempt_reports_item_removed(self, dps, monkeypatch):
        mod, rec = dps
        self._patch_exists(monkeypatch, mod, exists=False)
        slskd = _FakeSlskd()
        success, chosen, fail_reason = mod._attempt_candidate_downloads(
            slskd, [_result()], queue_id=1, expected_artist="A", expected_title="T",
        )
        assert (success, chosen, fail_reason) == (False, None, "item_removed")
        assert slskd.calls == []


# ---------------------------------------------------------------------------
# 3. The wiring: sink threaded through the run; early-fail removed; DEBUG skip
# ---------------------------------------------------------------------------


class TestTheWiring:
    @classmethod
    def _pipeline_src(cls) -> str:
        path = REPO_ROOT / "services" / "downloads" / "download_pipeline_service.py"
        return _strip_python(path.read_text(encoding="utf-8"))

    def test_the_search_run_collects_misses(self):
        src = self._pipeline_src()
        assert "miss_sink=_misses" in src, (
            "every query's miss must feed the run-level consolidation"
        )
        assert "_warn_closest_search_miss(" in src, (
            "the run must warn once from the collected misses"
        )

    def test_the_old_slotless_early_fail_is_gone(self):
        src = self._pipeline_src()
        assert "Best match peer has 0 free slots — skipping download" not in src, (
            "the early return that failed the item without trying the next "
            "candidate must be gone"
        )
        assert "_attempt_candidate_downloads(" in src, (
            "the candidate loop must be the one path that runs"
        )

    def test_the_generic_fail_path_reports_the_real_reason(self):
        src = self._pipeline_src()
        idx = src.index("notes=fail_reason")
        guard = src.rfind("if not success:", max(0, idx - 400), idx)
        assert guard != -1, (
            "the search event must carry the real fail reason, not a "
            "hardcoded 'download_failed' that hid peer_no_free_slots"
        )

    def test_the_orchestrator_skip_lines_are_debug(self):
        path = REPO_ROOT / "services" / "queue" / "queue_orchestrator.py"
        src = _strip_python(path.read_text(encoding="utf-8"))
        assert 'logger.debug("Another queue cycle holds the lock — skipping cycle")' in src
        assert 'logger.debug("Another queue cycle holds the lock — skipping batch")' in src
        assert 'logger.info("Another queue cycle holds the lock' not in src, (
            "expected dual-driver contention must not flood INFO"
        )


# ---------------------------------------------------------------------------
# 4. Controls
# ---------------------------------------------------------------------------


class TestControls:
    def test_a_qualifying_best_is_returned_untouched(self, dps):
        mod, rec = dps
        # The filename must clear the hard title gate — artist + title + album
        # evidence as the pipeline parses it.
        good = _result(
            filename="BIGBANG\\For The World (2008)\\BIGBANG - For The World - 04 - LIE.flac",
            username="goodpeer",
        )
        best = mod._select_best_result(
            [good],
            expected_artist="BIGBANG", expected_title="LIE",
            expected_album="For The World",
        )
        assert best is good
        assert rec.of("info", "Selected download candidate")

    def test_blocked_peers_are_still_filtered_from_searches(self, dps):
        mod, rec = dps
        mod._block_peer("bad", "bad\\x.flac")
        kept = mod._filter_blocked_peers([
            _result(filename="bad\\x.flac", username="bad"),
            _result(filename="ok.flac", username="ok"),
        ])
        assert [r["username"] for r in kept] == ["ok"]
