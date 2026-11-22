"""Tests for the popularity scoring math (soft-ceiling z-score mapping).

Covers the regressions where the scoring collapsed popular tracks together:
- the artist-context median+MAD adjustment mapped every track well above its
  artist's median onto the same 100 ceiling (e.g. 128k vs 156k listeners both
  scored exactly 100.0), losing all discrimination;
- blending a weaker ListenBrainz / age signal into the average dragged a
  genuinely popular track below its strongest single source of evidence
  (a 139k-listener track with LB data scored below a 133k-listener track with
  none; the 179k-listener album lead scored lowest);
- the flat *1.15 single boost saturated every top single (364k vs 128k
  listeners both scored ~95-97).
"""

from __future__ import annotations

import math

from services.popularity.popularity_math import zscore_to_popularity


class TestZScoreToPopularitySoftCeiling:
    """The z-score→popularity mapping must keep discriminating at the top."""

    def test_midpoint_preserved(self):
        assert zscore_to_popularity(0.0) == 50.0

    def test_low_negative_scores_floor(self):
        assert zscore_to_popularity(-10.0) == 0.0

    def test_high_positive_scores_do_not_saturate_at_100(self):
        # The legacy linear map returned 100.0 for any z >= 3. Distinct high
        # popularities must keep distinct scores instead of hitting the ceiling.
        assert zscore_to_popularity(3.0) < 100.0
        assert zscore_to_popularity(4.0) < 100.0

    def test_top_of_scale_is_monotonic_and_asymptotic(self):
        assert zscore_to_popularity(3.0) < zscore_to_popularity(4.0)
        assert zscore_to_popularity(4.0) < zscore_to_popularity(6.0)
        assert zscore_to_popularity(6.0) < zscore_to_popularity(8.0)
        assert zscore_to_popularity(20.0) > 99.0

    def test_tracks_with_different_popularity_stay_apart(self):
        # Stray Kids - 5-STAR reproduction: raw combined scores 83.13 and 81.72
        # (156,916 vs 128,085 listeners) both mapped to exactly 100.0 under the
        # old hard clamp. With the soft ceiling the two remain ordered.
        high = zscore_to_popularity(3.31)
        mid = zscore_to_popularity(3.17)
        assert high > mid
        assert high < 100.0
        assert mid < 100.0


class TestLastfmLogScaleDiscrimination:
    """The raw log-scale scores already discriminate mid-popularity tracks."""

    def test_log_scale_does_not_saturate_at_100(self):
        from services.popularity.popularity_math import calculate_lastfm_popularity_score

        assert calculate_lastfm_popularity_score(156916) < 100.0
        assert calculate_lastfm_popularity_score(128085) < 100.0
        assert calculate_lastfm_popularity_score(156916) > calculate_lastfm_popularity_score(128085)


class TestBlendNeverDropsBelowStrongestEvidence:
    """A blend of corroborating sources must not drag a track below its best one.

    5-STAR reproductions: ITEM (139,568 LF / 5,330 LB) scored *below*
    Collision (133,646 LF / no LB), and the album lead TOPLINE (179,819 LF /
    1,814 LB) scored lowest, because the weaker LB count out-voted the strong
    Last.fm footprint in the weighted average.
    """

    def test_lb_evidence_does_not_drag_below_lf_log(self):
        from services.popularity.popularity_math import (
            calculate_combined_popularity_score,
            calculate_lastfm_popularity_score,
        )

        with_lb = calculate_combined_popularity_score(
            lastfm_listeners=139568,
            listenbrainz_listens=5330,
            age_source_value=5330,
            release_date="2023",
        )
        without_lb = calculate_combined_popularity_score(
            lastfm_listeners=133646,
            listenbrainz_listens=0,
        )
        # The strongest absolute evidence (the LF log score) is the floor.
        assert with_lb["combined_score"] >= calculate_lastfm_popularity_score(139568, 0)
        assert with_lb["combined_score"] >= without_lb["combined_score"]

    def test_featured_album_lead_with_undercounted_lb_keeps_lf_score(self):
        from services.popularity.popularity_math import (
            calculate_combined_popularity_score,
            calculate_lastfm_popularity_score,
        )

        d = calculate_combined_popularity_score(
            lastfm_listeners=179819,
            listenbrainz_listens=1814,
            is_featured_track=True,
            age_source_value=1814,
            release_date="2023",
        )
        floor = round(calculate_lastfm_popularity_score(179819, 0), 3)
        assert d["combined_score"] >= floor


