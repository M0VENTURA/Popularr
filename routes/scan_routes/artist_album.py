"""
Artist, album and track rescan routes.

Routes should remain thin:
- parse request data
- dispatch to pipelines
- flash + redirect
"""

from __future__ import annotations

import logging
from urllib.parse import unquote

from quart import flash, redirect, request, url_for

logger = logging.getLogger(__name__)

from routes.scan_routes import scans_bp
from routes.scan_routes._common import (
    form_bool,
    guard_and_launch_popularity_scan,
    redirect_for_album,
    redirect_for_artist,
    run_async,
)

from services.scanning.pipelines.album_pipeline import run_album_pipeline
from services.scanning.pipelines.artist_pipeline import run_artist_pipeline
from services.scanning.pipelines.navidrome_targeted_pipeline import (
    run_navidrome_album_pipeline,
    run_navidrome_artist_pipeline,
)
from services.scanning.pipelines.popularity_pipeline import (
    run_popularity_album_scan,
    run_popularity_artist_scan,
    run_popularity_track_scan,
)

from helpers.logging_config import log_unified


@scans_bp.route("/scan/artist-custom", methods=["POST"])
async def scan_artist_custom():
    """Run a specific scan type for an artist."""
    form = await request.form
    scan_type = form.get("scan_type", "full")
    artist = (form.get("artist") or "").strip()
    force = form_bool(form.get("force"))

    if not artist:
        await flash("Error: No artist name provided", "danger")
        return redirect(url_for("ui.dashboard"))

    mode_label = "Forced" if force else "Changes Only"

    if scan_type == "full":
        # Same guard as the album "full" branch: the artist pipeline runs
        # popularity scoring, so it must never overlap another scan.
        started = guard_and_launch_popularity_scan(
            run_artist_pipeline,
            artist,
            force=force,
            scan_type="full",
        )
        if not started:
            await flash("A popularity scan is already running — skipped duplicate trigger", "warning")
            return redirect_for_artist(artist)

    elif scan_type == "navidrome":
        run_async(run_navidrome_artist_pipeline, artist, force)

    elif scan_type in {"popularity", "metadata", "singles", "finalise"}:
        started = guard_and_launch_popularity_scan(
            run_popularity_artist_scan,
            artist,
            force=force,
            scan_type=scan_type,
        )
        if not started:
            await flash("A popularity scan is already running — skipped duplicate trigger", "warning")
            return redirect_for_artist(artist)

    elif scan_type == "essentia":
        return redirect(url_for("scans.scan_essentia_mood"), code=307)

    else:
        await flash(f"Unknown scan type: {scan_type}", "danger")
        return redirect_for_artist(artist)

    await flash(
        f"{scan_type.title()} scan started for artist: {artist} ({mode_label})",
        "success",
    )
    return redirect_for_artist(artist)


@scans_bp.route("/scan/album-custom", methods=["POST"])
async def scan_album_custom():
    """Run a specific scan type for an album."""
    form = await request.form
    scan_type = form.get("scan_type", "full")
    artist = (form.get("artist") or "").strip()
    album = (form.get("album") or "").strip()
    force = form_bool(form.get("force"))

    if not artist or not album:
        await flash("Error: Artist and album name are required", "danger")
        return redirect(url_for("ui.dashboard"))

    mode_label = "Forced" if force else "Changes Only"

    if scan_type == "full":
        # ⚠️ The album "full" scan RUNS the popularity pipeline (Step 2/3 of
        # ``run_album_pipeline``), so it must pass through the SAME guard as
        # every other popularity trigger (dashboard API, scheduler, targeted
        # artist/album routes). It used to call ``run_async`` with NO guard:
        # a second trigger — a double click, or the album page during a
        # library scan — overlapped the first. Both marked the SAME shared
        # ``popularity_scan`` progress row running, the runner's skip-guards
        # saw the other generation's rows as fresh and self-froze them, and
        # the album burned 2× the wall-clock against the shared 1 req/s
        # MusicBrainz budget → "1 album(s) exceeded their per-album time
        # budget" for an album that had barely started.
        started = guard_and_launch_popularity_scan(
            run_album_pipeline,
            artist,
            album,
            force=force,
            scan_type="full",
        )
        if not started:
            await flash("A popularity scan is already running — skipped duplicate trigger", "warning")
            return redirect_for_album(artist, album)

    elif scan_type == "navidrome":
        run_async(run_navidrome_album_pipeline, artist, album, force)

    elif scan_type in {"popularity", "metadata", "singles", "finalise"}:
        started = guard_and_launch_popularity_scan(
            run_popularity_album_scan,
            artist,
            album,
            force=force,
            scan_type=scan_type,
        )
        if not started:
            await flash("A popularity scan is already running — skipped duplicate trigger", "warning")
            return redirect_for_album(artist, album)

    elif scan_type == "essentia":
        return redirect(url_for("scans.scan_essentia_mood"), code=307)

    else:
        await flash(f"Unknown scan type: {scan_type}", "danger")
        return redirect_for_album(artist, album)

    await flash(
        f"{scan_type.title()} scan started for album '{album}' by {artist} ({mode_label})",
        "success",
    )
    return redirect_for_album(artist, album)


@scans_bp.route(
    "/track/<path:artist>/<path:album>/<path:track_id>/rescan",
    methods=["POST"],
)
async def scan_track_rescan(artist, album, track_id):
    """Scan exactly ONE track, using the album's stored rows as context.

    ⭐ WHY THIS IS NOT THE ALBUM PIPELINE. The route used to run the whole
    artist pipeline; even an album-scoped version still re-processes every
    track on the album. This runs the normal track stage for the requested
    track only, while every sibling contributes its STORED score/stars to the
    album and artist distributions — so the rating matches what a full album
    scan would compute, without paying for the album-wide metadata, cover,
    missing-track and rename passes.
    """
    artist = unquote(artist)
    album = unquote(album)
    track_id = unquote(track_id)

    started = guard_and_launch_popularity_scan(
        run_popularity_track_scan,
        artist,
        album,
        track_id,
        force=True,
        # A full pass (no stage flag) so the target gets metadata, popularity,
        # singles, its file tags and its rating. The album-wide extras are
        # skipped by the runner's track-scoped gates.
        scan_type="full",
    )
    if not started:
        await flash(
            "A popularity scan is already running — skipped duplicate trigger",
            "warning",
        )
        return redirect(url_for("ui.track_detail", track_id=track_id))

    await flash(f"Rescan started for '{artist} - {album}'", "info")
    return redirect(url_for("ui.track_detail", track_id=track_id))
