"""Regression tests for the full-scan progress panel.

Symptom
-------
During a dashboard "All" (full) scan the Active Progress panel showed::

    Popularity Scan    0/?
    Venues - Aspire

The stage/counter line never advanced and the panel mislabelled the scan.

Root causes (all verified against the real functions)
-----------------------------------------------------
1. **Counter key mismatch.** The full-scan orchestrator writes
   ``processed_artists`` / ``total_artists``, but the panel renders
   ``processed_items`` / ``total_items``, so the counter was always ``0/?``.
   ``_normalise_entry`` read only the ``*_items`` spellings.

2. **The tracker merge skipped ``full_scan``.** The in-memory tracker *does*
   hold processed/total counts, but ``_merge_tracker_into_entry`` only merged
   for ``{"popularity_scan", "library_scan", "combined_scan"}``.

3. **The tracker merge used ``or``**, so a legitimate ``0`` was treated as
   missing and a stale percentage won; and a blanket ``update()`` copied
   ``None`` over good values.

4. **Two writers, two percentage scales, one row.** The orchestrator reports
   0-100 across ALL artists; the per-album checkpoint in
   ``scan_stage_runner`` wrote its own 5-95 per-artist percentage to the SAME
   ``full_scan`` row, so the bar fell back to ~5% at every new artist.

5. **The rebuilt JS had no ``full_scan`` display name**, so the panel rendered
   the raw scan type instead of "Full Scan".

These tests pin 1-5 without needing a live scan.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from services.popularity import progress_tracker
from services.scanning.pipelines.progress_service import (
    _merge_tracker_into_entry,
    _normalise_entry,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _render_counter(entry: dict) -> str:
    """Mirror the dashboard's counter cell: `processed_items/total_items`."""
    processed = entry.get("processed_items")
    total = entry.get("total_items")
    processed = 0 if processed is None else processed
    total = "?" if total is None else total
    return f"{processed}/{total}"


@pytest.fixture(autouse=True)
def _reset_tracker():
    """Leave the shared tracker idle so tests cannot leak into each other."""
    progress_tracker._state.update({
        "running": False,
        "current_stage": None,
        "progress": 0,
        "message": "",
        "current_item": None,
        "processed_items": None,
        "total_items": None,
    })
    yield
    progress_tracker._state.update({
        "running": False,
        "current_stage": None,
        "progress": 0,
        "message": "",
        "current_item": None,
        "processed_items": None,
        "total_items": None,
    })


#: A full-scan progress row exactly as the orchestrator writes it mid-scan.
FULL_SCAN_STATE = {
    "scan_type": "full_scan",
    "is_running": True,
    "status": "running",
    "current_artist": "Venues",
    "current_item": "Venues - Aspire",
    "current_stage": "Popularity",
    "percent_complete": 17,
    "processed_artists": 12,
    "total_artists": 80,
}


# ---------------------------------------------------------------------------
# 1. The counter is no longer "0/?"
# ---------------------------------------------------------------------------

def test_artist_counters_populate_the_item_counters():
    """The reported bug: the panel rendered "0/?" for the whole scan."""
    entry = _normalise_entry("full_scan", FULL_SCAN_STATE)
    assert entry["processed_items"] == 12
    assert entry["total_items"] == 80
    assert _render_counter(entry) == "12/80"


def test_artist_counters_are_still_exposed():
    """The *_artists keys must survive — the abandoned-artist banner uses them."""
    entry = _normalise_entry("full_scan", FULL_SCAN_STATE)
    assert entry["processed_artists"] == 12
    assert entry["total_artists"] == 80


def test_explicit_item_counters_win_over_artist_counters():
    """When both are present the *_items values are authoritative.

    A popularity row writes ``processed_items`` itself; the alias must not
    overwrite it with the artist count.
    """
    state = dict(FULL_SCAN_STATE, processed_items=7, total_items=9)
    entry = _normalise_entry("full_scan", state)
    assert entry["processed_items"] == 7
    assert entry["total_items"] == 9


def test_zero_counts_are_preserved_not_treated_as_missing():
    """A legitimate 0 must not fall through to `?`.

    Uses ``is None`` rather than ``or``: with ``or`` a 0 would be replaced by
    the fallback candidate, and at scan start every counter IS 0.
    """
    state = {
        "scan_type": "full_scan",
        "is_running": True,
        "percent_complete": 0,
        "processed_artists": 0,
        "total_artists": 80,
    }
    entry = _normalise_entry("full_scan", state)
    assert entry["processed_items"] == 0
    assert entry["total_items"] == 80
    assert _render_counter(entry) == "0/80"


def test_genuinely_unknown_total_still_renders_question_mark():
    """With no total anywhere the panel should still say so, not invent one."""
    state = {"scan_type": "full_scan", "is_running": True, "percent_complete": 5}
    entry = _normalise_entry("full_scan", state)
    assert entry["processed_items"] is None
    assert entry["total_items"] is None
    assert _render_counter(entry) == "0/?"


# ---------------------------------------------------------------------------
# 2 & 3. Tracker merge semantics
# ---------------------------------------------------------------------------

