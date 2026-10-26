"""Tests for the artist top-% popularity marking + medium→high single bump.

Spec rules 2-3: tracks in the top 10% of the artist's catalogue by popularity
are marked ``popularity_marked`` and earn 5★ WITHOUT a single-detection source;
the marking band widens to 20% for tracks carrying a MEDIUM-confidence single
source, which is then upgraded to HIGH confidence (which also earns 5★).
"""

from __future__ import annotations

import json


class TestArtistTopMarkedCutoff:
    def _cutoff(self, scan_scores, db_scores=()):
        from services.popularity.scan_stage_runner import _artist_top_marked_cutoffs
        return _artist_top_marked_cutoffs(scan_scores, db_scores)

    def test_top_ten_percent_cutoff(self):
        # 20 tracks → top 10% = 2 tracks → cutoff = 2nd-highest score.
        scores = list(range(81, 101))  # 81..100
        top_cutoff, medium_cutoff, top_n, medium_n = self._cutoff(scores)
        assert top_n == 2
        assert top_cutoff == 99.0
        assert medium_n == 4
        assert medium_cutoff == 97.0

    def test_small_catalogue_rounds_up(self):
        # 5 tracks → ceil(5 * 0.10) = 1 track marked.
        top_cutoff, medium_cutoff, top_n, medium_n = self._cutoff([10, 20, 30, 40, 50])
        assert top_n == 1
        assert top_cutoff == 50.0
        assert medium_n == 1
        assert medium_cutoff == 50.0

    def test_no_scores_returns_none(self):
        top_cutoff, medium_cutoff, top_n, medium_n = self._cutoff([], [])
        assert top_cutoff is None
        assert medium_cutoff is None
        assert top_n == 0
        assert medium_n == 0

    def test_merges_scan_and_db_scores(self):
        top_cutoff, medium_cutoff, top_n, medium_n = self._cutoff([90], [91, 92, 93])
        assert top_n == 1
        assert top_cutoff == 93.0

    def test_zero_scores_are_ignored(self):
        top_cutoff, medium_cutoff, top_n, medium_n = self._cutoff([90, 0, 0], [])
        assert top_n == 1
        assert top_cutoff == 90.0


class TestReanchorScoresToAlbumRelative:
    """Stored final_score values mix two scales — re-anchor onto album-relative.

    Albums scanned before the album-relative re-map keep their RAW combined
    score (hits at 85-95), while freshly-scanned albums persist album-relative
    values centred at ~50.  Merging the two in the artist-wide distribution
    inflates the top-10% marking cutoff (raw outliers dominate the top of the
    list) and skews artist z-scores, so stored albums are re-anchored against
    their own stored distribution before they join the scan scores.
    """

    def _reanchor(self, rows):
        from services.popularity.popularity_math import reanchor_scores_to_album_relative
        return reanchor_scores_to_album_relative(rows)

    def test_raw_scale_album_compressed_to_album_relative(self):
        # An old raw-scale album: the hit at 92 compresses onto the same
        # ~50-70 band a fresh album-relative scan would produce (median ~50).
        rows = [
            ("Old", 92.0), ("Old", 88.0), ("Old", 85.0), ("Old", 82.0), ("Old", 79.0),
            ("Old", 76.0), ("Old", 73.0), ("Old", 70.0), ("Old", 66.0), ("Old", 61.0),
        ]
        scores = self._reanchor(rows)
        assert len(scores) == len(rows)
        assert max(scores) < 75.0  # no longer an 85-95 raw outlier
        from statistics import median
        assert 45 <= median(scores) <= 55  # album-relative median ≈ 50

    def test_already_album_relative_album_stays_centred(self):
        # A post-feature album is already centred at ~50; re-anchoring keeps it
        # on the same scale (the median stays ~50) instead of pushing it up.
        rows = [
            ("New", 50.0), ("New", 52.0), ("New", 54.0), ("New", 55.0), ("New", 56.0),
            ("New", 57.0), ("New", 58.0), ("New", 59.0), ("New", 60.0), ("New", 62.0),
        ]
        scores = self._reanchor(rows)
        from statistics import median
        assert 45 <= median(scores) <= 55
        assert max(scores) < 75.0

    def test_mixed_raw_and_remapped_albums_merge_consistently(self):
        # Raw-scale albums and already-remapped albums re-anchor onto the same
        # scale, so neither population dominates the merged distribution.
        rows = (
            [("Old", s) for s in (92.0, 88.0, 85.0, 82.0, 79.0, 76.0, 73.0, 70.0, 66.0, 61.0)]
            + [("New", s) for s in (50.0, 52.0, 54.0, 55.0, 56.0, 57.0, 58.0, 59.0, 60.0, 62.0)]
        )
        scores = self._reanchor(rows)
        assert max(scores) < 75.0
        assert all(0.0 < s <= 100.0 for s in scores)

    def test_small_album_keeps_stored_value(self):
        # < 3 valid scores → no distribution to compare against → unchanged
        # (matches the fresh-scan behaviour for tiny albums).
        assert self._reanchor([("Solo", 80.0)]) == [80.0]
        assert sorted(self._reanchor([("Duo", 50.0), ("Duo", 90.0)])) == [50.0, 90.0]

    def test_zero_scores_ignored_and_empty_input(self):
        # Zero scores carry no signal: dropped from the album reference AND the
        # output, while the valid tracks are still re-anchored to ~50.
        scores = self._reanchor([("A", 0.0), ("A", 5.0), ("A", 6.0), ("A", 7.0)])
        assert len(scores) == 3
        assert 0.0 not in scores
        from statistics import median
        assert 45 <= median(scores) <= 55
        assert self._reanchor([]) == []


