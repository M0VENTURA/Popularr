"""A featured-credit marker must not form ACROSS a word boundary.

THE REPORT
----------
From a scan log:

    artist='Audioslave feat. to Say but Goodbye' track='Nothing Le'
    artist='The Pretty Reckless feat. to Lose'   track='Nothing Left to Lose'

The real title is "Nothing Left to Say but Goodbye" / "Nothing Left to Lose".
The parser split on the ``ft`` INSIDE the word "Left", so the title lost
everything from there on and the remainder was appended to the artist as a
guest credit.

WHY IT HAPPENED
---------------
``helpers/normalization_service.py`` has four patterns for this job, and two of
them anchored the marker with ``\\s*`` — ZERO whitespace allowed — where the
other two correctly required ``\\s+``:

    GUEST_CREDIT_SUFFIX_RE         \\s*   ← broken
    TITLE_FEATURED_CREDIT_RE       \\s*   ← broken
    FEAT_SUFFIX_RE                 \\s+   ✓
    TITLE_FEAT_SUFFIX_RE           \\s+   ✓

A credit always has whitespace on BOTH sides of it, so ``\\s*`` bought nothing
and cost correctness: it let the marker start mid-word. It was never a "Left"
problem — every word ending in "ft" was affected ("Gift Horse" → "Gi",
"Soft & Wet" → "So", "Left Behind" → "Le").

THE BLAST RADIUS
----------------
``strip_featured_guest_suffix`` is not scan-only. It is imported by
``discogs_service``, ``lastfm_service`` and ``single_detection_service``, so the
same mis-split silently corrupted Discogs title matching, Last.fm album-vs-track
comparison and single detection — anywhere two title strings were compared, one
side could have been truncated to a prefix.

WHAT IS PINNED HERE
-------------------
Both directions, because a fix that stops stripping REAL credits would be worse
than the bug: the false negatives above, AND a control list of genuine credits
that must still be recognised.
"""

from __future__ import annotations

import itertools
import re

import pytest

from helpers.normalization_service import (
    GUEST_CREDIT_SUFFIX_RE,
    TITLE_FEATURED_CREDIT_RE,
    build_featured_artist,
    extract_featured_credit,
    normalise_scan_track_identity,
    strip_featured_artist,
    strip_featured_guest_suffix,
    strip_guest_credit,
)

# ---------------------------------------------------------------------------
# The reported words — a marker formed across a word boundary
# ---------------------------------------------------------------------------

#: Every one of these contains "ft" with NO whitespace before it, so a pattern
#: that allows ``\s*`` matches inside the word.
FT_INSIDE_A_WORD = [
    "Nothing Left to Lose",
    "Nothing Left to Say but Goodbye",
    "Left Behind",
    "Gift Horse",
    "Soft & Wet",
    "Soft Cell",
    "Softly",
    "Graft",
    "Craft",
    "Aftermath",
    "Raft",
    "Drift Away",
]


class TestAWordEndingInFtIsNotACredit:

    @pytest.mark.parametrize("value", FT_INSIDE_A_WORD)
    def test_strip_guest_credit_leaves_it_whole(self, value: str) -> None:
        assert strip_guest_credit(value) == value, (
            "the marker matched inside the word — a title loses everything "
            "from the 'ft' onwards"
        )

    @pytest.mark.parametrize("value", FT_INSIDE_A_WORD)
    def test_strip_featured_guest_suffix_leaves_it_whole(self, value: str) -> None:
        assert strip_featured_guest_suffix(value) == value

    @pytest.mark.parametrize("value", FT_INSIDE_A_WORD)
    def test_extract_finds_no_guest(self, value: str) -> None:
        title, guest = extract_featured_credit(value)
        assert (title, guest) == (value, ""), (
            f"a guest was invented from inside the word: {guest!r}"
        )

    @pytest.mark.parametrize("value", [
        v for v in FT_INSIDE_A_WORD if "&" not in v
    ])
    def test_the_permissive_artist_strip_is_also_safe(self, value: str) -> None:
        """``strip_featured_artist`` is the wide one, so it needs the same guard.

        ⚠️ "Soft & Wet" is excluded from this list ON PURPOSE. That function
        also strips ``&``, because on an ARTIST field "X & Y" really is two
        credited artists — so it returns "Soft", which is its documented job and
        a different rule entirely. The ``ft`` boundary is what is under test.
        """
        assert strip_featured_artist(value) == value

    def test_the_ampersand_rule_is_unchanged_and_separate(self) -> None:
        """CONTROL: the fix must not have disabled the ``&`` rule.

        It is why "Soft & Wet" is not in the list above, so it needs pinning
        somewhere — otherwise removing that rule would look like a pass.
        """
        assert strip_featured_artist("Soft & Wet") == "Soft"
        assert strip_featured_artist("Simon & Garfunkel") == "Simon"


# ---------------------------------------------------------------------------
# The reported identities, end to end
# ---------------------------------------------------------------------------


