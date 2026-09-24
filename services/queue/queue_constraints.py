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

# ⚠️ Statuses the QUEUE PAGE displays — and therefore the set both its count
# pills and its list must be derived from.
#
# THE DEFECT THIS EXISTS TO PREVENT: the stats bar and the list were computed
# from two DIFFERENT definitions of "the queue", so they disagreed on screen.
#   * the "Queued" pill was a hand-written list in the CLIENT
#     (downloads.js:2239) that included ``unmatched`` / ``matched`` /
#     ``pending_match`` / ``discovered``;
#   * ``get_active_queue`` (the listing) filtered on
#     ACTIVE | FAILED | PENDING_RETRY, which excludes all four, AND added
#     ``source NOT IN ('local','discovered')``.
# Reported as "74 queued ... but the active queue shows 18 items". The pill
# counted a strict SUPERSET, so no amount of paging could ever reconcile them.
#
# ⚠️ THIS IS DELIBERATELY WIDER THAN ``ACTIVE_QUEUE_STATUSES`` and is NOT the
# same thing as "active". It includes the local-disk states (``unmatched``)
# because the count pills include them — the user's decision is that the LIST
# catches up to the count, so anything counted is now renderable.
#
# ⚠️ DO NOT use this for work decisions. ``get_active_queue`` remains the
# strict, source-filtered query that the slskd reaper and the folder matcher
# rely on; using this set there would treat a local-disk folder as an active
# transfer and cancel/mismatch it. This set is for DISPLAY only.
# ⚠️⚠️ THE QUEUE PAGE'S THREE CARDS, AS STATUS SETS — and the ONE source of
# truth for "how many are in each".
#
# THE DEFECT THIS EXISTS TO PREVENT: the page showed "74 queued / 0 active /
# 0 ready" above a list of 18 items, and adding files appeared to do nothing.
# The counts and the lists were computed from DIFFERENT definitions of "the
# queue": the "Queued" pill was a hand-written list inside the CLIENT
# (downloads.js / download-queue.js) that included ``unmatched``/``matched``/
# ``pending_match``/``discovered``, while the list came from ``get_active_queue``
# — which excludes all four AND drops every ``source IN
# ('local','discovered')`` row. The pill therefore counted a strict SUPERSET of
# what could ever be rendered, so NO amount of paging could reconcile them.
#
# THE INVARIANT (pinned by a test): the three sections must be pairwise
# DISJOINT, and ``QUEUE_DISPLAY_STATUSES`` is DEFINED as their union. Every
# row the page can show therefore belongs to exactly ONE card, and each card's
# count is that set's count — so a pill reading "N" always has exactly N rows
# beneath it. That is what makes the two halves unable to drift again.
#
# ⚠️ The membership below is chosen so each status sits in the section whose
# PILL counts it. In particular ``unmatched`` is in READY (not ACTIVE), which
# matches the UI: the "Completed & Ready to Organize" card is where un-matched
# disk folders have always been shown, with a warning badge. Putting it in
# ACTIVE would have recreated the bug in the opposite direction — counted under
# "Queued" but rendered by the Ready card.
FAILED_SECTION: frozenset[str] = frozenset({
    "failed",
})

#: Exactly what ``get_completed_queue`` selects, so the Ready card's list and
#: its count are the same set. ⚠️ Must stay a subset of the counted statuses —
#: ``imported``/``in_collection``/``awaiting_selection`` are NOT displayable.
READY_SECTION: frozenset[str] = frozenset(
    COMPLETED_QUEUE_STATUSES & (
        ACTIVE_QUEUE_STATUSES
        | COMPLETED_QUEUE_STATUSES
        | {"unmatched", "matched", "pending_match", "discovered"}
    )
)

#: In-flight work + parked-but-pending search work + the statuses the "Queued"
#: pill counts. Subtracting the other two sections keeps the three disjoint
#: (``moving`` is in ACTIVE_QUEUE_STATUSES *and* in COMPLETED_QUEUE_STATUSES).
ACTIVE_SECTION: frozenset[str] = frozenset(
    (
        ACTIVE_QUEUE_STATUSES
        | PENDING_RETRY_STATUSES
        | {"matched", "pending_match", "discovered"}
    )
    - READY_SECTION
    - FAILED_SECTION
)

#: Every status the queue page can display. DEFINED as the union of the three
#: sections — deliberately not written out by hand, so it cannot contain a
#: status that no card renders.
#:
#: ``removed``/``cancelled``/``deleted`` are deliberately EXCLUDED: they are
#: tombstones for rows the user deleted, no card shows them, and
#: ``get_failed_queue`` only ever returns ``failed``. Counting them (the
#: previous behaviour) inflated the Failed badge with rows that could not be
#: listed — the same count-vs-list defect in miniature.
QUEUE_DISPLAY_STATUSES: frozenset[str] = frozenset(
    ACTIVE_SECTION | READY_SECTION | FAILED_SECTION
)

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
    "QUEUE_DISPLAY_STATUSES",
    "ACTIVE_SECTION",
    "READY_SECTION",
    "FAILED_SECTION",
    "ALL_QUEUE_STATUSES",
    "STATUS_DISPLAY_CONFIG",
    "DEFAULT_STATUS_DISPLAY",
    "is_valid_queue_status",
    "get_status_display",
]