"""Edition/mastering words are never Final Genres.

The audit found "Remaster" stored as a genre and read by the playlist surface.
These attributes describe the FILE, not the music: they stay intercepted from
the voting pool and are dropped from the final list. Cover is the deliberate
exception (it drives the cover playlists).
"""

from __future__ import annotations

import pytest

from services.enrichment.genre_aggregation_service import _append_extra_genres


class TestVersionOnlyMarkersAreDropped:
    @pytest.mark.parametrize(
        ("title", "banned"),
        [
            ("Song (Remastered 2011)", "Remaster"),
            ("Song (Remaster)", "Remaster"),
            ("Song (Deluxe Edition)", "Deluxe Edition"),
            ("Song (Mono)", "Mono"),
            ("Song (Radio Edit)", "Radio Edit"),
            ("Song (Demo)", "Demo"),
        ],
    )
    def test_a_version_word_never_reaches_the_final_list(self, title, banned):
        genres = _append_extra_genres(["Rock"], title, "Album")
        assert banned not in genres
        assert "Rock" in genres

    def test_intercepted_edition_tags_are_not_appended(self):
        genres = _append_extra_genres(
            ["Rock"],
            "Song",
            "Album",
            {"Mono", "Stereo", "Deluxe Edition", "Radio Edit", "Instrumental"},
        )
        assert genres == ["Rock"]

    def test_cover_is_still_a_marker(self):
        genres = _append_extra_genres(["Rock"], "Song (Metallica Cover)", "Album")
        assert "Cover" in genres

    def test_remaster_from_the_sources_is_dropped_too(self):
        genres = _append_extra_genres(
            ["Rock"], "Song", "Album", {"Remaster", "Remastered"}
        )
        assert genres == ["Rock"]


class TestDisplayCasing:
    @pytest.mark.parametrize(
        ("raw", "canonical"),
        [
            ("alternative rock", "Alternative Rock"),
            ("melodic death metal", "Melodic Death Metal"),
            ("experimental", "Experimental"),
        ],
    )
    def test_the_vocabulary_display_is_used(self, raw, canonical):
        assert _append_extra_genres([raw], "Song", "Album") == [canonical]
