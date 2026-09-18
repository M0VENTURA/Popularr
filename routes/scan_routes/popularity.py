"""Popularity, metadata and singles scan routes."""

from __future__ import annotations

import logging
from typing import Any

from quart import flash, jsonify, redirect, request, url_for
from sqlalchemy import text

import services.scanning.runtime_state as runtime_state
from db.engine import db_session
from helpers.artist_sort import artist_sort_letter_sql, artist_sort_sql, artist_starts_with_letter_sql
from routes.scan_routes import scans_bp
from routes.scan_routes._common import form_bool, is_process_alive, run_async
from services.popularity.pipeline import run_popularity_from_artist, run_popularity_scan
from services.scanning.scan_state import (
    progress_path,
    write_progress_file,
    write_progress_with_current_artist,
)

logger = logging.getLogger(__name__)


def _resolve_first_artist_for_letter(letter: str) -> str:
    """Resolve the first matching local library artist for a starting letter.

    This keeps artist-page scans independent from Navidrome availability/state.

    The letter is matched against the artist's CORE name, and the ordering is
    the same filing order the /artists list uses.  That matters: the list files
    ``The Offspring`` under **O**, so pressing O here must find it and pressing T
    must not.  Matching ``UPPER(artist) LIKE 'O%'`` — the obvious spelling —
    would do exactly the opposite and make the letter tiles disagree with the
    sections they sit above.
    """
    letter_upper = letter.upper()
    artist_expr = "COALESCE(NULLIF(album_artist, ''), artist)"
    letter_expr = artist_sort_letter_sql(artist_expr)
    # The outer query only exposes the inner SELECT's alias, so this MUST order
    # by `t.artist_name` — referencing album_artist/artist out here would name
    # columns that do not exist in the outer scope.
    order_expr = artist_sort_sql("t.artist_name")

    with db_session() as session:
        if letter_upper == "#":
            result = session.execute(
                text(f"""
                    SELECT artist_name FROM (
                        SELECT DISTINCT {artist_expr} AS artist_name
                        FROM tracks
                        WHERE {artist_expr} IS NOT NULL
                          AND {artist_expr} <> ''
                          AND {letter_expr} NOT BETWEEN 'A' AND 'Z'
                    ) t
                    ORDER BY {order_expr}
                    LIMIT 1
                """)
            )
        else:
            result = session.execute(
                text(f"""
                    SELECT artist_name FROM (
                        SELECT DISTINCT {artist_expr} AS artist_name
                        FROM tracks
                        WHERE {artist_expr} IS NOT NULL
                          AND {artist_expr} <> ''
                          AND {artist_starts_with_letter_sql(artist_expr)}
                    ) t
                    ORDER BY {order_expr}
                    LIMIT 1
                """),
                {"prefix": f"{letter_upper}%"},
            )

        row = result.fetchone()
        if not row or not row[0]:
            raise ValueError(f"No artists found in library starting with '{letter}'")

        return str(row[0])


@scans_bp.route("/scan/popularity", methods=["POST"])
async def scan_popularity_route():
    """Run popularity, metadata, or singles scan modes."""
    mode = request.args.get("mode", "all")
    force_start = form_bool(request.args.get("force_start"))

    with runtime_state.scan_lock:
        pop_alive = (
            is_process_alive(runtime_state.scan_process_popularity)
            or runtime_state.is_runtime_running("popularity")
        )
        if pop_alive and not force_start:
            return jsonify({"scan_running": True, "message": "A popularity scan is already running."}), 409

        force_rescan = mode in {
            "force",
            "resume_force",
            "metadata_force",
            "singles_resume_force",
            "singles_detection_force",
        }

        if mode in {"metadata", "metadata_force"}:
            scan_mode = "metadata"
            progress_file = progress_path("metadata_lookup_scan_progress.json")
        elif mode in {"singles", "singles_resume", "singles_resume_force"}:
            scan_mode = "singles"
            progress_file = progress_path("singles_scan_progress.json")
        elif mode in {"singles_detection", "singles_detection_force"}:
            scan_mode = "singles_detection"
            progress_file = progress_path("singles_scan_progress.json")
        elif mode in {"popularity", "missing"}:
            # True popularity-only scan (score + rate on popularity alone).
            scan_mode = "popularity"
            progress_file = progress_path("popularity_scan_progress.json")
        else:
            scan_mode = "all"
            progress_file = progress_path("popularity_scan_progress.json")

        write_progress_file(progress_file, "popularity_scan", True, {"status": "starting"})

        # Build kwargs for the new orchestrator directly
        scan_kwargs: dict[str, Any] = {
            "force": force_rescan,
            "progress_file": progress_file,
        }
        if scan_mode == "metadata":
            scan_kwargs["metadata_only"] = True
        elif scan_mode == "singles":
            scan_kwargs["singles_only"] = True
        elif scan_mode == "singles_detection":
            scan_kwargs["singles_with_missing_popularity"] = True
        elif scan_mode == "popularity":
            scan_kwargs["popularity_only"] = True

        thread = run_async(
            run_popularity_scan,
            daemon=False,
            **scan_kwargs,
        )
        runtime_state.scan_process_popularity = {"thread": thread, "type": "popularity"}

    await flash("✅ Popularity-related scan started", "success")
    return redirect(url_for("ui.dashboard"))