class TestArtistTopMarkedCutoffMixedScales:
    """Raw-scale DB outliers must not inflate the top-10% cutoff.

    Reproduction of the A Snow Capped Romance regression: the fresh album's
    re-mapped scores (top ~67, median ~50) are ranked against the artist's
    stored scores, and a handful of OLD raw-scale albums (hits at 85-95) sit at
    the top of the merged list.  The inflated cutoff pushes a genuinely top-10%
    fresh track (63.9) below the cut — so its medium→high single bump and 5★
    never fire.  Re-anchoring the stored albums onto the album-relative scale
    fixes the comparison.
    """

    def _cutoff(self, scan_scores, db_rows):
        from services.popularity.popularity_math import reanchor_scores_to_album_relative
        from services.popularity.scan_stage_runner import _artist_top_marked_cutoffs
        db_reanchored = reanchor_scores_to_album_relative(db_rows)
        return _artist_top_marked_cutoffs(scan_scores, db_reanchored)

    def _db(self):
        rows = [
            ("OldA", 92.0), ("OldA", 88.0), ("OldA", 85.0), ("OldA", 82.0), ("OldA", 79.0),
            ("OldA", 76.0), ("OldA", 73.0), ("OldA", 70.0), ("OldA", 66.0), ("OldA", 61.0),
            ("OldB", 89.0), ("OldB", 86.0), ("OldB", 83.0), ("OldB", 80.0), ("OldB", 78.0),
            ("OldB", 75.0), ("OldB", 72.0), ("OldB", 68.0), ("OldB", 64.0), ("OldB", 59.0),
            ("NewA", 50.0), ("NewA", 52.0), ("NewA", 54.0), ("NewA", 55.0), ("NewA", 56.0),
            ("NewA", 57.0), ("NewA", 58.0), ("NewA", 59.0), ("NewA", 60.0), ("NewA", 62.0),
            ("NewB", 48.0), ("NewB", 50.0), ("NewB", 51.0), ("NewB", 53.0), ("NewB", 55.0),
            ("NewB", 56.0), ("NewB", 58.0), ("NewB", 59.0), ("NewB", 60.0), ("NewB", 61.0),
        ]
        return rows

    def test_raw_merge_cutoff_inflated_and_fixed_by_reanchor(self):
        from services.popularity.scan_stage_runner import _artist_top_marked_cutoffs
        scan_scores = [67.3, 63.9, 62.2, 57.8, 57.8, 52.6, 51.2,
                       48.8, 48.3, 48.1, 47.5, 46.3, 11.1]
        db_rows = self._db()

        # Raw-scale merge: the old albums' 85-95 hits dominate → inflated cutoff.
        raw_top_cutoff, raw_medium_cutoff, raw_top_n, raw_medium_n = _artist_top_marked_cutoffs(
            scan_scores, [s for _alb, s in db_rows]
        )
        assert raw_top_n == 6
        assert raw_top_cutoff >= 80.0
        assert 63.9 < raw_top_cutoff  # the top-10% fresh track is pushed out

        # After re-anchoring stored albums onto the album-relative scale, the
        # cutoff lands in the fresh scale's ~63-67 band and 63.9 clears it.
        fixed_top_cutoff, fixed_medium_cutoff, fixed_top_n, fixed_medium_n = self._cutoff(scan_scores, db_rows)
        assert fixed_top_n == 6
        assert fixed_top_cutoff <= 65.0
        assert 63.9 >= fixed_top_cutoff


