"""Four scoring defects found by auditing two exported popularity reports.

The evidence is a pair of CSVs (a Various-Artists compilation and a two-track
single-artist album). Each defect below is stated with the number that exposed
it, because "the score looks wrong" is not actionable and "26 of 100 tracks are
5★" is.

1. **`final_score` on a compilation measured rank WITHIN each artist.** Two
   tracks on one album were scored against two different populations, so the
   column could not order the album. Smashing Pumpkins averaged 1,888,518
   Last.fm listeners and scored 66.92; Powderfinger averaged 16,977 and scored
   66.99 — 111x the audience for 0.07 of a point. Sorting the album by
   `final_score` produced 25 adjacent star inversions.
2. **The compilation 5★ guard OR'd a scale-free score against absolute reach.**
   The listener bar was therefore unreachable: Pearl Jam's "Leaving Here"
   (19,396 listeners) held 5★ on score 70.27.
3. **`listenbrainz_score` stored 0.0 beside a real listen count.** Bush's
   "Comedown" held 500,794 listens with score 0.0 while the other 16 rows
   matched `16*log10(n+1)` exactly, so that track lost ListenBrainz's whole 30%
   weight.
4. **`listenbrainz_score` meant two different things.** On a 2-track album 438
   listens scored **75.0** (the album percentile) where the log score is
   **42.28**; on every other album the column holds the log score.
"""

from __future__ import annotations

import math

import pytest

from services.popularity import popularity_math as pm


# ---------------------------------------------------------------------------
# 1. Compilation remap must not rewrite final_score
# ---------------------------------------------------------------------------


class TestTheCompilationRemapLeavesFinalScoreComparable:
    """``popularity_score`` is artist-relative; ``final_score`` is not."""

    @staticmethod
    def _run(monkeypatch, tracks, scores_map):
        from services.popularity import scan_stage_runner as runner

        monkeypatch.setattr(
            runner, "_load_track_artist_scores",
            lambda artist, **kw: list(scores_map.get(artist) or []),
        )
        monkeypatch.setattr(runner, "_persist_album_relative_scores", lambda rows: None)
        return runner._apply_album_relative_normalization(tracks, is_compilation=True)

    @staticmethod
    def _tracks():
        return [
            {"track_id": f"t{i}", "artist": artist, "_raw_combined": raw,
             "popularity_score": raw, "final_score": raw}
            for i, (artist, raw) in enumerate(
                [("Artist A", 90.0), ("Artist B", 85.0), ("Artist C", 80.0),
                 ("Artist D", 75.0), ("Artist E", 70.0), ("Artist F", 65.0)],
                start=1,
            )
        ]

    #: Every artist's catalogue is a distinct band, exactly as the real
    #: per-artist lookups return.
    SCORES = {
        "Artist A": [90, 88, 86, 84, 82, 80],
        "Artist B": [85, 83, 81, 79, 77, 75],
        "Artist C": [80, 78, 76, 74, 72, 70],
        "Artist D": [75, 73, 71, 69, 67, 65],
        "Artist E": [70, 68, 66, 64, 62, 60],
        "Artist F": [65, 63, 61, 59, 57, 55],
    }

    def test_final_score_keeps_the_pre_remap_blend(self, monkeypatch) -> None:
        """⭐ THE ORDERING DEFECT. ``final_score`` must stay on ONE scale for
        the whole album, so it cannot be the per-artist remap."""
        tracks = self._tracks()
        self._run(monkeypatch, tracks, self.SCORES)
        for t in tracks:
            assert t["final_score"] == t["_raw_combined"], (
                f"{t['artist']}: final_score was rewritten by the per-artist "
                "remap, so this row is not comparable with its album siblings"
            )

    def test_popularity_score_is_still_the_artist_remap(self, monkeypatch) -> None:
        """CONTROL — the remap still happens; it is what the compilation star
        tier reads ("is this a hit for THIS artist")."""
        tracks = self._tracks()
        self._run(monkeypatch, tracks, self.SCORES)
        for t in tracks:
            assert t["popularity_score"] != t["_raw_combined"]
            assert t["_artist_relative_score"] == t["popularity_score"]

    def test_final_score_order_follows_the_audience(self, monkeypatch) -> None:
        """The reported inversion, stated as an invariant: ordering by
        ``final_score`` must agree with ordering by raw popularity."""
        tracks = [
            {"track_id": "sp", "artist": "The Smashing Pumpkins",
             "_raw_combined": 78.0, "popularity_score": 78.0, "final_score": 78.0},
            {"track_id": "pf", "artist": "Powderfinger",
             "_raw_combined": 40.0, "popularity_score": 40.0, "final_score": 40.0},
        ]
        # Each sits at its own catalogue median, so the remap pulls them
        # TOGETHER — which is precisely what used not to matter for final_score.
        self._run(monkeypatch, tracks, {
            "The Smashing Pumpkins": [78, 76, 74, 72, 70],
            "Powderfinger": [40, 38, 36, 34, 32],
        })
        by_final = sorted(tracks, key=lambda t: -t["final_score"])
        assert by_final[0]["track_id"] == "sp", (
            "the act with 111x the audience must not be ordered below the other"
        )

    def test_a_stale_per_artist_final_score_is_repaired(self, monkeypatch) -> None:
        """A frozen row can still carry the per-artist value an earlier scan
        wrote. Preservation (`setdefault`) would keep it forever."""
        tracks = [{
            "track_id": "stale", "artist": "Artist A",
            "_raw_combined": 90.0,
            "popularity_score": 55.0,          # the old remapped value
            "final_score": 55.0,               # and it was written to final too
        }]
        self._run(monkeypatch, tracks, {"Artist A": [90, 88, 86, 84, 82, 80]})
        assert tracks[0]["final_score"] == 90.0

    def test_a_zero_raw_row_is_untouched(self, monkeypatch) -> None:
        tracks = [{"track_id": "x", "artist": "A", "_raw_combined": 0,
                   "popularity_score": 60.0, "final_score": 12.0}]
        assert self._run(monkeypatch, tracks, {"A": [80, 75, 70]}) == 0
        assert tracks[0]["final_score"] == 12.0


