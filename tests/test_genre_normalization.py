"""One canonical genre splitter, and the delimiters it must get right.

The collection's export held 834 raw genre strings that reduced to 625
case-insensitively. The difference was noise, but the SHAPE of the noise matters
here: values mixed delimiter families (``,`` from our own columns, ``\\`` from
Navidrome/ID3v2.3, ``|`` from providers), and a splitter that handled only some
of them produced a single compound genre name — and therefore a compound
playlist.

The other half of the problem is the opposite failure: a blind split destroys
genres that legitimately contain the separator. ``death/doom``, ``drum & bass``
and ``r&b`` are ONE genre each.
"""

from __future__ import annotations

import pytest

from services.enrichment.genre_normalization import (
    canonical_spelling,
    clean_genre_values,
    is_noise_genre,
    split_genre_values,
)


class TestEveryDelimiterFamilySplits:
    @pytest.mark.parametrize(
        "value, expected",
        [
            # A comma-joined list from our own columns.
            ("Rock, Metal, Punk", ["Rock", "Metal", "Punk"]),
            # A semicolon-joined list.
            ("Rock; Metal", ["Rock", "Metal"]),
            # ⭐ NAVidrome / ID3v2.3 join with a backslash.
            ("metal\\nu metal\\rock", ["metal", "nu metal", "rock"]),
            # ⭐ A pipe-delimited provider value.
            ("Gothic Metal | Symphonic Metal | Power Metal",
             ["Gothic Metal", "Symphonic Metal", "Power Metal"]),
            # ⭐ A value mixing families — the case a first-match splitter missed.
            ("Rock, Metal; Punk", ["Rock", "Metal", "Punk"]),
            ("Gothic Metal | Rock, Punk", ["Gothic Metal", "Rock", "Punk"]),
        ],
    )
    def test_it_splits(self, value: str, expected: list[str]) -> None:
        assert split_genre_values(value) == expected

    def test_a_json_array_is_flattened_not_re_split(self) -> None:
        assert split_genre_values('["Punk", "Cover", "Rock"]') == [
            "Punk",
            "Cover",
            "Rock",
        ]


class TestCompoundGenresSurvive:
    """The unsafe separators must not tear a real genre apart."""

    @pytest.mark.parametrize(
        "value",
        ["Death/Doom", "Drum & Bass", "R&B", "Rock & Roll"],
    )
    def test_a_known_compound_is_one_token(self, value: str) -> None:
        assert split_genre_values(value) == [value]

    def test_a_compound_inside_a_list_keeps_its_name(self) -> None:
        assert split_genre_values("Drum & Bass, Rock") == ["Drum & Bass", "Rock"]
        assert split_genre_values("Death/Doom, Rock") == ["Death/Doom", "Rock"]

    def test_an_unknown_composite_is_still_split(self) -> None:
        """Nothing recognised it as a whole, so the separators ARE separators."""
        assert split_genre_values("Rock/Metal") == ["Rock", "Metal"]


class TestNoiseIsNotAGenre:
    @pytest.mark.parametrize(
        "value",
        [
            "Offizielle Charts",
            "1-4 Wochen",
            "5+ Wochen",
            "Laut.De",
            "Ph_Temp_Checken",
            "Fixt Music",
            "70'S",
            "80's",
            "Deutsch",
            "German",
            "Australian",
            "Norway",
        ],
    )
    def test_it_is_rejected(self, value: str) -> None:
        assert is_noise_genre(value) is True
        assert clean_genre_values(value) == []

    def test_a_real_genre_survives(self) -> None:
        assert is_noise_genre("Gothic Metal") is False
        assert clean_genre_values("Gothic Metal, Rock") == ["Gothic Metal", "Rock"]


class TestCanonicalSpelling:
    @pytest.mark.parametrize(
        "value, expected",
        [
            ("Brit Pop", "Britpop"),
            ("Pop-Punk", "Pop Punk"),
            ("Hip Hop", "Hip-Hop"),
            ("Alt Rock", "Alternative Rock"),
            ("Pîwer Metal", "Power Metal"),
        ],
    )
    def test_it_is_corrected(self, value: str, expected: str) -> None:
        assert canonical_spelling(value) == expected

    def test_an_unmapped_value_keeps_its_words(self) -> None:
        assert canonical_spelling("  Gothic   Metal ") == "Gothic Metal"