class TestPopularityMarkingBump:
    def _bump(self, tracks):
        from services.popularity.scan_stage_runner import _apply_popularity_marking_bump
        return _apply_popularity_marking_bump(tracks)

    def _track(self, marked, is_single=True, confidence="medium", sources=""):
        return {
            "track_id": "t1", "title": "Song",
            "popularity_marked": marked,
            "is_single": is_single,
            "single_confidence": confidence,
            "single_sources": sources,
        }

    def test_marked_medium_single_upgraded_to_high(self):
        [t] = self._bump([self._track(marked=True)])
        assert t["single_confidence"] == "high"
        parsed = json.loads(t["single_sources"])
        assert any(s.get("source") == "popularity_marked" and s.get("matched") for s in parsed)

    def test_unmarked_medium_single_unchanged(self):
        [t] = self._bump([self._track(marked=False)])
        assert t["single_confidence"] == "medium"

    def test_marked_low_single_not_upgraded(self):
        # The bump only upgrades MEDIUM → HIGH (spec rule 3); a low/none
        # verdict needs real metadata evidence, popularity alone is not enough.
        [t] = self._bump([self._track(marked=True, confidence="low", is_single=False)])
        assert t["single_confidence"] == "low"

    def test_marked_high_single_kept_high(self):
        [t] = self._bump([self._track(marked=True, confidence="high")])
        assert t["single_confidence"] == "high"

    def test_appends_source_without_duplicating(self):
        sources = '[{"source": "popularity_marked", "matched": true, "confidence": 0.5}]'
        [t] = self._bump([self._track(marked=True, sources=sources)])
        parsed = json.loads(t["single_sources"])
        assert sum(1 for s in parsed if s.get("source") == "popularity_marked") == 1