# ---------------------------------------------------------------------------
# 2. The 5★ conjunction (behaviour is pinned in test_compilation_tier_guards)
# ---------------------------------------------------------------------------


class TestTheTierGateIsAConjunction:
    def test_the_reported_row_is_gated(self, monkeypatch) -> None:
        """⭐ Pearl Jam's "Leaving Here": 19,396 Last.fm listeners, remapped
        score 70.27 — 5★ under the old `or`, because the score cleared 55."""
        from services.popularity.stages import finalise_stage as fs

        rules = {
            "enabled": 1, "min_local_catalogue": 5,
            "rank_percentile_5star": 0.02, "rank_percentile_4star": 0.10,
            "top_tier_min_score": 55.0, "top_tier_min_listeners": 250_000,
            "standout_floor_percentile": 0.0,
        }
        monkeypatch.setattr(
            "helpers.config_helpers.get_compilation_online_catalogue_config",
            lambda: rules,
        )
        import services.popularity.popularity_sources as ps
        monkeypatch.setattr(
            ps, "get_online_artist_track_rank",
            lambda artist, title, lastfm_client=None: {
                "rank": 1, "total": 164, "percentile": 1 / 164,
                "listeners": 19_396, "top_listeners": 500_000,
                "source": "lastfm_artist_top_tracks",
            },
        )
        track = {
            "track_id": "t-leaving", "title": "Leaving Here (Eddie Holland Cover)",
            "artist": "Pearl Jam", "album": "Triple J's Hottest 100 (1996)",
            "popularity_score": 70.27, "final_score": 70.27, "_raw_combined": 70.27,
            "lastfm_listeners": 19_396, "listenbrainz_listens": 0,
            "is_single": True, "single_confidence": "medium", "is_live": False,
            "popularity_marked": False, "exclude_from_stats": False,
        }
        stars = fs._assign_stars(
            track, [70.27], [70.27] * 3,
            album_model={"has_benchmark": True, "era": "solid", "catalog_cutoff": 99.0,
                         "max_5star_slots": 3, "album_top_n": 3},
            is_compilation=True, compilation_raw_scores=[70.27] * 5,
        )
        assert stars == 4, "a 19k-listener track must not hold 5★"
        assert track.get("_compilation_top_tier_gated") is True

    def test_a_250k_track_keeps_five(self, monkeypatch) -> None:
        """CONTROL — the same remapped-score situation, but with real reach."""
        from services.popularity.stages import finalise_stage as fs

        monkeypatch.setattr(
            "helpers.config_helpers.get_compilation_online_catalogue_config",
            lambda: {"enabled": 1, "min_local_catalogue": 5,
                     "rank_percentile_5star": 0.02, "rank_percentile_4star": 0.10,
                     "top_tier_min_score": 55.0, "top_tier_min_listeners": 250_000,
                     "standout_floor_percentile": 0.0},
        )
        import services.popularity.popularity_sources as ps
        monkeypatch.setattr(
            ps, "get_online_artist_track_rank",
            lambda artist, title, lastfm_client=None: {
                "rank": 1, "total": 164, "percentile": 1 / 164,
                "listeners": 300_000, "top_listeners": 500_000,
                "source": "lastfm_artist_top_tracks",
            },
        )
        track = {
            "track_id": "t-big", "title": "Big Hit", "artist": "Some Band",
            "album": "A Compilation", "popularity_score": 70.27,
            "final_score": 70.27, "_raw_combined": 70.27,
            "lastfm_listeners": 300_000, "listenbrainz_listens": 0,
            "is_single": True, "single_confidence": "medium", "is_live": False,
            "popularity_marked": False, "exclude_from_stats": False,
        }
        stars = fs._assign_stars(
            track, [70.27], [70.27] * 3,
            album_model={"has_benchmark": True, "era": "solid", "catalog_cutoff": 99.0,
                         "max_5star_slots": 3, "album_top_n": 3},
            is_compilation=True, compilation_raw_scores=[70.27] * 5,
        )
        assert stars == 5


