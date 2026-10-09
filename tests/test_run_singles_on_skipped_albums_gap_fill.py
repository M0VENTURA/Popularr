"""``features.run_singles_on_skipped_albums`` — the singles gap fill.

REPORTED (as the intent behind the option, which nothing read)

    > The idea behind it was that if it had scanned an album but somehow
    > missed a track it wouldn't keep skipping that track.

The album window answers *"has this album been scanned?"*, which is NOT the
same question as *"did every track get a verdict?"*. A track the earlier pass
never reached — its worker abandoned on the per-album budget, a track added to
the album afterwards, a detection that ERRORED and therefore stamped no
timestamp — leaves the album looking finished, so ``was_album_scanned`` skips
it again on every pass and the gap is never filled.

These tests pin the two decisions the fix is built from, both extracted so
they can be called directly:

* ``_tracks_without_singles_verdict`` — the ONE definition of "assessed",
  shared by the ``skip_unchanged_albums`` gate and the gap fill so they can
  never disagree;
* ``_singles_gap_fill`` — when the option turns unassessed tracks into a
  reason NOT to skip.

⚠️ The load-bearing distinction is that a NEGATIVE verdict is still a verdict.
``single_detection_last_updated`` is stamped only when detection completed, so
"assessed as not a single" must count as assessed — otherwise every ordinary
track would keep the album un-skipped for ever.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest


def _assessed(**extra):
    track = {
        "title": "Plans",
        "is_single": False,
        "single_confidence": "low",
        "single_detection_last_updated": datetime.now(timezone.utc),
    }
    track.update(extra)
    return track


# ---------------------------------------------------------------------------
# The predicate
# ---------------------------------------------------------------------------
class TestTracksWithoutASinglesVerdict:
    def test_a_track_with_no_timestamp_is_unassessed(self):
        from services.popularity.scan_stage_runner import (
            _tracks_without_singles_verdict,
        )

        track = {"title": "Never reached", "is_single": False}

        assert _tracks_without_singles_verdict([track]) == [track]

    def test_a_negative_verdict_counts_as_assessed(self):
        """THE LOAD-BEARING CASE.

        "Not a single" is the commonest outcome. It HAS a timestamp, so it is
        a completed assessment — treating it as missing would keep every
        ordinary album un-skipped on every pass.
        """
        from services.popularity.scan_stage_runner import (
            _tracks_without_singles_verdict,
        )

        assert _tracks_without_singles_verdict([_assessed()]) == []

    def test_a_positive_verdict_counts_as_assessed(self):
        from services.popularity.scan_stage_runner import (
            _tracks_without_singles_verdict,
        )

        assert _tracks_without_singles_verdict([_assessed(is_single=True)]) == []

    def test_a_manual_override_without_a_timestamp_is_still_unassessed(self):
        """A user override is not a detection run — nothing stamped a time.

        Pinned so the predicate keeps answering the question it is asked
        ("did a pass complete?") rather than guessing at intent.
        """
        from services.popularity.scan_stage_runner import (
            _tracks_without_singles_verdict,
        )

        track = {"title": "Manual", "single_manual_override": True}

        assert _tracks_without_singles_verdict([track]) == [track]

    def test_only_the_unassessed_tracks_are_returned(self):
        from services.popularity.scan_stage_runner import (
            _tracks_without_singles_verdict,
        )

        missed = {"title": "The Dark Side of Love"}
        tracks = [_assessed(title="Plans"), missed, _assessed(title="Circles")]

        assert _tracks_without_singles_verdict(tracks) == [missed]

    def test_no_tracks_is_not_a_gap(self):
        from services.popularity.scan_stage_runner import (
            _tracks_without_singles_verdict,
        )

        assert _tracks_without_singles_verdict(None) == []
        assert _tracks_without_singles_verdict([]) == []

    def test_a_blank_timestamp_is_unassessed(self):
        """``''`` and ``None`` both mean "never stamped"."""
        from services.popularity.scan_stage_runner import (
            _tracks_without_singles_verdict,
        )

        assert len(_tracks_without_singles_verdict([_assessed(single_detection_last_updated="")])) == 1
        assert len(_tracks_without_singles_verdict([_assessed(single_detection_last_updated=None)])) == 1

    def test_a_none_entry_cannot_crash_the_scan(self):
        """Conservative: an unusable row counts as unassessed (un-skip)."""
        from services.popularity.scan_stage_runner import (
            _tracks_without_singles_verdict,
        )

        assert _tracks_without_singles_verdict([None]) == [None]


# ---------------------------------------------------------------------------
# Can this pass fill the gap at all?
# ---------------------------------------------------------------------------
class TestPassRunsSingles:
    def test_a_metadata_only_pass_cannot(self):
        from services.popularity.scan_stage_runner import _pass_runs_singles

        assert _pass_runs_singles({"metadata_only": True}) is False

    def test_a_popularity_only_pass_cannot(self):
        from services.popularity.scan_stage_runner import _pass_runs_singles

        assert _pass_runs_singles({"popularity_only": True}) is False

    @pytest.mark.parametrize(
        "options",
        [
            {},
            {"singles_only": True},
            {"singles_with_missing_popularity": True},
            {"finalise_only": True, "singles_only": True},
            {"singles_detection_only": True},
        ],
        ids=["combined", "singles", "singles-missing-pop", "finalise", "singles-only"],
    )
    def test_every_other_pass_can(self, options):
        from services.popularity.scan_stage_runner import _pass_runs_singles

        assert _pass_runs_singles(options) is True

    def test_it_matches_track_stages_own_gate(self):
        """One definition: the predicate must mirror the gate it stands for.

        ``track_stage``'s singles section is guarded by
        ``if not metadata_only and not popularity_only and not _sd_fresh:`` —
        if that ever changes, this predicate has to change with it or the
        option will claim to fill a phase the pass does not run.
        """
        from pathlib import Path
        from services.popularity.stages import track_stage

        source = Path(track_stage.__file__).read_text(encoding="utf-8")

        assert "if not metadata_only and not popularity_only and not _sd_fresh:" in source


# ---------------------------------------------------------------------------
# The gap fill itself
# ---------------------------------------------------------------------------
class TestSinglesGapFill:
    def test_it_returns_the_unassessed_tracks(self):
        from services.popularity.scan_stage_runner import _singles_gap_fill

        missed = {"title": "Never reached"}
        tracks = [_assessed(), missed]

        assert _singles_gap_fill(
            tracks=tracks, runs_singles=True, enabled=True
        ) == [missed]

    def test_the_option_off_means_no_fill(self):
        from services.popularity.scan_stage_runner import _singles_gap_fill

        # The default in the Config UI, and the reason it exists: it costs
        # Discogs/MusicBrainz lookups, so it is opt-in.
        assert _singles_gap_fill(
            tracks=[{"title": "Never reached"}], runs_singles=True, enabled=False
        ) == []

    def test_a_pass_without_a_singles_phase_means_no_fill(self):
        from services.popularity.scan_stage_runner import _singles_gap_fill

        assert _singles_gap_fill(
            tracks=[{"title": "Never reached"}], runs_singles=False, enabled=True
        ) == []

    def test_a_complete_album_is_still_skipped(self):
        """CONTROL — the option must not disable skipping for healthy albums."""
        from services.popularity.scan_stage_runner import _singles_gap_fill

        tracks = [_assessed(title=t) for t in ("Plans", "Circles", "Murmurs")]

        assert _singles_gap_fill(tracks=tracks, runs_singles=True, enabled=True) == []

    def test_no_tracks_means_no_fill(self):
        from services.popularity.scan_stage_runner import _singles_gap_fill

        assert _singles_gap_fill(tracks=None, runs_singles=True, enabled=True) == []
        assert _singles_gap_fill(tracks=[], runs_singles=True, enabled=True) == []


# ---------------------------------------------------------------------------
# Wiring: the runner must use one predicate for both decisions
# ---------------------------------------------------------------------------
class TestTheRunnerWiring:
    @pytest.fixture(autouse=True)
    def _source(self):
        from services.popularity import scan_stage_runner as runner

        self.source = inspect.getsource(runner)
        return self.source

    def test_the_skip_gate_and_the_gap_fill_share_the_predicate(self):
        """The gate must not re-derive "assessed" of its own.

        Two definitions of it is exactly how a gate ends up unable to satisfy
        itself (the failure mode the singles-freshness fix had to undo).
        """
        assert "all_done = not _singles_unassessed" in self.source
        assert (
            'all(t.get("single_detection_last_updated") for t in tracks)'
            not in self.source
        ), "the gate went back to re-deriving 'assessed' inline"

    def test_the_runner_asks_whether_the_pass_runs_singles(self):
        assert "_runs_singles = _pass_runs_singles(options)" in self.source

    def test_the_gap_fill_reads_the_config_option(self):
        assert (
            'enabled=bool(get_feature("run_singles_on_skipped_albums", False))'
            in self.source
        )

    def test_an_unassessed_track_un_skips_the_album(self):
        index = self.source.index("_unassessed = _singles_gap_fill(")
        window = self.source[index : index + 700]

        assert "if _unassessed:" in window
        assert "skip_album = False" in window, (
            "the gap fill must un-skip the album, or the loop skips it anyway"
        )

    def test_the_gap_fill_does_not_force_a_metadata_rerun(self):
        """It fills the singles verdict, it is not the completeness override.

        The completeness override sets ``force_metadata_for_this_album`` to
        push the album through the METADATA pass; doing that here would turn a
        one-verdict backfill into a full metadata re-run per missed track.
        """
        index = self.source.index("_unassessed = _singles_gap_fill(")
        window = self.source[index : index + 700]

        assert "force_metadata_for_this_album" not in window

    def test_it_only_runs_when_something_else_decided_to_skip(self):
        """CONTROL — Force / an album filter must keep today's behaviour."""
        before = self.source[: self.source.index("_unassessed = _singles_gap_fill(")]
        last_guard = before.rindex("if skip_album:")

        assert "if skip_album:" in before[last_guard:]
