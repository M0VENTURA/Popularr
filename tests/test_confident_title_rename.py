"""A confident MusicBrainz rename is APPLIED by the scan, not offered.

REQUEST

> "I think if it's a high confidence that the track is correct the rename could
> happen. It would need to be similar track length, track number, and the file
> name should have a similarity"

WHY IT MATTERED

A completed scan kept leaving titles for review even when it already knew the
answer — the reported "Mike Bray (Don't Drink The Water) → Members Only Suite:
iii. Mike Bray (Don't Drink the Water)" bar reappearing after every scan.

WHAT MAKES A RENAME SAFE

All three signals gate it, and each covers a different failure:

* **position** — a same-titled track in another slot is a different row;
* **length** — the strongest evidence this is the same RECORDING, not another
  version;
* **title similarity** — the name must already be recognisably the same, or the
  file probably holds a different track and renaming would MASK that.

⚠️ THE SIMILARITY MEASURE IS THE SUBTLE PART.

Both shared normalisers are WRONG here, and the tests below pin that:

* ``normalize_title_for_lookup`` deletes the parenthetical, so
  "Song (Bonus Track)" and "Song" both become "song";
* ``normalize_title_for_compare`` also deletes PERFORMANCE markers, so
  "Intro (Live at Wembley)" becomes "intro";
* the shared ``fuzzy_match_score`` is ``token_set_ratio``, which returns 1.0
  whenever one side's tokens are a SUBSET of the other's — so "Song" vs
  "A Completely Different Song" scores a perfect 1.0.

For MATCHING that generosity is deliberate. For GRANTING A RENAME it would let a
shared common word rewrite a file. So this path folds TYPOGRAPHY ONLY and uses a
symmetric ratio.
"""

from __future__ import annotations

import pytest

from services.popularity.popularity_sources import (
    TITLE_RENAME_MIN_SIMILARITY,
    _symmetric_title_ratio,
    _title_rename_confidence,
    _typography_key,
)


def _conf(local, release, *, duration=220.0, length_ms=221000.0,
          pos="8", mb_pos=8, disc="1", mb_disc=1):
    return _title_rename_confidence(
        local, release,
        local_duration=duration,
        release_length_ms=length_ms,
        local_disc=disc,
        local_position=pos,
        release_disc=mb_disc,
        release_position=mb_pos,
    )


class TestTheReportedRenameIsApplied:
    def test_a_medley_movement_title_is_renamed(self) -> None:
        """THE REPORT — same slot, same length, recognisably the same name."""
        verdict = _conf(
            "Mike Bray (Don't Drink The Water)",
            "Members Only Suite: iii. Mike Bray (Don\u2019t Drink the Water)",
        )
        assert verdict["ok"] is True, verdict
        assert verdict["reason"] == "confident"

    def test_a_typography_only_difference_is_renamed(self) -> None:
        verdict = _conf("Baby, It's Cold Outside", "Baby, It\u2019s Cold Outside")
        assert verdict["ok"] is True, verdict

    def test_case_only_difference_is_identical(self) -> None:
        """CONTROL — a case-only difference is not a rename at all.

        The scan already skips titles that match case-insensitively before
        asking, so this is reported as ``identical`` rather than a rename. It is
        asserted so nobody "fixes" it into a rename later.
        """
        verdict = _conf("Our Job To Rock", "Our Job to Rock")
        assert verdict["ok"] is False
        assert verdict["reason"] == "identical"


class TestTheThreeGates:
    def test_a_different_track_number_blocks_the_rename(self) -> None:
        verdict = _conf("Mike Bray", "Members Only Suite: iii. Mike Bray",
                        pos="9", mb_pos=8)
        assert verdict["ok"] is False
        assert verdict["reason"] == "track_number_differs"

    def test_a_different_length_blocks_the_rename(self) -> None:
        """A different LENGTH means a different recording, whatever the title."""
        verdict = _conf("Mike Bray", "Members Only Suite: iii. Mike Bray",
                        duration=220.0, length_ms=260000.0)
        assert verdict["ok"] is False
        assert verdict["reason"] == "length_differs"

    def test_a_different_disc_blocks_the_rename(self) -> None:
        verdict = _conf("Mike Bray", "Members Only Suite: iii. Mike Bray",
                        disc="2", mb_disc=1)
        assert verdict["ok"] is False
        assert verdict["reason"] == "disc_differs"

    def test_an_unrelated_title_blocks_the_rename(self) -> None:
        """The file probably holds a different track; renaming would mask it."""
        verdict = _conf("Manchild", "Members Only Suite: iii. Mike Bray")
        assert verdict["ok"] is False
        assert verdict["reason"] == "title_too_dissimilar"

    def test_missing_length_evidence_is_permissive_but_not_blind(self) -> None:
        """CONTROL — an untagged file still has to pass the title gate."""
        ok = _conf(
            "Mike Bray (Don't Drink The Water)",
            "Members Only Suite: iii. Mike Bray (Don't Drink the Water)",
            duration=None, length_ms=None,
        )
        assert ok["ok"] is True, ok
        bad = _conf(
            "Manchild",
            "Members Only Suite: iii. Mike Bray (Don't Drink the Water)",
            duration=None, length_ms=None,
        )
        assert bad["ok"] is False
        assert bad["reason"] == "title_too_dissimilar"


class TestTheSimilarityMeasureIsNotSubsetBased:
    """Pins WHY the shared helpers cannot be reused for a rename."""

    @pytest.mark.parametrize(
        "parent,shorter",
        [
            ("A Completely Different Song", "Song"),
            ("Song of the South", "Song"),
            ("Song (Bonus Track)", "Song"),
            ("Intro (Live at Wembley)", "Intro"),
        ],
    )
    def test_a_shared_word_does_not_reach_the_floor(self, parent, shorter) -> None:
        ratio = _symmetric_title_ratio(
            _typography_key(shorter), _typography_key(parent)
        )
        assert ratio < TITLE_RENAME_MIN_SIMILARITY, (
            f"{shorter!r} vs {parent!r} scored {ratio:.3f} — a shared word must "
            "not authorise a rename"
        )

    def test_a_performance_marker_is_not_deleted(self) -> None:
        """The marker IS the evidence the two recordings differ."""
        assert "live" in _typography_key("Intro (Live at Wembley)")
        assert "bonus" in _typography_key("Song (Bonus Track)")

    def test_typography_is_folded(self) -> None:
        assert _typography_key("Don\u2019t Stop") == _typography_key("Don't Stop")
        assert _typography_key("Don't Stop") == _typography_key("Dont Stop")

    def test_a_genuine_rename_clears_the_floor(self) -> None:
        ratio = _symmetric_title_ratio(
            _typography_key("Mike Bray (Don't Drink The Water)"),
            _typography_key(
                "Members Only Suite: iii. Mike Bray (Don\u2019t Drink the Water)"
            ),
        )
        assert ratio >= TITLE_RENAME_MIN_SIMILARITY, ratio

    def test_the_two_bands_are_well_separated(self) -> None:
        """A real margin, not a hair's breadth either way."""
        rename = _symmetric_title_ratio(
            _typography_key("Mike Bray (Don't Drink The Water)"),
            _typography_key("Members Only Suite: iii. Mike Bray (Don't Drink the Water)"),
        )
        different = _symmetric_title_ratio(
            _typography_key("Manchild"), _typography_key("Members Only Suite")
        )
        assert rename - different > 0.30, (rename, different)
