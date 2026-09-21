"""
Essentia scan routes.

Routes should only:
- parse request/filter data
- prevent duplicate execution
- start pipeline asynchronously
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

from services.scanning.pipelines.essentia_pipeline import run_essentia_pipeline


@scans_bp.route("/scan/mood", methods=["POST"])
async def scan_mood():
    """Deprecated AcousticBrainz route; forward to Essentia."""
    await flash(
        "AcousticBrainz mood scan has been retired. Starting Essentia instead.",
        "info",
    )
    return await scan_essentia_mood()


@scans_bp.route("/scan/essentia-mood", methods=["POST"])
async def scan_essentia_mood():
    """Run Essentia local-ML mood/genre enrichment."""
    form = await request.form
    mode = request.args.get("mode", "all")

    artist_filter = (
        form.get("artist")
        or request.args.get("artist")
        or ""
    ).strip()

    album_filter = (
        form.get("album")
        or request.args.get("album")
        or ""
    ).strip()

    track_id_filter = (
        form.get("track_id")
        or request.args.get("track_id")
        or ""
    ).strip()

    force_scan = (
        mode in {"force", "resume_force"}
        or bool(artist_filter or album_filter or track_id_filter)
    )

    redirect_target = url_for("ui.dashboard")

    if track_id_filter:
        redirect_target = url_for("ui.track_detail", track_id=track_id_filter)
    elif artist_filter and album_filter:
        redirect_target = url_for(
            "ui.album_detail",
            album_path=f"{artist_filter}/{album_filter}",
        )
    elif artist_filter:
        redirect_target = url_for("ui.artist_detail", name=artist_filter)

    progress_file = get_scan_progress_path("essentia_mood_scan")

    with scan_lock:
        if is_runtime_running("essentia_mood"):
            await flash("Essentia mood scan is already running", "warning")
            return redirect(redirect_target)

        clear_stop_request(progress_file)

        def _worker():
            try:
                run_essentia_pipeline(
                    progress_file=progress_file,
                    force=force_scan,
                    artist_filter=artist_filter,
                    album_filter=album_filter,
                    track_id_filter=track_id_filter,
                )
            finally:
                clear_runtime("essentia_mood")

        thread = run_async(_worker, daemon=False)
        set_runtime("essentia_mood", {"thread": thread, "type": "essentia_mood"})

    await flash("✅ Essentia scan started", "success")
    return redirect(redirect_target)


@scans_bp.route("/scan/stop-mood", methods=["POST"])
async def scan_stop_mood():
    """Deprecated AcousticBrainz stop route; forward to Essentia stop."""
    return await scan_stop_essentia_mood()


@scans_bp.route("/scan/stop-essentia-mood", methods=["POST"])
async def scan_stop_essentia_mood():
    """Request a graceful Essentia scan stop."""
    request_scan_stop(
        get_scan_progress_path("essentia_mood_scan"),
        "essentia_mood_scan",
    )

    clear_runtime("essentia_mood")

    return await stop_response("Essentia mood scan stop requested")