class TestAlbumRelativeNormalization:
    def _normalize(self, tracks, is_compilation=False):
        from services.popularity.scan_stage_runner import _apply_album_relative_normalization
        return _apply_album_relative_normalization(tracks, is_compilation=is_compilation)

    def test_remaps_fresh_scores(self):
        tracks = [
            {"track_id": f"t{i}", "title": f"Song {i}", "_raw_combined": raw,
             "popularity_score": raw, "final_score": raw}
            for i, raw in enumerate([95.0, 90.0, 85.0, 80.0, 75.0, 70.0], start=1)
        ]
        changed = self._normalize(tracks)
        assert changed == len(tracks)
        # The album median track (~82.5) now sits near 50; the top track is
        # above it, and nothing clamps at 100.
        scores = sorted(t["popularity_score"] for t in tracks)
        assert scores[0] < scores[-1]
        assert scores[-1] < 100.0
        assert scores[-1] > 50.0

    def test_no_fresh_scores_skips(self):
        tracks = [{"track_id": "t1", "_raw_combined": 0, "popularity_score": 60.0}]
        assert self._normalize(tracks) == 0

    def test_too_few_fresh_scores_skips(self):
        tracks = [
            {"track_id": "t1", "_raw_combined": 80.0, "popularity_score": 80.0},
            {"track_id": "t2", "_raw_combined": 75.0, "popularity_score": 75.0},
        ]
        assert self._normalize(tracks) == 0

    def test_frozen_tracks_keep_stored_score(self):
        # A cached/frozen track (no _raw_combined) is left untouched while its
        # freshly-scored siblings are re-mapped.
        fresh = [
            {"track_id": f"t{i}", "_raw_combined": raw,
             "popularity_score": raw, "final_score": raw}
            for i, raw in enumerate([95.0, 90.0, 85.0, 80.0, 75.0, 70.0], start=1)
        ]
        frozen = {"track_id": "tf", "_raw_combined": 0, "popularity_score": 60.0, "final_score": 60.0}
        tracks = fresh + [frozen]
        self._normalize(tracks)
        assert frozen["popularity_score"] == 60.0
        assert all(t["popularity_score"] != t["_raw_combined"] for t in fresh)

    def test_bonus_tracks_excluded_from_album_reference(self):
        # A studio album padded with extra live cuts: the live bonus tracks
        # (exclude_from_stats=True) must NOT anchor the album's average.  The
        # core tracks re-map against the core-only distribution, and the bonus
        # tracks are scored relative to that same (higher) reference — so they
        # land lower than they would if they dragged the median down.
        tracks = [
            {"track_id": f"t{i}", "title": f"Song {i}", "_raw_combined": raw,
             "popularity_score": raw, "final_score": raw, "exclude_from_stats": False}
            for i, raw in enumerate([95.0, 90.0, 85.0, 80.0, 75.0, 70.0], start=1)
        ]
        bonus = [
            {"track_id": f"b{i}", "title": f"Bonus (Live) {i}", "_raw_combined": raw,
             "popularity_score": raw, "final_score": raw, "exclude_from_stats": True}
            for i, raw in enumerate([30.0, 25.0, 20.0], start=1)
        ]
        self._normalize(tracks + bonus)
        # The core tracks are still spread and re-mapped above the midpoint.
        core_scores = [t["popularity_score"] for t in tracks]
        assert all(t["popularity_score"] != t["_raw_combined"] for t in tracks)
        assert max(core_scores) < 100.0
        assert max(core_scores) > 60.0
        # The live bonus tracks score BELOW the core median — they are rated
        # against the core distribution, not their own low numbers.
        assert max(t["popularity_score"] for t in bonus) < min(core_scores)

    def test_live_album_all_tracks_excluded_falls_back_to_full(self):
        # A true live album flags EVERY track excluded — the drop must not
        # empty the reference, so the album is scored against itself (each
        # track re-maps normally).
        tracks = [
            {"track_id": f"t{i}", "title": f"Live Song {i}", "_raw_combined": raw,
             "popularity_score": raw, "final_score": raw, "exclude_from_stats": True}
            for i, raw in enumerate([95.0, 90.0, 85.0, 80.0, 75.0, 70.0], start=1)
        ]
        self._normalize(tracks)
        assert all(t["popularity_score"] != t["_raw_combined"] for t in tracks)


