"""Popularity scan: the second round of performance fixes.

Round one (``test_popularity_scan_performance.py``) restored the MBID cache and
memoised album facts. This covers the remaining items from
``documentation/popularity-scan-audit.md``:

1. ``_assign_stars`` rebuilt the artist-catalogue reference for EVERY track —
   O(tracks x catalogue) calls to ``_curve_metric`` per album. Hoisted, and the
   percentile helpers now bisect a prepared pool instead of re-filtering and
   re-sorting it per track.
2. ``_persist_album_relative_scores`` / ``_persist_popularity_marking`` issued
   one UPDATE per row, twice per album (provisional + reconcile).
3. ``_run_full_enrichment`` ran twice per METADATA scan: ``enrich_album`` calls
   it when not deferred, and ``enrich_album_extras`` calls it unconditionally.
4. Cover detection had no freshness gate, so every non-forced scan re-ran the
   quick check for every track — up to ~50 HTTP requests for a track with a
   work MBID, and a throwaway ``CoverDetector`` each time.
5. The end-of-scan ISRC sync ran AFTER the playlist deferral was cleared, so an
   abandoned artist worker could run it a second time; and the legacy
   full-library path never ran it at all.
"""

from __future__ import annotations

import importlib
import random
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture()
def fs():
    return importlib.import_module("services.popularity.stages.finalise_stage")


@pytest.fixture()
def policy():
    return importlib.import_module("services.popularity.popularity_cache_policy")


# ---------------------------------------------------------------------------
# 1. The curve pools are prepared once and bisected
# ---------------------------------------------------------------------------

def _old_rank(score, pool):
    valid = [float(v) for v in (pool or []) if float(v or 0) > 0]
    if not valid:
        return None
    if len(valid) == 1:
        return 1.0
    rank = sum(1 for v in valid if v > score) + 1
    return 1.0 - (rank - 1) / (len(valid) - 1)


def _old_below(score, pool):
    valid = [float(v) for v in (pool or []) if float(v or 0) > 0]
    if not valid:
        return None
    return sum(1 for v in valid if v < score) / len(valid)


class TestThePercentilesAreEquivalent:
    """CONTROL FIRST — the optimisation must not change a single star."""

    @pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
    def test_bisect_matches_the_old_linear_scan(self, fs, seed):
        rng = random.Random(seed)
        for _ in range(60):
            pool = [round(rng.uniform(0, 100), 2) for _ in range(rng.randint(0, 30))]
            if pool and rng.random() < 0.4:
                pool[0] = pool[-1]  # duplicates
            if rng.random() < 0.3:
                pool += [0.0, -1.0]  # non-positive must be ignored
            for score in (0.0, 1.0, 50.0, 100.0, rng.uniform(0, 100)):
                assert fs._rank_percentile(score, pool) == _old_rank(score, pool)
                assert fs._below_percentile(score, pool) == _old_below(score, pool)

    def test_a_prepared_pool_gives_the_same_answer_as_a_raw_one(self, fs):
        raw = [5.0, 1.0, 9.0, 3.0, 3.0]
        prepared = fs._SortedPool(sorted(v for v in raw if v > 0))
        for score in (0.0, 3.0, 5.0, 9.0, 10.0):
            assert fs._rank_percentile(score, raw) == fs._rank_percentile(score, prepared)
            assert fs._below_percentile(score, raw) == fs._below_percentile(score, prepared)


class TestTheCurvePoolsAreHoisted:
    def test_the_artist_reference_is_prepared_once_per_album(self, fs):
        """``_SortedPool`` is the marker that lets the percentile skip the rebuild."""
        import inspect

        src = inspect.getsource(fs.post_album_star_ratings)
        assert "_curve_artist_pool = _SortedPool(" in src, (
            "the artist-catalogue curve reference is not prepared once per album, "
            "so _assign_stars rebuilds it for every track (O(tracks x catalogue))"
        )
        assert "album_curve_artist_pool=_curve_artist_pool" in src, (
            "the precomputed pool is not passed to _assign_stars"
        )

    def test_the_assign_path_accepts_a_precomputed_pool(self, fs):
        import inspect

        src = inspect.getsource(fs._assign_stars)
        assert "album_curve_artist_pool is not None" in src, (
            "_assign_stars does not honour a precomputed artist pool"
        )

    def test_a_prepared_pool_is_not_resorted(self, fs):
        """The O(1) marker is the whole point — a plain list would re-sort."""
        prepared = fs._SortedPool([1.0, 2.0, 3.0])
        assert fs._valid_sorted_pool(prepared) is prepared
        assert isinstance(fs._valid_sorted_pool([3.0, 1.0, 2.0]), fs._SortedPool)


