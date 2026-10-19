"""A typographic variant is NOT a metadata change to approve.

REPORTED
--------
> Several tracks were deferred for manual approval because the local files use
> straight apostrophes while MusicBrainz proposes curly apostrophes. This
> affected Catatonia's "Baby, It's Cold Outside" and "You've Got a Lot to Answer
> For", as well as Carpathian's "End of the 1980's" and "Drop It Like It's Hot".

WHY IT HAPPENED
---------------
``metadata_proposal_service._norm()`` was case- and whitespace-insensitive only:

```python
return " ".join(_as_text(value).casefold().split())
```

So ``U+0027`` and ``U+2019`` produced DIFFERENT keys and the diff reported a
change. Note the four examples are all SINGLES — where the album title equals the
track title — so it is the **album-level** comparison
(``_norm(proposed) == _norm(current)``) that fired. Track titles already go
through ``titles_match_for_review`` → ``normalize_title_for_compare`` →
``normalize_string``, which folds punctuation; the album-level path had no such
matcher.

THE FIX IS NARROW ON PURPOSE. ``normalize_unicode_punctuation`` maps smart
quotes, primes, typographic dashes and NBSP to ASCII — it **deletes nothing**. So
two spellings of the same character become one key, while a real difference is
untouched. The controls below pin that: if someone later "improves" this by
stripping punctuation wholesale, ``AC/DC`` vs ``ACDC`` starts passing and these
tests fail.
"""
from __future__ import annotations

import pytest

from services.metadata.metadata_proposal_service import _norm

STRAIGHT_APOSTROPHE = "\u0027"   # '
CURLY_APOSTROPHE = "\u2019"      # ’
LEFT_SINGLE_QUOTE = "\u2018"     # ‘

#: The four singles named in the report, as (library, MusicBrainz) pairs.
REPORTED_PAIRS = [
    (
        f"Baby, It{STRAIGHT_APOSTROPHE}s Cold Outside",
        f"Baby, It{CURLY_APOSTROPHE}s Cold Outside",
    ),
    (
        f"You{STRAIGHT_APOSTROPHE}ve Got a Lot to Answer For",
        f"You{CURLY_APOSTROPHE}ve Got a Lot to Answer For",
    ),
    (
        f"End of the 1980{STRAIGHT_APOSTROPHE}s",
        f"End of the 1980{CURLY_APOSTROPHE}s",
    ),
    (
        f"Drop It Like It{STRAIGHT_APOSTROPHE}s Hot",
        f"Drop It Like It{CURLY_APOSTROPHE}s Hot",
    ),
]


# ===========================================================================
# 1. The reported deferrals
# ===========================================================================
class TestTheReportedTitlesAreNotChanges:
    @pytest.mark.parametrize("library,musicbrainz", REPORTED_PAIRS, ids=[
        "baby-its-cold-outside",
        "youve-got-a-lot-to-answer-for",
        "end-of-the-1980s",
        "drop-it-like-its-hot",
    ])
    def test_straight_and_curly_apostrophes_are_the_same_key(self, library, musicbrainz):
        assert _norm(library) == _norm(musicbrainz), (
            f"{library!r} vs {musicbrainz!r} produced different keys, so this is "
            "reported as metadata needing approval — the reported deferral"
        )

    def test_the_left_single_quotation_mark_folds_too(self):
        """‘ (U+2018) shows up when a source uses the opening form by mistake."""
        assert _norm(f"It{LEFT_SINGLE_QUOTE}s") == _norm("It's")

    def test_mixed_apostrophes_in_one_title(self):
        assert _norm(f"Don{LEFT_SINGLE_QUOTE}t It{CURLY_APOSTROPHE}s") == _norm("Don't It's")


# ===========================================================================
# 2. The rest of the typographic class
# ===========================================================================
class TestTheOtherTypographicVariants:
    @pytest.mark.parametrize(
        "ascii_form,typographic_form",
        [
            ('He said "hello"', "He said \u201chello\u201d"),          # curly double quotes
            ("Mother's Milk", "Mother\u2019s Milk"),
            ("6'2\"", "6\u20322\u2033"),                              # prime / double prime
            ("Song \u2013 One", "Song \u2014 One"),                   # en dash vs em dash
            ("Part\u2010One", "Part-One"),                            # U+2010 hyphen vs '-'
            ("Two  Spaces", "Two\u00a0Spaces"),                       # non-breaking space
        ],
    )
    def test_it_folds_to_the_ascii_form(self, ascii_form, typographic_form):
        assert _norm(ascii_form) == _norm(typographic_form), (
            f"{ascii_form!r} vs {typographic_form!r} should be the same key"
        )


# ===========================================================================
# 3. CONTROLS — the fold must not swallow real differences
# ===========================================================================
class TestRealDifferencesStillDiffer:
    @pytest.mark.parametrize(
        "left,right",
        [
            ("Baby, It's Cold Outside", "Baby, It's Warm Inside"),
            ("Song", "Song (Live)"),
            ("Song", "Song (Acoustic)"),
            ("Song", "Song Pt. 2"),
            ("AC/DC", "ACDC"),
            ("Album", "Album Deluxe"),
            ("1980's", "1990's"),
            ("", "Song"),
            ("Song", ""),
        ],
    )
    def test_they_are_still_different_keys(self, left, right):
        """⚠️ If this starts passing, the key has become too permissive.

        ``AC/DC`` vs ``ACDC`` is the important one: it proves the fold MAPS
        characters rather than DELETING punctuation. Removing punctuation would
        also make `Song (Live)` equal `Song`, which would silently discard a
        genuine title difference.
        """
        assert _norm(left) != _norm(right), (
            f"{left!r} and {right!r} collapsed to the same key: {_norm(left)!r}"
        )

    def test_case_and_whitespace_are_still_folded(self):
        """The original contract must survive the change."""
        assert _norm("  Baby,  It's Cold Outside ") == _norm("baby, it's cold outside")

    def test_none_and_blank_stay_empty(self):
        assert _norm(None) == ""
        assert _norm("   ") == ""


# ===========================================================================
# 4. It reaches the album-level comparison that actually fired
# ===========================================================================
class TestTheAlbumLevelComparisonUsesTheKey:
    def test_the_single_album_title_comparison_is_the_folded_one(self):
        """These were SINGLES, so album title == track title.

        Pins that the album-level builder compares with ``_norm`` — if the
        comparison moved to a third, independent rule the deferrals could come
        back without any of the tests above noticing.
        """
        import inspect

        from services.metadata import metadata_proposal_service as mps

        source = inspect.getsource(mps)
        assert "_norm(proposed) == _norm(current" in source, (
            "the album/track change test no longer uses _norm(); re-point this test"
        )
        # And _norm itself must fold punctuation (not merely lowercase).
        assert "normalize_unicode_punctuation" in inspect.getsource(mps._norm)
