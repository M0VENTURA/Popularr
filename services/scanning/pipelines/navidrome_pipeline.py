"""
Navidrome import-only scan pipeline.

Uses scan_state as the single source of truth for:
- progress
- stop requests
- checkpoints
"""

from __future__ import annotations

import concurrent.futures
import os
import threading
from typing import Any

import structlog

from helpers.logging_config import log_unified
from services.scanning.scan_history_service import record_scan
from services.scanning.navidrome_import import scan_artist_to_db
from services.scanning.scan_state import (
    clear_scan_checkpoint,
    clear_stop_request,
    get_scan_checkpoint_path,
    get_scan_progress_path,
    is_stop_requested,
    load_scan_checkpoint,
    save_artist_scan_checkpoint,
    write_progress_with_current_artist,
)

logger = structlog.get_logger(__name__)

# Default worker count for parallel artist import.
# Can be overridden via config or env var.
_PARALLEL_WORKERS = int(os.environ.get("POPULARLR_IMPORT_WORKERS", "4"))


def sync_remote_navidrome_before_import(nav_client: Any) -> bool:
    """Trigger ONE Navidrome server-side scan and WAIT until VERIFIED.

    This is the ONLY automatic remote sync: the frequent per-tag-write
    triggers were removed (they paused the server and locked the database).
    The wait ensures freshly written file tags are fully ingested by
    Navidrome before the import reads them back.

    ⚠️ The return value matters: when False the remote scan was NOT
    verified (``startScan`` failed — e.g. the Navidrome user is not admin —
    or the scan never finished).  A caller that proceeds anyway imports
    Navidrome's PRE-save rows, whose upsert OVERWRITES the metadata just
    saved on the album page — "the import puts the old data back".  So this
    says so loudly instead of claiming "scan finished".
    """
    if not nav_client:
        return True  # no client configured — nothing to sync, nothing to verify

    try:
        log_unified("Navidrome Import - Triggering remote Navidrome scan (waiting for completion)…")
        remote_sync_ok = bool(nav_client.trigger_and_wait_for_scan())
        if remote_sync_ok:
            log_unified("Navidrome Import - Remote Navidrome scan finished")
        else:
            log_unified(
                "⚠️ Navidrome Import - Remote Navidrome scan FAILED or timed out. "
                "Navidrome may serve STALE metadata — recently saved edits could be "
                "overwritten by this import. Check that the Navidrome user is an "
                "admin (startScan requires admin) and that Navidrome can scan."
            )
            logger.warning(
                "Navidrome import proceeding WITHOUT a verified remote scan — "
                "stale rows may overwrite recently saved metadata"
            )
        return remote_sync_ok
    except Exception as exc:
        logger.warning(
            "Remote Navidrome sync failed — proceeding with import",
            error=str(exc),
        )
        log_unified(
            f"⚠️ Navidrome Import - Remote scan error: {exc}. "
            "Navidrome may serve STALE metadata to this import."
        )
        return False


