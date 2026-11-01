"""Sequel / short-title / version separation and the Last.fm ceiling.

Reported collisions:

* Metallica "The Unforgiven II" inherited "The Unforgiven"'s Last.fm totals.
* Pearl Jam "Spin the Black Circle" inherited "Black"'s.
* Stray Kids Japanese versions inherited the Korean originals'.
* Thousands of rows had listeners > 1,000 and playcount 0 but scored 100.

The first three share one cause: ``fuzzy_match_score`` is ``token_set_ratio``,
which returns 1.0 for a strict SUBSET — so any shorter key "matched" a longer
one. The last is two causes: a missing playcount was folded into 0, and the
album-relative Last.fm z handed every album's lead track the full ceiling.
"""

from __future__ import annotations

from services.popularity.popularity_matching import aggregation_keys_compatible
from services.popularity.popularity_math import (
    LISTENER_ONLY_LASTFM_CEILING,
    calculate_combined_popularity_score,
    calculate_lastfm_popularity_score,
)


class TestSequelNumeralsAreDistinct:
    def test_the_unforgiven_and_its_sequel_do_not_match(self) -> None:
        assert not aggregation_keys_compatible(
            "the unforgiven", "the unforgiven ii"
        )
        assert not aggregation_keys_compatible(
            "the unforgiven ii", "the unforgiven"
        )

    def test_part_two_is_a_different_song(self) -> None:
        for longer in ("song part 2", "song pt 2", "song ii", "song 2"):
            assert not aggregation_keys_compatible("song", longer), longer


class TestAShortTitleIsNotAContainedMatch:
    def test_black_does_not_match_spin_the_black_circle(self) -> None:
        assert not aggregation_keys_compatible(
            "black", "spin the black circle"
        )
        assert not aggregation_keys_compatible(
            "spin the black circle", "black"
        )

    def test_one_word_soft_variant_still_aggregates(self) -> None:
        # "Radio Edit" is a SOFT marker: same performance, safe to sum.
        assert aggregation_keys_compatible("song", "song radio edit")

    def test_a_session_annotation_still_aggregates(self) -> None:
        assert aggregation_keys_compatible(
            "100 words",
            "100 words from sing the sorrow sessions 2003",
        )


class TestVersionedRecordingsStaySeparate:
    def test_japanese_versions_do_not_inherit_the_original(self) -> None:
        pairs = [
            ("maniac", "maniac japanese version"),
            ("maniac", "maniac japanese ver"),
            ("venom", "venom japanese ver"),
            ("chk chk boom", "chk chk boom japanese ver"),
            ("silent cry", "silent cry japanese ver"),
        ]
        for base, versioned in pairs:
            assert not aggregation_keys_compatible(base, versioned), versioned

    def test_live_and_acoustic_stay_separate_upstream(self) -> None:
        # The aggregation gate deliberately leaves marker agreement to the
        # context-aware upstream (a plainly titled track on an unplugged
        # release must still match). That upstream is the one that separates
        # a live/acoustic performance from the studio cut.
        from services.popularity.popularity_matching import title_variants_compatible

        assert not title_variants_compatible(
            "see you in hell", "see you in hell live"
        )
        assert not title_variants_compatible("song", "song acoustic")


class TestTheLastfmCurveKeepsItsHeadroom:
    def test_the_top_of_the_catalogue_is_no_longer_flat_100(self) -> None:
        two_million = calculate_lastfm_popularity_score(2_000_000)
        four_million = calculate_lastfm_popularity_score(4_200_000)
        assert two_million < 100.0
        assert four_million < 100.0
        assert four_million > two_million

    def test_the_old_saturation_point_is_now_mid_scale(self) -> None:
        # 1.78M listeners used to be exactly 100.
        assert calculate_lastfm_popularity_score(1_780_000) < 85.0


class TestListenerOnlyEvidenceIsLowerConfidence:
    def test_a_missing_playcount_caps_the_lastfm_component(self) -> None:
        d = calculate_combined_popularity_score(
            lastfm_listeners=40_000_000,
            listenbrainz_listens=0,
            lastfm_playcount_available=False,
        )
        assert d["lastfm_score"] <= LISTENER_ONLY_LASTFM_CEILING

    def test_a_measured_playcount_keeps_the_full_score(self) -> None:
        d = calculate_combined_popularity_score(
            lastfm_listeners=40_000_000,
            listenbrainz_listens=0,
            lastfm_playcount_available=True,
        )
        assert d["lastfm_score"] > LISTENER_ONLY_LASTFM_CEILING


class TestTheAlbumRelativeLeadIsCappedByTheAbsoluteCurve:
    def test_a_small_albums_lead_does_not_reach_100(self) -> None:
        d = calculate_combined_popularity_score(
            lastfm_listeners=16_000,
            listenbrainz_listens=0,
            album_lf_listeners=[16_000, 9_000, 6_000, 4_000],
        )
        assert d["lastfm_score"] < 100.0
        assert d["combined_score"] < 100.0


class TestLiveTitleEvidenceIsNarrow:
    def test_live_titles_are_recognised(self) -> None:
        from services.popularity.stages.track_stage import _is_live_title

        assert _is_live_title("Blind (live)")
        assert _is_live_title("Cocaine (Live)")
        assert _is_live_title("About A Girl - Live")

    def test_bare_live_words_are_not_live_versions(self) -> None:
        from services.popularity.stages.track_stage import _is_live_title

        for title in (
            "Stayin' Alive",
            "Eat You Alive",
            "Live While You Can",
            "Live and Let Die",
        ):
            assert not _is_live_title(title), title


class TestTheRecordingLookupPublishesLive:
    def test_a_live_release_group_marks_the_recording_live(self) -> None:
        from services.enrichment.musicbrainz_service import MusicBrainzService

        svc = MusicBrainzService(enabled=True)
        meta = svc._recording_to_metadata(
            {
                "id": "rec-live",
                "title": "Blind",
                "artist-credit": [
                    {"artist": {"id": "a-korn", "name": "Korn"}}
                ],
                "releases": [
                    {
                        "id": "rel-live",
                        "title": "Live & Rare",
                        "date": "2006",
                        "release-group": {
                            "id": "rg-live",
                            "title": "Live & Rare",
                            "secondary-types": ["Live"],
                        },
                    }
                ],
                "relations": [],
                "genres": [],
            },
            "rec-live",
            0.9,
        )

        assert meta["is_live"] is True

    def test_a_studio_release_group_is_not_live(self) -> None:
        from services.enrichment.musicbrainz_service import MusicBrainzService

        svc = MusicBrainzService(enabled=True)
        meta = svc._recording_to_metadata(
            {
                "id": "rec-studio",
                "title": "Blind",
                "artist-credit": [
                    {"artist": {"id": "a-korn", "name": "Korn"}}
                ],
                "releases": [
                    {
                        "id": "rel-studio",
                        "title": "Korn",
                        "date": "1994",
                        "release-group": {
                            "id": "rg-studio",
                            "title": "Korn",
                            "secondary-types": [],
                        },
                    }
                ],
                "relations": [],
                "genres": [],
            },
            "rec-studio",
            0.9,
        )

        assert meta["is_live"] is False
