"""
General scan start/stop/status routes.
"""

from __future__ import annotations

from typing import Any

import structlog
from quart import flash, jsonify, redirect, request, url_for

from routes.scan_routes import scans_bp
from routes.scan_routes._common import form_bool, run_async, stop_response

from services.scanning.runtime_state import (
    scan_lock,
    is_runtime_running,
    clear_runtime,
    set_runtime,
)
from services.scanning.scan_state import (
    get_scan_progress_path,
    read_progress_file,
    request_scan_stop,
)
from services.scanning.pipeline import start_library_scan
from services.scanning.pipelines.artist_pipeline import run_artist_pipeline
from services.scanning.pipelines.popularity_pipeline import run_popularity_mode
from services.scanning.pipelines.navidrome_pipeline import run_navidrome_import_scan

logger = structlog.get_logger(__name__)


# -------------------------------------------------------------------------
# Start scan
# -------------------------------------------------------------------------

@scans_bp.route("/scan/start", methods=["POST"])
async def scan_start() -> Any:
    form = await request.form
    scan_type = form.get("scan_type", "full")
    artist = (form.get("artist") or "").strip()
    force = form_bool(form.get("force"))
    restart = form_bool(form.get("restart"))

    logger.info("Scan start requested", scan_type=scan_type, artist=artist, force=force, restart=restart)

    # -------------------------------------------------
    # Artist-only scan
    # -------------------------------------------------
    if scan_type == "artist":
        if not artist:
            await flash("Error: No artist name provided", "danger")
            return redirect(url_for("ui.dashboard"))

        run_async(run_artist_pipeline, artist, force)

        await flash(f"Scan started for artist: {artist}", "success")
        return redirect(url_for("ui.artist_detail", name=artist))

    # -------------------------------------------------
    # Prevent duplicate scans
    # -------------------------------------------------
    with scan_lock:
        if is_runtime_running("library"):
            logger.warning("Scan rejected: library scan already running")
            await flash("A scan is already running", "warning")
            return redirect(url_for("ui.dashboard"))

    # -------------------------------------------------
    # Full/library scan
    #
    # Resume semantics (default): continue from the last checkpoint, skipping
    # artists already scanned and recently-scanned items.
    #   restart (no force): start from the BEGINNING, still skipping
    #       recently-scanned items (change detection / skip windows apply).
    #   force (no restart): RESUME from the last checkpoint in FORCED mode
    #       (re-scan everything from the resume point, ignoring skip windows).
    #   force + restart: start from the beginning in FORCED mode.
    # -------------------------------------------------
    if scan_type in {"full", "force"}:
        with scan_lock:
            if is_runtime_running("library"):
                await flash("A scan is already running", "warning")
                return redirect(url_for("ui.dashboard"))

            # The ``force`` checkbox (or the legacy ``force`` scan type) is a
            # FORCED RESUME by default — the ``restart`` checkbox is what
            # restarts from the top.  ``scan_type == "force"`` alone is thus a
            # forced resume, matching the requested semantics.
            _force = force or scan_type == "force"
            _restart = restart

            def _full_scan_worker() -> None:
                try:
                    start_library_scan(
                        artist_filter=None,
                        resume=True,
                        force=_force,
                        restart=_restart,
                    )
                finally:
                    clear_runtime("library")

            thread = run_async(_full_scan_worker)
            set_runtime("library", {"thread": thread, "type": "library"})

        mode_desc = "forced restart" if _restart and _force else (
            "restart" if _restart else ("forced resume" if _force else "resume")
        )
        await flash(f"Scan started: {scan_type} ({mode_desc})", "success")
        return redirect(url_for("ui.dashboard"))

    # -------------------------------------------------
    # Navidrome scan
    # -------------------------------------------------
    if scan_type == "navidrome":
        run_async(run_navidrome_import_scan, mode="all")

        await flash("Navidrome import scan started", "success")
        return redirect(url_for("ui.dashboard"))

    # -------------------------------------------------
    # Popularity scans
    # -------------------------------------------------
    if scan_type in {"popularity", "metadata", "singles", "singles_detection"}:
        run_async(run_popularity_mode, mode=scan_type)

        await flash(f"{scan_type} scan started", "success")
        return redirect(url_for("ui.dashboard"))

    logger.error("Unknown scan type requested", scan_type=scan_type)
    await flash(f"Unknown scan type: {scan_type}", "danger")
    return redirect(url_for("ui.dashboard"))


# -------------------------------------------------------------------------
# Stop scan (generic)
# -------------------------------------------------------------------------

@scans_bp.route("/scan/stop", methods=["POST"])
async def scan_stop() -> Any:
    path = get_scan_progress_path("library_scan")
    request_scan_stop(path)
    
    logger.info("Stop requested for library scan")
    return await stop_response("Stop requested for library scan")


# -------------------------------------------------------------------------
# Stop specific scans
# -------------------------------------------------------------------------

@scans_bp.route("/scan/stop-popularity", methods=["POST"])
async def scan_stop_popularity() -> Any:
    request_scan_stop(get_scan_progress_path("popularity_scan"))
    request_scan_stop(get_scan_progress_path("singles_scan"))

    logger.info("Stop requested for popularity/singles scans")
    return await stop_response("Popularity scan stop requested")


@scans_bp.route("/scan/stop-singles", methods=["POST"])
async def scan_stop_singles() -> Any:
    request_scan_stop(get_scan_progress_path("singles_scan"))

    logger.info("Stop requested for singles detection scan")
    return await stop_response("Single detection scan stop requested")


# -------------------------------------------------------------------------
# Stop all scans
# -------------------------------------------------------------------------

@scans_bp.route("/scan/stop-all", methods=["POST"])
async def scan_stop_all() -> Any:
    scan_types = [
        "full_scan",
        "navidrome_scan",
        "popularity_scan",
        "singles_scan",
        "essentia_mood_scan",
        "combined_scan",
        "missing_releases_scan",
        "mp3_import",
        "library_scan",
        "metadata_lookup_scan",
    ]
    
    for scan_type in scan_types:
        request_scan_stop(get_scan_progress_path(scan_type))
        # Clear runtime tracking (safe cleanup)
        clear_runtime(scan_type.replace("_scan", ""))

    logger.info("Stop requested for all scans", scan_types_affected=len(scan_types))
    return await stop_response("Stop requested for all scans", "success")


# -------------------------------------------------------------------------
# Status
# -------------------------------------------------------------------------

@scans_bp.route("/scan/status")
def scan_status() -> Any:
    running = is_runtime_running("library")
    return jsonify({"running": running})


# -------------------------------------------------------------------------
# Clear stuck scans
# -------------------------------------------------------------------------

@scans_bp.route("/scan/clear-stuck", methods=["POST"])
async def scan_clear_stuck() -> Any:
    cleared = 0

    scan_types = [
        "navidrome_scan",
        "popularity_scan",
        "singles_scan",
        "essentia_mood_scan",
        "combined_scan",
        "missing_releases_scan",
        "mp3_import",
    ]

    for scan_type in scan_types:
        path = get_scan_progress_path(scan_type)
        state = read_progress_file(path)

        if state.get("is_running"):
            request_scan_stop(path)
            cleared += 1

    if cleared:
        logger.info("Cleared stuck scans", cleared_count=cleared)
        await flash(f"✅ Cleared {cleared} stuck scan(s)", "success")
    else:
        await flash("No stuck scans found", "info")

    return redirect(url_for("ui.dashboard"))
