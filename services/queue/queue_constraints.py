"""
Queue constants and status definitions.

Single source of truth for:

- Queue status values
- Queue status groups
- Status display metadata
- Queue status validation

Do not duplicate queue status strings elsewhere.
Import from this module instead.
"""

from __future__ import annotations

# =============================================================================
# QUEUE STATUS GROUPS
# =============================================================================

# Active queue = ONLY active search/download/transfer work.  ``unmatched`` /
# ``discovered`` are passive local-disk states (folders waiting in the
# Matched Folders section) and must NEVER appear in the active queue — local
# disk folders are injected into ``download_queue`` as ``unmatched`` by the
# watcher/discovery flow, and allowing them here bleeds ambient disk folders
# into the search/download queue (the "strict queue vs. local disk" boundary).
ACTIVE_QUEUE_STATUSES: frozenset[str] = frozenset({
    "queued",
    "searching",
    "processing",
    "downloading",
    "queried",
    "copy_recommended",
    "moving",
})

PROCESSING_STATUSES: frozenset[str] = frozenset({
    "queued",
    "searching",
    "processing",
    "downloading",
})

MATCHABLE_QUEUE_STATUSES: frozenset[str] = frozenset({
    "queued",
    "searching",
    "downloading",
    "matched",
    "completed",
    "unmatched",
    "queried",
    "discovered",
    "pending_match",
    "possible_duplicate",
    "duplicate",
})

COMPLETED_QUEUE_STATUSES: frozenset[str] = frozenset({
    "completed",
    "unmatched",
    "possible_duplicate",
    "moving",
})

COLLECTION_STATUSES: frozenset[str] = frozenset({
    "completed",
    "imported",
    "in_collection",
})

FAILED_STATUSES: frozenset[str] = frozenset({
    "failed",
    "removed",
    "cancelled",
    "deleted",
})

# Statuses where an item is parked waiting for its retry window before it
# returns to the active queue automatically (``backed_off`` = a search or
# download failed and the item is cooling off; ``pending_release`` = awaiting
# the release date).  These are PENDING states — never terminal ``failed``.
PENDING_RETRY_STATUSES: frozenset[str] = frozenset({
    "backed_off",
    "pending_release",
})

TERMINAL_QUEUE_STATUSES: frozenset[str] = frozenset({
    "completed",
    "failed",
    "imported",
    "in_collection",
    "removed",
    "cancelled",
    "deleted",
})

# ⚠️ Statuses a row must have to BLOCK re-queueing the same track/release.
#
# This is deliberately NOT ``ACTIVE_QUEUE_STATUSES``. ``unmatched`` is neither
# active nor terminal (a local-disk folder waiting to be matched) yet it must
# still block, because re-queueing a track that is sitting on disk unmatched
# would download a duplicate.
#
# ⚠️ AND IT DELIBERATELY EXCLUDES EVERY TERMINAL STATUS. Including them was a
# real defect with two reported symptoms:
#
#   * ``insert_queue_item`` deduped against ``completed``/``imported``/
#     ``in_collection``/``unmatched`` rows. Those rows are NOT shown in the
#     queue, so the user got "already in the queue" for a track they could not
#     see, and the new row was never inserted — "files I'm adding to download
#     aren't showing".
#   * ``add_release_tracks_to_queue_detailed`` skipped the whole release with
#     reason ``already_active`` for the same invisible rows — "a release
#     already has items in the queue, but they aren't there".
#
# An ``imported``/``completed`` row means the track is IN THE LIBRARY, which is
# a separate check (``find_library_track``) with its own, more accurate message.
# A ``removed``/``cancelled``/``deleted``/``failed`` row is explicitly one the
# user wants gone or retried, so it must never block a fresh add either.
BLOCKING_REQUEUE_STATUSES: frozenset[str] = frozenset({
    "queued",
    "searching",
    "processing",
    "downloading",
    "moving",
    "queried",
    "copy_recommended",
    "matched",
    "unmatched",
    "pending_match",
    "possible_duplicate",
    "duplicate",
    "backed_off",
    "pending_release",
})

# =============================================================================
# ALL VALID STATUSES
# =============================================================================

