"""Search quality: artist aliases, combined credits, edition text, and years.

Reported, four days of log:

* "Three Pitch Shifter files were downloaded, then deleted and the peer blocked for
  'artist mismatch'. The peer's folder is Pitchshifter/Pitch Shifter, so these
  were probably the right files."
* "BTOB, iKON, SF9, THE BOYZ, Stray Kids & ATEEZ: the best result was the correct
  BTOB file, rejected for no_artist_evidence."
* "(20th Anniversary Brexit Edition), (acoustic Alt. version) and -Japanese
  ver.- are all searched as written."
* "Year mismatch caused 670 rejections."
"""

from __future__ import annotations

import pytest

from helpers.normalization_service import (
    artist_name_key,
    normalize_artist,
    primary_artist_credit,
    split_artist_credit,
)


class TestArtistNamesIgnoreSpacing:
    """``Pitch Shifter`` and ``Pitchshifter`` are one act."""

    @pytest.mark.parametrize(
        "variant",
        [
            "Pitchshifter",
            "pitch shifter",
            "PITCHSHIFTER",
            "Pitch Shifter",
            "Pitchshifter feat. Burton C. Bell",
        ],
    )
    def test_every_spelling_shares_one_key(self, variant: str) -> None:
        assert artist_name_key(variant) == artist_name_key("Pitch Shifter")

    def test_normalize_artist_still_keeps_the_space(self) -> None:
        """CONTROL — this helper is used for STORAGE; it must not change."""
        assert normalize_artist("Pitch Shifter") == "pitch shifter"
        assert normalize_artist("Pitchshifter") == "pitchshifter"
        assert normalize_artist("Pitch Shifter") != normalize_artist("Pitchshifter")

    def test_diacritics_and_punctuation_still_fold(self) -> None:
        assert artist_name_key("Motörhead") == artist_name_key("Motorhead")
        assert artist_name_key("AC/DC") == artist_name_key("ACDC")


class TestCombinedCreditsSplit:
    """A combined credit is several artists; a file names ONE of them."""

    def test_the_reported_btob_credit(self) -> None:
        credit = "BTOB, iKON, SF9, THE BOYZ, Stray Kids & ATEEZ"
        assert split_artist_credit(credit) == [
            "BTOB", "iKON", "SF9", "THE BOYZ", "Stray Kids", "ATEEZ",
        ]
        assert primary_artist_credit(credit) == "BTOB"

    def test_a_guest_credit_splits_on_its_marker(self) -> None:
        assert split_artist_credit("Powerwolf feat. X") == ["Powerwolf", "X"]
        assert primary_artist_credit("Orpheus Omega / Triple Kill") == "Orpheus Omega"

    def test_a_single_artist_is_unchanged(self) -> None:
        assert split_artist_credit("Orville Peck") == ["Orville Peck"]
        assert primary_artist_credit("Orville Peck") == "Orville Peck"

    def test_empty_input(self) -> None:
        assert split_artist_credit("") == []
        assert primary_artist_credit("") == ""


class TestTheSearchQueryUsesThePrimaryArtist:
    def test_a_combined_credit_searches_as_its_first_artist(self) -> None:
        from services.downloads.download_pipeline_service import build_search_query

        query = build_search_query({
            "artist": "BTOB, iKON, SF9, THE BOYZ, Stray Kids & ATEEZ",
            "title": "Missing You",
        })
        assert query.startswith("BTOB"), query
        assert "iKON" not in query


class TestEditionTextIsNotSearched:
    """Search the bare title; the edition only disambiguates."""

    @pytest.mark.parametrize(
        "title",
        [
            "Song (20th Anniversary Brexit Edition)",
            "Song (Deluxe Edition)",
            "Song -Japanese ver.-",
        ],
    )
    def test_the_primary_query_is_the_bare_title(self, title: str) -> None:
        from services.downloads.download_pipeline_service import build_search_query

        query = build_search_query({"artist": "Artist", "title": title})
        assert query == "Artist - Song", query

    def test_a_track_variant_is_not_stripped(self) -> None:
        """CONTROL — "(acoustic ...)" is a VERSION of the recording, not an
        edition of the release. ``strip_album_edition_marker`` deliberately keeps
        it; the scorer's own variant logic handles it."""
        from services.downloads.download_pipeline_service import build_search_query

        query = build_search_query({"artist": "Artist", "title": "Song (acoustic)"})
        assert "acoustic" in query.casefold()

    def test_the_annotated_form_survives_as_a_fallback(self) -> None:
        """It must still be available to disambiguate between results."""
        from services.downloads.download_pipeline_service import (
            _build_fallback_search_queries,
            build_search_query,
        )

        item = {"artist": "Artist", "title": "Song (Deluxe Edition)"}
        primary = build_search_query(item)
        fallbacks = _build_fallback_search_queries(item, primary)
        assert any("Deluxe" in q for q in fallbacks), fallbacks


class TestYearIsAPenaltyNotARejection:
    def test_it_costs_points_but_keeps_the_candidate(self) -> None:
        from services.downloads.download_pipeline_service import (
            YEAR_MISMATCH_PENALTY,
            _score_result,
        )

        good = _score_result(
            {"filename": "Artist - Album (2003)/01. Song.mp3", "bitrate": 320},
            "Artist", "Song", expected_year="2003",
        )
        bad = _score_result(
            {"filename": "Artist - Album (2017)/01. Song.mp3", "bitrate": 320},
            "Artist", "Song", expected_year="2003",
        )

        assert bad > 0, "the reported cases had artist AND title matching"
        assert bad == pytest.approx(good - YEAR_MISMATCH_PENALTY)
        assert bad >= 45.0, "still above min_score, so it is selectable"

    def test_a_year_within_one_is_not_a_disagreement(self) -> None:
        from services.downloads.download_pipeline_service import _year_mismatch_penalty

        assert _year_mismatch_penalty("Artist - Album (2004)/x.mp3", "2003") == 0.0
        assert _year_mismatch_penalty("Artist - Album (2005)/x.mp3", "2003") > 0.0


class TestAReleaseIsNotQueuedAsATrack:
    def test_a_title_that_is_its_album_searches_the_release(self) -> None:
        from services.downloads.download_pipeline_service import build_search_query

        item = {"artist": "Turmion Kätilöt", "title": "Resodance", "album": "Resodance"}
        query = build_search_query(item)
        # It is a release, not a track: "Artist - Album" is the honest query.
        assert query == "Turmion Kätilöt - Resodance"

    def test_a_real_track_is_untouched(self) -> None:
        from services.downloads.download_pipeline_service import build_search_query

        item = {"artist": "Artist", "title": "Song", "album": "Album", "duration": 200}
        assert build_search_query(item) == "Artist - Song"
