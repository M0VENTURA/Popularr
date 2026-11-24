"""A separated value is a LIST, not one genre name.

REPORTED VIA THE v5 VOCABULARY
------------------------------
Adding ``"rock pop"`` as an alias of ``pop_rock`` made "Rock, Pop" classify as
Pop Rock — one genre — instead of splitting into Rock and Pop.

WHY IT HAPPENED
---------------
``normalize_token`` strips ALL punctuation, so "Rock, Pop" keys to ``rockpop``:
exactly the key "Rock Pop" produces. ``tokenize_values`` tried the WHOLE value
first (correctly, so ``death/doom`` and ``r&b`` are not shredded), found a match,
and never split.

The same mechanism applied to every separator. Measured against the shipped
vocabularies, 21 comma-joined pairs collapsed into a single genre on the
224-entry file and 44 on the 318-entry one — a bigger taxonomy has more
"<word> <word>" aliases, so the new file would have made it twice as bad.

THE RULE
--------
Split on the separator hierarchy, then ask whether EVERY part resolves. If they
all do, the value was a list. If they do not, it is a name that happens to
contain a separator, and it is kept whole.

    "Rock, Pop"      -> ["Rock", "Pop"]      both resolve  -> a list
    "Rock/Pop"       -> ["Rock", "Pop"]      both resolve  -> a list
    "death/doom"     -> kept whole           "death" is not a genre
    "r&b"            -> kept whole           "r" is not a genre
    "Rock"           -> kept whole           one part
"""

from __future__ import annotations

import pytest

from services.enrichment.genre_vocabulary import (
    clear_vocabulary_cache,
    get_vocabulary,
    tokenize_values,
)


@pytest.fixture(scope="module")
def vocab():
    clear_vocabulary_cache()
    return get_vocabulary()


class TestASeparatedListSplits:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("Rock, Pop", ["Rock", "Pop"]),
            ("Pop, Rock", ["Pop", "Rock"]),
            ("Folk, Punk", ["Folk", "Punk"]),
            ("Rock; Pop", ["Rock", "Pop"]),
            ("Rock | Pop", ["Rock", "Pop"]),
            ("Hip Hop, Rap", ["Hip Hop", "Rap"]),
            ("Rock, Pop, Folk", ["Rock", "Pop", "Folk"]),
            # A SOFT joiner splits only when the whole value does not resolve.
            ("Funk / Soul", ["Funk", "Soul"]),
        ],
    )
    def test_it_splits_every_separator(self, vocab, value, expected):
        assert tokenize_values(value, vocab) == expected, (
            f"{value!r} was treated as one value; a genre list must split or "
            "every genre in it is lost"
        )

    def test_a_soft_joiner_that_names_a_genre_stays_whole(self, vocab):
        """⚠️ "Rock / Pop" IS Pop Rock — the rule the report specifies.

        ``/`` and ``&`` separate genres in a list but also occur inside real
        names, so the whole value is tried FIRST. Splitting "Rock / Pop" into
        Rock + Pop would discard the actual reading.
        """
        assert tokenize_values("Rock / Pop", vocab) == ["Rock / Pop"]
        assert tokenize_values("Rock/Pop", vocab) == ["Rock/Pop"]
        assert vocab.classify("Rock/Pop").canonical == "pop_rock"

    def test_the_reported_case_is_not_one_genre(self, vocab):
        """The exact regression: "Rock, Pop" must never become Pop Rock."""
        assert tokenize_values("Rock, Pop", vocab) != ["Rock, Pop"]

    def test_a_serialized_array_still_splits_on_commas(self, vocab):
        assert tokenize_values('["Rock", "Pop"]', vocab) == ["Rock", "Pop"]


class TestARealNameStaysWhole:
    @pytest.mark.parametrize(
        "value",
        ["death/doom", "r&b", "Pop Rock", "rock pop", "alt. metal", "alt-metal",
         "nü metal", "Static-X", "Rock", "Hardcore Punk", "Metalcore"],
    )
    def test_it_is_not_shredded(self, vocab, value):
        assert tokenize_values(value, vocab) == [value], (
            f"{value!r} is a single name and must survive tokenisation intact"
        )

    def test_the_parts_must_ALL_resolve(self, vocab):
        """The discriminator: "death" is not a genre, so death/doom is one name."""
        from services.enrichment.genre_vocabulary import (
            KIND_UNKNOWN, _all_parts_are_known, _split_on_separators,
        )

        assert _all_parts_are_known(["Rock", "Pop"], vocab) is True
        assert _all_parts_are_known(["death", "doom"], vocab) is False
        assert _split_on_separators("death/doom") == ["death", "doom"]
        # ...and "doom" alone is genuinely a genre, so the failure is specifically
        # the unknown part, not a blanket rule.
        assert vocab.classify("doom").kind != KIND_UNKNOWN


class TestNoGenreNameContainsASeparatorThatWouldBeMisread:
    def test_no_genre_spelling_contains_a_comma(self, vocab):
        """⚠️ This is what makes splitting on a comma safe.

        If a future entry legitimately contained a comma, the tokeniser would
        shred it — so the assumption is asserted rather than left implicit.
        """
        offenders = []
        for gid, entry in vocab.genres.items():
            names = [entry.display, gid.replace("_", " "), *entry.aliases]
            offenders += [(gid, n) for n in names if "," in n]
        assert not offenders, (
            f"a genre name contains a comma, which the tokeniser splits on: {offenders}"
        )