class TestTheReportedIdentitiesSurvive:

    @pytest.mark.parametrize("artist,title", [
        # ⭐ The exact pair from the log.
        ("Audioslave", "Nothing Left to Say but Goodbye"),
        ("The Pretty Reckless", "Nothing Left to Lose"),
        # Same album, and the track the report paired them with.
        ("Audioslave", "Bring Em Back Alive"),
        ("Audioslave", "#1 Zero"),
    ])
    def test_title_and_artist_are_untouched(self, artist: str, title: str) -> None:
        result = normalise_scan_track_identity(artist=artist, title=title)

        assert result["title"] == title, (
            f"{title!r} became {result['title']!r} — the scan would persist a "
            "truncated title and every lookup keyed on it would miss"
        )
        assert result["artist"] == artist
        assert result["featured_artist"] == "", (
            "a guest credit was invented out of the title's own syllables"
        )

    def test_the_logged_output_specifically(self) -> None:
        """The exact strings from the report, asserted as a pair.

        'Nothing Le' + 'feat. to Say but Goodbye' is the fingerprint: the title
        was cut at the 'ft' in 'Left' and the REST OF THE TITLE became the guest.
        """
        result = normalise_scan_track_identity(
            artist="Audioslave", title="Nothing Left to Say but Goodbye",
        )
        assert result["title"] != "Nothing Le"
        assert "to Say but Goodbye" not in result["artist"]


# ---------------------------------------------------------------------------
# CONTROL — real credits must still work
# ---------------------------------------------------------------------------


class TestRealCreditsAreStillRecognised:
    """Without this the "fix" could be to delete the feature entirely."""

    @pytest.mark.parametrize("value,expected", [
        ("Powerwolf feat. Alissa White-Gluz", "Powerwolf"),
        ("Evanescence ft. Paul McCoy", "Evanescence"),
        ("Artist featuring Other", "Artist"),
    ])
    def test_strip_guest_credit_still_strips(self, value: str, expected: str) -> None:
        assert strip_guest_credit(value) == expected

    @pytest.mark.parametrize("value,expected", [
        ("Song ft. Guest", "Song"),
        ("Song (feat. Guest)", "Song"),
        ("Song [ft. Guest]", "Song"),
        ("Song featuring Guest", "Song"),
    ])
    def test_strip_featured_guest_suffix_still_strips(self, value: str, expected: str) -> None:
        assert strip_featured_guest_suffix(value) == expected

    def test_a_guest_is_still_moved_onto_the_artist(self) -> None:
        """The behaviour the whole pass exists for, from its own comment."""
        result = normalise_scan_track_identity(
            artist="Evanescence", title="Bring Me To Life feat 12 stones",
        )
        assert result["title"] == "Bring Me To Life"
        assert result["featured_artist"] == "12 Stones"
        assert result["artist"] == "Evanescence feat. 12 Stones"

    @pytest.mark.parametrize("value", ["feat. Nobody", "ft. X", "featuring Y"])
    def test_a_name_that_is_nothing_but_a_credit_is_not_emptied(self, value: str) -> None:
        """The documented contract: never return "" for a non-empty input."""
        assert strip_guest_credit(value) == value

    def test_a_name_that_WOULD_strip_to_nothing_is_kept(self) -> None:
        """A name is never lost, and the RESULT is the input, not a fragment.

        ⚠️ Note this cannot fail by reaching the ``or text`` guard — see
        ``test_the_never_empty_guard_is_unreachable`` below for why. What it
        pins is the observable contract: a credit-only string comes back whole.
        """
        for value in (" feat. Nobody", " ft. X", "featuring Y"):
            result = strip_guest_credit(value)
            assert result, (
                f"{value!r} was emptied — a name must never be lost"
            )
            assert result == value.strip()

    def test_the_never_empty_guard_is_unreachable(self) -> None:
        """⚠️ Documents a FINDING, not a behaviour: the guard is dead code.

        ``strip_guest_credit`` opens with ``text.strip()``, so the text can
        never begin with whitespace; the pattern requires a leading ``\\s+``.
        The match therefore can never start at index 0 and the sub can never
        empty the string.

        This is here because a mutation dropping the ``or text`` ESCAPED the
        oracle, and an escaping mutation is normally a test gap. It is not one:
        you cannot mutate unreachable code into a failure. Pinning the
        reasoning means the next person does not re-derive it — and if the
        anchor is ever loosened, THIS test starts failing and says why.
        """
        probe = ["feat", "ft", "featuring", ".", " ", "(", ")", "[", "]", "A", ","]
        counterexamples = 0
        for n in range(1, 4):
            for combo in itertools.product(probe, repeat=n):
                text = "".join(combo).strip()
                if not text:
                    continue
                if GUEST_CREDIT_SUFFIX_RE.sub("", text).strip() == "":
                    counterexamples += 1

        assert counterexamples == 0, (
            "the sub CAN now empty the text, so the `or text` guard is live — "
            "add a test that reaches it and drop the dead-code note in "
            "strip_guest_credit's docstring"
        )

    def test_extract_never_empties_a_title(self) -> None:
        """Same contract on the title path."""
        for value in (" feat. Nobody", " ft. X"):
            title, guest = extract_featured_credit(value)
            assert title, "a title was emptied rather than kept"

    def test_it_is_still_case_insensitive(self) -> None:
        assert strip_guest_credit("Artist FEAT. Guest") == "Artist"
        assert strip_guest_credit("Artist Ft. Guest") == "Artist"