class TestCompilationTrackArtistNormalization:
    """Compilation albums re-map each track against its own TRACK artist.

    On a compilation / Various-Artists album every track has a different
    artist, so comparing a track against the compilation's median (the "album
    artist" reference) is meaningless.  Each track is re-mapped against its own
    track artist's stored catalogue popularity instead.
    """

    def _normalize(self, tracks, artist_scores_map=None):
        from services.popularity import scan_stage_runner as s
        monkeypatched = artist_scores_map is not None

        def _fake_load(track_artist, **kwargs):
            # ⚠️ ``**kwargs``, not a bare ``track_artist``: the real loader is
            # called as ``_load_track_artist_scores(primary,
            # exclude_album=...)`` and a fake that refuses the keyword raises
            # TypeError inside the function under test, so all three tests in
            # this class were failing on the FAKE rather than on the code.
            return list(artist_scores_map.get(track_artist) or [])

        if monkeypatched:
            original = s._load_track_artist_scores
            s._load_track_artist_scores = _fake_load
        try:
            return s._apply_album_relative_normalization(tracks, is_compilation=True)
        finally:
            if monkeypatched:
                s._load_track_artist_scores = original

    def _tracks(self):
        return [
            {"track_id": f"t{i}", "artist": artist, "_raw_combined": raw,
             "popularity_score": raw, "final_score": raw}
            for i, (artist, raw) in enumerate([
                ("Artist A", 90.0),
                ("Artist B", 85.0),
                ("Artist C", 80.0),
                ("Artist D", 75.0),
                ("Artist E", 70.0),
                ("Artist F", 65.0),
            ], start=1)
        ]

    def test_compilation_path_remaps_against_track_artist(self):
        # Each track is re-mapped against ITS OWN artist's distribution — a
        # track that tops its artist's catalogue scores high, one that sits at
        # the catalogue median lands near 50.
        scores_map = {
            "Artist A": [90, 88, 86, 84, 82, 80],
            "Artist B": [85, 83, 81, 79, 77, 75],
            "Artist C": [80, 78, 76, 74, 72, 70],
            "Artist D": [75, 73, 71, 69, 67, 65],
            "Artist E": [70, 68, 66, 64, 62, 60],
            "Artist F": [65, 63, 61, 59, 57, 55],
        }
        tracks = self._tracks()
        changed = self._normalize(tracks, scores_map)
        assert changed == len(tracks)
        # Every track sits at or above its own artist's median → ~50 or higher,
        # and none clamps at 100.
        for t, artist in zip(tracks, scores_map):
            assert 45 <= t["popularity_score"] <= 99
            assert t["popularity_score"] != t["_raw_combined"]

    def test_artist_median_track_scores_near_midpoint(self):
        # A track sitting AT its artist's catalogue median re-maps to ~50.
        scores_map = {
            "Artist A": [90, 88, 86, 84, 82, 80],
            "Artist B": [85, 83, 81, 79, 77, 75],
            "Artist C": [80, 78, 76, 74, 72, 70],
            "Artist D": [75, 73, 71, 69, 67, 65],
            "Artist E": [70, 68, 66, 64, 62, 60],
            "Artist F": [65, 63, 61, 59, 57, 55],
        }
        tracks = [
            {"track_id": "t1", "artist": "Artist A", "_raw_combined": 85.0,
             "popularity_score": 85.0, "final_score": 85.0},
        ]
        self._normalize(tracks, scores_map)
        assert 30 <= tracks[0]["popularity_score"] <= 70

    def test_artist_without_scores_falls_back_to_album_distribution(self):
        # An artist with no stored catalogue scores (first scan) cannot be
        # re-mapped against its own catalogue — the track still lands on the
        # relative scale via the compilation's OWN internal raw distribution
        # (so a raw 90-65 tracklist isn't left sitting on its raw peak).
        tracks = self._tracks()
        changed = self._normalize(tracks, {})
        assert changed == len(tracks)
        assert all(t["popularity_score"] != t["_raw_combined"] for t in tracks)

    def test_no_fresh_scores_skips(self):
        tracks = [{"track_id": "t1", "artist": "A", "_raw_combined": 0, "popularity_score": 60.0}]
        assert self._normalize(tracks, {"A": [80, 75, 70, 65, 60, 55]}) == 0

    def test_non_compilation_still_uses_album_distribution(self):
        # Regular albums are untouched by the compilation path — they still
        # re-map against the album's own distribution.
        tracks = self._tracks()
        from services.popularity import scan_stage_runner as s
        original = s._load_track_artist_scores
        s._load_track_artist_scores = lambda artist: [99.0, 99.0, 99.0, 99.0, 99.0, 99.0]
        try:
            changed_album = s._apply_album_relative_normalization(tracks, is_compilation=False)
        finally:
            s._load_track_artist_scores = original
        assert changed_album == len(tracks)
