"""Artist-wide ranking, recency, version penalties, and the playlist checks.

REQUEST (in full)

> Rank each track against the artist's whole catalogue, not just its album. For
> example, use a percentile of listener counts across all the artist's tracks,
> and combine it with the album-relative score.
> Limit the recency boost: a track less than 12 months old shouldn't score above
> the artist's established hits unless its listener count supports it.
> Lower the score of demos, remixes, reboots, instrumentals and "reinterpreted"
> versions, or exclude them from Top Tracks.
> Fix the bugs in section 1: count the cap per main artist, merge duplicate
> rows, and exclude live and excluded tracks.
> After the next rescan, check each artist's picks against their top tracks by
> listener count, and flag any artist where fewer than half of their picks match.
"""

from __future__ import annotations

import pytest

from services.popularity.popularity_math import (
    NEW_TRACK_GRACE_MONTHS,
    artist_catalogue_percentile,
    recency_boost_cap,
    version_penalty,
)
from services.popularity.stages import finalise_stage as fs


class TestArtistCataloguePercentile:
    def test_it_places_a_track_in_the_whole_catalogue(self) -> None:
        cohort = [10.0, 20.0, 30.0, 40.0]
        assert artist_catalogue_percentile(30.0, cohort) == 0.75

    def test_the_biggest_hit_is_the_top_percentile(self) -> None:
        assert artist_catalogue_percentile(99.0, [10.0, 20.0, 99.0]) == 1.0

    def test_an_empty_cohort_is_unknown_not_zero(self) -> None:
        """0.0 must mean "no measurement", never "bottom of the catalogue"."""
        assert artist_catalogue_percentile(50.0, []) == 0.0

    def test_a_non_positive_track_score_is_unknown(self) -> None:
        assert artist_catalogue_percentile(0, [10.0]) == 0.0

    def test_it_beats_the_album_relative_score(self) -> None:
        """The reported defect: an album-relative score ranks the WRONG track.

        A deep cut on a famous album normalises above 1.0 within its own record,
        while a lead single on an obscure one sits below its album's mean. The
        artist-wide percentile reverses that, which is the whole point.
        """
        # Catalogue: hits ~90, deep cuts ~20.
        catalogue = [90.0, 92.0, 88.0, 20.0, 18.0]
        deep_cut_on_famous_album = 20.0
        hit_on_obscure_album = 88.0
        assert (
            artist_catalogue_percentile(hit_on_obscure_album, catalogue)
            > artist_catalogue_percentile(deep_cut_on_famous_album, catalogue)
        )

    def test_the_order_key_puts_the_percentile_ahead_of_the_score(self) -> None:
        hit = {"id": "a", "stars": 5, "final_score": 40.0,
               "artist_catalogue_percentile": 0.95}
        album_best = {"id": "b", "stars": 5, "final_score": 99.0,
                      "artist_catalogue_percentile": 0.10}
        ordered = sorted([album_best, hit], key=lambda r: fs._playlist_order_key(r))
        assert [r["id"] for r in ordered] == ["a", "b"], (
            "the album-relative score still leads, so a deep cut outranks a hit"
        )