@scans_bp.route("/scan/singles", methods=["POST"])
async def scan_singles():
    """Run single detection only."""
    with runtime_state.scan_lock:
        if is_process_alive(runtime_state.scan_process_singles):
            await flash("Single detection scan is already running", "warning")
            return redirect(url_for("ui.dashboard"))

        progress_file = progress_path("singles_scan_progress.json")
        thread = run_async(
            run_popularity_scan,
            singles_only=True,
            progress_file=progress_file,
            daemon=False,
        )
        runtime_state.scan_process_singles = {"thread": thread, "type": "singles"}

    await flash("✅ Singles detection scan started", "success")
    return redirect(url_for("ui.dashboard"))


@scans_bp.route("/api/scan/from-artist", methods=["POST"])
async def api_scan_from_artist():
    """API endpoint to start a popularity scan from a specific artist or letter.

    Expected JSON payload:
        {
            "artist": "Artist Name",
            "letter": "A",
            "scan_mode": "changes" | "forced"
        }

    If ``letter`` is supplied, the route resolves the first matching artist from
    the local library and starts from that artist.
    """
    try:
        data = (await request.get_json()) or {}

        artist = str(data.get("artist", "") or "").strip()
        letter = str(data.get("letter", "") or "").strip()
        scan_mode = str(data.get("scan_mode", "changes") or "changes").strip().lower()

        if letter:
            try:
                artist = _resolve_first_artist_for_letter(letter)
                logger.info("Letter '%s' scan resolved to artist '%s' from local library", letter, artist)
            except Exception as exc:
                logger.error("Error resolving library artist for letter '%s': %s", letter, exc, exc_info=True)
                return jsonify({"success": False, "error": f"Failed to resolve artist from library: {exc}"}), 500

        if not artist:
            return jsonify({"success": False, "error": "Artist name or letter is required"}), 400

        force_rescan = scan_mode == "forced"
        progress_file = progress_path("popularity_scan_progress.json")
        mode_desc = "Full (Forced)" if force_rescan else "Changes Only"
        message_suffix = f" from library letter '{letter}'" if letter else ""

        with runtime_state.scan_lock:
            if is_process_alive(runtime_state.scan_process_popularity):
                return jsonify({"success": False, "error": "Popularity scan is already running"}), 400

            # Cross-process guard — a scan active in another worker (invisible
            # to the per-process runtime registry) shows as running in the
            # shared DB scan state.  Without it a letter/full scan can overlap
            # a scan started elsewhere and the shared progress row flips to
            # complete as soon as either finishes, hiding the still-running one.
            from services.scanning.pipelines.popularity_pipeline import is_popularity_scan_active

            if is_popularity_scan_active():
                return jsonify({"success": False, "error": "Popularity scan is already running"}), 400

            write_progress_with_current_artist(
                progress_file,
                "popularity_scan",
                True,
                current_artist=artist,
                extra={
                    "status": "starting",
                    "resume_from": artist,
                    "current_artist": artist,
                    "processed_artists": 0,
                    "total_artists": 0,
                    "percent_complete": 0,
                },
            )

            logger.info(
                "Starting popularity scan from artist '%s' (%s)%s",
                artist,
                mode_desc,
                message_suffix,
            )

            thread = run_async(
                run_popularity_from_artist,
                artist=artist,
                force_rescan=force_rescan,
                progress_file=progress_file,
                caller_scan_type="popularity",
                daemon=False,
            )

            runtime_state.scan_process_popularity = {"thread": thread, "type": "popularity"}

        return jsonify(
            {
                "success": True,
                "message": f"Popularity scan started from artist: {artist}",
                "artist": artist,
                "mode": mode_desc,
            }
        )

    except Exception as exc:
        logger.error("Error starting scan from artist: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500