# ---------------------------------------------------------------------------
# 3. ListenBrainz score / count reconciliation
# ---------------------------------------------------------------------------


class TestListenbrainzReconciliation:
    def test_a_zero_score_beside_a_real_count_is_recomputed(self) -> None:
        """⭐ THE REPORTED ROW. 500,794 listens must not carry score 0.0.

        Compared against the formula rather than a hard-coded decimal: the
        point is the INVARIANT (score == 16*log10(n+1)), not one rounding.
        """
        for listens in (500_794, 545_750):
            got = pm.reconcile_listenbrainz_score(listens, 0.0)
            assert got == round(min(100.0, math.log10(listens + 1) * 16.0), 3)
            assert got > 90.0, "a half-million-listener track scores in the 90s"
        assert pm.reconcile_listenbrainz_score(545_750, None) == round(
            min(100.0, math.log10(545_751) * 16.0), 3
        )

    def test_a_consistent_pair_is_returned_unchanged(self) -> None:
        """CONTROL — never rewrite a score that already agrees with its count."""
        assert pm.reconcile_listenbrainz_score(500_794, 91.189) == 91.189

    def test_no_listen_count_means_no_score(self) -> None:
        """CONTROL — 0 listens genuinely means "no ListenBrainz data", which
        must stay 0.0 rather than becoming a one-listen score."""
        assert pm.reconcile_listenbrainz_score(0, 0.0) == 0.0
        assert pm.reconcile_listenbrainz_score(None, None) == 0.0

    def test_it_can_only_raise_a_score(self) -> None:
        """CONTRACTS: a positive score is never lowered, so this is safe to run
        on every row without changing any established value."""
        for listens, score in [(1_000, 90.0), (5_000_000, 12.0), (10, 99.0)]:
            assert pm.reconcile_listenbrainz_score(listens, score) == score

    def test_the_result_matches_the_formula_exactly(self) -> None:
        for listens in (1, 438, 9_538, 500_794, 2_592_191):
            got = pm.reconcile_listenbrainz_score(listens, 0.0)
            assert got == round(min(100.0, math.log10(listens + 1) * 16.0), 3)

    def test_the_track_payload_reconciles_before_persisting(self) -> None:
        """The repair must reach ``update_payload`` (the dict that is written),
        not merely the in-memory result."""
        import inspect

        from services.popularity.stages import track_stage

        src = inspect.getsource(track_stage)
        assert "reconcile_listenbrainz_score(" in src
        # It is applied to the payload key that is persisted.
        block = src[src.index("reconcile_listenbrainz_score("):]
        assert 'update_payload["listenbrainz_score"] = _lb_reconciled' in block[:1200]


