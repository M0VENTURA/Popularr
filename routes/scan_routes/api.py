"""
JSON API routes for scan status, launches and dashboard polling.

These routes are API-facing controllers only.

Responsibilities:
- Parse JSON/query input
- Start lightweight scan requests
- Return runtime/progress JSON for dashboard
- Avoid business logic and direct progress-file manipulation

Progress files are read through services.scanning.scan_state.
Runtime state is read through services.scanning.runtime_state.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from quart import jsonify, make_response, request
from sqlalchemy import text
from db.engine import db_session
from routes.scan_routes import scans_bp
from routes.schemas import ScanRequest
from pydantic import ValidationError
from routes.scan_routes._common import run_async
from services.scanning.pipelines.popularity_pipeline import run_popularity_mode
from services.scanning.runtime_state import (
    scan_lock,
    is_runtime_running,
    set_runtime,
    clear_runtime,
    get_runtime,
    is_process_alive,
)
import services.scanning.runtime_state as runtime_state  # for legacy globals

from services.scanning.scan_state import (
    get_scan_progress_path,
    read_progress_file,
    clear_progress_file,
)
from helpers.logging_config import log_unified

from services.scanning.pipelines.artist_pipeline import run_artist_pipeline

logger = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------

SCAN_STATUS_LABELS = {
    "library": "Main Library Scan",
    "navidrome": "Navidrome Sync",
    "popularity": "Popularity Update",
    "singles": "Single Detection",
    "essentia_mood": "Essentia Mood",
    "combined": "Combined Scan",
    "missing_releases": "Missing Releases Scan",
    "mp3_import": "MP3 Import",
    "artist": "Artist Scan",
}


PROGRESS_SCAN_TYPES = [
    "library_scan",
    "navidrome_scan",
    "metadata_lookup_scan",
    "popularity_scan",
    "singles_scan",
    "essentia_mood_scan",
    "combined_scan",
    "missing_releases_scan",
    "mp3_import",
]


def _runtime_status_payload() -> dict:
    """Return known runtime states using the runtime registry."""
    return {
        key: {
            "name": label,
            "running": is_runtime_running(key),
        }
        for key, label in SCAN_STATUS_LABELS.items()
    }


# -------------------------------------------------------------------------
# API: Start artist scan
# -------------------------------------------------------------------------

@scans_bp.route("/api/scan/artist", methods=["POST"])
async def api_scan_single_artist():
    """Start an artist scan from JSON input."""
    data = (await request.get_json(silent=True)) or {}

    artist = str(data.get("artist", "")).strip()
    force = bool(data.get("force", False))

    if not artist:
        return jsonify({
            "success": False,
            "error": "Artist name is required",
        }), 400

    with scan_lock:
        if is_runtime_running("artist"):
            return jsonify({
                "success": False,
                "error": "An artist scan is already running",
            }), 409

        def _worker():
            try:
                run_artist_pipeline(artist, force)
            finally:
                clear_runtime("artist")

        thread = run_async(_worker)
        set_runtime("artist", {"thread": thread, "type": "artist"})

    return jsonify({
        "success": True,
        "message": f"Scan started for artist: {artist}",
        "artist": artist,
        "mode": "Forced" if force else "Changes Only",
    })


# -------------------------------------------------------------------------
# API: Runtime status
# -------------------------------------------------------------------------

@scans_bp.route("/api/scan-status")
def api_scan_status():
    """
    Return status for known in-process scan references.

    Note:
    Runtime state is per Flask worker only.
    Progress files remain the cross-process source of truth.
    """
    with scan_lock:
        return jsonify(_runtime_status_payload())


# -------------------------------------------------------------------------
# API: Resume artist options
# -------------------------------------------------------------------------

@scans_bp.route("/api/scan/resume-options")
def api_scan_resume_options():
    """Artists a resumed scan could start from, for the dashboard prompt.

    Shown when a scan is started with **Restart unchecked**. Ordered:
    the artist the interrupted full scan stopped at, then the most recently
    touched artists (manually scanned, or with albums scanned).

    Each entry carries the resolved ``mode`` so the UI can state what will
    happen: ``next`` (the artist had finished — continue after it) or
    ``restart`` (it was interrupted — re-scan that artist).
    """
    try:
        from services.scanning.resume_options_service import get_resume_options

        result = get_resume_options()
    except Exception as exc:
        logger.exception("Failed to build resume options", error=str(exc))
        result = {"success": False, "error": str(exc), "has_options": False,
                  "artists": [], "recommended": None}

    response = jsonify(result)
    # The resume point changes as scans run; never let a proxy cache it.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# -------------------------------------------------------------------------
# API: Recent scan history
# -------------------------------------------------------------------------

@scans_bp.route("/api/recent-scans")
def api_recent_scans():
    """Return latest album scan events for dashboard refresh."""
    try:
        limit = min(request.args.get("limit", 100, type=int), 100)

        from services.scanning.scan_history_service import get_recent_album_scans

        scans = get_recent_album_scans(limit=limit)

        response = jsonify({
            "scans": scans,
            "fetched_at": datetime.utcnow().isoformat() + "Z",
        })

    except Exception as exc:
        response = jsonify({
            "scans": [],
            "error": str(exc),
            "fetched_at": datetime.utcnow().isoformat() + "Z",
        })

    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"

    return response


# -------------------------------------------------------------------------
# API: Progress polling
# -------------------------------------------------------------------------

@scans_bp.route("/api/scan-progress")
def api_scan_progress():
    """
    Unified scan progress endpoint.

    - Uses progress_service as the single source of truth
    - Merges scan_state (persistent) + progress_tracker (live stage data)
    - Preserves legacy payload structure for dashboard compatibility
    """
    try:
        from services.scanning.pipelines.progress_service import get_scan_progress

        result = get_scan_progress()

        active_scans = result.get("active_scans", [])

        # -----------------------------------------------------------------
        # Primary scan selection (matches legacy behaviour: first active)
        # -----------------------------------------------------------------
        primary = active_scans[0] if active_scans else {
            "scan_type": "none",
            "is_running": False,
            "percent_complete": 0,
            "status": "idle",
            "current_artist": None,
            "current_album": None,
            "current_stage": None,
            "message": None,
        }

        # -----------------------------------------------------------------
        # Final payload (backward compatible)
        # -----------------------------------------------------------------
        payload = {
            **primary,
            "is_running": result.get("is_running", False),
            "active_scans": active_scans,
            "active_scan_count": result.get("active_scan_count", 0),
        }

        return jsonify(payload)

    except Exception as exc:
        # Safe fallback — never break dashboard polling
        return jsonify({
            "scan_type": "error",
            "is_running": False,
            "percent_complete": 0,
            "status": "error",
            "active_scans": [],
            "active_scan_count": 0,
            "error": str(exc),
        }), 500


@scans_bp.route("/api/scan-progress/stream")
async def api_scan_progress_stream():
    """Server-Sent Events stream of unified scan progress.

    Pushes the same payload as ``/api/scan-progress`` whenever it changes
    (checked on a 1s tick) and keeps the connection alive with heartbeat
    comments between updates.  Lets any page — the mobile live-scan toast,
    the dashboard status bar — render scan progress without polling.

    Event format: ``data: {json}\n\n`` with the unified progress payload
    (``is_running``, ``active_scans``, ``active_scan_count``).
    """
    async def event_stream():
        last_sig: str | None = None
        while True:
            try:
                from services.scanning.pipelines.progress_service import get_scan_progress
                # get_scan_progress is sync DB + file I/O — offload it so the
                # per-second tick never blocks the event loop.
                result = await asyncio.to_thread(get_scan_progress)
            except Exception:
                result = {"is_running": False, "active_scans": []}
            try:
                sig = json.dumps(result, sort_keys=True, default=str)
            except (TypeError, ValueError):
                sig = json.dumps({"is_running": False, "active_scans": []})
            if sig != last_sig:
                last_sig = sig
                yield f"data: {sig}\n\n"
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(1.0)

    response = await make_response(
        event_stream(),
        {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
    response.timeout = None
    return response

@scans_bp.route("/api/popularity/run", methods=["POST"])
async def api_popularity_run_compat():
    raw = (await request.get_json(silent=True)) or {}

    # Validate with Pydantic
    try:
        params = ScanRequest(**raw)
    except ValidationError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    mode = params.mode

    # Legacy boolean flag overrides (still supported for backward compat)
    if raw.get("metadata_only"):
        mode = "metadata"
    elif raw.get("singles_only"):
        mode = "singles"

    force = bool(params.force or raw.get("force", False))
    restart = bool(getattr(params, "restart", False) or raw.get("restart", False))

    # Resume semantics:
    #   default  → resume from the last checkpoint (skip already-scanned
    #              artists; recently-scanned items stay skipped)
    #   restart  → start from the top (skipping recently-scanned items
    #              unless force is also set)
    #   force    → resume from the checkpoint in FORCED mode (re-scan
    #              everything from the resume point, ignore skip windows)
    #   force+restart → start from the top in FORCED mode
    # The dashboard "All" scan checkpoints under ``full_scan``; the other
    # modes checkpoint under ``popularity_scan``.
    #
    # An explicit ``resume_from`` in the request OVERRIDES the checkpoint. The
    # dashboard's resume prompt sends the artist the user picked; without this
    # the choice would be silently ignored and the scan would resume from
    # whatever the last interrupted run happened to leave behind. When the
    # chosen artist had already FINISHED, the client sends the artist AFTER it
    # (the service resolves that), because the scan loop processes the named
    # artist itself.
    resume_from = None
    _checkpoint_scan_type = "full_scan" if mode == "all" else "popularity_scan"
    _checkpoint_path = get_scan_progress_path(_checkpoint_scan_type)
    _requested_resume = str(
        raw.get("resume_from") or getattr(params, "resume_from", "") or ""
    ).strip()
    if restart:
        try:
            from services.scanning.scan_state import clear_scan_checkpoint
            clear_scan_checkpoint(_checkpoint_path)
        except Exception:
            pass
    elif _requested_resume:
        resume_from = _requested_resume
    else:
        try:
            from services.scanning.scan_state import load_scan_checkpoint
            _cp = load_scan_checkpoint(_checkpoint_path)
            resume_from = _cp.get("last_scanned_artist") or None
        except Exception:
            resume_from = None

    # Record "started" only AFTER the duplicate guards below — a rejected
    # start (already-running / stale DB state) must not leave an orphaned
    # "started" scan_history row.  ``renderRecentScans`` renders ANY non-failed
    # ``_SCAN_SESSION_`` group as "completed", so an orphaned "started" from a
    # 409 looked like "full scan completed" with the footer idle and no worker
    # ever launching (the exact "goes straight to completed" symptom).
    from services.scanning.scan_history_service import record_scan

    with scan_lock:
        if is_runtime_running("popularity"):
            return jsonify({
                "success": False,
                "error": "A popularity scan is already running",
            }), 409

        # Cross-process guard: a scan running in ANOTHER hypercorn worker is
        # invisible to ``is_runtime_running`` (per-process registry), but it
        # has marked the shared DB scan state as running.  Without this check
        # two scans can overlap, and the first to finish marks the shared
        # state complete while the other is still going — which makes a full
        # scan look like it halted mid-letter on the dashboard.
        from services.scanning.pipelines.popularity_pipeline import is_popularity_scan_active

        if is_popularity_scan_active():
            # Self-heal STALE scan-state rows: a crashed scan (daemon thread
            # died before its finally, or a previous process was killed
            # mid-scan) can leave ``scan_states.is_running=True`` forever.
            # ``reset_stale_scan_states`` only runs at startup, so a row that
            # went stale while the app stayed up blocks EVERY subsequent scan
            # (dashboard + scheduler) with a phantom "already running".  If no
            # live worker owns the row, clear it and proceed — otherwise it is
            # a genuinely active scan and we reject.
            _live_owner = False
            for _scan_type in ("popularity_scan", "full_scan"):
                _state = read_progress_file(get_scan_progress_path(_scan_type))
                if _state.get("is_running"):
                    # The in-process runtime registry is authoritative for
                    # "someone in THIS worker owns it" — a row marked running
                    # with no live thread is stale.
                    _rt = get_runtime(_scan_type.replace("_scan", ""))
                    if _rt is not None:
                        _owner_thread = _rt.get("thread") if isinstance(_rt, dict) else _rt
                        if is_process_alive(_owner_thread):
                            _live_owner = True
                            break
            if _live_owner:
                return jsonify({
                    "success": False,
                    "error": "A popularity scan is already running",
                }), 409
            # No live owner — the DB row is stale.  Reset it so THIS scan
            # starts cleanly instead of being permanently blocked.
            try:
                for _scan_type in ("popularity_scan", "full_scan"):
                    _state = read_progress_file(get_scan_progress_path(_scan_type))
                    if _state.get("is_running"):
                        clear_progress_file(get_scan_progress_path(_scan_type))
                log_unified("Cleared stale scan-state rows (phantom 'already running') before starting scan")
            except Exception:
                pass

        record_scan(mode, "started", message=f"{mode} scan started", artist="_SCAN_SESSION_", album=mode)

        def _worker():
            try:
                # ── Clear STALE stop flags before the scan runs ──────────
                # A previous "Stop" left ``scan_states.stop_requested=True``
                # on the scan type; without clearing it here the new scan is
                # IMMEDIATELY stopped again (the reported "any scan after
                # Stop is stopped at once").  Clear both the "all" full-scan
                # flag and the per-mode popularity flag.
                try:
                    from services.scanning.scan_state import clear_stop_request
                    for _st in ("full_scan", "popularity_scan", "singles_scan",
                                "metadata_lookup_scan"):
                        clear_stop_request(_st)
                except Exception as _clear_exc:
                    logger.debug("Stop-flag clear failed before scan", error=str(_clear_exc))

                log_unified(f"[POPULARITY] Worker starting mode={mode} force={force} restart={restart} resume_from={resume_from or 'top'}")
                run_popularity_mode(
                    mode=mode,
                    force_rescan=force,
                    resume_from=resume_from,
                )
                log_unified(f"[POPULARITY] Worker finished mode={mode}")
            except Exception as exc:
                # A daemon-thread exception would otherwise be swallowed by
                # ``run_async`` (no handler) — the route returns "started",
                # the worker dies silently, and the dashboard shows an
                # orphaned "started" record as "completed" with the footer
                # idle and nothing in the logs.  Surface it so the failure is
                # diagnosable.
                import traceback
                log_unified(f"[POPULARITY] Worker failed: {exc}")
                logger.error("[POPULARITY] Worker failed: %s\n%s", exc, traceback.format_exc())
                try:
                    record_scan(mode, "failed", message=f"{mode} scan failed: {exc}", artist="_SCAN_SESSION_", album=mode)
                except Exception:
                    pass
            finally:
                clear_runtime("popularity")
                runtime_state.scan_process_popularity = None

        thread = run_async(_worker)
        set_runtime("popularity", {"thread": thread, "type": mode})
        runtime_state.scan_process_popularity = {"thread": thread, "type": mode}

    return jsonify({
        "success": True,
        "message": f"{mode.capitalize()} scan started",
        "forced": force,
    })


# -------------------------------------------------------------------------
# API: Essentia mood scan
# -------------------------------------------------------------------------

@scans_bp.route("/api/essentia/run", methods=["POST"])
async def api_essentia_run():
    """Start an Essentia mood/genre scan via JSON API."""
    from services.scanning.pipelines.essentia_pipeline import run_essentia_pipeline

    with scan_lock:
        if is_runtime_running("essentia_mood"):
            return jsonify({
                "success": False,
                "error": "An Essentia scan is already running",
            }), 409

        progress_file = get_scan_progress_path("essentia_mood_scan")

        def _worker():
            try:
                run_essentia_pipeline(progress_file=progress_file)
            finally:
                clear_runtime("essentia_mood")

        thread = run_async(_worker)
        set_runtime("essentia_mood", {"thread": thread, "type": "essentia_mood"})

    return jsonify({
        "success": True,
        "message": "Essentia mood/genre scan started",
    })


# -------------------------------------------------------------------------
# API: Navidrome import pipeline
# -------------------------------------------------------------------------

@scans_bp.route("/api/navidrome/import", methods=["POST"])
async def api_navidrome_import():
    """Start the local Navidrome import pipeline via JSON API."""
    from services.scanning.pipelines.navidrome_pipeline import run_navidrome_import_scan

    data = (await request.get_json(silent=True)) or {}
    mode = str(data.get("mode", "all")).strip().lower() or "all"

    with scan_lock:
        if is_runtime_running("navidrome"):
            return jsonify({
                "success": False,
                "error": "A Navidrome import scan is already running",
            }), 409

        progress_file = get_scan_progress_path("navidrome_scan")

        def _worker():
            try:
                run_navidrome_import_scan(mode=mode, progress_file=progress_file)
            finally:
                clear_runtime("navidrome")

        thread = run_async(_worker)
        set_runtime("navidrome", {"thread": thread, "type": "navidrome"})

    return jsonify({
        "success": True,
        "message": f"Navidrome import scan started (mode={mode})",
    })


# -------------------------------------------------------------------------
# API: Legacy popularity status endpoint (compat for dashboard.html)
# -------------------------------------------------------------------------

@scans_bp.route("/api/popularity/status", methods=["GET"])
def api_popularity_status_compat():
    """
    Backward-compatible popularity status endpoint.

    The legacy dashboard.html polls /api/popularity/status every 2 seconds
    and expects { success, running, message, progress, processed_items, total_items }.
    This bridges to the modern scan-progress service.
    """
    try:
        from services.scanning.pipelines.progress_service import get_scan_progress

        result = get_scan_progress()

        # Find the active popularity scan, if any
        pop_scan = None
        for active in result.get("active_scans", []):
            if active.get("scan_type") in ("popularity_scan", "singles_scan"):
                pop_scan = active
                break

        if pop_scan:
            return jsonify({
                "success": True,
                "running": pop_scan.get("is_running", False),
                "message": pop_scan.get("message") or pop_scan.get("status") or "Running...",
                "progress": pop_scan.get("percent_complete", 0),
                "processed_items": pop_scan.get("processed_items") or pop_scan.get("processed_artists") or 0,
                "total_items": pop_scan.get("total_items") or pop_scan.get("total_artists") or 0,
            })

        return jsonify({
            "success": True,
            "running": False,
            "message": "Idle",
            "progress": 0,
            "processed_items": 0,
            "total_items": 0,
        })

    except Exception as exc:
        return jsonify({
            "success": False,
            "running": False,
            "message": str(exc),
            "progress": 0,
            "processed_items": 0,
            "total_items": 0,
        }), 500