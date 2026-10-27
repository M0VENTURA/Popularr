"""An ordinary word at the end of a title must never become an edition marker.

Reported
--------
> Tracks that have "us" in the track title keep renaming to "(us)".

Root cause
----------
``_split_trailing_edition_marker`` walks BACKWARDS from the last word of a name
while each word is an edition-marker, glue, or a number, then brackets the run.
The vocabulary it accepted included ordinary English words — ``us``, ``uk``,
``clean``, ``special``, ``super``, ``tracks``, ``track``, ``masters``,
``apple``, ``european`` — because they also appear in real markers ("US
Version", "Japanese Edition", "Clean Version").

Any ONE of them was accepted as sufficient, so every real title ENDING in one
was rewritten::

    "One of Us"                     ->  "One (of Us)"
    "Abandon Us"                    ->  "Abandon (Us)"
    "Anarchy In The UK"             ->  "Anarchy In (The UK)"
    "Coming Clean"                  ->  "Coming (Clean)"
    "Following My Own Tracks"       ->  "Following My Own (Tracks)"
    "Secret Track"                  ->  "Secret (Track)"

The rename is not cosmetic: ``repair_annotations()`` writes its result back to
``tracks.title`` and the same album pass pushes it into the audio file tags, so
the library is permanently renamed. The rewrite is also IDEMPOTENT, so a name
that is already ``"One (of Us)"`` looks stable and is never revisited — which is
why it "keeps" happening rather than correcting itself.

The fix
-------
The vocabulary is split into words that can ESTABLISH a marker and words that
may only CONFIRM one. A run is a marker only if it contains a strong word, so
"Song US Version" still brackets while "One of Us" is left alone. The bogus
brackets already written are undone by
``unbracket_ambiguous_edition_marker()``, restricted to words that are never a
legitimate standalone annotation — ``(Clean)``, ``(Explicit)``, ``(Bonus)`` and
``(Deluxe)`` mean something real and stay bracketed.
"""

from __future__ import annotations

import pytest

from helpers.normalization_service import (
    bracket_trailing_edition_marker,
    normalize_title_for_lastfm,
    repair_annotations,
    strip_album_edition_marker,
    unbracket_ambiguous_edition_marker,
)

#: Real titles from the reported collection that the old sweep corrupted.
CORRUPTED = [
    "One of Us",
    "Abandon Us",
    "Among Us",
    "Feed Us",
    "God Save Us",
    "Bigger Than Us",
    "The Distance Between Us",
    "They Don't Really Care About Us",
    "What Happened to Us?",
    "Thieves Like Us",
    "Anarchy In The UK",
    "Coming Clean",
    "Following My Own Tracks",
    "Secret Track",
    "Hidden Track",
    "The Hidden Masters",
    "No Gods No Masters",
    "Toffee Apple",
    "Poison Apple",
    "She's So European",
    "I'm Super",
    "They said I was special",
]


# ===========================================================================
# 1. THE REPORTED BUG
# ===========================================================================

class TestOrdinaryTitleWordsAreNotMarkers:
    """Every one of these was rewritten into a parenthetical before the fix."""

    @pytest.mark.parametrize("title", CORRUPTED)
    def test_the_title_is_left_alone(self, title: str) -> None:
        assert bracket_trailing_edition_marker(title) == title, (
            f"{title!r} was rewritten — it ends in an ordinary word, not an "
            "edition marker"
        )

    @pytest.mark.parametrize("title", CORRUPTED)
    def test_the_stored_title_repair_does_not_touch_it(self, title: str) -> None:
        """This is the path that WRITES to tracks.title and the file tags."""
        assert repair_annotations(title) == title

    @pytest.mark.parametrize("title", CORRUPTED)
    def test_the_lastfm_key_keeps_the_word(self, title: str) -> None:
        """Losing the final word also broke the Last.fm lookup key."""
        key = normalize_title_for_lastfm(title)
        assert "(" not in key and ")" not in key

    def test_the_reported_word_is_the_regression(self) -> None:
        assert bracket_trailing_edition_marker("God Is With Us") == "God Is With Us"
        assert repair_annotations("God Is With Us") == "God Is With Us"


# ===========================================================================
# 2. A real marker still brackets
# ===========================================================================

