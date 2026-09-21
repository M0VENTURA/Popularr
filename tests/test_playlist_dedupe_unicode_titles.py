"""Duplicate playlist entries survived de-duplication (Korn — "Y'all Want a Single").

REPORT: "The playlist creation is removing duplicates, but I found one that got
past. Korn - Y'all Want a Single is showing twice on one playlist, but the
track times are 2 seconds different which is why it could be getting missed on
the deduplication."

The duration difference is real but is NOT what defeats the grouping — duration
is not part of the de-duplication key at all. Every playlist builder groups on

    (normalised artist, normalised title)

via ``_normalise_essential_title``, which lowercased and stripped bracketed
segments but did NOT fold Unicode punctuation. The library holds the recording
twice with different apostrophes:

    "Y'all Want a Single"   straight  U+0027
    "Y’all Want a Single"   curly     U+2019

Those produced two different keys, so both copies reached the winner list and
both were written to the playlist. The same hole exists on the artist half of
the key, and for dashes ("–"/"—") and NBSP, which fail identically.

Verified by oracle against ``origin/develop`` (ed68930b).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

STRAIGHT = "Y'all Want a Single"
CURLY = "Y\u2019all Want a Single"


class TestTitleKeyFoldsUnicodePunctuation:
    def test_the_two_apostrophe_forms_are_the_same_key(self):
        """The reported pair must collapse to one key."""
        from services.popularity.stages.finalise_stage import _normalise_essential_title

        assert _normalise_essential_title(STRAIGHT) == _normalise_essential_title(CURLY)

    def test_it_still_lowercases_and_strips_brackets(self):
        """The behaviour other callers (and tests) already rely on."""
        from services.popularity.stages.finalise_stage import _normalise_essential_title as n

        assert n("Walk (2018 Remaster)") == "walk"
        assert n("Play [Deluxe Edition]") == "play"
        assert n("Alive (Live)") == "alive"
        assert n("Foo (Version) Bar") == "foo bar"
        assert n("  The   Song  ") == "the song"
        assert n("") == ""
        assert n(None) == ""

    def test_other_punctuation_classes_are_folded_too(self):
        """Not just apostrophes — the whole classic class."""
        from services.popularity.stages.finalise_stage import _normalise_essential_title as n

        # En dash / em dash / em dash legacy
        assert n("Song \u2013 Live") == n("Song - Live")
        assert n("Song \u2014 Live") == n("Song - Live")
        # Curly double quotes and prime
        assert n('Song \u201cX\u201d') == n('Song "X"')
        # Non-breaking space
        assert n("Song\u00a0Two") == n("Song Two")

    def test_genuinely_different_titles_stay_different(self):
        """The key must not over-collapse and merge unrelated tracks."""
        from services.popularity.stages.finalise_stage import _normalise_essential_title as n

        assert n(STRAIGHT) != n("Y'all Want a Double")
        assert n("Song") != n("Song Two")
        # Bracketed *content* differs, but both strip to the bare title —
        # that is the deliberate existing behaviour for version markers.
        assert n("Alive (Live)") == n("Alive")


class TestArtistKeyFoldsUnicodePunctuation:
    def test_apostrophe_artist_forms_are_the_same_key(self):
        from services.popularity.stages.finalise_stage import _normalise_artist_key

        assert _normalise_artist_key("Guns N\u2019 Roses") == _normalise_artist_key("Guns N' Roses")

    def test_it_is_case_insensitive_and_trims(self):
        from services.popularity.stages.finalise_stage import _normalise_artist_key

        assert _normalise_artist_key("  Korn  ") == "korn"
        assert _normalise_artist_key(None) == ""

    def test_different_artists_stay_different(self):
        from services.popularity.stages.finalise_stage import _normalise_artist_key

        assert _normalise_artist_key("Korn") != _normalise_artist_key("Muse")


class TestThePlaylistBuildersUseTheFoldedKey:
    """Both halves of the key, in every builder that de-duplicates."""

    def test_no_builder_builds_a_raw_artist_key(self):
        """A raw ``.casefold()`` artist key is the un-folded form."""
        import inspect

        from services.popularity.stages import finalise_stage as fs

        for name in (
            "_create_genre_top_track_playlists",
            "_create_new_music_playlist",
        ):
            source = inspect.getsource(getattr(fs, name))
            assert 'str(row.get("artist") or "").strip().casefold()' not in source, (
                f"{name} must build its artist key through _normalise_artist_key"
            )
            assert 're.sub(r"\\s+", " ", t["artist"]).strip().casefold()' not in source, (
                f"{name} must build its artist key through _normalise_artist_key"
            )

    def test_the_genre_builder_groups_on_the_folded_key(self):
        import inspect

        from services.popularity.stages import finalise_stage as fs

        source = inspect.getsource(fs._create_genre_top_track_playlists)
        assert "_normalise_artist_key(" in source
        assert "_normalise_essential_title(" in source

    def test_the_new_music_builder_groups_on_the_folded_key(self):
        import inspect

        from services.popularity.stages import finalise_stage as fs

        source = inspect.getsource(fs._create_new_music_playlist)
        assert "_normalise_artist_key(" in source
        assert "_normalise_essential_title(" in source


class TestTheDuplicatePairProducesOneWinner:
    """End to end: two rows, one apostrophe each → ONE playlist entry."""

    def test_a_straight_and_curly_pair_collapse_to_one(self):
        from collections import defaultdict

        from services.popularity.stages.finalise_stage import (
            _normalise_artist_key,
            _normalise_essential_title,
        )

        rows = [
            {"artist": "Korn", "title": STRAIGHT, "score": 50.0, "stars": 4,
             "duration": 187, "is_live": 0, "is_compilation": 0},
            {"artist": "Korn", "title": CURLY, "score": 51.0, "stars": 4,
             "duration": 189, "is_live": 0, "is_compilation": 0},
        ]

        grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for r in rows:
            grouped[(_normalise_artist_key(r["artist"]),
                     _normalise_essential_title(r["title"]))].append(r)

        assert len(grouped) == 1, (
            f"the duplicate pair must group as ONE track, got {len(grouped)} groups"
        )
        assert len(next(iter(grouped.values()))) == 2, "both copies should be in the group"

    def test_the_two_second_difference_does_not_matter(self):
        """Duration is not part of the key, so it cannot be the cause."""
        from services.popularity.stages.finalise_stage import _normalise_essential_title

        # Identical titles, wildly different durations → still one key.
        assert _normalise_essential_title(STRAIGHT) == _normalise_essential_title(CURLY)