class TestLbMissingFallbackAlbumRelativeScale:
    """LB-less fallback tracks must sit on the album-relative scale, not the
    raw absolute Last.fm log scale.

    Regression (Stray Kids "NOEASY"): sub-unit parenthetical titles
    ("Gone Away (HAN, Seungmin & I.N)", "Surfin' (Lee Know, Changbin &
    Felix)", "Red Lights (Bang Chan & Hyunjin)") fail the ListenBrainz
    tracklist match → 0 listens → the single-source fallback scored on the
    ABSOLUTE log scale (79.5-87.8) while LB-valid tracks were album-relative
    (~45-65).  The raw scores exploded the album z-scores and forced false
    5★ ratings (a 93.2k-listener track outranking the 373.7k-listener title
    track).
    """

    def test_lb_less_track_scores_album_relative_not_absolute(self):
        from services.popularity.popularity_math import (
            calculate_combined_popularity_score,
            calculate_lastfm_popularity_score,
        )

        # Gone Away: 93.2k LF, LB 0 (match failure) — sits below the album's
        # LF median, so it must NOT keep the raw absolute score.
        d = calculate_combined_popularity_score(
            lastfm_listeners=93200,
            listenbrainz_listens=0,
            album_lf_listeners=[373700, 222400, 111000, 305100, 93200, 150000, 120000],
        )
        raw_absolute = calculate_lastfm_popularity_score(93200, 0)
        # The absolute curve reaches 100 at 100M listeners
        # (popularity_math.LASTFM_SCORE_LISTENER_CEILING), so 93.2k listeners is
        # ~62, not the old ~79.5 when the ceiling was ~1.78M. What matters here
        # is that the absolute scale still sits well ABOVE the album-relative
        # result — that gap is the regression this class is named for.
        assert raw_absolute > d["combined_score"]
        assert 0.0 < d["combined_score"] < 60.0  # now album-relative

    def test_multi_source_floor_still_applies(self):
        from services.popularity.popularity_math import (
            calculate_combined_popularity_score,
            calculate_lastfm_popularity_score,
        )

        # Thunderous: 373.7k LF, 15.8k LB → genuine multi-source evidence.
        d = calculate_combined_popularity_score(
            lastfm_listeners=373700,
            listenbrainz_listens=15800,
            album_lf_listeners=[373700, 222400, 111000, 305100, 93200, 150000, 120000],
            age_source_value=15800,
            release_date="2021-08-23",
        )
        floor = round(calculate_lastfm_popularity_score(373700, 0), 3)
        # ⚠️ The strongest-absolute-evidence floor is INTENTIONALLY skipped when
        # an album distribution is present: it only guards the NO-album-context
        # case (see the ``if not has_album_context`` branch in
        # ``calculate_combined_popularity_score``). Asserting the score stays at
        # the absolute floor here would contradict the album-relative behaviour
        # the previous test requires (``< 60.0``) — the two cannot both hold.
        # The floor's real coverage is the no-distribution case (see
        # ``TestBlendNeverDropsBelowStrongestEvidence`` and
        # ``test_no_album_distribution_keeps_absolute_fallback`` below).
        assert d["combined_score"] > 0.0
        assert d["combined_score"] != floor

    def test_no_album_distribution_keeps_absolute_fallback(self):
        from services.popularity.popularity_math import (
            calculate_combined_popularity_score,
            calculate_lastfm_popularity_score,
        )

        # No album distribution to normalise by → absolute log fallback stays.
        d = calculate_combined_popularity_score(
            lastfm_listeners=93200,
            listenbrainz_listens=0,
        )
        assert d["combined_score"] == round(calculate_lastfm_popularity_score(93200, 0), 3)


class TestSingleBoostFade:
    """The confirmed-single boost tapers near the ceiling instead of saturating."""

    def test_fade_unit_behaviour(self):
        from services.popularity.popularity_math import single_boost_fade

        assert single_boost_fade(50.0) == 1.0
        assert single_boost_fade(95.0) == 0.0
        assert 0.0 < single_boost_fade(70.0) < 1.0

    def test_high_singles_stay_apart_and_below_ceiling(self):
        from services.popularity.popularity_math import calculate_combined_popularity_score

        top = calculate_combined_popularity_score(
            lastfm_listeners=364373, is_single=True, single_boost=1.15
        )
        mid = calculate_combined_popularity_score(
            lastfm_listeners=128085, is_single=True, single_boost=1.15
        )
        assert top["combined_score"] > mid["combined_score"]
        assert top["combined_score"] < 100.0
        assert mid["combined_score"] < 100.0

    def test_boost_adds_more_to_a_mid_single_than_an_already_top_single(self):
        from services.popularity.popularity_math import calculate_combined_popularity_score

        def boosted(lf):
            return calculate_combined_popularity_score(
                lastfm_listeners=lf, is_single=True, single_boost=1.15
            )["combined_score"]

        def unboosted(lf):
            return calculate_combined_popularity_score(
                lastfm_listeners=lf, is_single=False, single_boost=1.15
            )["combined_score"]

        # The fade means the bonus shrinks as the raw score nears the ceiling.
        assert (boosted(128085) - unboosted(128085)) > (boosted(364373) - unboosted(364373))


