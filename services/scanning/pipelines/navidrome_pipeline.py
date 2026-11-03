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
from datetime import datetime, timezone

from db.engine import db_session
from sqlalchemy import text
from typing import Any

import structlog

from helpers.logging_config import log_unified
from services.scanning.scan_history_service import record_scan
from services.scanning.navidrome_import import (
    reset_album_dedupe,
    scan_artist_to_db,
)
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

logger = structlog.get_logger("popularr.navidrome_import")

# Default worker count for parallel artist import.
#
# ⚠️ KEEP THIS LOW. Each artist worker fetches its album tracklists with up to
# ``navidrome.album_fetch_workers`` (default 4) threads, so 4 artists could put
# 16 concurrent requests on a SQLite-backed Navidrome. The reported burst drove
# `Fetched X album(s)` from 0.0s to 6.7–9.8s. Two artists (2 × 4 = 8 requests)
# keeps the import moving without the stall. Overridable via env/config.
_PARALLEL_WORKERS = max(
    1, min(4, int(os.environ.get("POPULARLR_IMPORT_WORKERS", "2")))
)


def _epoch(value: Any) -> float | None:
    """Epoch seconds for a Navidrome/DB timestamp, or None when unparseable."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        return float(value)
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _latest_dirty_local_write_epoch() -> float | None:
    """Newest local tag write among dirty rows, or None."""
    try:
        with db_session() as session:
            value = session.execute(
                text(
                    "SELECT MAX(local_tags_written_at) FROM tracks "
                    "WHERE COALESCE(metadata_dirty_for_navidrome, FALSE) = TRUE"
                )
            ).scalar()
        return _epoch(value)
    except Exception:
        return None


def _skip_scan_after_local_write_enabled() -> bool:
    try:
        from helpers.config_helpers import get_config

        return bool(
            (get_config().get("navidrome") or {}).get(
                "skip_scan_after_local_write", True
            )
        )
    except Exception:
        return True


def sync_remote_navidrome_before_import(
    nav_client: Any,
    *,
    checkpoint_file: str | None = None,
    use_dirty_state: bool = False,
    force: bool = False,
) -> bool:
    """Ensure Navidrome has ingested local file changes, VERIFIED.

    Direct callers (and older call sites) get the original behaviour: one
    drain-and-wait scan. The import passes ``use_dirty_state=True`` so it only
    starts a scan when there is a durable reason to (dirty local corrections or
    no successful-import checkpoint yet). If no scan is needed it still proves
    the server is reachable/current; otherwise it refuses the import.

    ⚠️ The return value matters: when False the import MUST NOT read Navidrome.
    An unverified scan can expose the OLD indexed metadata, and the upsert
    would then overwrite the correction Popularr just saved.
    """
    if not nav_client:
        return True  # no client configured — nothing to sync, nothing to verify

    if use_dirty_state and not force:
        try:
            from services.scanning.navidrome_rescan_service import (
                consume_rescan_required,
                is_rescan_required,
            )
            from services.scanning.reconciliation import (
                has_dirty_navidrome_metadata,
            )
            from services.scanning.scan_state import load_scan_checkpoint

            checkpoint = (
                load_scan_checkpoint(checkpoint_file)
                if checkpoint_file
                else {}
            )
            dirty = bool(has_dirty_navidrome_metadata() or is_rescan_required())
            has_successful_import = bool(
                checkpoint.get("last_successful_import_ts")
            )
            if dirty and _skip_scan_after_local_write_enabled():
                # Dirty rows do not mean Navidrome has not scanned since the
                # write. Re-triggering a full library scan on every import is
                # what made repeated imports slow. If the last completed scan
                # is at/after the newest local tag write, read that verified
                # state instead.
                status = nav_client.get_scan_status()
                latest_write = _latest_dirty_local_write_epoch()
                last_scan = _epoch(status.get("lastScan"))
                if (
                    status.get("success")
                    and not status.get("scanning")
                    and latest_write is not None
                    and last_scan is not None
                    and last_scan >= latest_write
                ):
                    consume_rescan_required()
                    logger.info(
                        "Navidrome already scanned after the latest local tag write",
                        last_scan=status.get("lastScan"),
                        latest_local_write=latest_write,
                    )
                    log_unified(
                        "Navidrome Import - Navidrome already scanned after the "
                        "latest local write; using that verified state"
                    )
                    return True
            if not dirty and has_successful_import:
                status = nav_client.get_scan_status()
                if status.get("success") and not status.get("scanning"):
                    logger.info(
                        "Navidrome import freshness already established — no new scan needed"
                    )
                    log_unified(
                        "Navidrome Import - Using current verified Navidrome state "
                        "(no metadata changes pending)"
                    )
                    return True
                log_unified(
                    "⚠️ Navidrome Import deferred — Navidrome is still scanning or "
                    "its state cannot be verified"
                )
                logger.warning(
                    "Navidrome import deferred — freshness cannot be verified",
                    error=status.get("error"),
                )
                return False
            consume_rescan_required()
        except Exception as exc:
            logger.warning(
                "Navidrome import freshness pre-check failed — refusing to import stale state",
                error=str(exc),
            )
            return False

    try:
        log_unified("Navidrome Import - Triggering remote Navidrome scan (waiting for completion)…")
        remote_sync_ok = bool(nav_client.trigger_and_wait_for_scan())
        if remote_sync_ok:
            log_unified("Navidrome Import - Remote Navidrome scan finished")
        else:
            log_unified(
                "⚠️ Navidrome Import DEFERRED - Remote Navidrome scan FAILED or "
                "timed out. Navidrome may serve STALE metadata, so this import "
                "will NOT run and existing local metadata is preserved. Check "
                "that the Navidrome user is an admin (startScan requires admin) "
                "and that Navidrome can scan."
            )
            logger.warning(
                "Navidrome import deferred — remote scan not verified; "
                "stale rows cannot overwrite local metadata"
            )
        return remote_sync_ok
    except Exception as exc:
        logger.warning(
            "Remote Navidrome sync failed — import deferred",
            error=str(exc),
        )
        log_unified(
            f"⚠️ Navidrome Import DEFERRED - Remote scan error: {exc}. "
            "Existing local metadata is preserved."
        )
        return False


def ensure_navidrome_import_freshness(checkpoint_file: str | None = None) -> bool:
    """Prove Navidrome is current ONCE, for a scan that imports many artists.

    ``scan_artist_to_db`` verifies freshness when the caller does not vouch for
    it, and the dirty test is GLOBAL — one row with
    ``metadata_dirty_for_navidrome`` makes every per-artist import trigger
    ``trigger_and_wait_for_scan``. In a full scan that turned one pending
    correction into a Navidrome scan-and-wait per artist. The orchestrator now
    calls this once and passes ``freshness_verified=True`` down.

    Returns False when freshness could not be verified; the caller must then
    fall back to per-artist verification (None), never to the unverified path.
    """
    from services.scanning.navidrome_import import _get_fallback_client

    client = _get_fallback_client()
    if client is None:
        # No Navidrome configured — nothing to verify, nothing to import.
        return True
    return sync_remote_navidrome_before_import(
        client,
        checkpoint_file=checkpoint_file,
        use_dirty_state=True,
    )


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
        # One authoritative import at a time. The progress file is the existing
        # cross-process signal (web workers + standalone queue worker).
        try:
            from services.navidrome.import_gate import is_import_running_uncached

            if is_import_running_uncached():
                logger.warning("Navidrome import already running — not starting a second")
                log_unified("Navidrome Import - skipped (another import is already running)")
                record_scan(
                    "navidrome",
                    "failed",
                    message="Navidrome import skipped: another import is already running",
                )
                return
        except Exception:
            pass  # fail open only for the guard read; the import itself still runs

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
                # ⚠️ THIS client is exempt from the import gate. The import
                # defers other work's incidental Subsonic traffic while it runs
                # (see services/navidrome/import_gate), so the import's own
                # client must be marked or it would defer ITSELF — every
                # getArtists/getAlbum returning {} and the import silently
                # discovering nothing.
                is_import=True,
            )

        # ── Remote Navidrome sync BEFORE the import ───────────────────────
        # Verified drain-and-wait. The import only asks for a scan when local
        # metadata is dirty or no successful-import checkpoint exists yet.
        remote_sync_ok = sync_remote_navidrome_before_import(
            nav_client,
            checkpoint_file=checkpoint_file,
            use_dirty_state=True,
            force=force_rescan,
        )
        if not remote_sync_ok:
            # FAIL SAFE: never import an unverified Navidrome view. Existing
            # local metadata is known-good and the checkpoint must not move.
            write_progress_with_current_artist(
                progress_file,
                "navidrome_scan",
                False,
                extra={
                    "status": "deferred",
                    "message": "Remote Navidrome scan could not be verified",
                    "reason": "remote scan not verified",
                    "exit_code": 0,
                },
            )
            log_unified(
                "Navidrome Import - DEFERRED (remote scan not verified); "
                "local metadata preserved and import checkpoint unchanged"
            )
            record_scan(
                "navidrome",
                "deferred",
                message="Navidrome import deferred: remote scan not verified",
            )
            return

        try:
            from services.scanning.reconciliation import (
                has_dirty_navidrome_metadata,
            )

            _metadata_dirty = bool(has_dirty_navidrome_metadata())
        except Exception:
            # Unknown is not "clean": force the full/reconciling path.
            _metadata_dirty = True

        checkpoint = load_scan_checkpoint(checkpoint_file)

        # Check scan marker — skip if nothing changed since the last SUCCESSFUL
        # import. Dirty local metadata forbids the skip: the import still has
        # to observe/confirm the corrected values.
        current_marker: int | None = None
        current_last_scan: Any = None
        if nav_client and not force_rescan and not _metadata_dirty:
            try:
                scan_status = nav_client.get_scan_status()
                if scan_status.get("success"):
                    current_marker = scan_status.get("count")
                    current_last_scan = scan_status.get("lastScan")
                    last_marker = checkpoint.get("last_successful_import_marker")
                    last_scan_ts = checkpoint.get("last_successful_import_ts")

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
                                    "message": "No new tracks since last successful import",
                                    "exit_code": 0,
                                },
                            )
                            log_unified("Navidrome Import - Skipped (no changes)")
                            return
            except Exception as exc:
                logger.debug("Could not check scan marker", error=str(exc))

        # The per-album fast-skip state stores the Navidrome scan marker. Fetch
        # it even when dirty metadata prevented the marker-based whole-import
        # skip above; an empty marker simply disables the per-album fast path.
        if nav_client and current_last_scan is None:
            try:
                _status = nav_client.get_scan_status()
                if _status.get("success"):
                    current_last_scan = _status.get("lastScan")
                    current_marker = _status.get("count")
            except Exception as exc:
                logger.debug("Could not read scan marker for album state", error=str(exc))
        _scan_marker = (
            str(current_last_scan) if current_last_scan not in (None, "") else ""
        )

        # Build artist index
        artist_map: dict[str, dict[str, Any]] = {}
        if nav_client:
            try:
                # Delta scan: only import artists whose albums/songs changed
                # since the previous SUCCESSFUL import. There is deliberately
                # NO fallback to the scan that just completed — that timestamp
                # describes the wrong baseline and can exclude the very changes
                # this import is meant to read.
                delta_since = None
                # A dirty local correction is not a reason to re-read every
                # unchanged artist. The correction is preserved in the DB and
                # will be confirmed when Navidrome actually changes; using the
                # delta index here avoids a full-library tracklist crawl on
                # every import while dirty rows exist.
                if not force_rescan:
                    delta_since = checkpoint.get("last_successful_import_ts")

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
        reset_album_dedupe()
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
        _import_success = [True]

        def _process_one(artist_name: str, info: dict[str, Any]) -> bool:
            """Import one artist. Returns True on success."""
            if _stop_requested[0]:
                return False

            artist_id = info.get("id") if isinstance(info, dict) else None
            if not artist_id:
                return False

            _artist_result = scan_artist_to_db(
                artist_name,
                artist_id,
                verbose=False,
                force=force_rescan,
                filter_missing=filter_missing,
                diff_mode=not force_rescan and not _metadata_dirty,
                client=nav_client,
                freshness_verified=True,
                dedupe_albums=True,
                scan_marker=_scan_marker,
            )
            if isinstance(_artist_result, dict):
                if _artist_result.get("success") is False:
                    _import_success[0] = False
                    logger.warning(
                        "Artist import did not complete — successful-import "
                        "checkpoint will not advance",
                        artist=artist_name,
                        reason=_artist_result.get("reason"),
                    )
                if int(_artist_result.get("metadata_conflicts") or 0) > 0:
                    # A preserved local correction is NOT a failed import: the
                    # verified Navidrome state was read and the dirty rows stay
                    # protected. Blocking the checkpoint here made every later
                    # import re-read the same artists forever (the reported
                    # repeated conflict/import loop).
                    logger.warning(
                        "Artist import preserved local corrections; checkpoint "
                        "advances, rows remain dirty until Navidrome confirms",
                        artist=artist_name,
                        conflicts=_artist_result.get("metadata_conflicts"),
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
                        _import_success[0] = False
                        for f in futures:
                            f.cancel()
                        break
                    try:
                        future.result()
                    except Exception as exc:
                        _import_success[0] = False
                        logger.error("Artist import failed", error=str(exc))
        else:
            # Serial mode (resume or forced)
            for index, (artist_name, info) in enumerate(
                artists[start_index:], start=start_index + 1
            ):
                if is_stop_requested(progress_file):
                    _import_success[0] = False
                    break
                _process_one(artist_name, info)

        if not _import_success[0]:
            write_progress_with_current_artist(
                progress_file,
                "navidrome_scan",
                False,
                extra={
                    "status": "partial",
                    "message": "Import stopped or an artist import failed",
                    "exit_code": 1,
                },
            )
            log_unified(
                "Navidrome Import - PARTIAL; checkpoint NOT advanced so the "
                "next run retries the incomplete reconciliation"
            )
            record_scan(
                "navidrome",
                "failed",
                message="Navidrome import partial — checkpoint not advanced",
            )
            return

        clear_scan_checkpoint(checkpoint_file)

        # Save the SUCCESSFUL-import checkpoint. The marker/timestamp describe
        # the last completed and committed import, never a scan that merely ran.
        if nav_client and not force_rescan:
            try:
                if current_marker is None or current_last_scan is None:
                    scan_status = nav_client.get_scan_status()
                    if current_marker is None:
                        current_marker = scan_status.get("count")
                    if current_last_scan is None:
                        current_last_scan = scan_status.get("lastScan")

                marker_extra: dict[str, Any] = {}
                if current_marker is not None:
                    marker_extra["last_successful_import_marker"] = current_marker
                    marker_extra["scan_marker"] = current_marker
                if current_last_scan is not None:
                    marker_extra["last_successful_import_ts"] = str(current_last_scan)
                    marker_extra["last_scan_ts"] = str(current_last_scan)
                marker_extra["last_successful_import_at"] = datetime.now().isoformat()

                # A successful import makes previously rejected playlist IDs
                # worth retrying: the mapping may have been refreshed.
                try:
                    from services.playlists.song_id_reconciliation import (
                        clear_playlist_song_rejections,
                        server_key,
                    )

                    clear_playlist_song_rejections(server_key(nav_client))
                except Exception as exc:
                    logger.debug("Playlist rejection retry reset failed", error=str(exc))

                if marker_extra:
                    save_artist_scan_checkpoint(
                        "__marker__",
                        checkpoint_path=checkpoint_file,
                        extra=marker_extra,
                    )
            except Exception as exc:
                logger.debug("Could not save successful-import checkpoint", error=str(exc))

        write_progress_with_current_artist(
            progress_file,
            "navidrome_scan",
            False,
            extra={"status": "complete", "exit_code": 0},
        )

        log_unified("Navidrome Import - Complete")
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