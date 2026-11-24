"""A separated genre value is a LIST, not one genre name.

REPORTED
--------
Adding an alias made "Rock, Pop" classify as Pop Rock — ONE genre — instead of
splitting into Rock and Pop. The problem was far wider than one alias:

    Pop, Metal   -> Glam Metal (via the "pop metal" alias)   <- the worst case
    Punk, Rock   -> Punk (the Rock reading was lost)
    Folk, Rock   -> Folk Rock       Industrial, Metal -> Industrial Metal
    Alternative, Rock -> Alternative Rock    Emo, Pop -> Emo Pop

ROOT CAUSE — TWO SEPARATE FAULTS
--------------------------------
1. ``normalize_token`` DELETED all punctuation, so "Rock, Pop" produced the same
   key (``rockpop``) as "Rock Pop". A list therefore matched a single name.
   ⚠️ The alias was incidental: v4's ``pop_rock`` had only ``["pop-rock"]``, yet
   its own DISPLAY name "Pop Rock" already collided with "Pop, Rock".

2. The tokenizer tried the WHOLE value before splitting, so that false key match
   prevented the split.

THE RULE
--------
hard delimiters (``;`` ``,`` ``|``) split FIRST, before any whole-value match.
Protect the few real names that contain one. Then soft joiners (``/`` ``&`` ``+``)
match the whole value first, so ``death/doom`` and ``r&b`` stay intact while
``Funk / Soul`` splits.
"""

from __future__ import annotations

import pytest

from services.enrichment.genre_vocabulary import (
    clear_vocabulary_cache,
    get_vocabulary,
    normalize_token,
    tokenize_values,
)


@pytest.fixture(scope="module")
def vocab():
    clear_vocabulary_cache()
    return get_vocabulary()


class TestTheReportedRegressionTable:
    """The exact table from the report, one case per row."""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Rock, Pop", ["Rock", "Pop"]),
            ("Pop Rock", ["Pop Rock"]),
            ("Pop, Metal", ["Pop", "Metal"]),
            ("Punk, Rock", ["Punk", "Rock"]),
            ("Folk, World, & Country", ["Folk, World, & Country"]),
            ("death/doom", ["death/doom"]),
            ("R&B", ["R&B"]),
            ("Drum & Bass", ["Drum & Bass"]),
            ("Funk / Soul", ["Funk", "Soul"]),
            ("Hip-Hop; Rap", ["Hip-Hop", "Rap"]),
        ],
    )
    def test_it_matches(self, vocab, text, expected):
        assert tokenize_values(text, vocab) == expected


class TestTheWorstCase:
    def test_pop_metal_does_not_become_glam_metal(self, vocab):
        """The report called this the worst case: an alias made a two-genre list
        resolve to a third, unrelated genre."""
        tokens = tokenize_values("Pop, Metal", vocab)
        assert tokens == ["Pop", "Metal"]
        for token in tokens:
            canonical = vocab.classify(token).canonical
            assert canonical != "glam_metal", (
                f"{token!r} resolved to Glam Metal via the 'pop metal' alias"
            )

    def test_punk_rock_keeps_both_readings(self, vocab):
        """It used to collapse to Punk, silently losing Rock."""
        assert tokenize_values("Punk, Rock", vocab) == ["Punk", "Rock"]