# ---------------------------------------------------------------------------
# 5. The 4★ evidence guard — "no evidence" must not earn 4★
# ---------------------------------------------------------------------------


class TestACompilationTrackWithNoEvidenceCannotHoldFourStars:
    """⭐ Demoting a 5★ to a flat 4★ was only half the fix.

    The score that reached 5★ was a scale-free "#1 of N" rank, so a track with
    no popularity evidence at all still landed exactly one tier below its rank.
    Measured: Allen Ginsberg's "Ballad of the Skeletons" — final_score 0.0, 26
    Last.fm listeners, 0 ListenBrainz listens, not a single — was rated 4★.
    """

    RULES = {
        "enabled": 1, "min_local_catalogue": 5,
        "rank_percentile_5star": 0.02, "rank_percentile_4star": 0.10,
        "rank_percentile_3star": 0.35, "rank_percentile_2star": 0.65,
        "min_catalogue_size": 5,
        "top_tier_min_score": 55.0, "top_tier_min_listeners": 250_000,
        "high_tier_min_score": 45.0, "high_tier_min_listeners": 1_000,
        "standout_floor_percentile": 0.0, "standout_floor_stars": 4,
    }

    @staticmethod
    def _rate(monkeypatch, *, score, lf, rank, total, single=True, conf="high", rules=None):
        from services.popularity.stages import finalise_stage as fs
        import services.popularity.popularity_sources as ps

        monkeypatch.setattr(
            ps, "get_online_artist_track_rank",
            lambda artist, title, lastfm_client=None: {
                "rank": rank, "total": total, "percentile": rank / total,
                "listeners": lf, "top_listeners": 500_000,
                "source": "lastfm_artist_top_tracks",
            },
        )
        track = {
            "track_id": "t", "title": "T", "artist": "X", "album": "A Comp",
            "popularity_score": score, "final_score": score, "_raw_combined": score,
            "lastfm_listeners": lf, "listenbrainz_listens": 0,
            "is_single": single, "single_confidence": conf, "is_live": False,
            "popularity_marked": False, "exclude_from_stats": False,
        }
        stars = fs._assign_stars(
            track, [score], [score] * 3,
            album_model={"has_benchmark": True, "era": "solid",
                         "catalog_cutoff": 99.0, "max_5star_slots": 3,
                         "album_top_n": 3},
            is_compilation=True,
            compilation_online_rules=dict(rules or TestACompilationTrackWithNoEvidenceCannotHoldFourStars.RULES),
            compilation_raw_scores=[score] * 5,
        )
        return stars, track

    def test_the_reported_row_drops_to_one_star(self, monkeypatch) -> None:
        """⭐ THE REPORTED CASE. 0.0 score, 26 listeners, not a single."""
        stars, track = self._rate(
            monkeypatch, score=0.0, lf=26, rank=1, total=40,
            single=False, conf="low",
        )
        assert stars == 1, "a 26-listener track with no blend must not hold 4★"
        assert track.get("_compilation_high_tier_gated") is True

    def test_a_low_score_with_modest_reach_drops_to_the_ladder(self, monkeypatch) -> None:
        """The same objection one tier down: 44.8 at 713 listeners was 4★."""
        stars, track = self._rate(monkeypatch, score=44.8, lf=713, rank=3, total=171)
        assert stars == 3
        assert track.get("_compilation_high_tier_gated") is True

    def test_a_genuine_hit_keeps_four(self, monkeypatch) -> None:
        """CONTROL — 83.6 score and 137,600 listeners clears both 4★ bars."""
        stars, track = self._rate(monkeypatch, score=83.6, lf=137_600, rank=1, total=164)
        assert stars == 4
        assert track.get("_compilation_high_tier_gated") is None

    def test_the_guard_never_lowers_a_track_already_below_four(self, monkeypatch) -> None:
        """CONTROL — it is a 4★/5★ guard; a 2★ stays 2★ and is not touched."""
        stars, track = self._rate(
            monkeypatch, score=36.0, lf=900, rank=150, total=171,
            rules={**self.RULES, "standout_floor_percentile": 0.0},
        )
        assert stars <= 3
        assert track.get("_compilation_high_tier_gated") is None

    def test_zero_on_both_bars_disables_it(self, monkeypatch) -> None:
        """CONTROL — 0/0 restores the previous behaviour exactly."""
        stars, track = self._rate(
            monkeypatch, score=44.8, lf=713, rank=3, total=171,
            rules={**self.RULES, "high_tier_min_score": 0, "high_tier_min_listeners": 0},
        )
        assert stars == 4
        assert track.get("_compilation_high_tier_gated") is None

    def test_one_bar_alone_cannot_carry_it(self, monkeypatch) -> None:
        """Both 4★ bars are required, matching the 5★ pair."""
        stars, _ = self._rate(
            monkeypatch, score=44.8, lf=900_000, rank=3, total=171,
        )
        assert stars == 3, "reach alone must not earn 4★ when the score bar fails"

    def test_the_shipped_defaults_carry_the_bars(self) -> None:
        """The config page is the source of truth; the defaults must ship on."""
        from helpers.config_helpers import get_compilation_online_catalogue_config

        rules = get_compilation_online_catalogue_config({})
        assert rules["high_tier_min_score"] == 45.0
        assert rules["high_tier_min_listeners"] == 1000.0

    def test_the_bars_are_on_the_config_page(self) -> None:
        """§4.1 — every configurable option must appear on the Config page."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        for rel in ("templates/pages/config.html", "test_site/templates/Pages/config.html"):
            html = (root / rel).read_text(encoding="utf-8")
            assert "compilation_online_high_tier_min_score" in html, rel
            assert "compilation_online_high_tier_min_listeners" in html, rel

    def test_the_bars_round_trip_through_the_config_js(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        for rel in (
            "static/js/config.js",
            "static/js/pages/config.js",
            "test_site/static/js/pages/config.js",
        ):
            js = (root / rel).read_text(encoding="utf-8")
            assert "high_tier_min_score:" in js, rel
            assert "high_tier_min_listeners:" in js, rel


class TestListenbrainzScoreIsAlwaysAbsolute:
    def test_a_two_track_album_does_not_report_the_percentile(self) -> None:
        """⭐ THE REPORTED ROW. Cult of Luna: 438 listens stored score 75.0
        (percentile 0.75) where the log score is 42.28."""
        d = pm.calculate_combined_popularity_score(
            lastfm_listeners=7_413,
            listenbrainz_listens=438,
            album_lb_listens=[438, 192],
        )
        assert d["listenbrainz_score"] == round(
            min(100.0, math.log10(439) * 16.0), 3
        ), "the reported component must be the absolute log score"
        assert d["listenbrainz_score"] < 50.0, (
            "75.0 was the album percentile leaking into an absolute column"
        )

    def test_the_percentile_still_drives_the_blend(self) -> None:
        """CONTROL — the boost is not removed, only kept out of the reported
        column: the album's best LB track must still out-blend the other."""
        top = pm.calculate_combined_popularity_score(
            lastfm_listeners=1_000,
            listenbrainz_listens=438,
            album_lb_listens=[438, 192],
        )
        bottom = pm.calculate_combined_popularity_score(
            lastfm_listeners=1_000,
            listenbrainz_listens=192,
            album_lb_listens=[438, 192],
        )
        assert top["combined_score"] > bottom["combined_score"]

    def test_no_album_distribution_is_unchanged(self) -> None:
        """CONTROL — with no album context the blend and the report already
        agreed; that must not move."""
        d = pm.calculate_combined_popularity_score(
            lastfm_listeners=7_413,
            listenbrainz_listens=438,
        )
        assert d["listenbrainz_score"] == round(
            min(100.0, math.log10(439) * 16.0), 3
        )

    def test_zero_listens_scores_zero(self) -> None:
        d = pm.calculate_combined_popularity_score(
            lastfm_listeners=1_000, listenbrainz_listens=0,
            album_lb_listens=[438, 192],
        )
        assert d["listenbrainz_score"] == 0.0
