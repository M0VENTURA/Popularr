"""Abandoned artists must not survive into a later scan's banner.

REPORTED
--------
The dashboard showed "Artists skipped / abandoned — investigate" listing
artists that had since been rescanned successfully, and pressing × did not
clear it — it came back.

ROOT CAUSE
----------
``scan_state.write_progress_with_current_artist`` MERGES ``extra`` into the
stored ``extra_data`` JSON: a key the caller does not supply keeps its previous
value. Nothing ever cleared ``abandoned_artists`` —
``_abandoned_artists`` is rebuilt as ``{}`` in memory for the new run, but the
``starting`` and ``complete`` progress writes both omitted the key, so the
PERSISTED list from an earlier run lived on.

``get_scan_progress`` only returns scans with ``is_running`` true, so a banner
taken from that list can only ever be showing an earlier run's abandons — the
current run has barely started.

The × "not sticking" follows from the same data: the fingerprint is built from
the artist names and reasons, which are identical on every render of the stale
list, so the dismissal matched an id that kept being re-created — and any real
re-run produced a DIFFERENT id (the budget seconds change), which then showed
up as a new alert.

The fix is to clear the list when a scan STARTS, so the persisted row only ever
describes the run currently in progress.
"""

from __future__ import annotations

import inspect

from services.scanning.pipelines import popularity_pipeline


def _full_scan_source() -> str:
    """The source of the full-scan orchestrator that owns the progress row."""
    return inspect.getsource(popularity_pipeline._run_full_scan_as_artist_pipeline)


def _starting_write_source() -> str:
    """The source of the ``status: "starting"`` progress write."""
    body = _full_scan_source()
    marker = '"status": "starting"'
    index = body.index(marker)
    # Back up to the enclosing write call and forward past its closing brace.
    start = body.rindex("write_progress_with_current_artist(", 0, index)
    end = body.index("\n    )", index)
    return body[start:end]


class TestTheStartingWriteClearsAbandonments:
    def test_the_starting_write_explicitly_clears_the_list(self):
        """An OMITTED key is preserved by the merge, so it must be supplied."""
        source = _starting_write_source()
        assert '"abandoned_artists": {}' in source, (
            "the starting progress write does not clear abandoned_artists, so "
            "the previous run's banner content persists into this one"
        )

    def test_the_clear_is_inside_the_starting_extra_block(self):
        """Guards against the value being added to a different call."""
        source = _starting_write_source()
        # Count the KEY, not the word: the explanatory comment names the field.
        assert source.count('"abandoned_artists":') == 1
        assert '"status": "starting"' in source


class TestTheMergeSemanticsThatCauseIt:
    """Pins WHY the explicit clear is required — not merely that it is there."""

    def test_an_omitted_key_survives_a_write(self):
        from services.scanning.scan_state import write_progress_with_current_artist
        import inspect as _inspect

        body = _inspect.getsource(write_progress_with_current_artist)
        assert "current_extra = dict(state.extra_data or {})" in body
        assert "current_extra[k] = v" in body, (
            "the writer is expected to merge into the stored JSON; if this "
            "changes to a replace, revisit the clear in run_full_scan"
        )

    def test_the_completion_write_does_not_resurrect_the_list(self):
        """A completed run reports "complete" — the banner must be empty by then
        for a scan that ran to the end, because a fresh run cleared it first."""
        body = _full_scan_source()
        tail = body.split('"status": status', 1)[1][:400]
        assert '"abandoned_artists": ' not in tail