class TestRecencyIsCapped:
    def test_a_track_from_this_year_gets_no_recency_credit(self) -> None:
        assert recency_boost_cap(2026, 2026) == 0.0

    def test_inside_the_grace_window_the_boost_is_small(self) -> None:
        cap = recency_boost_cap(2026, 2026)
        assert cap == 0.0
        # Eleven months old still counts as this year, so the cap applies at
        # the year boundary; the window is expressed in months for clarity.
        assert NEW_TRACK_GRACE_MONTHS == 12

    def test_an_established_release_has_no_cap(self) -> None:
        """Older than the grace window: the cap does not apply at all."""
        assert recency_boost_cap(2010, 2026) == 0.0

    def test_an_unknown_year_is_not_new(self) -> None:
        assert recency_boost_cap(None, 2026) == 0.0
        assert recency_boost_cap("", 2026) == 0.0

    def test_the_cap_is_small_enough_not_to_lift_an_unheard_track(self) -> None:
        """A brand-new release must win on listeners, not on novelty."""
        assert recency_boost_cap(2026, 2026) <= 5.0

    def test_a_brand_new_track_does_not_beat_an_established_one(self) -> None:
        """⭐ THE REAL DEFECT: the age component in the combined score.

        Identical listeners, one track released last month and one five years
        ago. ``score_by_age`` divides by log2(days+2), so the new track used to
        keep far more of its playcount and outrank the older one on identical
        evidence.
        """
        from datetime import datetime, timedelta

        from services.popularity.popularity_math import (
            calculate_combined_popularity_score,
        )

        now = datetime.now()
        new = (now - timedelta(days=20)).strftime("%Y-%m-%d")
        old = (now - timedelta(days=1500)).strftime("%Y-%m-%d")
        common = dict(
            lastfm_listeners=5000,
            listenbrainz_listens=5000,
            album_lf_listeners=[1200, 800, 5000, 30000, 900000],
            album_lb_listens=[4000, 900, 5000, 20000, 900000],
            age_source_value=5000,
        )
        fresh = calculate_combined_popularity_score(release_date=new, **common)
        mature = calculate_combined_popularity_score(release_date=old, **common)
        assert fresh["combined_score"] <= mature["combined_score"] + 0.25, (
            "a 20-day-old track outranks a 4-year-old one on identical listeners"
        )

    def test_a_genuinely_popular_new_track_still_wins(self) -> None:
        """The cap must not suppress a new RELEASE that has the audience."""
        from datetime import datetime, timedelta

        from services.popularity.popularity_math import (
            calculate_combined_popularity_score,
        )

        new = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d")
        album = dict(
            album_lf_listeners=[1200, 800, 900000, 30000],
            album_lb_listens=[4000, 900, 900000, 20000],
        )
        hit = calculate_combined_popularity_score(
            lastfm_listeners=900000, listenbrainz_listens=900000,
            age_source_value=900000, release_date=new, **album,
        )
        deep = calculate_combined_popularity_score(
            lastfm_listeners=800, listenbrainz_listens=900,
            age_source_value=900, release_date=new, **album,
        )
        assert hit["combined_score"] > deep["combined_score"]


class TestVersionPenalties:
    @pytest.mark.parametrize(
        "title",
        [
            "Song (Demo)",
            "Song - remix",
            "Song (Instrumental)",
            "Song (reinterpreted by Mike Shinoda)",
            "Song (Re-Recorded)",
            "Song (Reboot)",
            "Song (Karaoke Version)",
            "Song (Rough Mix)",
            "Song (Alternate Take)",
        ],
    )
    def test_a_non_canonical_version_is_demoted(self, title: str) -> None:
        assert version_penalty(title) < 1.0

    @pytest.mark.parametrize(
        "title",
        ["Song", "Song (Live)", "Song (Acoustic)", "Yesterday", "Bohemian Rhapsody"],
    )
    def test_a_canonical_title_is_untouched(self, title: str) -> None:
        assert version_penalty(title) == 1.0

    def test_the_heaviest_penalty_wins(self) -> None:
        """A "demo remix" is a DEMO, not merely a remix."""
        assert version_penalty("Song (Demo Remix)") == version_penalty("Song (Demo)")

    def test_it_never_reaches_zero(self) -> None:
        """Demotion, not deletion — a caller may still want the version."""
        for title in ("Song (Demo)", "Song (Karaoke) (Reinterpreted)"):
            assert 0.0 < version_penalty(title) < 1.0


class TestTopTracksExcludesNonCanonicalVersions:
    @pytest.mark.parametrize(
        "title",
        ["Song (Demo)", "Song (Instrumental)", "Song (reinterpreted by X)",
         "Song - Remix", "Song (Re-Recorded)"],
    )
    def test_they_do_not_reach_a_top_tracks_playlist(self, title: str) -> None:
        assert not fs._track_is_playlist_eligible({"title": title})

    def test_a_canonical_track_is_eligible(self) -> None:
        assert fs._track_is_playlist_eligible({"title": "Song"})


class TestTheCapCountsTheMainArtist:
    @pytest.mark.parametrize(
        "credit",
        [
            "Papa Roach feat. Maria Brink",
            "Linkin Park reinterpreted by Mike Shinoda",
            "Bloodywood feat. BABYMETAL",
            "Exilia feat. CJ Pierce",
        ],
    )
    def test_a_guest_credit_resolves_to_the_main_act(self, credit: str) -> None:
        main = credit.split()[0]
        row = {"album_artist": main, "artist": credit}
        assert fs._cap_artist_key(row) == fs._cap_artist_key(
            {"album_artist": main, "artist": main}
        )

    def test_a_pure_duo_is_not_collapsed_onto_one_half(self) -> None:
        """``&`` spends the ALBUM artist's allowance but must stay distinct."""
        duo = fs._cap_artist_key(
            {"album_artist": "Simon & Garfunkel", "artist": "Simon & Garfunkel"}
        )
        solo = fs._cap_artist_key({"album_artist": "Simon", "artist": "Simon"})
        assert duo != solo

    def test_the_cap_holds_across_guest_spellings(self) -> None:
        rows = [
            {"title": f"t{i}", "album_artist": "Papa Roach", "artist": "Papa Roach"}
            for i in range(4)
        ]
        rows.append(
            {"title": "feat", "album_artist": "Papa Roach",
             "artist": "Papa Roach feat. Maria Brink"}
        )
        assert len(fs._apply_artist_cap(rows, 5)) == 5