# ---------------------------------------------------------------------------
# 2. The two per-row UPDATE loops are one statement each
# ---------------------------------------------------------------------------

class TestThePersistLoopsAreBatched:
    def _capture(self, monkeypatch, runner):
        executed: list[tuple[str, object]] = []

        class _Session:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, stmt, params=None):
                executed.append((str(stmt), params))
                return None

        monkeypatch.setattr(runner, "db_session", lambda: _Session())
        return executed

    def test_relative_scores_issue_one_statement(self, monkeypatch):
        runner = importlib.import_module("services.popularity.scan_stage_runner")
        executed = self._capture(monkeypatch, runner)
        rows = [{"track_id": str(i), "final_score": 10.0 + i} for i in range(12)]

        runner._persist_album_relative_scores(rows)

        assert len(executed) == 1, (
            f"{len(executed)} statements for one album — the per-row loop is back"
        )
        _sql, params = executed[0]
        assert isinstance(params, list) and len(params) == 12
        assert params[0] == {"s": 10.0, "id": "0"}

    def test_marking_issues_one_statement(self, monkeypatch):
        runner = importlib.import_module("services.popularity.scan_stage_runner")
        executed = self._capture(monkeypatch, runner)
        rows = [
            {"track_id": str(i), "popularity_marked": True, "single_confidence": "high"}
            for i in range(10)
        ]

        runner._persist_popularity_marking(rows)

        assert len(executed) == 1
        _sql, params = executed[0]
        assert len(params) == 10

    def test_the_per_row_guards_still_filter(self, monkeypatch):
        """CONTROL: batching must not write rows the old loops skipped."""
        runner = importlib.import_module("services.popularity.scan_stage_runner")
        executed = self._capture(monkeypatch, runner)
        rows = [
            {"track_id": "keep", "popularity_marked": True},
            {"track_id": "", "popularity_marked": True},            # no id
            {"track_id": "ctx", "_context_only": True},             # context only
            {"track_id": "frozen", "popularity_frozen": True},      # no conf + frozen
        ]

        runner._persist_popularity_marking(rows)

        _sql, params = executed[0]
        assert [p["id"] for p in params] == ["keep"]

    def test_a_bad_track_id_is_skipped_not_fatal(self, monkeypatch):
        runner = importlib.import_module("services.popularity.scan_stage_runner")
        executed = self._capture(monkeypatch, runner)
        runner._persist_album_relative_scores(
            [{"track_id": "", "final_score": 1.0}, {"track_id": "ok", "final_score": 2.0}]
        )
        _sql, params = executed[0]
        assert params == [{"s": 2.0, "id": "ok"}]


# ---------------------------------------------------------------------------
# 3. Full enrichment runs once per album
# ---------------------------------------------------------------------------

class TestFullEnrichmentRunsOncePerAlbum:
    def test_the_second_call_reuses_the_first_result(self, fs, monkeypatch):
        album_stage = importlib.import_module("services.popularity.stages.album_stage")
        calls = {"n": 0}

        def _spy(*args, **kwargs):
            calls["n"] += 1
            return {"artist_meta": True}, {"lastfm": [], "listenbrainz": []}

        for name in (
            "_fetch_album_art_with_fallback",
        ):
            monkeypatch.setattr(album_stage, name, lambda *a, **k: "cached")
        for name in (
            "_read_artist_metadata",
        ):
            monkeypatch.setattr(album_stage, name, lambda *a, **k: {"artist_meta": True})
        monkeypatch.setattr(album_stage, "_fetch_artist_lastfm_tags", lambda *a, **k: None)
        monkeypatch.setattr(album_stage, "_persist_release_extended_fields", lambda *a, **k: None)
        monkeypatch.setattr(album_stage, "_correct_soundtrack_album_artist", lambda *a, **k: None)
        monkeypatch.setattr(album_stage, "_fetch_musicbrainz_artist_id", lambda *a, **k: None)
        monkeypatch.setattr(
            album_stage,
            "_read_similar_artists",
            lambda *a, **k: {"lastfm": ["x"], "listenbrainz": []},
        )
        monkeypatch.setattr(album_stage, "_fetch_discogs_artist_id", lambda *a, **k: None)
        monkeypatch.setattr(album_stage, "_apply_live_remix_album_tagging", lambda *a, **k: None)
        monkeypatch.setattr(album_stage, "_persist_alternate_takes", lambda *a, **k: None)

        ctx: dict = {}
        real = album_stage._run_full_enrichment
        # Count real invocations by wrapping the memo'd entry point.
        album_stage._run_full_enrichment = lambda *a, **k: (
            calls.__setitem__("n", calls["n"] + 1) or real(*a, **k)
        )
        try:
            album_stage._run_full_enrichment("A", "B", ctx, [], "album", {}, None)
            first = album_stage._run_full_enrichment("A", "B", ctx, [], "album", {}, None)
        finally:
            album_stage._run_full_enrichment = real

        assert "_full_enrichment_result" in ctx
        assert first == ctx["_full_enrichment_result"], (
            "the second call did not return the first call's result, which would "
            "blank similar_artists / artist_metadata on album_result"
        )

    def test_the_memo_returns_the_values_not_a_flag(self, fs):
        """⚠️ A bare "already done" guard would blank the caller's assignments."""
        album_stage = importlib.import_module("services.popularity.stages.album_stage")
        ctx = {"_full_enrichment_result": ({"m": 1}, {"lastfm": ["a"], "listenbrainz": []})}
        # The memo short-circuits before any of the expensive calls; if it
        # returned None the unpack below would fail.
        metadata, similar = album_stage._run_full_enrichment(
            "A", "B", ctx, [], "album", {}, None
        )
        assert metadata == {"m": 1}
        assert similar["lastfm"] == ["a"]