def run_navidrome_import_scan(
    *,
    mode: str = "all",
    restart_requested: bool = False,
    progress_file: str | None = None,
    checkpoint_file: str | None = None,
) -> None:

    progress_file = progress_file or get_scan_progress_path("navidrome_scan")
    checkpoint_file = checkpoint_file or get_scan_checkpoint_path("navidrome_scan")

    force_rescan = mode in {"force", "resume_force"}
    filter_missing = mode == "missing"
    workers = _PARALLEL_WORKERS if not force_rescan else 1  # serial for force scans

    record_scan("navidrome", "started", message=f"Navidrome import (mode={mode})")

    try:
        os.environ["SPTNR_SKIP_SINGLES"] = "1"

        if restart_requested:
            clear_scan_checkpoint(checkpoint_file)

        clear_stop_request(progress_file)

        write_progress_with_current_artist(
            progress_file,
            "navidrome_scan",
            True,
            extra={"status": "starting", "mode": mode},
        )

        # Build client
        from api_clients.navidrome import NavidromeClient
        from helpers.config_helpers import get_navidrome_config

        nav_config = get_navidrome_config()
        nav_client = None
        if nav_config:
            nav_client = NavidromeClient(
                base_url=nav_config.get("base_url"),
                username=nav_config.get("user"),
                password=nav_config.get("pass"),
            )

        # ── Remote Navidrome sync BEFORE the import ───────────────────────
        # Verified drain-and-wait (see sync_remote_navidrome_before_import):
        # a False return means Navidrome may serve STALE rows whose upsert
        # would overwrite metadata just saved — the helper logs that loudly.
        remote_sync_ok = sync_remote_navidrome_before_import(nav_client)

        # Check scan marker — skip if nothing changed since last run
        current_marker: int | None = None
        current_last_scan: Any = None
        if nav_client and not force_rescan:
            try:
                scan_status = nav_client.get_scan_status()
                if scan_status.get("success"):
                    current_marker = scan_status.get("count")
                    current_last_scan = scan_status.get("lastScan")
                    checkpoint = load_scan_checkpoint(checkpoint_file)
                    last_marker = checkpoint.get("scan_marker")
                    last_scan_ts = checkpoint.get("last_scan_ts")

                    # Skip only when BOTH the file-count marker and the last
                    # Navidrome scan timestamp are unchanged.
                    marker_unchanged = last_marker is not None and current_marker == last_marker
                    scan_unchanged = (
                        last_scan_ts is not None
                        and current_last_scan is not None
                        and str(last_scan_ts) == str(current_last_scan)
                    )
                    if marker_unchanged and scan_unchanged:
                        # Double-check: if DB has no tracks, don't skip
                        _db_has_tracks = True
                        try:
                            from db.engine import db_session
                            from sqlalchemy import text as sa_text
                            with db_session() as session:
                                _count = session.execute(sa_text("SELECT COUNT(*) FROM tracks")).scalar() or 0
                                _db_has_tracks = _count > 0
                        except Exception:
                            pass
                        if not _db_has_tracks:
                            logger.info("DB empty despite marker — forcing import")
                        else:
                            logger.info(
                                "Navidrome scan marker unchanged — skipping, nothing to import",
                                marker=current_marker,
                            )
                            write_progress_with_current_artist(
                                progress_file,
                                "navidrome_scan",
                                False,
                                extra={
                                    "status": "skipped",
                                    "message": "No new tracks since last scan",
                                    "exit_code": 0,
                                },
                            )
                            log_unified("Navidrome Import - Skipped (no changes)")
                            return
            except Exception as exc:
                logger.debug("Could not check scan marker", error=str(exc))

        # Build artist index
        artist_map: dict[str, dict[str, Any]] = {}
        if nav_client:
            try:
                # Delta scan: only import artists whose albums/songs changed
                # since the previous scan, instead of crawling the full library.
                delta_since = None
                if not force_rescan:
                    checkpoint = load_scan_checkpoint(checkpoint_file)
                    delta_since = checkpoint.get("last_scan_ts") or current_last_scan

                if not force_rescan and delta_since is not None:
                    try:
                        from services.scanning.navidrome_service import build_delta_artist_index
                        delta_map = build_delta_artist_index(
                            nav_client,
                            delta_since,
                            page_size=int(os.environ.get("POPULARLR_IMPORT_PAGE_SIZE", "200")),
                        )
                        if delta_map:
                            artist_map = delta_map
                            logger.info(
                                "Navidrome delta index built",
                                changed_artists=len(delta_map),
                                since=delta_since,
                            )
                            log_unified(
                                f"Navidrome Import - Delta mode: {len(delta_map)} changed artists "
                                f"since {delta_since}"
                            )
                    except Exception as exc:
                        logger.warning(
                            "Delta artist index failed — falling back to full index",
                            error=str(exc),
                        )

                if not artist_map:
                    artist_map = nav_client.build_artist_index() or {}
            except Exception as exc:
                logger.error("Failed to build artist index", error=str(exc))

        artists = list(artist_map.items())
        total = len(artists)
        logger.info(
            "Navidrome import starting",
            artist_count=total,
            workers=workers,
        )

        start_index = 0

        if mode in {"resume", "resume_force"}:
            checkpoint = load_scan_checkpoint(checkpoint_file)
            last_artist = checkpoint.get("last_scanned_artist")

            if last_artist:
                for idx, (artist_name, _) in enumerate(artists):
                    if artist_name == last_artist:
                        start_index = idx + 1
                        break

        # ------------------------------------------------------------------
        # Parallel artist processing
        # ------------------------------------------------------------------
        _progress_lock = threading.Lock()
        _processed_count = [0]  # mutable for closure
        _stop_requested = [False]

        def _process_one(artist_name: str, info: dict[str, Any]) -> bool:
            """Import one artist. Returns True on success."""
            if _stop_requested[0]:
                return False

            artist_id = info.get("id") if isinstance(info, dict) else None
            if not artist_id:
                return False

            scan_artist_to_db(
                artist_name,
                artist_id,
                verbose=False,
                force=force_rescan,
                filter_missing=filter_missing,
                diff_mode=not force_rescan,
                client=nav_client,
            )

            with _progress_lock:
                _processed_count[0] += 1
                idx = _processed_count[0]
                pct = int((idx / total) * 100) if total else 0

                write_progress_with_current_artist(
                    progress_file,
                    "navidrome_scan",
                    True,
                    current_artist=artist_name,
                    extra={
                        "status": "running",
                        "processed_artists": idx,
                        "total_artists": total,
                        "percent_complete": pct,
                    },
                )
                save_artist_scan_checkpoint(
                    artist_name,
                    checkpoint_path=checkpoint_file,
                )
            return True

        if workers > 1 and total > 1:
            # Parallel mode
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_process_one, name, info): name
                    for name, info in artists[start_index:]
                }
                for future in concurrent.futures.as_completed(futures):
                    if is_stop_requested(progress_file):
                        _stop_requested[0] = True
                        for f in futures:
                            f.cancel()
                        break
                    try:
                        future.result()
                    except Exception as exc:
                        logger.error("Artist import failed", error=str(exc))
        else:
            # Serial mode (resume or forced)
            for index, (artist_name, info) in enumerate(
                artists[start_index:], start=start_index + 1
            ):
                if is_stop_requested(progress_file):
                    break
                _process_one(artist_name, info)

        clear_scan_checkpoint(checkpoint_file)

        # Save final marker so next run can skip if nothing changed
        if nav_client and not force_rescan:
            # Re-fetch marker after scan if we skipped the initial check
            try:
                if current_marker is None or current_last_scan is None:
                    scan_status = nav_client.get_scan_status()
                    if current_marker is None:
                        current_marker = scan_status.get("count")
                    if current_last_scan is None:
                        current_last_scan = scan_status.get("lastScan")

                marker_extra: dict[str, Any] = {}
                if current_marker is not None:
                    marker_extra["scan_marker"] = current_marker
                if current_last_scan is not None:
                    marker_extra["last_scan_ts"] = str(current_last_scan)

                if marker_extra:
                    save_artist_scan_checkpoint(
                        "__marker__",
                        checkpoint_path=checkpoint_file,
                        extra=marker_extra,
                    )
            except Exception as exc:
                logger.debug("Could not save scan marker", error=str(exc))

        write_progress_with_current_artist(
            progress_file,
            "navidrome_scan",
            False,
            extra={"status": "complete", "exit_code": 0},
        )

        log_unified(
            "Navidrome Import - Complete"
            + ("" if remote_sync_ok else " (remote scan NOT verified — see warning above)")
        )
        record_scan("navidrome", "completed", message="Navidrome import complete")

    except Exception as exc:
        logger.exception("Navidrome import scan failed", error=str(exc))
        record_scan("navidrome", "failed", message=f"Navidrome import failed: {exc}")

        write_progress_with_current_artist(
            progress_file,
            "navidrome_scan",
            False,
            extra={"status": "error", "error": str(exc), "exit_code": 1},
        )
        raise

    finally:
        os.environ.pop("SPTNR_SKIP_SINGLES", None)