"""An UNBRACKETED trailing edition marker created a duplicate album.

Reported
--------
> Some albums have the release name as the musicbrainz album comment,
> e.g. "The Mirror's Truth Version". This isn't being corrected during a
> metadata update and this is overwriting as a new version on every import.

Root cause
----------
Every album-name rule in ``helpers.normalization_service`` is BRACKET-ANCHORED
(``\\(...\\)`` at the end of the string). A tag source that writes the marker
WITHOUT brackets is therefore invisible to all of them at once, which produces
two distinct failures:

1. **The lookup key keeps the marker.**
   ``normalize_title_for_lookup`` drops a bracketed annotation (it runs
   ``strip_parentheses``) but retains an unbracketed one::

       "The Mirror's Truth (Version)"  ->  "the mirror s truth"
       "The Mirror's Truth Version"    ->  "the mirror s truth version"

   The same album under the two spellings yields two DIFFERENT keys, so the
   release reads as missing while also being owned, and each import writes
   another copy — "overwriting as a new version on every import".

2. **The repair is a no-op.** ``repair_annotations`` and
   ``strip_album_edition_marker`` both left the unbracketed form untouched, so a
   metadata update never corrected the stored name — "isn't being corrected
   during a metadata update". ``has_edition_annotation`` did not even RECOGNISE
   it, so every caller keyed off that predicate saw a plain album name.

The fix
-------
One transformation (``bracket_trailing_edition_marker``) rewrites the
unbracketed trailing marker into its bracketed form before any other rule runs.
There is deliberately NO second parallel rule set with its own vocabulary —
which is exactly how the ``tour``/``(tour edition)`` divergence documented in
this module happened. Both spellings then travel the SAME code path.

These guards assert on the FAILURE each prevents, so a regression names itself.
"""

from __future__ import annotations

import pytest

from helpers.normalization_service import (
    bracket_trailing_edition_marker,
    clean_album_name_for_storage,
    has_edition_annotation,
    normalize_title_for_lookup,
    repair_annotations,
    strip_album_edition_marker,
)

REPORTED = "The Mirror's Truth Version"
PLAIN = "The Mirror's Truth"


# ===========================================================================
# 1. THE REPORTED BUG: the two spellings must be one album
# ===========================================================================

class TestBothSpellingsShareOneLookupKey:
    """The duplicate-album mechanism, asserted directly.

    A mismatch here is what makes one album exist twice: the owned copy and a
    "missing" release that re-imports forever.
    """

    def test_unbracketed_marker_is_dropped_from_the_key(self) -> None:
        assert normalize_title_for_lookup(REPORTED) == normalize_title_for_lookup(PLAIN)

    def test_bracketed_marker_is_dropped_from_the_key(self) -> None:
        assert normalize_title_for_lookup(f"{PLAIN} (Version)") == (
            normalize_title_for_lookup(PLAIN)
        )

    @pytest.mark.parametrize(
        "variant",
        [
            f"{PLAIN} Version",
            f"{PLAIN} (Version)",
            f"{PLAIN} Deluxe Edition",
            f"{PLAIN} (Deluxe Edition)",
            f"{PLAIN} Deluxe Version",
            f"{PLAIN} (Deluxe Version)",
            f"{PLAIN} Remastered",
            f"{PLAIN} (Remastered)",
            f"{PLAIN} Collector's Edition",
        ],
    )
    def test_every_spelling_collapses_to_the_plain_title(self, variant: str) -> None:
        assert normalize_title_for_lookup(variant) == normalize_title_for_lookup(PLAIN), (
            f"{variant!r} produced a DIFFERENT lookup key from {PLAIN!r}, so the "
            "same album will be treated as a separate release and re-imported"
        )

    def test_the_bracketed_and_unbracketed_keys_agree(self) -> None:
        """The exact asymmetry that caused the report."""
        assert normalize_title_for_lookup(f"{PLAIN} Version") == (
            normalize_title_for_lookup(f"{PLAIN} (Version)")
        )


