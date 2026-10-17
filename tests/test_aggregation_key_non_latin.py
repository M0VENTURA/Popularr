"""``normalize_for_aggregation`` must not collapse non-Latin titles to ONE key.

Reported
--------
Arkona — "Во славу Великим!": all 14 tracks read the IDENTICAL ListenBrainz
count (109,751) and the album's distribution flattened to nothing.

Root cause
----------
The closing sweep ``re.sub(r"[^a-z0-9]+", " ", value)`` deleted EVERY non-Latin
character, so each Cyrillic (or CJK) title normalised to the EMPTY string.
Every such title on one album then shared that single ``""`` key in the
prefetched popularity map (and in the title-keyed album-tracklist map), so one
provider count was written into the slot all of them read.

The fix
-------
``\\w`` is Unicode-aware, so letters/digits from any script survive the sweep.
``_`` is folded to a separator first, so its handling is unchanged, and the
ASCII/Latin output is byte-identical to before (pinned below).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest


def _norm(title: str) -> str:
    from services.popularity.popularity_matching import normalize_for_aggregation

    return normalize_for_aggregation(title)


class TestNonLatinTitlesKeepDistinctKeys:
    def test_a_cyrillic_title_is_not_empty(self):
        """The single fact that caused the report: this used to be ``""``."""
        assert _norm("Во славу Великим!") != ""

    def test_distinct_cyrillic_titles_do_not_collide(self):
        titles = ["Во славу Великим!", "Другая песня", "Третья вещь", "Четвёртая"]
        keys = [_norm(t) for t in titles]

        assert all(keys)
        assert len(set(keys)) == len(titles)

    def test_the_arkona_shape_yields_fourteen_distinct_keys(self):
        """The PROPERTY that stops the shared prefetch slot: N distinct titles
        give N distinct keys. Before the fix all fourteen were ``""``."""
        keys = {_norm(f"Песня {i}") for i in range(1, 15)}

        assert len(keys) == 14

    def test_cjk_is_preserved(self):
        assert _norm("彼女の歌") == "彼女の歌"

    def test_punctuation_still_separates_non_latin_words(self):
        assert _norm("Во славу, Великим!") == "во славу великим"


class TestAsciiAndLatinOutputIsUnchanged:
    """CONTROL — the ASCII/Latin contract must be byte-identical to before."""

    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Slavic Song", "slavic song"),
            # diacritics folded, NOT split into a stray word
            ("Café", "cafe"),
            ("Hoppípolla", "hoppipolla"),
            # space-delimited dashes stay separators; underscores separate
            ("Foo - Bar", "foo bar"),
            ("Track_One", "track one"),
            # cover attribution + feat./remaster suffixes stripped
            ("Gangnam Style (PSY Cover)", "gangnam style"),
            ("Song feat. X", "song"),
            ("Song (Remastered)", "song"),
        ],
    )
    def test_the_shape_is_pinned(self, title, expected):
        assert _norm(title) == expected