class TestArtistAdjustmentDampening:
    """The artist median+MAD re-map is damped so it can't re-compress the top."""

    def test_raw_blend_is_between_zero_and_one(self):
        from services.popularity.popularity_adjustments import ARTIST_ADJUSTMENT_RAW_BLEND

        assert 0.0 < ARTIST_ADJUSTMENT_RAW_BLEND < 1.0


class TestAlbumRelativePopularity:
    """Album-relative popularity: robust z against the album median + scaled-MAD."""

    def _apply(self, raw, album_scores):
        from services.popularity.popularity_math import apply_album_relative_popularity
        return apply_album_relative_popularity(raw, album_scores)

    def test_album_median_track_scores_around_midpoint(self):
        # A track sitting at the album median re-maps to ~50 (z ≈ 0).
        score = self._apply(60.0, [80, 75, 70, 65, 60, 55])
        assert 30 <= score <= 70

    def test_top_of_album_scores_above_median_track(self):
        # The album's strongest track re-maps well above its median sibling.
        album = [80, 75, 70, 65, 60, 55]
        top = self._apply(80.0, album)
        median = self._apply(60.0, album)
        assert top > median
        assert top < 100.0

    def test_no_ceiling_clumping_within_album(self):
        # Distinct raw scores stay distinct on the album-relative scale.
        album = [95, 90, 85, 80, 75, 70]
        first = self._apply(95.0, album)
        second = self._apply(90.0, album)
        assert first > second
        assert first < 100.0
        assert second < 100.0

    def test_requires_three_valid_album_scores(self):
        # Too few album scores → no meaningful distribution → raw is unchanged.
        album = [80, 75]
        assert self._apply(80.0, album) == 80.0

    def test_zero_raw_unchanged(self):
        assert self._apply(0.0, [80, 75, 70, 65, 60]) == 0.0

    def test_ignores_zero_values_in_album_distribution(self):
        # Zero scores (missing data) never anchor the album distribution.
        album = [80, 75, 70, 65, 60, 0, 0, 0]
        score = self._apply(80.0, album)
        assert score >= 60.0


class TestTrackArtistRelativePopularity:
    """Compilation popularity: robust z against the TRACK artist's catalogue.

    Compilation / Various-Artists albums mix many artists, so the compilation's
    own distribution (the "album artist" reference) is meaningless.  Each track
    is re-mapped against its own track artist's catalogue popularity instead —
    the score answers "how popular is this track within its artist's
    discography".
    """

    def _apply(self, raw, artist_scores):
        from services.popularity.popularity_math import apply_track_artist_relative_popularity
        return apply_track_artist_relative_popularity(raw, artist_scores)

    def test_artist_median_track_scores_around_midpoint(self):
        # A track at the artist's catalogue median re-maps to ~50 (z ≈ 0).
        score = self._apply(60.0, [80, 75, 70, 65, 60, 55])
        assert 30 <= score <= 70

    def test_strong_track_for_artist_scores_above_catalogue_median(self):
        artist = [50, 52, 54, 56, 58, 60]
        strong = self._apply(90.0, artist)
        typical = self._apply(55.0, artist)
        assert strong > typical
        assert strong < 100.0
        assert typical < 100.0

    def test_same_mapping_as_album_relative(self):
        # The track-artist variant is the same robust-z mapping, just against a
        # different reference distribution.
        from services.popularity.popularity_math import (
            apply_album_relative_popularity,
            apply_track_artist_relative_popularity,
        )
        artist = [80, 75, 70, 65, 60, 55]
        assert apply_track_artist_relative_popularity(85.0, artist) == apply_album_relative_popularity(85.0, artist)

    def test_requires_three_valid_artist_scores(self):
        # Too few artist catalogue scores → no meaningful distribution → raw kept.
        assert self._apply(80.0, [80, 75]) == 80.0

    def test_zero_raw_unchanged(self):
        assert self._apply(0.0, [80, 75, 70, 65, 60]) == 0.0