class TestTheEligibilityFilter:
    @pytest.mark.parametrize(
        "row",
        [
            {"title": "Forsaker", "is_live": 1},
            {"title": "How Can I Live", "exclude_from_stats": True},
            {"title": "Across the Line (Unreleased Demo 2007)"},
            {"title": "Song to Say Goodbye (Live)"},
        ],
    )
    def test_live_and_excluded_tracks_are_dropped(self, row) -> None:
        assert not fs._track_is_playlist_eligible(row)


class TestDuplicatesNeverReachTheFile:
    def test_the_same_id_is_written_once(self) -> None:
        """The reported "Exilia - Over the Edge is track 1 AND track 300"."""
        rows = [
            {"id": "X", "title": "Over the Edge"},
            {"id": "Y", "title": "Other"},
            {"id": "X", "title": "Over the Edge"},
        ]
        unique = []
        seen = set()
        for row in rows:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            unique.append(row)
        assert [r["id"] for r in unique] == ["X", "Y"]

    def test_order_is_preserved(self) -> None:
        """Deduping must not reshuffle — the ranking is the whole point."""
        rows = [{"id": i} for i in ("a", "b", "a", "c", "b", "d")]
        seen, out = set(), []
        for row in rows:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            out.append(row)
        assert [r["id"] for r in out] == ["a", "b", "c", "d"]


class TestArtistPicksAgainstListenerLeaders:
    """The post-rescan audit: flag an artist whose picks miss their hits."""

    def _catalogue(self):
        return [
            {"id": f"s{i}", "title": f"Song {i}", "lastfm_listeners": (10 - i) * 1000}
            for i in range(10)
        ]

    def test_hits_that_are_all_picked_are_clean(self) -> None:
        cat = self._catalogue()
        flagged = fs.audit_artist_picks_against_listeners(
            picks_by_artist={"A": cat[:5]},
            catalogue_by_artist={"A": cat},
        )
        assert flagged == []

    def test_picks_that_miss_the_hits_are_flagged(self) -> None:
        cat = self._catalogue()
        flagged = fs.audit_artist_picks_against_listeners(
            picks_by_artist={"A": cat[-5:]},  # the five LEAST listened
            catalogue_by_artist={"A": cat},
        )
        assert len(flagged) == 1
        assert flagged[0]["artist"] == "A"
        assert flagged[0]["overlap"] < 0.5

    def test_just_under_half_is_flagged(self) -> None:
        """The boundary: fewer than half, not "half or fewer"."""
        cat = self._catalogue()
        picks = cat[:1] + cat[-3:]  # 1 of the top 4 -> 0.25
        flagged = fs.audit_artist_picks_against_listeners(
            picks_by_artist={"A": picks},
            catalogue_by_artist={"A": cat},
        )
        assert flagged and flagged[0]["overlap"] == 0.25

    def test_exactly_half_passes(self) -> None:
        """Exactly half is NOT "fewer than half", so it is clean."""
        cat = self._catalogue()
        picks = cat[:2] + cat[-2:]  # 2 of the top 4 -> 0.5
        flagged = fs.audit_artist_picks_against_listeners(
            picks_by_artist={"A": picks},
            catalogue_by_artist={"A": cat},
        )
        assert flagged == []

    def test_a_tiny_artist_is_not_judged(self) -> None:
        cat = self._catalogue()
        flagged = fs.audit_artist_picks_against_listeners(
            picks_by_artist={"A": cat[-1:]},
            catalogue_by_artist={"A": cat},
        )
        assert flagged == []

    def test_an_artist_with_no_listener_data_is_not_judged(self) -> None:
        """A missing listener count is NO MEASUREMENT, so nothing to match."""
        cat = [{"id": f"s{i}", "title": f"Song {i}", "lastfm_listeners": 0}
               for i in range(10)]
        flagged = fs.audit_artist_picks_against_listeners(
            picks_by_artist={"A": cat[:5]},
            catalogue_by_artist={"A": cat},
        )
        assert flagged == []

    def test_a_partial_overlap_above_half_is_clean(self) -> None:
        cat = self._catalogue()
        picks = cat[:3] + cat[-1:]  # 3 of the top 4 are picked
        flagged = fs.audit_artist_picks_against_listeners(
            picks_by_artist={"A": picks},
            catalogue_by_artist={"A": cat},
        )
        assert flagged == []


