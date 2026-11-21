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

import re
import time
import unicodedata
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

__all__ = [
    "preview_genre_playlist_resync",
    "start_preview_genre_playlist_resync",
    "apply_genre_playlist_resync",
    "SCAN_TYPE",
    "PREVIEW_SCAN_TYPE",
    "PREVIEW_PROGRESS_FILE",
    "is_resync_running",
    "is_preview_running",
]

#: The ``scan_states`` row this job writes. Kept apart from real scan types so it
#: cannot be mistaken for a scan, resumed, or picked up by the scan scheduler.
SCAN_TYPE = "genre_playlist_resync"

_PROGRESS_FILE = f"{SCAN_TYPE}_progress.json"

#: ⚠️ THE PREVIEW GETS ITS OWN ROW, never the apply's.
#:
#: A preview must not make the dashboard announce a finished resync, nor let the
#: Config card's poller restore itself with a summary for a job that never ran —
#: which is why the apply row is off limits. But a full-library preview is ALSO
#: long enough to outlive the proxy (~100s on 69k tracks, measured), so it cannot
#: stay a synchronous request either: the gateway answers with its own HTML error
#: page and the browser's ``resp.json()`` dies on ``Unexpected token '<'``. A
#: dedicated row lets the preview report progress and be polled without touching
#: the apply's state.
PREVIEW_SCAN_TYPE = "genre_playlist_resync_preview"
PREVIEW_PROGRESS_FILE = f"{PREVIEW_SCAN_TYPE}_progress.json"

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
    scan_type: str = SCAN_TYPE,
    progress_file: str = _PROGRESS_FILE,
) -> None:
    """Publish one progress frame. Never fatal — a progress write must not
    abort the job it is describing.

    ``scan_type``/``progress_file`` default to the APPLY row; the background
    preview passes its own pair so the two never overwrite each other.
    """
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
            progress_file, scan_type, running, extra=extra
        )
    except Exception as exc:
        logger.debug("Genre/playlist resync progress write failed", error=str(exc))


#: A progress row older than this is treated as a DEAD job, not a running one.
#:
#: The job is a daemon thread, so a restart (or a killed worker) leaves its last
#: row saying ``is_running: true`` forever — and because that row is the only
#: "is running" signal, every retry answered 409 even though nothing was running.
#: The live job now publishes at least every few seconds (the walk every 500
#: tracks, the save every chunk, the file writes every 2s), so a quarter of an
#: hour of silence means the writer is gone.
_PROGRESS_STALE_SECONDS = 15 * 60


def _progress_row_is_stale(
    state: dict[str, Any], stale_after: float = _PROGRESS_STALE_SECONDS
) -> bool:
    """True when a row claims to be running but has not been touched recently."""
    if not state.get("is_running"):
        return False
    raw = str(state.get("last_updated") or "").strip()
    if not raw:
        # No timestamp: do not invent staleness (older rows may predate it).
        return False
    try:
        from datetime import datetime

        updated = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return False
    now = datetime.now(updated.tzinfo) if updated.tzinfo else datetime.now()
    return (now - updated).total_seconds() > stale_after


def is_preview_running() -> bool:
    """True while the background PREVIEW holds its own progress row."""
    try:
        from services.scanning.scan_state import read_progress_file

        state = read_progress_file(PREVIEW_PROGRESS_FILE) or {}
        return bool(state.get("is_running")) and not _progress_row_is_stale(state)
    except Exception:
        return False


def is_resync_running() -> bool:
    """True while a resync job holds the progress row.

    A stale row (last frame longer ago than ``_PROGRESS_STALE_SECONDS``) is a
    dead job, so the caller may start a new one instead of waiting forever.
    """
    try:
        from services.scanning.scan_state import read_progress_file

        state = read_progress_file(_PROGRESS_FILE) or {}
        return bool(state.get("is_running")) and not _progress_row_is_stale(state)
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

def _genre_changes(dry_run: bool, on_progress=None):
    """Genre changes from stored evidence. Thin wrapper over the resync service.

    Delegated rather than reimplemented: "what should this track's genres be"
    has exactly one answer, and it lives in ``genre_resync_service``.
    """
    from services.enrichment.genre_resync_service import resync_genres

    kwargs = {"dry_run": dry_run}
    if on_progress is not None:
        kwargs["on_progress"] = on_progress
    return resync_genres(**kwargs)


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