# ---------------------------------------------------------------------------
# 4. Cover verdicts are not re-derived every scan
# ---------------------------------------------------------------------------

class TestCoverVerdictFreshness:
    def _track(self, **over):
        base = {"id": "t1", "title": "Song", "artist": "Band"}
        base.update(over)
        return base

    def test_a_recently_checked_negative_is_fresh(self, policy):
        track = self._track(cover_last_checked=datetime.now(timezone.utc))
        assert policy.cover_verdict_is_fresh(track) is True, (
            "a completed 'not a cover' result is not being reused, so the quick "
            "check re-runs for every track on every scan"
        )

    def test_an_old_check_is_stale(self, policy):
        old = datetime.now(timezone.utc) - timedelta(days=policy.COVER_RECHECK_DAYS + 1)
        assert policy.cover_verdict_is_fresh(self._track(cover_last_checked=old)) is False

    def test_never_checked_is_not_fresh(self, policy):
        assert policy.cover_verdict_is_fresh(self._track()) is False

    def test_a_confirmed_cover_is_never_short_circuited(self, policy):
        """Positives are re-verified by the deep pass on purpose."""
        track = self._track(
            is_cover=1,
            original_cover_artist="Someone",
            cover_last_checked=datetime.now(timezone.utc),
        )
        assert policy.cover_verdict_is_fresh(track) is False, (
            "a positive verdict was treated as settled, which would freeze a "
            "stale cover attribution forever"
        )

    def test_a_manual_override_is_always_settled(self, policy):
        assert policy.cover_verdict_is_fresh(self._track(cover_manual_override=True)) is True

    def test_a_string_timestamp_is_accepted(self, policy):
        track = self._track(
            cover_last_checked=datetime.now(timezone.utc).isoformat()
        )
        assert policy.cover_verdict_is_fresh(track) is True

    def test_the_quick_path_stamps_a_settled_negative(self):
        """Without the stamp nothing can ever become fresh."""
        import inspect

        stage = importlib.import_module("services.popularity.stages.track_stage")
        src = inspect.getsource(stage.process_track)
        assert 'update_payload["cover_last_checked"]' in src
        # ...but NOT when the wording was stripped: that needs the deep pass.
        assert "_cover_wording_removed" in src

    def test_the_quick_path_consults_the_gate(self):
        import inspect

        stage = importlib.import_module("services.popularity.stages.track_stage")
        src = inspect.getsource(stage.process_track)
        assert "cover_verdict_is_fresh" in src


# ---------------------------------------------------------------------------
# 5. The ISRC sync ordering and the missing legacy call
# ---------------------------------------------------------------------------

class TestTheIsrcSyncIsOrderedAndPresent:
    def test_the_deferral_is_cleared_after_the_sync(self):
        src = (
            importlib.import_module(
                "services.scanning.pipelines.popularity_pipeline"
            ).__file__
        )
        text = open(src, encoding="utf-8").read()
        sync_at = text.index("_sync_isrc_popularity()")
        # Anchor on the CALL (line start, not the `from ... import` above it).
        resume_at = text.index("\n            resume_global_playlists()")
        assert sync_at < resume_at, (
            "the playlist deferral is cleared BEFORE the ISRC sync, so an "
            "abandoned artist worker can run the whole-library sync a second time"
        )

    def test_the_legacy_full_scan_path_calls_it(self):
        """It used to defer the sync via every finalise_scan and never call it."""
        src = importlib.import_module("services.scanning.pipeline").__file__
        text = open(src, encoding="utf-8").read()
        assert "_sync_isrc_popularity()" in text, (
            "run_full_library_scan defers the per-artist ISRC sync but never runs "
            "it at the end, so ISRC popularity is never propagated on this path"
        )
