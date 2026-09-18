"""
Unified scan progress aggregation service.

Responsible for:
- Reading all scan progress files (scan_state)
- Merging active scans into a single API response
- Enhancing with in-memory stage progress (progress_tracker)
- Providing a stable contract for WebUI polling

This replaces legacy:
- unified_scan.get_scan_progress()
- _validate_and_cleanup_progress_file()
- scan_process_* checks in routes
"""

from __future__ import annotations

import os
from typing import Any

from services.scanning.scan_state import (
    read_progress_file,
)

from helpers.config_helpers import get_state_directory

from services.popularity.progress_tracker import get_state as get_tracker_state

# -------------------------------------------------------------------------
# Known scan types (extendable)
# -------------------------------------------------------------------------

SCAN_TYPES = [
    # "full_scan" first so the dashboard footer's primary scan (active[0])
    # is the artist-based full-scan progress, not the per-artist sub-pipeline
    # rows (navidrome/popularity/essentia) that flicker underneath it.
    "full_scan",
    "library_scan",
    "navidrome_scan",
    "popularity_scan",
    "singles_scan",
    "essentia_mood_scan",
    "combined_scan",
    "missing_releases_scan",
    "mp3_import",
]


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------

def _build_progress_path(scan_type: str) -> str:
    return os.path.join(get_state_directory(), f"{scan_type}_progress.json")


def _normalise_entry(scan_type: str, state: dict[str, Any]) -> dict[str, Any]:
    """
    Convert raw progress JSON into a consistent API entry.
    """

    # ── Counter aliasing ───────────────────────────────────────────────────
    # Writers disagree on the spelling: the full-scan orchestrator records
    # ``processed_artists``/``total_artists``, popularity uses
    # ``processed_artists`` too, and the in-memory tracker uses
    # ``processed_items``/``total_items``.  The dashboard renders the *_items
    # pair (``${scan.processed_items || 0}/${scan.total_items || "?"}``), so an
    # entry carrying only the artist counters rendered "0/?" for the entire
    # scan — the reported symptom.  Fall back across BOTH spellings here, at
    # the single point the API contract is built, rather than forcing every
    # writer to know which key the UI happens to read.
    #
    # ``api_popularity_status_compat`` (routes/scan_routes/api.py) already did
    # this fallback for its own response; doing it on the entry means every
    # consumer gets it, and the renderer no longer has to guess.
    #
    # Explicit ``is None`` checks, NOT ``or``: a legitimate ``0`` (scan just
    # started, or an empty artist list) must not be replaced by the next
    # candidate, and the artist counters are what a stale row would otherwise
    # win with.
    processed_items = state.get("processed_items")
    if processed_items is None:
        processed_items = state.get("processed_artists")
    total_items = state.get("total_items")
    if total_items is None:
        total_items = state.get("total_artists")

    return {
        "scan_type": state.get("scan_type") or scan_type,
        "is_running": bool(state.get("is_running", False)),
        "percent_complete": int(state.get("percent_complete", 0) or 0),
        "current_stage": state.get("current_stage"),
        "current_artist": state.get("current_artist"),
        "current_album": state.get("current_album"),
        "current_item": state.get("current_item"),

        # Progress counters
        "processed_artists": state.get("processed_artists"),
        "total_artists": state.get("total_artists"),
        "processed_items": processed_items,
        "total_items": total_items,

        # Optional extras
        "status": state.get("status"),
        "message": state.get("message"),
        "last_updated": state.get("last_updated"),

        # Per-artist failure / abandonment records (full-scan orchestrator
        # writes these when a bounded artist pipeline exceeds its budget or
        # raises) — the dashboard shows an investigation banner.
        "abandoned_artists": state.get("abandoned_artists") or {},
    }


def _merge_tracker_into_entry(entry: dict[str, Any]) -> None:
    """
    Enhance active scan with in-memory stage-level detail.

    Only applies to pipelines that use progress_tracker (popularity, etc.).
    """

    tracker = get_tracker_state()

    if not tracker.get("running"):
        return

    # Only merge into primary scan types (avoid polluting navidrome/library)
    #
    # NOTE: "full_scan" is DELIBERATELY absent. The tracker reports the
    # CURRENT artist's album fraction on a 5-95 scale, while the full-scan
    # orchestrator's percent_complete spans ALL artists on 0-100, and the
    # orchestrator's stage labels ("Metadata" / "Popularity" / "Singles
    # Detection" / "Essentia") are display strings where the tracker's are
    # technical ("album" / "finalising"). Merging would overwrite a
    # monotonic overall percentage with a per-artist one and downgrade the
    # stage label — i.e. exactly the "doesn't properly detail where the scan
    # is" symptom. The full-scan row already carries its own stage + item.
    if entry["scan_type"] not in {"popularity_scan", "library_scan", "combined_scan"}:
        return

    # Each field is only taken from the tracker when the tracker actually has
    # a value. A blanket ``update()`` copied ``None`` straight over a good
    # value, so a partially-populated tracker erased stage/item/counters that
    # the DB row had set correctly.
    for key in (
        "current_stage", "message", "current_item",
        "processed_items", "total_items",
    ):
        value = tracker.get(key)
        if value is not None:
            entry[key] = value

    # ``progress`` is 0 at the very start, and 0 is falsy — the previous
    # ``or entry.get(...)`` therefore masked a legitimate 0 with a stale
    # percentage. Prefer the tracker when it has advanced past 0, or when the
    # entry has no percentage of its own.
    tracker_progress = tracker.get("progress")
    if tracker_progress:
        entry["percent_complete"] = tracker_progress
    elif entry.get("percent_complete") is None:
        entry["percent_complete"] = tracker_progress or 0


# -------------------------------------------------------------------------
# Main API entrypoint
# -------------------------------------------------------------------------

def get_scan_progress() -> dict[str, Any]:
    """
    Return unified progress for all scans.

    Output structure:
    {
        "is_running": bool,
        "active_scan_count": int,
        "active_scans": [...],
    }
    """

    active_scans: list[dict[str, Any]] = []

    for scan_type in SCAN_TYPES:
        path = _build_progress_path(scan_type)

        state = read_progress_file(path)
        if not state:
            continue

        if not state.get("is_running", False):
            continue

        entry = _normalise_entry(scan_type, state)

        # Enhance with live tracker data where appropriate
        _merge_tracker_into_entry(entry)

        active_scans.append(entry)

    # ---------------------------------------------------------------------
    # Deduplicate by scan_type (safety for race conditions)
    # ---------------------------------------------------------------------

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()

    for entry in active_scans:
        scan_type = str(entry.get("scan_type") or "")
        if scan_type in seen:
            continue
        seen.add(scan_type)
        deduped.append(entry)

    active_scans = deduped

    return {
        "is_running": bool(active_scans),
        "active_scan_count": len(active_scans),
        "active_scans": active_scans,
    }