def test_tracker_merge_does_not_touch_full_scan():
    """``full_scan`` must NOT take the tracker's stage or percentage.

    The orchestrator owns the overall 0-100 percentage and the display stage
    labels; the tracker reports the current artist's album fraction and a
    technical stage name. Merging them made the bar jump backwards, which is
    the "doesn't properly detail where the scan is" symptom.
    """
    progress_tracker._state.update({
        "running": True, "current_stage": "album", "progress": 3,
        "message": "Preparing something else", "current_item": "other",
        "processed_items": 1, "total_items": 2,
    })
    entry = _normalise_entry("full_scan", FULL_SCAN_STATE)
    _merge_tracker_into_entry(entry)

    assert entry["percent_complete"] == 17, "overall % must survive the merge"
    assert entry["current_stage"] == "Popularity", "display stage must survive"
    assert entry["current_item"] == "Venues - Aspire"
    assert entry["processed_items"] == 12
    assert entry["total_items"] == 80


def test_tracker_still_enriches_popularity_scan():
    """The merge must keep working for the scan types it DOES cover."""
    progress_tracker._state.update({
        "running": True, "current_stage": "album", "progress": 42,
        "message": "Preparing X", "current_item": "X", "processed_items": 7,
        "total_items": 9,
    })
    entry = _normalise_entry("popularity_scan", {
        "scan_type": "popularity_scan", "is_running": True, "percent_complete": 5,
    })
    _merge_tracker_into_entry(entry)
    assert entry["percent_complete"] == 42
    assert entry["current_stage"] == "album"
    assert _render_counter(entry) == "7/9"


def test_tracker_none_does_not_clobber_good_values():
    """A partially-populated tracker must not erase values the row already has."""
    progress_tracker._state.update({
        "running": True, "current_stage": None, "progress": 0,
        "message": None, "current_item": None,
        "processed_items": None, "total_items": None,
    })
    entry = _normalise_entry("popularity_scan", {
        "scan_type": "popularity_scan", "is_running": True,
        "percent_complete": 33, "current_stage": "albums", "current_item": "keep me",
        "processed_items": 3, "total_items": 10,
    })
    _merge_tracker_into_entry(entry)
    assert entry["percent_complete"] == 33
    assert entry["current_stage"] == "albums"
    assert entry["current_item"] == "keep me"
    assert entry["processed_items"] == 3
    assert entry["total_items"] == 10


# ---------------------------------------------------------------------------
# 4. Percentage ownership on the shared full_scan row
# ---------------------------------------------------------------------------

def test_checkpoint_write_does_not_override_full_scan_percentage():
    """The per-album checkpoint must not write its own % to the full_scan row.

    Source-level assertion: the two writers use different scales, and the
    checkpoint runs for every artist, so it reset the bar repeatedly.
    """
    src = (REPO_ROOT / "services" / "popularity" / "scan_stage_runner.py").read_text(
        encoding="utf-8"
    )
    # Locate the checkpoint write block.
    marker = "_row_scan_type = \"full_scan\" if effective_stop_file"
    assert marker in src, "the scan_stage_runner checkpoint block moved; re-point this test"
    block = src.split(marker, 1)[1][:1200]
    assert 'if _row_scan_type == "full_scan":' in block, (
        "the full_scan branch must be special-cased so it does not write "
        "percent_complete (the orchestrator owns that value)"
    )
    # The full_scan branch's extra dict must not carry percent_complete.
    full_branch = block.split('if _row_scan_type == "full_scan":', 1)[1]
    full_branch = full_branch.split("else:", 1)[0]
    assert "percent_complete" not in full_branch, (
        "the full_scan checkpoint must not set percent_complete"
    )


def test_full_scan_writer_records_item_counters():
    """The orchestrator must write the counters the panel actually renders."""
    src = (
        REPO_ROOT / "services" / "scanning" / "pipelines" / "popularity_pipeline.py"
    ).read_text(encoding="utf-8")
    assert '"processed_items": _i' in src
    assert '"total_items": total' in src


# ---------------------------------------------------------------------------
# 5. Display names
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "rel_path",
    ["static/js/dashboard.js", "test_site/static/js/pages/dashboard.js"],
)
def test_both_dashboards_label_full_scan(rel_path: str):
    """Neither tree may render the raw `full_scan` string."""
    src = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
    assert re.search(r"full_scan:\s*'Full Scan'", src) or re.search(
        r'full_scan:\s*"Full Scan"', src
    ), f"{rel_path} has no display name for full_scan"


@pytest.mark.parametrize(
    "rel_path",
    ["static/js/dashboard.js", "test_site/static/js/pages/dashboard.js"],
)
def test_dashboards_use_nullish_coalescing_for_the_counter(rel_path: str):
    """`||` renders a legitimate 0 total as `?`; `??` does not."""
    src = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
    counter_lines = [
        line for line in src.splitlines()
        if "processed_items" in line and "total_items" in line
    ]
    assert counter_lines, f"{rel_path}: counter line not found"
    for line in counter_lines:
        assert "??" in line, (
            f"{rel_path}: use nullish coalescing so a 0 total is not shown as '?': {line.strip()}"
        )