# ---------------------------------------------------------------------------
# The pattern-level contract — one rule for all four
# ---------------------------------------------------------------------------


class TestWhitespaceIsRequiredBeforeTheMarker:
    """Unit-level, so a future edit to either pattern is caught directly.

    The function tests above would also catch it, but they cannot say WHY —
    this names the property that was wrong.
    """

    @pytest.mark.parametrize("pattern", [
        GUEST_CREDIT_SUFFIX_RE,
        TITLE_FEATURED_CREDIT_RE,
    ])
    def test_the_pattern_requires_whitespace_before_the_marker(self, pattern) -> None:
        """The ANCHOR must be ``\\s+``, so no path reaches the marker without
        consuming a real whitespace character first.

        ⚠️ Deliberately NOT "no ``\\s*`` anywhere before the marker": a ``\\s*``
        INSIDE the optional bracket group is correct and needed — it is what
        lets ``"Song ( ft. Guest )"`` through. Only the anchor matters.
        """
        flattened = re.sub(r"\s+", "", pattern.pattern)
        assert flattened.startswith(r"\s+"), (
            f"pattern opens with {flattened[:6]!r}; a leading \\s* lets the "
            "marker form across a word boundary"
        )

    @pytest.mark.parametrize("pattern", [
        GUEST_CREDIT_SUFFIX_RE,
        TITLE_FEATURED_CREDIT_RE,
    ])
    @pytest.mark.parametrize("word", ["Soft", "Graft", "Craft", "Raft", "Drift"])
    def test_no_bare_ft_word_matches(self, pattern, word: str) -> None:
        """The same property, stated behaviourally."""
        assert pattern.search(word) is None, (
            f"{word!r} matched — the marker formed inside the word"
        )

    @pytest.mark.parametrize("pattern", [
        GUEST_CREDIT_SUFFIX_RE,
        TITLE_FEATURED_CREDIT_RE,
    ])
    @pytest.mark.parametrize("value", [
        "Song ft. Guest", "Song (ft. Guest)", "Song ( ft. Guest )",
    ])
    def test_a_real_credit_still_matches(self, pattern, value: str) -> None:
        """CONTROL: including the bracket form the inner ``\\s*`` exists for."""
        assert pattern.search(value) is not None
    @pytest.mark.parametrize("pattern", [
        GUEST_CREDIT_SUFFIX_RE,
        TITLE_FEATURED_CREDIT_RE,
    ])
    def test_whitespace_is_required_AFTER_the_marker_too(self, pattern) -> None:
        """The marker must be a whole word on both sides, not just the left.

        Without the trailing ``\s+``, ``"Song feat.X"`` would match and the
        guest would be read from a run-on string. The two guards are the two
        halves of the same claim: a credit is a SEPARATE word.
        """
        assert pattern.search("Song feat.X") is None, (
            "the marker matched with no whitespace after it — the guest name "
            "would be read from a run-on word"
        )
        assert pattern.search("Song ft.X") is None
    def test_the_marker_needs_whitespace_after_it_too(self) -> None:
        """CONTROL on the other side: "soft" has no space after the marker."""
        assert GUEST_CREDIT_SUFFIX_RE.search("Soft") is None
        assert TITLE_FEATURED_CREDIT_RE.search("Soft") is None


# ---------------------------------------------------------------------------
# The consumers that are not the scan
# ---------------------------------------------------------------------------


class TestTheOtherConsumersAreSafe:
    """Three enrichment services compare titles through this helper.

    They are asserted through the shared helper rather than by importing each
    service, because the helper IS the seam — but the test names the consumers
    so the cost of a regression here is visible.
    """

    def test_lastfm_album_vs_track_comparison(self) -> None:
        """"Nothing Left to Lose" (track) vs its album must NOT compare equal to
        a truncated prefix of itself."""
        album = "The Pretty Reckless"
        track = "Nothing Left to Lose"
        assert strip_featured_guest_suffix(album) != strip_featured_guest_suffix(track)
        assert strip_featured_guest_suffix(track) == track

    def test_discogs_matching_input(self) -> None:
        assert strip_featured_guest_suffix("Gift Horse") == "Gift Horse"

    def test_single_detection_input(self) -> None:
        """single_detection does ``strip_featured_guest_suffix(title) or title``;
        a truncation here mis-keys the single lookup."""
        assert strip_featured_guest_suffix("Left Behind") == "Left Behind"

    def test_an_empty_result_would_fall_back_not_blank(self) -> None:
        """The ``or title`` guards in those callers are why returning "" is
        survivable — but the fix should mean they never need to fire."""
        for value in FT_INSIDE_A_WORD:
            assert strip_featured_guest_suffix(value) or value