def _playlist_name_key(name: Any) -> str:
    """The key two playlist NAMES must agree on to be the same playlist.

    ⭐⭐ EXACT-STRING MATCHING MADE EXISTING PLAYLISTS LOOK NEW.

    The preview decided "is this a create?" with ``name in server_counts`` — an
    exact, case-sensitive test. Any difference between the name the PLAN
    generates and the name Navidrome holds therefore reported a CREATE over a
    playlist that was already there:

        Experimental – Top Tracks     (server, 81 tracks)  "new, 0 -> 69"
        Stoner Metal – Top Tracks     (server, 42 tracks)  "new, 0 -> 42"
        Symphonic Rock – Top Tracks   (server, 56 tracks)  "new, 0 -> 61"

    Applying that would have created a second playlist beside each one.

    Normalisation folds everything that is not part of a playlist's identity:
    case, surrounding whitespace, dash variants (``-``/``–``/``—``), and runs
    of internal whitespace. It deliberately does NOT strip words — "Top Tracks"
    versus "Great Covers" must stay different.

    ⚠️ THE RISK THIS ACCEPTS: two playlists whose names differ only in case or
    punctuation are treated as one. For MANAGED genre playlists that is the
    desired behaviour — the name is generated from a template, so a variant
    spelling is the same slot. It is also why this normaliser is used ONLY for
    the managed-genre comparison, never for deleting user playlists.
    """
    text = str(name or "").strip().casefold()
    if not text:
        return ""
    # Unicode-normalise first so accented templates compare consistently.
    text = unicodedata.normalize("NFKC", text)
    # Dash variants: hyphen, en dash, em dash, minus sign.
    text = re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]", "-", text)
    # Collapse whitespace runs and drop the spaces AROUND the separator, so
    # "Stoner Metal - Top Tracks" and "Stoner Metal-Top Tracks" agree.
    text = re.sub(r"\s*-\s*", "-", text)
    return re.sub(r"\s+", " ", text).strip()


def _server_playlists(
    only: set[str] | None = None,
) -> tuple[dict[str, list[str]], dict[str, int]]:
    """Current playlist contents, plus every playlist's declared size.

    Returns ``(contents_by_name, count_by_name)``.

    ⚠️ CONTENTS ARE FETCHED ONLY FOR ``only`` — THE PLAYLISTS BEING COMPARED.

    This used to read EVERY playlist on the server, one ``getPlaylist`` at a
    time. On a 659-playlist library that is 659 serial full-content reads, and it
    measurably flooded Navidrome: the log showed the preview's reads timing out,
    two consecutive timeouts opening the client's 60s HOST COOLDOWN, and that
    cooldown then failing the unrelated playlist index ("host cooldown opened —
    failing fast for 60s: 2 consecutive requests to 192.168.1.200 failed"). A
    preview only ever diffs the genre playlists it plans to write, so the other
    ~595 reads were pure load with no answer attached.

    The LISTING already carries ``songCount``, so existence and size — all the
    "is it new?" and "does it have tracks?" questions — cost nothing extra. Only
    the ordered song IDs need the per-playlist call, and only for ``only``. Pass
    ``only=None`` to fetch everything (nothing needs that today; it exists so a
    caller cannot accidentally get a half-populated map by forgetting the arg).

    A playlist whose contents cannot be read is OMITTED from ``contents`` rather
    than assumed empty, so an unreadable playlist never shows up as "everything
    would be removed".
    """
    from services.playlists.playlist_navidrome_service import (
        _current_song_ids,
    )

    contents: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    try:
        from api_clients.navidrome import NavidromeClient
        from helpers.config_helpers import get_navidrome_users_normalized

        users = [
            u for u in (get_navidrome_users_normalized() or [])
            if str(u.get("base_url") or "").strip()
        ]
    except Exception as exc:
        logger.debug("Genre/playlist resync: Navidrome is not configured", error=str(exc))
        return contents, counts

    if not users:
        return contents, counts

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
        return contents, counts

    try:
        listings = client.fetch_all_playlists() or []
    except Exception as exc:
        logger.debug("Genre/playlist resync: playlist listing failed", error=str(exc))
        return contents, counts

    for entry in listings:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        # ⚠️ KEYED BY THE NORMALISED NAME, NOT THE RAW ONE.
        #
        # The plan's name and the server's name can differ in case, dash
        # character or spacing, and an exact key made an existing playlist look
        # new — see ``_playlist_name_key``. The caller normalises its planned
        # names with the same function, so both sides agree.
        key = _playlist_name_key(name)
        if not key:
            continue
        try:
            counts[key] = int(entry.get("songCount") or 0)
        except (TypeError, ValueError):
            counts[key] = 0

        if only is not None and key not in only:
            # Existence and size came free from the listing; the contents are
            # only needed by the diff, and this playlist is not in it.
            continue

        ids = _current_song_ids(client, str(entry.get("id") or ""))
        if ids is None:
            continue
        contents[key] = list(ids)
    return contents, counts


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

