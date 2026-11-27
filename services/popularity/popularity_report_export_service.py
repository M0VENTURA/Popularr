"""Background jobs for the collection-wide popularity CSV.

The collection report reads the whole ``tracks`` table, so building it inside
the request outlives the reverse proxy's ~100s origin timeout (the reported
timeout). This runs it on a daemon thread, writes the CSV to the system temp
dir, and returns a job id the Config page (and the dashboard) can poll.

Mirrors ``services.playlists.playlist_export_service``: in-memory jobs, a
bounded history, and a download endpoint that serves the finished file.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
import time
import uuid
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_JOBS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()
_MAX_JOBS = 5

_UNSAFE_RE = re.compile(r'[\\/:*?"<>|]+')


def _export_dir(job_id: str) -> str:
    return os.path.join(tempfile.gettempdir(), "popularr_exports", f"report-{job_id}")


def _prune_locked() -> None:
    """Keep only the newest ``_MAX_JOBS`` jobs and delete older files."""
    if len(_JOBS) <= _MAX_JOBS:
        return
    ordered = sorted(_JOBS.values(), key=lambda j: j.get("created") or 0)
    for old in ordered[: len(_JOBS) - _MAX_JOBS]:
        _JOBS.pop(old.get("id"), None)
        path = old.get("path")
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            os.rmdir(_export_dir(str(old.get("id"))))
        except OSError:
            pass


def _worker(job: dict[str, Any]) -> None:
    try:
        from services.popularity.popularity_report_service import (
            build_collection_popularity_report,
            ensure_playlist_index,
            playlist_index_has_entries,
            popularity_report_filename,
        )

        # ⚠️⚠️ THE FILE IS THE POINT — DO NOT MAKE IT WAIT FOR A PLAYLIST SYNC.
        #
        # Reported: "collection csv seems to take a long time to complete".
        # The playlist column is DERIVED from the index this refresh maintains,
        # and the refresh re-fetches every playlist whose changed-stamp moved —
        # one Navidrome round trip each (~5s per ~1,200-track playlist; the
        # service's own note records 66 changed playlists taking 5m36s). The
        # export sat in "pending" through ALL of it before writing a row.
        #
        # So: refresh only when the index is EMPTY (a first export needs it, or
        # the column would be blank), build and publish the CSV, then bring the
        # index up to date afterwards for the NEXT export. The column can be one
        # export behind; it is never wrong, only older.
        if not playlist_index_has_entries():
            job["phase"] = "Building the playlist index (first export)…"
            try:
                ensure_playlist_index()
            except Exception as exc:
                logger.debug(
                    "Playlist index refresh failed before report", error=str(exc)
                )

        job["status"] = "running"
        job["phase"] = "Building the CSV…"
        report = build_collection_popularity_report() or ""

        filename = popularity_report_filename()
        filename = _UNSAFE_RE.sub("_", filename) or "popularity-report.csv"
        directory = _export_dir(job["id"])
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, filename)
        # ⚠️ BOM and body written separately. ``"\ufeff" + report`` copied the
        # whole multi-megabyte string just to prepend one character.
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write("\ufeff")
            handle.write(report)

        job.update({
            "status": "done",
            "path": path,
            "filename": filename,
            # Measuring the written file avoids encoding a second full copy of
            # the CSV purely to learn its size.
            "bytes": os.path.getsize(path),
            "rows": max(0, report.count("\n") - 1),
        })

        # Housekeeping AFTER the download is available. Failure is harmless.
        job["phase"] = "Refreshing the playlist index…"
        try:
            ensure_playlist_index()
        except Exception as exc:
            logger.debug("Playlist index refresh after report failed", error=str(exc))
    except Exception as exc:
        logger.error("Popularity report export failed", error=str(exc))
        job.update({"status": "error", "error": str(exc)})
    finally:
        job["finished"] = time.time()


def create_report_job() -> dict[str, Any]:
    """Start a collection popularity CSV job; returns the job view."""
    job: dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "status": "pending",
        "path": None,
        "filename": None,
        "bytes": 0,
        "rows": 0,
        "error": None,
        "created": time.time(),
        "finished": None,
    }
    with _LOCK:
        _prune_locked()
        _JOBS[job["id"]] = job

    thread = threading.Thread(
        target=_worker, args=(job,), name="popularity-report-export", daemon=True
    )
    thread.start()
    return _view(job)


def _view(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job.get("id"),
        "status": job.get("status"),
        "filename": job.get("filename"),
        "bytes": job.get("bytes") or 0,
        "rows": job.get("rows") or 0,
        "error": job.get("error"),
        "created": job.get("created"),
        "finished": job.get("finished"),
        "download_url": (
            f"/api/popularity/report/download/{job.get('id')}"
            if job.get("status") == "done"
            else None
        ),
    }


def get_report_job(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        job = _JOBS.get(str(job_id))
        return _view(job) if job else None


def latest_completed_job() -> dict[str, Any] | None:
    """The newest finished report, for the dashboard's ready card."""
    with _LOCK:
        done = [j for j in _JOBS.values() if j.get("status") == "done"]
        if not done:
            return None
        return _view(max(done, key=lambda j: j.get("finished") or 0))


def report_file(job_id: str) -> tuple[str, str] | None:
    """``(path, filename)`` for a finished job, or None."""
    with _LOCK:
        job = _JOBS.get(str(job_id))
        if not job or job.get("status") != "done":
            return None
        path = str(job.get("path") or "")
        filename = str(job.get("filename") or "popularity-report.csv")
    return (path, filename) if path and os.path.isfile(path) else None
