"""Two-step genre → playlist resync, with a background apply and live progress.

WHY THIS EXISTS
---------------
Changing the genre vocabulary, the selection policy or the playlist pooling
limit has two consequences that used to need two different tools:

    * ``tracks.genres`` (the Final Genres) is stale until something recomputes it
      — that is the Config page's "Genre Re-evaluation";
    * every genre playlist on Navidrome is stale until something rebuilds it —
      that used to need the undocumented ``/api/playlists/rebuild-all`` endpoint.

Neither alone fixes the reported symptom ("the playlists don't match the Final
Genres"), because the genre has to move FIRST and the playlist has to follow.
This service does both as one job, and reports what it did.

THE TWO STEPS
-------------
``preview_genre_playlist_resync()`` — read-only. Computes the genre changes that
would be written, then asks the REAL pool builder for the ordered list each
genre playlist should contain (``plan_only``), compares that against what
Navidrome currently holds, and reports:

    * per-track genre changes (before → after);
    * per-playlist membership changes (gains / losses / clears).

``apply_genre_playlist_resync()`` — the background job. Writes the genre
changes, then rebuilds the playlists, reporting progress as it goes.

⭐ THE PREVIEW USES THE SAME CODE AS THE APPLY.
The playlist part of the preview is ``_create_genre_top_track_playlists(...,
plan_only=True)``, not a second implementation. Two copies of "which tracks
belong to which genre" is exactly how a preview starts disagreeing with the
apply it is meant to describe — and the user would then be asked to approve one
thing and receive another.

PROGRESS
--------
Written to the shared ``scan_states`` row (``genre_playlist_resync``), so the
dashboard's existing scan-progress poller renders it with no new transport. Each
phase writes its own counters, so a long library walk is visible rather than a
silent spinner.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

__all__ = [
    "preview_genre_playlist_resync",
    "apply_genre_playlist_resync",
    "SCAN_TYPE",
    "is_resync_running",
]

#: The ``scan_states`` row this job writes. Kept apart from real scan types so it
#: cannot be mistaken for a scan, resumed, or picked up by the scan scheduler.
SCAN_TYPE = "genre_playlist_resync"

_PROGRESS_FILE = f"{SCAN_TYPE}_progress.json"

#: Phrases the phases report. Short: they render in the dashboard's one-line
#: status bar next to the percentage.
PHASE_GENRES = "Comparing genres"
PHASE_APPLY = "Writing genres"
PHASE_PLAYLISTS = "Rebuilding playlists"
PHASE_DONE = "Complete"


# ---------------------------------------------------------------------------
# Progress plumbing
# ---------------------------------------------------------------------------

def _write_progress(
    *,
    running: bool,
    percent: float,
    phase: str,
    processed: int = 0,
    total: int = 0,
    detail: str = "",
    status: str | None = None,
    result: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    """Publish one progress frame. Never fatal — a progress write must not
    abort the job it is describing."""
    try:
        from services.scanning.scan_state import write_progress_with_current_artist

        extra: dict[str, Any] = {
            "status": status or ("running" if running else "complete"),
            "mode": "genre_playlist_resync",
            "percent_complete": max(0.0, min(100.0, float(percent))),
            "processed_items": int(processed),
            "total_items": int(total),
            "processed_artists": int(processed),
            "total_artists": int(total),
            "current_stage": phase,
            "current_item": detail,
        }
        if result is not None:
            extra["resync_result"] = result
        if error:
            extra["error"] = error
        write_progress_with_current_artist(
            _PROGRESS_FILE, SCAN_TYPE, running, extra=extra
        )
    except Exception as exc:
        logger.debug("Genre/playlist resync progress write failed", error=str(exc))


def is_resync_running() -> bool:
    """True while a resync job holds the progress row."""
    try:
        from services.scanning.scan_state import read_progress_file

        state = read_progress_file(_PROGRESS_FILE) or {}
        return bool(state.get("is_running"))
    except Exception:
        return False


def _clear_progress() -> None:
    try:
        from services.scanning.scan_state import clear_progress_file

        clear_progress_file(_PROGRESS_FILE)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Lagging guard
# ---------------------------------------------------------------------------

_APPLY_LOCK = __import__("threading").Lock()
_APPLY_RUNNING = False


def _claim_apply() -> bool:
    global _APPLY_RUNNING
    with _APPLY_LOCK:
        if _APPLY_RUNNING:
            return False
        _APPLY_RUNNING = True
        return True


def _release_apply() -> None:
    global _APPLY_RUNNING
    with _APPLY_LOCK:
        _APPLY_RUNNING = False


# ---------------------------------------------------------------------------
# Step 1: the genre comparison
# ---------------------------------------------------------------------------

def _genre_changes(dry_run: bool):
    """Genre changes from stored evidence. Thin wrapper over the resync service.

    Delegated rather than reimplemented: "what should this track's genres be"
    has exactly one answer, and it lives in ``genre_resync_service``.
    """
    from services.enrichment.genre_resync_service import resync_genres

    return resync_genres(dry_run=dry_run)


# ---------------------------------------------------------------------------
# Step 2: the playlist comparison
# ---------------------------------------------------------------------------

def _plan_playlists(proposed_by_id: dict[str, str] | None = None) -> dict[str, list[str]]:
    """The ordered song list each genre playlist SHOULD contain, per Navidrome.

    Computed by the real builder in ``plan_only`` mode, so it cannot drift from
    what an apply would write.

    ⭐ ``proposed_by_id`` OVERLAYS the genres the apply is about to write.

    Without it the prediction pools by the genres still in the database, which
    describes the state BEFORE the job and would under-report exactly the
    changes the preview exists to show. The overlay is the whole point: a
    preview must predict the playlists that will exist AFTER the genre rewrite.
    """
    from services.popularity.stages.finalise_stage import (
        _create_genre_top_track_playlists,
        _fetch_genre_playlist_rows,
    )
    from services.popularity.stages.finalise_stage import _genre_refresh_config

    plan: dict[str, list[str]] = {}
    rows_override = None
    if proposed_by_id:
        try:
            min_stars, _max_genres = _genre_refresh_config()
            rows_override = _fetch_genre_playlist_rows(min_stars)
            for row in rows_override:
                track_id = str(row.get("id") or "")
                if track_id in proposed_by_id:
                    row["genres"] = proposed_by_id[track_id]
        except Exception as exc:
            logger.debug(
                "Genre/playlist resync: could not overlay proposed genres", error=str(exc)
            )
            rows_override = None

    _create_genre_top_track_playlists(
        plan_only=True, plan_out=plan, rows_override=rows_override
    )
    return plan


def _server_playlists() -> dict[str, list[str]]:
    """Current ordered contents of every Popularr-managed playlist, by name.

    One listing call plus one content fetch per playlist. A playlist whose
    contents cannot be read is OMITTED rather than assumed empty, so an
    unreadable playlist never shows up as "everything would be removed".

    ⚠️ DELIBERATELY NOT ``finalise_stage._navidrome_clients``.

    That factory is a PLAYLIST-ONLY scope guard: it collapses to one client per
    SERVER, which is correct for playlist writes (server-scoped) but would
    silently stop a second account syncing anything per-USER. A test pins its
    caller set to ``finalise_stage.py`` alone. A read-only comparison does not
    need that dedupe, so it builds one plain client from the configured account
    rather than widening the guard.
    """
    from services.playlists.playlist_navidrome_service import (
        _current_song_ids,
    )

    out: dict[str, list[str]] = {}
    try:
        from api_clients.navidrome import NavidromeClient
        from helpers.config_helpers import get_navidrome_users_normalized

        users = [
            u for u in (get_navidrome_users_normalized() or [])
            if str(u.get("base_url") or "").strip()
        ]
    except Exception as exc:
        logger.debug("Genre/playlist resync: Navidrome is not configured", error=str(exc))
        return out

    if not users:
        return out

    # ONE account is enough: playlist contents are server-scoped, so a second
    # account would return the same lists.
    user = users[0]
    try:
        client = NavidromeClient(
            base_url=user.get("base_url", ""),
            username=user.get("user", user.get("username", "")),
            password=user.get("pass", user.get("password", "")),
        )
    except Exception as exc:
        logger.debug("Genre/playlist resync: client build failed", error=str(exc))
        return out

    try:
        listings = client.fetch_all_playlists() or []
    except Exception as exc:
        logger.debug("Genre/playlist resync: playlist listing failed", error=str(exc))
        return out

    for entry in listings:
        name = str(entry.get("name") or "").strip()
        if not name or name in out:
            continue
        ids = _current_song_ids(client, str(entry.get("id") or ""))
        if ids is None:
            continue
        out[name] = list(ids)
    return out


def _diff(after: list[str], before: list[str]) -> dict[str, Any]:
    """Gains/losses between two ordered song-id lists."""
    before_set = set(before)
    after_set = set(after)
    return {
        "added": [i for i in after if i not in before_set],
        "removed": [i for i in before if i not in after_set],
        "count_before": len(before),
        "count_after": len(after),
        "order_changed": after != before,
    }


# ---------------------------------------------------------------------------
# Public: preview
# ---------------------------------------------------------------------------

def preview_genre_playlist_resync(*, limit_tracks: int | None = None) -> dict[str, Any]:
    """Report what an apply WOULD change. Writes nothing, anywhere.

    ⚠️ DELIBERATELY WRITES NO PROGRESS ROW. The shared row is the APPLY job's
    status: publishing a "complete" frame from a preview would make the
    dashboard announce a finished resync (and the Config card's poller restore
    itself with the wrong summary) every time someone pressed Preview. The
    preview is a synchronous request, so its caller renders the result directly.

    Returns::

        {"ok", "seconds",
         "genre_changes": {"changed", "tracks", "with_evidence", "tracks_detail"},
         "playlist_changes": [{name, added, removed, count_before, count_after,
                               is_new, is_clear, order_changed}],
         "summary": {playlists_touched, playlists_cleared, playlists_created,
                     tracks_gained, tracks_removed}}
    """
    started = time.monotonic()

    try:
        genre_result = _genre_changes(dry_run=True)
    except Exception as exc:
        logger.error("Genre/playlist resync preview failed at the genre step", error=str(exc))
        return {"ok": False, "error": f"genre step failed: {exc}"}

    changed_tracks = list(genre_result.get("changed_tracks") or [])
    if limit_tracks:
        changed_tracks = changed_tracks[: int(limit_tracks)]

    # The genres the apply would write, so the playlist prediction describes the
    # state AFTER the rewrite rather than the one it is about to replace.
    proposed_by_id = {
        str(t.get("track_id") or ""): str(t.get("after") or "")
        for t in changed_tracks
        if str(t.get("track_id") or "")
    }

    try:
        planned = _plan_playlists(proposed_by_id)
    except Exception as exc:
        logger.error("Genre/playlist resync preview failed at the playlist step", error=str(exc))
        return {"ok": False, "error": f"playlist step failed: {exc}"}

    current = _server_playlists()

    playlist_changes: list[dict[str, Any]] = []
    totals = {
        "playlists_touched": 0,
        "playlists_cleared": 0,
        "playlists_created": 0,
        "tracks_gained": 0,
        "tracks_removed": 0,
    }

    for name, after in sorted(planned.items()):
        before = current.get(name, [])
        is_new = name not in current
        is_clear = not after
        delta = _diff(after, before)
        if not is_new and not delta["added"] and not delta["removed"]:
            # Order-only changes are NOT reported: the weekly shuffle rotates a
            # chart on purpose, so flagging the rotation as a pending change
            # would make every preview look busy for no reason.
            continue
        totals["playlists_touched"] += 1
        if is_clear:
            totals["playlists_cleared"] += 1
        if is_new:
            totals["playlists_created"] += 1
        totals["tracks_gained"] += len(delta["added"])
        totals["tracks_removed"] += len(delta["removed"])
        playlist_changes.append(
            {"name": name, "is_new": is_new, "is_clear": is_clear, **delta}
        )

    # A playlist that EXISTS on the server and has tracks, but is not in the
    # plan at all, is one this pass would stop managing. Reported separately:
    # the apply does not delete it (the orphan sweep owns deletion).
    unmanaged = sorted(
        name for name in current if name not in planned and current[name]
    )

    elapsed = round(time.monotonic() - started, 2)

    return {
        "ok": True,
        "dry_run": True,
        "seconds": elapsed,
        "genre_changes": {
            "changed": int(genre_result.get("changed") or 0),
            "tracks": int(genre_result.get("tracks") or 0),
            "with_evidence": int(genre_result.get("with_evidence") or 0),
            "tracks_detail": changed_tracks,
        },
        "playlist_changes": playlist_changes,
        "unmanaged_playlists": unmanaged,
        "summary": totals,
    }


# ---------------------------------------------------------------------------
# Public: apply (background)
# ---------------------------------------------------------------------------

def apply_genre_playlist_resync(*, limit_tracks: int | None = None) -> dict[str, Any]:
    """Write the genre changes, then rebuild the genre playlists.

    SYNC function, meant to run on a worker thread. Progress is published to the
    shared ``scan_states`` row so the dashboard can render it; the return value
    is the same summary the preview produces plus what was actually written.

    ⭐ GENRES FIRST, PLAYLISTS SECOND. The playlists are built from the Final
    Genres, so rebuilding them before the new genres are written would sync the
    OLD membership back and the job would appear to do nothing.
    """
    if not _claim_apply():
        return {"ok": False, "error": "a resync is already running"}

    started = time.monotonic()
    try:
        _write_progress(
            running=True, percent=2.0, phase=PHASE_APPLY,
            detail="Recomputing genres from stored evidence…", status="running",
        )

        # ── Phase 1: compute + write the genres ────────────────────────────
        genre_result = _genre_changes(dry_run=False)
        if not genre_result.get("ok", True):
            raise RuntimeError(genre_result.get("error") or "genre step failed")
        changed = int(genre_result.get("changed") or 0)
        _write_progress(
            running=True, percent=50.0, phase=PHASE_APPLY,
            processed=changed, total=changed,
            detail=f"{changed} track genre set(s) updated", status="running",
        )

        # ── Phase 2: rebuild the playlists ────────────────────────────────
        # The genre playlists are derived data: after a library-wide genre
        # rewrite every one of them is potentially stale, so this rebuilds them
        # all rather than trying to guess an affected subset.
        _write_progress(
            running=True, percent=55.0, phase=PHASE_PLAYLISTS,
            detail="Rebuilding genre playlists…", status="running",
        )
        from services.popularity.stages.finalise_stage import (
            _create_genre_top_track_playlists,
            invalidate_genre_playlist_rows,
        )

        # The row cache is keyed on the tracks table and was filled BEFORE the
        # genre rewrite above; without this the rebuild would pool by the OLD
        # genres and re-push the very membership the job exists to correct.
        invalidate_genre_playlist_rows()

        written = _create_genre_top_track_playlists(cleanup_orphans=True)

        elapsed = round(time.monotonic() - started, 2)
        result = {
            "genres_changed": changed,
            "playlists_written": int(written or 0),
            "seconds": elapsed,
        }
        _write_progress(
            running=False, percent=100.0, phase=PHASE_DONE,
            processed=changed, total=changed,
            detail=(
                f"{changed} track genre set(s) · "
                f"{result['playlists_written']} playlist(s) synced"
            ),
            status="complete", result=result,
        )
        logger.info(
            "Genre/playlist resync complete",
            genres_changed=changed,
            playlists_written=result["playlists_written"],
            seconds=elapsed,
        )
        return {"ok": True, **result}

    except Exception as exc:
        logger.error("Genre/playlist resync failed", error=str(exc), exc_info=True)
        _write_progress(
            running=False, percent=0.0, phase="Failed",
            status="failed", error=str(exc),
        )
        return {"ok": False, "error": str(exc)}
    finally:
        _release_apply()