class TestPunctuationIsNotDeleted:
    def test_a_separator_never_vanishes_entirely(self, vocab):
        """⭐ THE ROOT CAUSE, stated precisely.

        ``'rock,pop'`` (a separator) must NOT share a key with ``'rockpop'`` (no
        separator at all). Deleting punctuation made a SEPARATOR and a JOINER the
        same character, which is what let a two-genre list match a single name.
        """
        assert normalize_token("rock,pop") != normalize_token("rockpop")

    def test_a_hyphen_still_folds(self, vocab):
        """A hyphen is a spelling difference, not a list."""
        assert normalize_token("hip-hop") == normalize_token("hip hop")

    def test_comma_space_and_bare_space_are_still_ambiguous(self, vocab):
        """⚠️ HONEST LIMITATION, asserted so nobody assumes otherwise.

        ``"Rock, Pop"`` and ``"Rock Pop"`` both normalise to ``'rock  pop'`` ->
        ``'rock pop'``. That is unavoidable: with punctuation folded to a space,
        "comma + space" and "space" are the same sequence.

        ⚠️ SO THE KEY ALONE CANNOT PREVENT THE COLLAPSE — THE SPLIT ORDER DOES.
        ``tokenize_values`` splits hard delimiters BEFORE any whole-value match,
        so a comma-separated value never reaches ``classify`` as one string. What
        makes that safe is that no genre name contains a comma, which
        ``test_no_two_genres_share_a_normalised_spelling`` and the exhaustive pair
        test both pin.
        """
        assert normalize_token("Rock, Pop") == normalize_token("Rock Pop")
        # ...and this is why the ordering, not the key, is the fix:
        assert tokenize_values("Rock, Pop", vocab) == ["Rock", "Pop"]
        assert tokenize_values("Rock Pop", vocab) == ["Rock Pop"]


class TestTheExhaustivePairTest:
    """⭐ THE GUARD AGAINST FUTURE ALIASES, as the report asked for.

    For EVERY pair of single-word genre names A and B, "A, B" must yield two
    tokens. A new alias that happens to spell a compound of two genre words would
    otherwise start swallowing lists again, silently.
    """

    def test_every_single_word_pair_splits(self, vocab):
        single = sorted({
            entry.display.strip()
            for entry in vocab.genres.values()
            if " " not in entry.display.strip()
        })
        assert len(single) > 50, (
            f"only {len(single)} single-word names found; the vocabulary did not load"
        )

        failures = []
        for first in single:
            for second in single:
                if first == second:
                    continue
                text = f"{first}, {second}"
                tokens = tokenize_values(text, vocab)
                if len(tokens) != 2:
                    failures.append((text, tokens))

        assert not failures, (
            f"{len(failures)} of {len(single) * (len(single) - 1)} pairs failed to "
            f"split; a new alias is probably spelling a compound genre. First few: "
            f"{failures[:8]}"
        )


class TestProtectedValues:
    def test_the_list_is_data_not_code(self, vocab):
        """It must be readable from the vocabulary so it can be edited."""
        assert isinstance(vocab.tokenizer, dict)
        assert "Folk, World, & Country" in (
            vocab.tokenizer.get("protected_values") or []
        )

    def test_a_protected_value_is_never_split(self, vocab):
        assert tokenize_values("Folk, World, & Country", vocab) == [
            "Folk, World, & Country"
        ]

    def test_a_bare_protected_word_still_splits_normally(self, vocab):
        """CONTROL — protecting the phrase must not protect its parts."""
        assert tokenize_values("Folk, Rock", vocab) == ["Folk", "Rock"]


class TestSoftJoiners:
    def test_a_real_name_survives(self, vocab):
        for name in ("death/doom", "R&B", "Drum & Bass"):
            assert tokenize_values(name, vocab) == [name], name

    def test_a_list_still_splits(self, vocab):
        assert tokenize_values("Funk / Soul", vocab) == ["Funk", "Soul"]

    def test_the_entries_the_names_expect_exist(self, vocab):
        """A name kept whole is only useful if it resolves."""
        assert vocab.classify("death/doom").canonical == "death_doom"
        assert vocab.classify("R&B").canonical == "r_and_b"
        assert vocab.classify("Drum & Bass").canonical == "drum_and_bass"
        assert vocab.classify("Rock Opera").canonical == "rock_opera"


class TestNoAliasCollisions:
    def test_no_two_genres_share_a_normalised_spelling(self, vocab):
        """⚠️ THE OTHER HALF OF THE ROOT CAUSE.

        With punctuation folded to a space this is now checkable: two genres must
        never claim the same key, or one silently shadows the other.
        """
        owner: dict[str, str] = {}
        clashes = []
        for gid, entry in vocab.genres.items():
            for spelling in (entry.display, gid.replace("_", " "), *entry.aliases):
                key = normalize_token(spelling)
                if not key:
                    continue
                if key in owner and owner[key] != gid:
                    clashes.append((key, owner[key], gid))
                owner.setdefault(key, gid)
        assert not clashes, f"genres sharing a lookup key: {clashes[:10]}"