ALL_QUEUE_STATUSES: frozenset[str] = frozenset({
    "queued",
    "searching",
    "processing",
    "downloading",
    "matched",
    "completed",
    "failed",
    "unmatched",
    "moving",
    "queried",
    "copy_recommended",
    "possible_duplicate",
    "imported",
    "awaiting_selection",
    "in_collection",
    "removed",
    "cancelled",
    "deleted",
    "pending_match",
    "duplicate",
    "discovered",
    "backed_off",
    "pending_release",
})

# =============================================================================
# STATUS DISPLAY CONFIGURATION
# =============================================================================

STATUS_DISPLAY_CONFIG: dict[str, dict[str, str]] = {
    "queued": {
        "label": "Queued",
        "css": "bg-warning text-dark",
        "icon": "clock",
    },
    "searching": {
        "label": "Searching",
        "css": "bg-warning text-dark",
        "icon": "search",
    },
    "processing": {
        "label": "Processing",
        "css": "bg-info",
        "icon": "arrow-repeat",
    },
    "downloading": {
        "label": "Downloading",
        "css": "bg-primary",
        "icon": "download",
    },
    "completed": {
        "label": "Completed",
        "css": "bg-success",
        "icon": "check-circle",
    },
    "failed": {
        "label": "Failed",
        "css": "bg-danger",
        "icon": "x-circle",
    },
    "unmatched": {
        "label": "Unmatched",
        "css": "bg-warning text-dark",
        "icon": "exclamation-triangle",
    },
    "moving": {
        "label": "Moving",
        "css": "bg-info text-dark",
        "icon": "arrow-right-circle",
    },
    "queried": {
        "label": "Queried",
        "css": "bg-secondary",
        "icon": "question-circle",
    },
    "copy_recommended": {
        "label": "Copy Recommended",
        "css": "bg-info text-dark",
        "icon": "files",
    },
    "possible_duplicate": {
        "label": "Possible Duplicate",
        "css": "bg-secondary",
        "icon": "copy",
    },
    "imported": {
        "label": "Imported",
        "css": "bg-success",
        "icon": "check2-all",
    },
    "awaiting_selection": {
        "label": "Select File",
        "css": "bg-primary",
        "icon": "hand-index",
    },
    "in_collection": {
        "label": "In Collection",
        "css": "bg-success",
        "icon": "collection",
    },
    "matched": {
        "label": "Matched",
        "css": "bg-info text-dark",
        "icon": "check-circle",
    },
    "pending_match": {
        "label": "Pending Match",
        "css": "bg-secondary",
        "icon": "hourglass",
    },
    "duplicate": {
        "label": "Duplicate",
        "css": "bg-secondary",
        "icon": "copy",
    },
    "discovered": {
        "label": "Discovered",
        "css": "bg-info text-dark",
        "icon": "search",
    },
    "backed_off": {
        "label": "Pending",
        "css": "bg-secondary",
        "icon": "hourglass-split",
    },
    "pending_release": {
        "label": "Pre-Release",
        "css": "bg-warning text-dark",
        "icon": "calendar-event",
    },
}

DEFAULT_STATUS_DISPLAY: dict[str, str] = {
    "label": "Unknown",
    "css": "bg-secondary",
    "icon": "question",
}

# =============================================================================
# HELPERS
# =============================================================================

def is_valid_queue_status(status: str | None) -> bool:
    """
    Return True if status is a recognised queue status.
    """

    return bool(status) and status in ALL_QUEUE_STATUSES


def get_status_display(status: str | None) -> dict[str, str]:
    """
    Return display configuration for a queue status.
    """

    if not status:
        return DEFAULT_STATUS_DISPLAY.copy()

    return STATUS_DISPLAY_CONFIG.get(
        status,
        {
            **DEFAULT_STATUS_DISPLAY,
            "label": status,
        },
    )


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    "ACTIVE_QUEUE_STATUSES",
    "PROCESSING_STATUSES",
    "MATCHABLE_QUEUE_STATUSES",
    "COMPLETED_QUEUE_STATUSES",
    "COLLECTION_STATUSES",
    "FAILED_STATUSES",
    "PENDING_RETRY_STATUSES",
    "TERMINAL_QUEUE_STATUSES",
    "ALL_QUEUE_STATUSES",
    "STATUS_DISPLAY_CONFIG",
    "DEFAULT_STATUS_DISPLAY",
    "is_valid_queue_status",
    "get_status_display",
]