class TestRealMarkersStillWork:
    """Narrowing the vocabulary must not disable the feature it belongs to."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            # The original reported bug — unchanged.
            ("The Mirror's Truth Version", "The Mirror's Truth (Version)"),
            ("American Idiot Deluxe Edition", "American Idiot (Deluxe Edition)"),
            ("Some Album Remastered", "Some Album (Remastered)"),
            # A CONFIRM-ONLY word is still part of a marker when a strong word
            # is present — the whole reason it stays in the sweep.
            ("Song US Version", "Song (US Version)"),
            ("Song UK Edition", "Song (UK Edition)"),
            ("Song Japanese Edition", "Song (Japanese Edition)"),
            ("Song European Version", "Song (European Version)"),
        ],
    )
    def test_marker_is_bracketed(self, raw: str, expected: str) -> None:
        assert bracket_trailing_edition_marker(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "Song US",
            "Song UK",
            "Song Japanese",
            "Song Clean",
            "Song Special",
        ],
    )
    def test_a_bare_region_or_ambiguous_word_is_not_enough(self, raw: str) -> None:
        assert bracket_trailing_edition_marker(raw) == raw


# ===========================================================================
# 3. Healing rows that were ALREADY corrupted
# ===========================================================================

class TestPriorCorruptionHeals:
    """Idempotency is why the damaged names never corrected themselves."""

    @pytest.mark.parametrize(
        "damaged,repaired",
        [
            ("One (of Us)", "One of Us"),
            ("Abandon (Us)", "Abandon Us"),
            ("Anarchy In (The UK)", "Anarchy In The UK"),
            ("Secret (Track)", "Secret Track"),
            ("Following My Own (Tracks)", "Following My Own Tracks"),
            ("Poison (Apple)", "Poison Apple"),
            ("No Gods (No Masters)", "No Gods No Masters"),
            ("They said I was (special)", "They said I was special"),
            ("What About (Us?)", "What About Us?"),
            ("Anarchy in (the U.K.)", "Anarchy in the U.K."),
        ],
    )
    def test_the_bracket_is_undone(self, damaged: str, repaired: str) -> None:
        assert unbracket_ambiguous_edition_marker(damaged) == repaired
        assert repair_annotations(damaged) == repaired

    @pytest.mark.parametrize(
        "damaged,repaired",
        [("One (of Us)", "One of Us"), ("Abandon (Us)", "Abandon Us")],
    )
    def test_healing_is_idempotent(self, damaged: str, repaired: str) -> None:
        assert repair_annotations(repaired) == repaired

    def test_a_healed_title_matches_the_never_damaged_one(self) -> None:
        """Same song, one row damaged — they must produce ONE key again."""
        assert normalize_title_for_lastfm("One (of Us)") == (
            normalize_title_for_lastfm("One of Us")
        )


# ===========================================================================
# 4. The inverse must not eat a REAL annotation
# ===========================================================================

class TestRealAnnotationsAreNotUnbracketed:
    """``(Clean)`` and ``(Explicit)`` label a different master of the track."""

    @pytest.mark.parametrize(
        "raw",
        [
            "Song (Clean)",
            "Song (Explicit)",
            "Song (Bonus)",
            "Song (Deluxe)",
            "Song (US Version)",
            "Song (Japanese Edition)",
            "Song (Remastered)",
            "Song (Live)",
            "Song (Acoustic)",
            "Song (Artist Cover)",
            "Song (feat. Someone)",
        ],
    )
    def test_the_bracket_survives(self, raw: str) -> None:
        assert unbracket_ambiguous_edition_marker(raw) == raw

    def test_a_form_annotation_survives(self) -> None:
        assert repair_annotations("Song (Live)") == "Song (Live)"

    def test_a_plain_bracket_is_never_unbracketed(self) -> None:
        for raw in ("(What's the Story) Morning Glory?", "Song (Reprise)"):
            assert unbracket_ambiguous_edition_marker(raw) == raw


# ===========================================================================
# 5. The lookup-key and strip paths agree with the repair
# ===========================================================================

class TestDownstreamPathsAgree:
    def test_strip_does_not_eat_the_title_word(self) -> None:
        """"Abandon Us" must not reduce to "Abandon" for a lookup key."""
        assert strip_album_edition_marker("Abandon Us") == "Abandon Us"
        assert strip_album_edition_marker("Abandon (Us)") == "Abandon Us"

    def test_the_album_strip_still_removes_a_real_marker(self) -> None:
        """A region word is stripped as part of a MARKER, not as a title word."""
        assert strip_album_edition_marker("Song Japanese Edition") == "Song"
        assert strip_album_edition_marker("Song Deluxe Edition") == "Song"

    def test_no_doubled_brackets(self) -> None:
        for raw in CORRUPTED + ["One (of Us)"]:
            assert "((" not in repair_annotations(raw)