def preview_genre_playlist_resync(
    *, limit_tracks: int | None = None, report_progress: bool = False
) -> dict[str, Any]:
    """Report what an apply WOULD change. Writes no genres and no playlists.

    ⚠️ ``report_progress`` IS OFF FOR THE INLINE CALL AND ON FOR THE BACKGROUND
    ONE.

    A ``/preview`` click used to run this synchronously, and at 69k tracks that
    measured 132s — past the proxy's ~100s origin timeout, so the gateway
    answered with its own HTML error page and the browser's ``resp.json()`` threw
    ``Unexpected token '<'``. The route now starts this on a worker thread and
    polls (:func:`start_preview_genre_playlist_resync`), which is when the row is
    wanted.

    The row is the PREVIEW's own (``PREVIEW_SCAN_TYPE``) — never the apply's, so
    a preview can never make the dashboard announce a resync that did not run.

    Returns::

        {"ok", "seconds",
         "genre_changes": {"changed", "tracks", "with_evidence", "tracks_detail"},
         "playlist_changes": [{name, added, removed, count_before, count_after,
                               is_new, is_clear, order_changed}],
         "summary": {playlists_touched, playlists_cleared, playlists_created,
                     tracks_gained, tracks_removed}}
    """
    started = time.monotonic()

    def _frame(percent: float, phase: str, detail: str = "",
               status: str = "running") -> None:
        if not report_progress:
            return
        _write_progress(
            running=status == "running",
            percent=percent,
            phase=phase,
            detail=detail,
            status=status,
            scan_type=PREVIEW_SCAN_TYPE,
            progress_file=PREVIEW_PROGRESS_FILE,
        )

    _frame(2.0, PHASE_GENRES, "Comparing stored genre evidence…")

    try:
        genre_result = _genre_changes(dry_run=True)
    except Exception as exc:
        logger.error("Genre/playlist resync preview failed at the genre step", error=str(exc))
        _frame(0.0, "Failed", str(exc), status="failed")
        return {"ok": False, "error": f"genre step failed: {exc}"}

    changed_tracks = list(genre_result.get("changed_tracks") or [])
    if limit_tracks:
        changed_tracks = changed_tracks[: int(limit_tracks)]

    _frame(
        50.0, PHASE_PLAYLISTS,
        f"{len(changed_tracks)} track genre change(s) found",
    )

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
        _frame(0.0, "Failed", str(exc), status="failed")
        return {"ok": False, "error": f"playlist step failed: {exc}"}

    _frame(80.0, PHASE_PLAYLISTS, "Comparing playlists with Navidrome…")

    # ⚠️ ONLY THE PLANNED PLAYLISTS NEED THEIR CONTENTS READ.
    #
    # The listing carries every playlist's ``songCount`` for free, so "does this
    # exist?" and "does it have tracks?" never need a getPlaylist call. Reading
    # all 659 playlists serially is what flooded Navidrome and tripped the
    # client's host cooldown, which then broke the unrelated playlist index.
    # Both sides of the comparison are keyed by the NORMALISED name (see
    # ``_playlist_name_key``): the plan's spelling and the server's spelling can
    # differ in case, dash or spacing, and an exact match reported a CREATE over
    # a playlist that was already there.
    current, server_counts = _server_playlists(
        only={_playlist_name_key(n) for n in planned}
    )

    playlist_changes: list[dict[str, Any]] = []
    totals = {
        "playlists_touched": 0,
        "playlists_cleared": 0,
        "playlists_created": 0,
        "tracks_gained": 0,
        "tracks_removed": 0,
    }

    for name, after in sorted(planned.items()):
        # ⚠️ "COULD NOT READ" IS NOT "EMPTY" AND NOT "MISSING".
        #
        # Only the planned playlists have their contents read, so absence from
        # ``current`` now means one of two very different things: the playlist
        # does not EXIST (a genuine create) or it exists but its contents timed
        # out. The listing's ``songCount`` separates them. Treating a read
        # failure as "new" would predict a create over a populated playlist;
        # treating it as "empty" would report every track as a gain.
        #
        # ``key`` is the NORMALISED name the server map is keyed by, so an
        # existing playlist whose stored spelling differs from the template is
        # still recognised as existing (see ``_playlist_name_key``).
        key = _playlist_name_key(name)
        exists = key in server_counts
        readable = key in current
        if exists and not readable:
            logger.debug(
                "Genre/playlist resync preview: playlist contents unreadable, "
                "not compared",
                playlist=name, song_count=server_counts.get(key),
            )
            continue

        before = current.get(key, [])
        is_new = not exists
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

    # A playlist that EXISTS on the server and holds tracks, but is not in the
    # plan at all, is one this pass would stop managing. Reported separately:
    # the apply does not delete it (the orphan sweep owns deletion).
    #
    # Decided from ``server_counts`` — the listing — because unplanned playlists
    # deliberately have no contents fetched. Both sides are normalised keys, so
    # a spelling difference cannot report a managed playlist as unmanaged and
    # send it to the orphan sweep.
    _planned_keys = {_playlist_name_key(n) for n in planned}
    unmanaged = sorted(
        name for name, count in server_counts.items()
        if name not in _planned_keys and count > 0
    )

    elapsed = round(time.monotonic() - started, 2)

    result = {
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

    if report_progress:
        # The full result rides on the row: this is how a poll-only client gets
        # the payload, since the originating request returned 202 long ago.
        _write_progress(
            running=False,
            percent=100.0,
            phase=PHASE_DONE,
            processed=totals["playlists_touched"],
            total=totals["playlists_touched"],
            detail=(
                f"{len(changed_tracks)} track change(s) · "
                f"{totals['playlists_touched']} playlist(s) would change"
            ),
            status="complete",
            result=result,
            scan_type=PREVIEW_SCAN_TYPE,
            progress_file=PREVIEW_PROGRESS_FILE,
        )

    return result


#: One preview at a time. Separate from the apply's claim so a preview is not
#: refused merely because an apply is running (and vice versa) — they touch
#: different state, and the preview is read-only.
_PREVIEW_LOCK = __import__("threading").Lock()
_PREVIEW_RUNNING = False


def start_preview_genre_playlist_resync(*, limit_tracks: int | None = None) -> dict[str, Any]:
    """STEP 1, in the BACKGROUND. Returns immediately with ``{"ok", "started"}``.

    Same reasoning as the apply: a full-library preview takes longer than the
    proxy will hold a request open, so the work moves to a worker thread and the
    caller polls ``/genres/playlist-resync/preview/status``. Returns
    ``started=False`` (not an error) when a preview is already running.
    """
    global _PREVIEW_RUNNING
    with _PREVIEW_LOCK:
        if _PREVIEW_RUNNING:
            return {"ok": True, "started": False, "already_running": True}
        _PREVIEW_RUNNING = True

    _write_progress(
        running=True, percent=1.0, phase=PHASE_GENRES,
        detail="Starting…", status="running",
        scan_type=PREVIEW_SCAN_TYPE, progress_file=PREVIEW_PROGRESS_FILE,
    )

    def _run() -> None:
        global _PREVIEW_RUNNING
        try:
            preview_genre_playlist_resync(
                limit_tracks=limit_tracks, report_progress=True
            )
        except Exception as exc:
            logger.error("Background genre/playlist preview failed", error=str(exc))
            _write_progress(
                running=False, percent=0.0, phase="Failed", status="failed",
                error=str(exc),
                scan_type=PREVIEW_SCAN_TYPE, progress_file=PREVIEW_PROGRESS_FILE,
            )
        finally:
            with _PREVIEW_LOCK:
                _PREVIEW_RUNNING = False

    import threading

    threading.Thread(
        target=_run, name="genre-playlist-preview", daemon=True
    ).start()
    return {"ok": True, "started": True}


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

    # ⭐ NO SCAN-START LOCK.
    #
    # This pass used to take ``try_acquire_scan_start_lock()`` for its whole run,
    # which made it mutually exclusive with a scan: while genres were being
    # rewritten every scan start was refused with "A scan is running" — the
    # reported "writing genres to files is stopping another scan from running".
    #
    # The lock exists to stop two SCANS colliding cross-process. A genre resync
    # is not a scan: its DB writes are chunked single-row UPDATEs and its file
    # writes go through ``tag_file_service._path_lock``, so it is safe beside one.
    # What must stay serialised is two APPLIES, and ``_claim_apply`` above owns
    # that.

    started = time.monotonic()
    try:
        _write_progress(
            running=True, percent=2.0, phase=PHASE_APPLY,
            detail="Recomputing genres from stored evidence…", status="running",
        )

        # ── Phase 1: compute + write the genres ────────────────────────────
        #
        # ⚠️ THIS PHASE USED TO PUBLISH NOTHING UNTIL IT WAS OVER.
        #
        # The bar opened at 2% and the next frame was 50% — written only after
        # the whole-library aggregation AND every per-file tag write had
        # finished. On a 70k-track library that is the longest stretch of the
        # job, so the UI sat at "2%" for minutes and looked hung.
        #
        # The callback splits the phase honestly: aggregation is the first half
        # of the band, the file writes the second.
        def _genre_progress(
            done: int, total: int, files_written: int, stage: str = "aggregate"
        ) -> None:
            try:
                if stage == "persist":
                    # Saving the genre COLUMN. It used to be one silent 70k-row
                    # UPDATE between "67000/67039" and the first file write.
                    frac = (done / total) if total else 0.0
                    _write_progress(
                        running=True, percent=23.0 + 2.0 * min(1.0, frac),
                        phase=PHASE_APPLY,
                        processed=done, total=total,
                        detail=f"Saving genres to the database ({done}/{total} tracks)…",
                        status="running",
                    )
                elif files_written or stage == "files":
                    # File-writing sub-phase: the tail of this band.
                    frac = (files_written / total) if total else 0.0
                    _write_progress(
                        running=True, percent=25.0 + 25.0 * min(1.0, frac),
                        phase=PHASE_APPLY,
                        processed=files_written, total=total,
                        detail=f"Writing genre tags to files ({files_written}/{total})…",
                        status="running",
                    )
                else:
                    # Aggregation sub-phase: the first band.
                    frac = (done / total) if total else 0.0
                    _write_progress(
                        running=True, percent=2.0 + 21.0 * min(1.0, frac),
                        phase=PHASE_APPLY,
                        processed=done, total=total,
                        detail=f"Recomputing genres ({done}/{total} tracks)…",
                        status="running",
                    )
            except Exception as exc:  # progress must never abort the job
                logger.debug(
                    "Genre/playlist resync progress frame failed", error=str(exc)
                )

        genre_result = _genre_changes(dry_run=False, on_progress=_genre_progress)
        if not genre_result.get("ok", True):
            raise RuntimeError(genre_result.get("error") or "genre step failed")
        changed = int(genre_result.get("changed") or 0)
        _write_progress(
            running=True, percent=50.0, phase=PHASE_APPLY,
            processed=changed, total=changed,
            detail=(
                f"{changed} track genre set(s) updated"
                f" · {int(genre_result.get('files_written') or 0)} file(s) written"
            ),
            status="running",
        )

        # ── Phase 2: rebuild the playlists ────────────────────────────────
        # The genre playlists are derived data: after a library-wide genre
        # rewrite every one of them is potentially stale, so this rebuilds them
        # all rather than trying to guess an affected subset.
        # The save may itself have ridden through a database restart; do not
        # start the playlist rebuild against a server that is still recovering.
        try:
            from db.engine import is_db_outage_active, wait_for_db_available

            if is_db_outage_active() and not wait_for_db_available(timeout_seconds=180):
                raise RuntimeError(
                    "the database is still recovering — retry the resync when "
                    "it is back"
                )
        except ImportError:  # pragma: no cover - db.engine is always present
            pass

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

        # ⭐ THE CURATED PLAYLISTS ARE GENRE-DERIVED TOO.
        #
        # "Metal - Great Covers" decides its membership with
        # ``classify_main_genre`` over each track's FINAL genres, so a library-
        # wide genre rewrite makes it stale in exactly the same way as the genre
        # top-tracks lists — yet only the top-tracks lists were rebuilt.
        #
        # That is the reported "the Metal Covers playlist has not changed: the
        # new covers export has exactly the same 150 file paths, in the same
        # order". The entries whose final genre is only "Cover" stayed because
        # nothing ever asked the covers builder to re-evaluate them.
        #
        # The covers family pools library-wide, so it is reconciled as a whole
        # rather than per genre — the same reasoning as the top-tracks rebuild.
        covers_written = 0
        try:
            from services.popularity.stages.finalise_stage import (
                _create_covers_playlist,
            )
            from helpers.config_helpers import get_config

            _pl_cfg = (get_config() or {}).get("playlists") or {}
            if bool(_pl_cfg.get("covers_playlist_enabled", False)):
                _write_progress(
                    running=True, percent=75.0, phase=PHASE_PLAYLISTS,
                    detail="Rebuilding covers playlists…", status="running",
                )
                if _create_covers_playlist(_pl_cfg):
                    covers_written = 1
        except Exception as exc:
            # A covers failure must not fail the genre job — the genres are
            # already written and the top-tracks playlists already synced.
            logger.warning(
                "Genre/playlist resync: covers rebuild failed", error=str(exc)
            )

        written = int(written or 0) + covers_written

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