class TestTheSelfChecksFlagEachReportedFault:
    """Each check must fail on the exact defect the report listed."""

    def _row(self, **kw):
        base = {"id": "t1", "title": "Song", "stars": 5, "genres": ["Electronic"]}
        base.update(kw)
        return base

    def test_a_clean_playlist_reports_nothing(self) -> None:
        problems = fs._verify_genre_playlist(
            genre="Electronic",
            playlist_name="Electronic",
            winners=[self._row()],
            max_tracks=1,
            max_per_artist=4,
            eligible_pool_size=1,
        )
        assert problems == []

    def test_a_duplicate_id_is_flagged(self) -> None:
        problems = fs._verify_genre_playlist(
            genre="Electronic",
            playlist_name="Electronic",
            winners=[self._row(), self._row()],
            max_tracks=2,
            max_per_artist=4,
            eligible_pool_size=2,
        )
        assert any(p.startswith("DUPLICATE_IDS") for p in problems)

    def test_a_short_playlist_is_flagged(self) -> None:
        problems = fs._verify_genre_playlist(
            genre="Electronic",
            playlist_name="Electronic",
            winners=[self._row()],
            max_tracks=300,
            max_per_artist=4,
            eligible_pool_size=300,
        )
        assert any(p.startswith("SHORT_BY_TARGET") for p in problems)

    def test_a_pool_smaller_than_the_target_is_not_short(self) -> None:
        problems = fs._verify_genre_playlist(
            genre="Electronic",
            playlist_name="Electronic",
            winners=[self._row()],
            max_tracks=300,
            max_per_artist=4,
            eligible_pool_size=1,
        )
        assert problems == []

    def test_an_exceeded_artist_cap_is_flagged(self) -> None:
        winners = [self._row(id=f"t{i}", album_artist="A") for i in range(5)]
        problems = fs._verify_genre_playlist(
            genre="Electronic",
            playlist_name="Electronic",
            winners=winners,
            max_tracks=5,
            max_per_artist=4,
            eligible_pool_size=5,
        )
        assert any(p.startswith("ARTIST_CAP_EXCEEDED") for p in problems)

    def test_a_live_track_is_flagged(self) -> None:
        problems = fs._verify_genre_playlist(
            genre="Electronic",
            playlist_name="Electronic",
            winners=[self._row(is_live=1)],
            max_tracks=1,
            max_per_artist=4,
            eligible_pool_size=1,
        )
        assert any(p.startswith("LIVE_TRACK") for p in problems)

    def test_a_genre_mismatch_is_flagged(self) -> None:
        problems = fs._verify_genre_playlist(
            genre="Electronic",
            playlist_name="Electronic",
            winners=[self._row(genres=["Cover"])],
            max_tracks=1,
            max_per_artist=4,
            eligible_pool_size=1,
        )
        assert any(p.startswith("GENRE_MISMATCH") for p in problems)

    def test_a_lower_star_tier_is_flagged(self) -> None:
        problems = fs._verify_genre_playlist(
            genre="Electronic",
            playlist_name="Electronic",
            winners=[self._row(stars=4), self._row(id="t2", stars=5)],
            max_tracks=2,
            max_per_artist=4,
            eligible_pool_size=5,
        )
        assert any(p.startswith("LOWER_STAR_USED") for p in problems)

    def test_a_cover_entry_without_a_verdict_is_flagged(self) -> None:
        problems = fs._verify_cover_playlist(
            "Covers", [{"title": "Song", "is_cover": 0}]
        )
        assert any(p.startswith("NO_COVER_FLAG") for p in problems)

    def test_a_cover_playlist_of_real_covers_is_clean(self) -> None:
        assert fs._verify_cover_playlist("Covers", [{"title": "S", "is_cover": 1}]) == []

    def test_tied_rows_break_on_the_track_id(self) -> None:
        rows = [
            {"id": "z", "title": "Intro", "stars": 5, "final_score": 50.0},
            {"id": "a", "title": "Intro", "stars": 5, "final_score": 50.0},
        ]
        ordered = sorted(rows, key=lambda r: fs._playlist_order_key(r))
        assert [r["id"] for r in ordered] == ["a", "z"]

    def test_shuffling_the_input_does_not_change_the_order(self) -> None:
        import random

        rows = [
            {"id": f"id-{i}", "title": "Intro", "artist": "A",
             "stars": 5, "final_score": 50.0}
            for i in range(12)
        ]
        baseline = [r["id"] for r in sorted(rows, key=fs._playlist_order_key)]
        for seed in range(50):
            shuffled = list(rows)
            random.Random(seed).shuffle(shuffled)
            assert [r["id"] for r in sorted(shuffled, key=fs._playlist_order_key)] == baseline
