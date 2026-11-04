"""Progress and checkpoint helpers for scanning.

Single source of truth for scan progress tracking across processes.
Provides database-backed checkpointing for resume capability, progress
fetching for WebUI display, and graceful stop/cancel state tracking.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog

from db.engine import db_session
from db.models import ScanState

logger = structlog.get_logger(__name__)

# -------------------------------------------------------------------------
# Internal helpers
# -------------------------------------------------------------------------

def _now() -> str:
    return datetime.now().isoformat()

def _extract_scan_type(path_or_type: str | None, default: str = "library") -> str:
    """
    Parses legacy file paths (e.g., '/database/popularity_progress.json') 
    into clean database primary keys (e.g., 'popularity'). Ensures 100% 
    backward compatibility with old callers passing paths.
    """
    if not path_or_type:
        return default
        
    # Standardize slashes and grab filename
    basename = path_or_type.replace("\\", "/").split("/")[-1]
    
    if basename.endswith("_progress.json"):
        return basename.replace("_progress.json", "")
    if basename.endswith("_checkpoint.json"):
        return basename.replace("_checkpoint.json", "")
        
    return basename

# -------------------------------------------------------------------------
# Progress state helpers
# -------------------------------------------------------------------------

def read_progress_file(path: str) -> dict[str, Any]:
    db_scan_type = _extract_scan_type(path)
    
    with db_session() as session:
        state = session.query(ScanState).filter_by(scan_type=db_scan_type).first()
        if not state:
            return {}
            
        result = {
            "scan_type": state.scan_type,
            "is_running": state.is_running,
            "status": state.status,
            "stop_requested": state.stop_requested,
            "current_artist": state.current_artist,
            "last_updated": state.updated_at.isoformat() if state.updated_at else _now()
        }
        
        # Merge arbitrary JSON payload (processed counts, percent complete, etc.)
        if state.extra_data:
            result.update(state.extra_data)
            
        return result

def write_progress_file(
    path: str,
    scan_type: str,
    is_running: bool,
    extra: dict[str, Any] | None = None,
) -> None:
    write_progress_with_current_artist(path, scan_type, is_running, extra=extra)

def write_progress_with_current_artist(
    path: str,
    scan_type: str,
    is_running: bool,
    current_artist: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    db_scan_type = _extract_scan_type(path, scan_type)
    extra = extra or {}
    
    status = extra.get("status", "running" if is_running else "idle")
    stop_requested = extra.get("stop_requested", False)
    
    with db_session() as session:
        state = session.query(ScanState).filter_by(scan_type=db_scan_type).first()
        if not state:
            state = ScanState(scan_type=db_scan_type)
            session.add(state)
            
        state.is_running = is_running
        state.status = status
        
        # Determine the artist priority — the value MUST be a plain string:
        # ``current_artist`` is a VARCHAR column and psycopg2 cannot adapt a
        # dict/list.  Several callers used to pass a dict
        # (``{"current_artist": ..., "processed_artists": ..., ...}``) via
        # ``extra``, which crashed the whole write with "can't adapt type
        # 'dict'" — the scan then started without a progress row, the
        # dashboard showed nothing, and the first checkpoint write later
        # re-created the row.  Defensively coerce any non-string value to a
        # display string (dicts become JSON-ish text) so no caller can ever
        # re-introduce the crash.
        final_artist = current_artist or extra.get("current_artist") or extra.get("resume_from")
        if final_artist is not None:
            if isinstance(final_artist, str):
                state.current_artist = final_artist
            else:
                try:
                    import json as _json
                    state.current_artist = _json.dumps(final_artist, default=str)[:255]
                except Exception:
                    state.current_artist = str(final_artist)[:255]
            
        if "stop_requested" in extra:
            state.stop_requested = stop_requested
            
        # Safely preserve and update JSON extra_data without overwriting top-level columns
        current_extra = dict(state.extra_data or {})
        top_level_keys = {"scan_type", "is_running", "status", "stop_requested", "current_artist", "last_updated", "resume_from"}
        
        for k, v in extra.items():
            if k not in top_level_keys:
                current_extra[k] = v
                
        state.extra_data = current_extra
        session.commit()

def clear_progress_file(path: str) -> None:
    db_scan_type = _extract_scan_type(path)
    with db_session() as session:
        session.query(ScanState).filter_by(scan_type=db_scan_type).delete()
        session.commit()


def reset_stale_scan_states() -> int:
    """Reset scan-state rows left ``running`` by a crash or reboot.

    The ``scan_states`` row is the cross-process "is a scan running?" flag
    the scheduler and dashboard consult.  When the server is killed mid-scan
    (reboot, OOM, ``docker stop``) the normal completion path never runs, so
    ``is_running`` stays ``True`` forever: the dashboard shows the scan as
    active, scheduled scans are skipped as "already running", and a stale
    ``stop_requested`` flag can abort the next scan immediately.

    Called once at startup (schema bootstrap — before any worker spawns) and
    again as a safety net when a worker boots.  Idempotent and safe to call
    concurrently: it only touches rows that are still flagged running, and no
    scan can have started yet.

    Returns the number of rows reset.
    """
    try:
        with db_session() as session:
            stale = session.query(ScanState).filter(
                ScanState.is_running.is_(True)
            ).all()
            for state in stale:
                state.is_running = False
                state.status = "interrupted"
                state.stop_requested = False
                # current_artist is kept so the dashboard can show where the
                # interrupted scan stopped (resume uses it as a hint).
            # A stopped row (is_running=False) can still carry a stale
            # stop_requested flag from a previous session's "Stop" click.
            # After a reboot that flag must not abort the next scan the
            # instant it starts — clear it too.  The count only includes the
            # is_running rows (the interrupted ones), matching the log line.
            stopped_stale = session.query(ScanState).filter(
                ScanState.is_running.is_(False),
                ScanState.stop_requested.is_(True),
            ).all()
            for state in stopped_stale:
                state.stop_requested = False
                if state.status == "stop_requested":
                    state.status = "idle"
            session.commit()
            return len(stale)
    except Exception as exc:
        logger.warning("Failed to reset stale scan states", error=str(exc))
        return 0

# -------------------------------------------------------------------------
# Stop / cancel helpers
# -------------------------------------------------------------------------

def request_scan_stop(path: str, scan_type: str = "library") -> None:
    db_scan_type = _extract_scan_type(path, scan_type)
    with db_session() as session:
        state = session.query(ScanState).filter_by(scan_type=db_scan_type).first()
        if not state:
            state = ScanState(scan_type=db_scan_type)
            session.add(state)
            
        state.is_running = False
        state.status = "stop_requested"
        state.stop_requested = True
        session.commit()

def is_stop_requested(path: str) -> bool:
    db_scan_type = _extract_scan_type(path)
    try:
        with db_session() as session:
            state = session.query(ScanState).filter_by(scan_type=db_scan_type).first()
            if not state:
                return False
            return bool(state.stop_requested) or state.status == "stop_requested"
    except Exception as exc:
        # FAIL OPEN. This is a control check called between every artist
        # (and every batch); a transient DB blip (Postgres restart, dropped
        # connection) used to raise out of the scan loop and abort the WHOLE
        # import with "Navidrome import scan failed". Not being able to read
        # the stop flag is not a reason to fail the scan — the next DB write
        # will surface a genuine outage, where it can be retried/deferred
        # properly.
        logger.warning(
            "Could not read scan stop state — assuming not stopped",
            scan_type=db_scan_type,
            error=str(exc),
        )
        return False

def clear_stop_request(path: str) -> None:
    db_scan_type = _extract_scan_type(path)
    with db_session() as session:
        state = session.query(ScanState).filter_by(scan_type=db_scan_type).first()
        if state:
            state.stop_requested = False
            if state.status == "stop_requested":
                state.status = "running" if state.is_running else "idle"
            session.commit()

# -------------------------------------------------------------------------
# Checkpoint helpers
# -------------------------------------------------------------------------

def load_scan_checkpoint(path: str | None = None) -> dict[str, Any]:
    db_scan_type = _extract_scan_type(path, "library")
    with db_session() as session:
        state = session.query(ScanState).filter_by(scan_type=db_scan_type).first()
        if not state or not state.last_scanned_artist:
            return {}

        checkpoint = {
            "last_scanned_artist": state.last_scanned_artist,
            "updated_at": state.updated_at.isoformat() if state.updated_at else _now()
        }

        # Merge arbitrary JSON payload (scan markers, delta timestamps, etc.)
        # so callers can read scan_marker / last_scan_ts back from the checkpoint.
        if state.extra_data:
            checkpoint.update(state.extra_data)

        return checkpoint

def save_artist_scan_checkpoint(
    artist_name: str,
    checkpoint_path: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    db_scan_type = _extract_scan_type(checkpoint_path, "library")
    with db_session() as session:
        state = session.query(ScanState).filter_by(scan_type=db_scan_type).first()
        if not state:
            state = ScanState(scan_type=db_scan_type)
            session.add(state)
            
        state.last_scanned_artist = artist_name
        
        if extra:
            current_extra = dict(state.extra_data or {})
            current_extra.update(extra)
            state.extra_data = current_extra
            
        session.commit()

def get_last_scanned_artist(path: str | None = None) -> str | None:
    checkpoint = load_scan_checkpoint(path)
    value = checkpoint.get("last_scanned_artist")
    return str(value) if value else None

def clear_scan_checkpoint(checkpoint_path: str | None = None) -> None:
    db_scan_type = _extract_scan_type(checkpoint_path, "library")
    with db_session() as session:
        state = session.query(ScanState).filter_by(scan_type=db_scan_type).first()
        if state:
            state.last_scanned_artist = None
            session.commit()

# -------------------------------------------------------------------------
# Compatibility aliases
# -------------------------------------------------------------------------

def get_resume_artist(path: str | None = None) -> str | None:
    return get_last_scanned_artist(path)

def save_progress(artist_name: str, checkpoint_path: str | None = None) -> None:
    save_artist_scan_checkpoint(artist_name, checkpoint_path)

# -------------------------------------------------------------------------
# Legacy Path Resolvers (Kept for external caller compatibility)
# -------------------------------------------------------------------------

def progress_path(filename: str) -> str:
    return filename

def get_navidrome_progress_path() -> str:
    return "navidrome"

def get_library_progress_path() -> str:
    return "library"

def get_scan_progress_path(scan_type: str = "library") -> str:
    return scan_type

def get_navidrome_checkpoint_path() -> str:
    return "navidrome"

def get_library_checkpoint_path() -> str:
    return "library"

def get_scan_checkpoint_path(scan_type: str = "library") -> str:
    return scan_type

def mark_navidrome_first_full_import_complete(scan_source: str = "unknown") -> None:
    with db_session() as session:
        state = session.query(ScanState).filter_by(scan_type="navidrome_first_import").first()
        if not state:
            state = ScanState(scan_type="navidrome_first_import")
            session.add(state)
        state.extra_data = {"complete": True, "scan_source": scan_source, "timestamp": _now()}
        session.commit()