class TestTheNameIsActuallyRepaired:
    """Symptom 2: a metadata update must now correct the stored name."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (REPORTED, f"{PLAIN} (Version)"),
            ("American Idiot Deluxe Edition", "American Idiot (Deluxe Edition)"),
            ("Some Album Remastered", "Some Album (Remastered)"),
            ("Some Album Deluxe", "Some Album (Deluxe)"),
            ("Some Album Expanded Edition", "Some Album (Expanded Edition)"),
        ],
    )
    def test_repair_brackets_an_unbracketed_marker(self, raw: str, expected: str) -> None:
        assert repair_annotations(raw) == expected

    def test_the_repair_is_what_a_metadata_update_would_write(self) -> None:
        """The stored-name path uses clean_album_name_for_storage."""
        assert clean_album_name_for_storage(REPORTED) == f"{PLAIN} (Version)"

    @pytest.mark.parametrize(
        "raw",
        [REPORTED, "American Idiot Deluxe Edition", "Some Album Remastered"],
    )
    def test_edition_annotation_is_now_recognised(self, raw: str) -> None:
        """It returned False before, so callers saw a plain album name."""
        assert has_edition_annotation(raw) is True

    def test_album_mode_strips_an_unbracketed_marker_entirely(self) -> None:
        """"album" mode discards the edition; the unbracketed form was immune."""
        assert strip_album_edition_marker(REPORTED) == f"{PLAIN} (Version)"


# ===========================================================================
# 2. Idempotency — a repair must not keep changing the name
# ===========================================================================

class TestRepairIsIdempotentAndCannotDoubleWrap:
    """The first attempt at this emitted the malformed ``((Version))``."""

    @pytest.mark.parametrize("raw", [REPORTED, f"{PLAIN} (Version)", PLAIN])
    def test_repair_twice_equals_repair_once(self, raw: str) -> None:
        once = repair_annotations(raw)
        assert repair_annotations(once) == once

    @pytest.mark.parametrize("raw", [REPORTED, f"{PLAIN} (Version)", PLAIN])
    def test_strip_twice_equals_strip_once(self, raw: str) -> None:
        once = strip_album_edition_marker(raw)
        assert strip_album_edition_marker(once) == once

    def test_no_doubled_brackets_are_ever_produced(self) -> None:
        for raw in (REPORTED, f"{PLAIN} (Version)", PLAIN, "X Deluxe Edition"):
            for fn in (repair_annotations, clean_album_name_for_storage):
                assert "((" not in fn(raw), f"{fn.__name__} double-wrapped {raw!r}"

    def test_bracketing_is_idempotent(self) -> None:
        once = bracket_trailing_edition_marker(REPORTED)
        assert bracket_trailing_edition_marker(once) == once

    def test_bracketing_leaves_an_already_bracketed_name_alone(self) -> None:
        assert bracket_trailing_edition_marker(f"{PLAIN} (Version)") == f"{PLAIN} (Version)"


# ===========================================================================
# 3. Form markers and real titles must survive untouched
# ===========================================================================

class TestNothingElseIsMangled:
    """A rule this broad is dangerous; these are the boundaries it must not cross."""

    @pytest.mark.parametrize(
        "raw",
        [
            # A FORM names a different RECORDING, not a different pressing, so
            # it must survive — exactly as the bracketed rule's leading
            # lookahead already preserves "X (Live Version)".
            f"{PLAIN} Live Version",
            f"{PLAIN} Live",
            f"{PLAIN} Acoustic Version",
            f"{PLAIN} Remix",
            f"{PLAIN} Instrumental",
            f"{PLAIN} Demo",
            f"{PLAIN} Unplugged",
        ],
    )
    def test_form_markers_are_preserved(self, raw: str) -> None:
        assert bracket_trailing_edition_marker(raw) == raw, (
            f"{raw!r} was rewritten — a form marker distinguishes two different "
            "recordings and must never be treated as an edition marker"
        )

    @pytest.mark.parametrize(
        "raw",
        [
            PLAIN,
            "The Wall",
            "OK Computer",
            "(What's the Story) Morning Glory?",
            "The Remixes",
            "Live at Wembley",
            "Version",
            "Edition",
            "Truth",
            "Version Of Events",
        ],
    )
    def test_ordinary_titles_are_untouched(self, raw: str) -> None:
        assert bracket_trailing_edition_marker(raw) == raw

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_blank_input_is_safe(self, raw: str) -> None:
        assert bracket_trailing_edition_marker(raw) == raw

    def test_a_name_of_only_a_marker_is_not_emptied(self) -> None:
        """Every rule returns the original rather than a blank album name."""
        for raw in ("Version", "Deluxe Edition", "Remastered"):
            assert bracket_trailing_edition_marker(raw) == raw
            assert repair_annotations(raw) == raw
            assert strip_album_edition_marker(raw) == raw

    def test_a_trailing_word_that_is_not_a_marker_is_kept(self) -> None:
        """Guards the backward token sweep against swallowing real words."""
        for raw in (
            "Love Is The Truth",
            "Nothing But The Truth",
            "The Mirror's Truth",
            "A Version Of Events",
        ):
            assert bracket_trailing_edition_marker(raw) == raw
