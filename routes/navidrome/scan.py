"""Navidrome scan routes.

These are Navidrome-specific entrypoints only. Generic scan orchestration,
status and stop-all routes remain under ``routes/scan_routes``.
"""

from __future__ import annotations

from typing import Any

import structlog
from quart import flash, jsonify, redirect, request, url_for

import services.scanning.runtime_state as runtime_state
from routes.navidrome import navidrome_bp, get_navidrome_client
from routes.scan_routes._common import form_bool, is_process_alive, run_async, stop_response
from services.scanning.pipelines.navidrome_pipeline import run_navidrome_import_scan
from services.scanning.scan_state import progress_path, request_scan_stop

logger = structlog.get_logger(__name__)


@navidrome_bp.route("/api/navidrome/scan/start", methods=["POST"])
def api_start_navidrome_scan() -> Any:
    """Trigger a Navidrome server-side library rescan (MANUAL).

    This is the only remaining remote ``startScan`` path — the automatic
    per-tag-write triggers were removed (they paused the server and locked
    the database).  The single automatic sync runs before the full Navidrome
    import and waits for completion.  This manual endpoint triggers + waits
    so the caller can rely on the scan being finished when it returns.
    """
    client = get_navidrome_client()
    if not client:
        return jsonify({"success": False, "error": "Navidrome not configured"})

    try:
        completed = client.trigger_and_wait_for_scan()
    except Exception as exc:
        logger.error("Navidrome manual scan failed", error=str(exc), exc_info=True)
        return jsonify({"success": False, "error": str(exc)})

    return jsonify({
        "success": completed,
        "message": "Scan completed" if completed else "Scan start failed or timed out",
    })


@navidrome_bp.route("/api/navidrome/scan/status", methods=["GET"])
def api_get_navidrome_scan_status() -> Any:
    """Return Navidrome server-side library scan status."""
    try:
        client = get_navidrome_client()
        if not client:
            return jsonify({"scanning": False, "success": False, "error": "Navidrome not configured"})
            
        return jsonify(client.get_scan_status())
    except Exception as exc:
        logger.error("Navidrome scan status failed", error=str(exc), exc_info=True)
        return jsonify({"scanning": False, "success": False, "error": str(exc)})


@navidrome_bp.route("/scan/navidrome", methods=["POST"])
async def scan_navidrome() -> Any:
    """Start the local Navidrome import-only scan pipeline."""
    mode = request.args.get("mode", "all")
    restart_requested = form_bool(request.args.get("restart"))
    force_start = form_bool(request.args.get("force_start"))

    with runtime_state.scan_lock:
        if is_process_alive(runtime_state.scan_process_navidrome) and not force_start:
            return jsonify({"scan_running": True, "message": "A Navidrome scan is already running."}), 409

        thread = run_async(
            run_navidrome_import_scan,
            mode=mode,
            restart_requested=restart_requested,
            daemon=False,
        )
        runtime_state.scan_process_navidrome = {"thread": thread, "type": "navidrome"}

    await flash("✅ Navidrome import started", "success")
    return redirect(url_for("ui.dashboard"))


@navidrome_bp.route("/scan/stop-navidrome", methods=["POST"])
async def scan_stop_navidrome() -> Any:
    """Request a graceful stop for the local Navidrome import scan."""
    with runtime_state.scan_lock:
        request_scan_stop(progress_path("navidrome_scan_progress.json"), "navidrome_scan")
        runtime_state.scan_process_navidrome = None
        
    return await stop_response("Navidrome sync scan stop requested")
