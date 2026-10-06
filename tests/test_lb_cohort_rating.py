"""Rate a track whose ListenBrainz source was rejected among its peers.

Reported (option chosen from the three offered after the weight investigation):

> Rate within cohorts — compare a REJECT_LB track against the median of other
> tracks that also lack a usable ListenBrainz value.

Why it matters: a track whose ListenBrainz value is unusable is scored from
Last.fm ALONE, but it was being measured against an album pool its siblings
had lifted with a healthy ListenBrainz component. On *Undertow* that left
"Prison Sex" at 57.3 against a median of ~57 — an album Z of +0.05 and 3★,
while two-source siblings sat at 60-62. The comparison, not the score, was
wrong.

Three pieces, tested separately because each fails differently:

1. ``_lb_unusable_cohort`` — WHICH tracks share the fate (zero listens, or the
   Log-MAD audit rejects them, exactly as ``track_stage`` does when it zeroes
   the count);
2. ``_assign_stars`` — the cohort REPLACES the album pool for those tracks, and
   only when it is big enough to be a distribution;
3. the caller builds it once per album and passes the flag per track.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import services.popularity.stages.finalise_stage as fs  # noqa: E402

# The reported album: one track whose LF/LB pair diverges wildly, one with no
# ListenBrainz data at all, and three healthy two-source siblings.
_DIVERGENT = {
    "track_id": "a",
    "lastfm_listeners": 477_200,
    "listenbrainz_listens": 290,
    "popularity_score": 57.3,
}
_NO_LB = {"track_id": "e", "lastfm_listeners": 300_000, "listenbrainz_listens": 0,
          "popularity_score": 55.0}
_HEALTHY = [
    {"track_id": "b", "lastfm_listeners": 350_000, "listenbrainz_listens": 350_000,
     "popularity_score": 61.0},
    {"track_id": "c", "lastfm_listeners": 340_000, "listenbrainz_listens": 345_000,
     "popularity_score": 62.0},
    {"track_id": "d", "lastfm_listeners": 330_000, "listenbrainz_listens": 348_000,
     "popularity_score": 60.5},
]


class TestWhichTracksShareTheFate:
    def test_the_divergent_and_the_missing_are_flagged(self):
        """The reported case: 477k Last.fm against 290 ListenBrainz listens."""
        ids, scores = fs._lb_unusable_cohort([_DIVERGENT, _NO_LB, *_HEALTHY])

        assert ids == {"a", "e"}, (
            "the REJECT_LB track and the one with no LB data are the cohort; "
            "healthy two-source tracks must not be swept in"
        )
        assert scores == [57.3, 55.0]

    def test_healthy_two_source_tracks_are_left_alone(self):
        ids, _scores = fs._lb_unusable_cohort(_HEALTHY)

        assert ids == set()

    def test_an_album_with_no_pairs_flags_only_the_missing_ones(self):
        """No pairs → the audit cannot run → only LB==0 is unusable."""
        rows = [
            {"track_id": "x", "lastfm_listeners": 500, "listenbrainz_listens": 4,
             "popularity_score": 40.0},
            {"track_id": "y", "lastfm_listeners": 600, "listenbrainz_listens": 0,
             "popularity_score": 30.0},
        ]

        ids, _scores = fs._lb_unusable_cohort(rows)

        assert ids == {"y"}

    def test_no_rows_means_nothing(self):
        assert fs._lb_unusable_cohort([]) == (set(), [])


class TestTheCohortReplacesTheAlbumPool:
    """``_assign_stars`` must be handed the cohort, not just the flag."""

    ALBUM_POOL = [62.0, 61.5, 63.0, 64.0, 60.5]   # lifted by two-source siblings
    COHORT_POOL = [57.3, 56.8, 58.1]              # Last.fm-only tracks

    @staticmethod
    def _track(**overrides):
        track = {
            "track_id": "t1",
            "title": "Prison Sex",
            "popularity_score": 57.3,
            "final_score": 57.3,
            "lastfm_listeners": 477_200,
            "listenbrainz_listens": 290,
        }
        track.update(overrides)
        return track

    @staticmethod
    def _stars(monkeypatch, seen, **assign_kwargs):
        """Drive ``_assign_stars`` with both album-relative pools captured.

        ``_album_z_band_star`` is what actually decides ``base_stars`` and
        ``_compute_album_z`` is the top-of-function z — the two places a cohort
        has to reach, so both are asserted rather than one.
        """
        def _capture_band(**kwargs):
            seen["band_pool"] = list(kwargs.get("album_scores") or [])
            return 1

        def _capture_album_z(score, pool):
            seen.setdefault("z_pools", []).append(list(pool))
            return 0.0, 1.0

        def _neutral_artist_z(score, pool):
            return 0.0, 1.0

        monkeypatch.setattr(fs, "_album_z_band_star", _capture_band)
        monkeypatch.setattr(fs, "_compute_album_z", _capture_album_z)
        monkeypatch.setattr(fs, "_compute_artist_z", _neutral_artist_z)

        return fs._assign_stars(
            TestTheCohortReplacesTheAlbumPool._track(),
            list(TestTheCohortReplacesTheAlbumPool.ALBUM_POOL),
            [57.0, 57.5, 58.0, 57.2, 57.8],
            popularity_only=False,
            **assign_kwargs,
        )

    def test_the_star_band_is_rated_against_its_cohort(self, monkeypatch):
        """``_album_z_band_star`` decides base_stars — the cohort must reach it.

        Getting only the top-of-function z right (the first attempt) changed
        almost nothing: the band call re-derived z from ``album_scores``.
        """
        seen: dict = {}

        self._stars(
            monkeypatch, seen,
            source_unusable=True, cohort_scores=self.COHORT_POOL,
        )

        assert seen["band_pool"] == self.COHORT_POOL, (
            "the star band must be measured against the distribution the "
            "track belongs to — Last.fm-only tracks, not the sibling-lifted "
            "album median"
        )

    def test_the_top_z_is_rated_against_its_cohort(self, monkeypatch):
        seen: dict = {}

        self._stars(
            monkeypatch, seen,
            source_unusable=True, cohort_scores=self.COHORT_POOL,
        )

        assert seen.get("z_pools") == [self.COHORT_POOL]

    def test_a_healthy_track_still_uses_the_album(self, monkeypatch):
        """CONTROL — cohort rating must not leak onto tracks with both sources."""
        seen: dict = {}

        self._stars(monkeypatch, seen, source_unusable=False,
                    cohort_scores=self.COHORT_POOL)

        assert seen["band_pool"] == self.ALBUM_POOL
        assert seen.get("z_pools") == [self.ALBUM_POOL]

    def test_a_cohort_that_is_too_small_falls_back(self, monkeypatch):
        """CONTROL — 2 tracks are not a distribution; unchanged behaviour."""
        seen: dict = {}

        self._stars(
            monkeypatch, seen,
            source_unusable=True, cohort_scores=self.COHORT_POOL[:2],
        )

        assert seen["band_pool"] == self.ALBUM_POOL, (
            "below _LB_COHORT_MIN_TRACKS the album pool is the honest default"
        )

    def test_a_compilation_track_keeps_its_own_rating_path(self, monkeypatch):
        """CONTROL — compilations rate on online/absolute rules, not an album.

        Their local-catalogue branch is hard-disabled in the code, so the
        stars come from the credited artist's ONLINE catalogue or absolute
        thresholds: there is no album-relative pool for a cohort to replace,
        and pretending otherwise would change nothing while implying it did.
        """
        seen: dict = {}
        artist_pools: list = []

        def _capture_band(**kwargs):
            seen["band_pool"] = list(kwargs.get("album_scores") or [])
            return 1

        def _capture_artist_z(score, pool):
            artist_pools.append(list(pool))
            return 0.0, 1.0

        monkeypatch.setattr(fs, "_album_z_band_star", _capture_band)
        monkeypatch.setattr(fs, "_compute_artist_z", _capture_artist_z)

        fs._assign_stars(
            self._track(),
            list(self.ALBUM_POOL),
            [57.0, 57.5, 58.0, 57.2, 57.8],
            popularity_only=False,
            source_unusable=True,
            cohort_scores=self.COHORT_POOL,
            is_compilation=True,
        )

        assert "band_pool" not in seen, (
            "a compilation returns before the album band — the cohort must "
            "not silently take that path over"
        )
        assert artist_pools and artist_pools[0] != self.COHORT_POOL

    def test_the_rejected_track_rates_higher_against_its_peers(self):
        """The point of the change, with the real z math and real thresholds.

        The z expectation is asserted from ``_compute_album_z`` itself, and the
        star expectation is a DIRECTION (never lower) rather than an exact
        number — star thresholds are configuration, so pinning a value here
        would turn every deliberate threshold change into a test failure.
        """
        lifted, _lifted_spread = fs._compute_album_z(57.3, self.ALBUM_POOL)
        cohort, _cohort_spread = fs._compute_album_z(57.3, self.COHORT_POOL)

        assert cohort > lifted, (
            f"against its own peers 57.3 must look better than against a "
            f"median its siblings' ListenBrainz points raised "
            f"(cohort z={cohort:+.2f} vs album z={lifted:+.2f})"
        )

        def _stars(album_pool: list[float], unusable: bool) -> int:
            return fs._assign_stars(
                self._track(),
                album_pool,
                [57.0, 57.5, 58.0, 57.2, 57.8],
                album_lf_listeners=[477_200] * len(album_pool),
                album_lb_listens=[350_000] * len(album_pool),
                is_compilation=False,
                source_unusable=unusable,
                cohort_scores=self.COHORT_POOL,
            )

        plain = _stars(self.ALBUM_POOL, unusable=False)
        cohorted = _stars(self.COHORT_POOL, unusable=True)

        assert cohorted >= plain, (
            f"cohort rating must not lower the rating "
            f"({cohorted} < {plain}); lifted z={lifted:+.2f}, cohort z={cohort:+.2f}"
        )


class TestTheCallerBuildsItOncePerAlbum:
    def test_the_cohort_is_built_before_the_rating_loop(self):
        source = (REPO_ROOT / "services" / "popularity" / "stages" /
                  "finalise_stage.py").read_text(encoding="utf-8")

        build = source.index("_lb_unusable_cohort(album_results)")
        loop = source.index("for track in album_results:", build)

        assert build < loop, "the cohort must be built once, before the loop"

    def test_each_track_is_flagged_from_its_own_id(self):
        source = (REPO_ROOT / "services" / "popularity" / "stages" /
                  "finalise_stage.py").read_text(encoding="utf-8")

        assert "source_unusable=(" in source
        assert "in _lb_unusable_ids" in source
        assert "cohort_scores=_lb_cohort_scores" in source
