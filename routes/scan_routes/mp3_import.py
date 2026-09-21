"""
MP3 metadata import scan routes.

Routes should only:
- parse form input
- prevent duplicate execution
- start MP3 import pipeline
- request graceful stop
"""

from __future__ import annotations

from quart import flash, redirect, request, url_for

from routes.scan_routes import scans_bp
from routes.scan_routes._common import run_async, stop_response

from services.scanning.runtime_state import (
    scan_lock,
    is_runtime_running,
    set_runtime,
    clear_runtime,
)

from services.scanning.scan_state import (
    get_scan_progress_path,
    clear_stop_request,
    request_scan_stop,
)

from services.scanning.pipelines.mp3_import_pipeline import run_mp3_import_pipeline


@scans_bp.route("/scan/mp3-import", methods=["POST"])
async def scan_mp3_import():
    """Run MP3 metadata import scan."""
    form = await request.form
    directory = form.get("directory") or None
    dry_run = str(form.get("dry_run") or "").strip().lower() == "on"

    progress_file = get_scan_progress_path("mp3_import")

    with scan_lock:
        if is_runtime_running("mp3_import"):
            await flash("MP3 metadata import scan is already running", "warning")
            return redirect(url_for("ui.dashboard"))

        clear_stop_request(progress_file)

        def _worker():
            try:
                run_mp3_import_pipeline(
                    directory=directory,
                    dry_run=dry_run,
                )
            finally:
                clear_runtime("mp3_import")

        thread = run_async(_worker)
        set_runtime("mp3_import", {"thread": thread, "type": "mp3_import"})

    await flash("MP3 metadata import scan started", "success")
    return redirect(url_for("ui.dashboard"))


@scans_bp.route("/scan/stop-mp3-import", methods=["POST"])
async def scan_stop_mp3_import():
    """Request a graceful MP3 import stop."""
    request_scan_stop(
        get_scan_progress_path("mp3_import"),
        "mp3_import",
    )

    clear_runtime("mp3_import")

    return await stop_response("MP3 import scan stop requested")