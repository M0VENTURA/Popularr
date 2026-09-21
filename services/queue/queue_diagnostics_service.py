"""Queue diagnostics service.

Provides monitoring and diagnostic utilities for the download queue:
    - Queue processor status and health checks.
    - Queue event logging and retrieval.
    - Queue event search and filtering.
    - Collection batch status checks.
    - slskd eligibility diagnostics.

Architecture:
    Maintains an in-memory event store (thread-safe) for recent queue
    events. Delegates persistent state queries to the queue repository.

    Event Store:
        Thread-safe list of dicts with type, message, and extra metadata.
        Used for real-time monitoring in the WebUI.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from db.repositories.queue import (
    get_queue_status_counts,
)

from db.repositories.tracks import (
    find_library_track,
)
from helpers.logging_config import log_queue
from helpers.response_helpers import _ok, _fail, _safe_int

logger = logging.getLogger(__name__)

_queue_events_lock = threading.Lock()
_queue_events: list[dict[str, Any]] = []


# =============================================================================
# EVENT STORE
# =============================================================================

def log_queue_event(
    event_type: str,
    message: str,
    **extra,
) -> None:
    event = {
        "type": event_type,
        "event_type": event_type,
        "message": message,
        **extra,
    }
    event.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    event.setdefault("created_at", event["timestamp"])

    with _queue_events_lock:
        _queue_events.append(event)

        if len(_queue_events) > 5000:
            del _queue_events[:-5000]

    # Persist to the dedicated queue log so the full history is available on
    # the /logs page (and survives worker/WebUI restarts).
    try:
        log_queue(f"[{event_type.upper()}] {message}")
    except Exception:
        pass


def clear_queue_events() -> None:
    with _queue_events_lock:
        _queue_events.clear()


def get_queue_events(
    limit: int = 50,
    event_type: str | None = None,
) -> list[dict[str, Any]]:

    with _queue_events_lock:

        events = list(
            reversed(_queue_events)
        )

        if event_type:
            events = [
                event
                for event in events
                if event.get("type") == event_type
            ]

        return events[:limit]


# =============================================================================
# PROCESSOR
# =============================================================================

def queue_processor_status():
    """
    Placeholder until queue processor state
    is fully centralized.
    """

    return _ok(
        running=False,
        migrated=False,
        message="Queue processor status not implemented",
    )


def queue_processor_restart():
    """
    Queue processor restart placeholder.
    """
    import subprocess, sys
    try:
        # Signal the queue processor to restart via its health-check file
        proc_file = os.environ.get("QUEUE_PROCESSOR_HEALTH_FILE", "")
        if proc_file and os.path.isfile(proc_file):
            os.remove(proc_file)
        return _ok(message="Queue processor restart signal sent")
    except Exception as exc:
        return _fail(str(exc))


# =============================================================================
# EVENTS
# =============================================================================

def _tail_queue_log(limit: int = 100) -> list[dict[str, Any]]:
    """Read the tail of the queue worker's log file as a fallback.

    The queue worker runs as a separate process, so the WebUI process's
    in-memory event store is empty across restarts.  Queue activity is written
    to ``queue.log`` (via ``log_queue_event``/``log_queue``); this function
    tails that file so the monitor viewer is never blank.  ``unified_scan.log``
    / ``info.log`` are only used as a legacy fallback when ``queue.log`` does
    not exist yet.

    Only meaningful download/queue lines are kept — scheduler bookkeeping
    (APScheduler registrations), SQL parameter dumps and Soulseek search lines
    are filtered out so search activity never leaks into the queue log.
    """
    # Noise markers: scheduler registrations, SQLAlchemy/psycopg2 dumps and
    # Soulseek search chatter must never surface in the queue activity viewer.
    _NOISE_MARKERS = (
        "APScheduler",
        "registered ",
        "Added job",
        "job store",
        "parameters:",
        "job_state",
        "next_run_time",
        "Binary object",
        "psycopg2",
        "[SEARCH]",
        "[SEARCH_LOG]",
        "search-events",
        "slskd_search_logs",
    )
    # Queue-relevant line markers. Keep the set tight enough to exclude
    # popularity/scan chatter but broad enough to cover every worker prefix.
    _QUEUE_MARKERS = (
        "[QUEUE",
        "[QUEUE_WORKER",
        "[PIPELINE]",
        "[COMPLETE]",
        "[MONITOR_FOLDER]",
        "[IMPORT]",
        "[START_DOWNLOAD]",
        "[RETRY_SCHEDULER]",
        "[AUTO_DISCOVERY]",
        "Soulseek",
        "slskd",
        "download_queue",
    )
    # Read more than 64KB: a busy popularity scan can push queue lines out of
    # a small tail window, leaving the viewer "stale" even though fresh
    # queue activity exists further up the file.
    _TAIL_BYTES = 256 * 1024
    _TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    try:
        from helpers.logging_config import resolve_log_dir
        import glob as _glob

        log_dir = resolve_log_dir()
        # Prefer the dedicated queue log family (queue.log + rotated backups).
        # When that family exists but has no matching lines yet (e.g. right
        # after startup) fall back to the shared scan logs so the monitor is
        # never blank; search lines are excluded via the markers above.
        queue_candidates = sorted(
            _glob.glob(os.path.join(log_dir, "queue.log") + "*"),
            key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
            reverse=True,
        )
        fallback_candidates = []
        for name in ("unified_scan.log", "info.log"):
            base = os.path.join(log_dir, name)
            fallback_candidates.extend(_glob.glob(base + "*"))
        fallback_candidates.sort(
            key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
            reverse=True,
        )

        def _collect(candidates: list[str]) -> list[dict[str, Any]]:
            collected: list[dict[str, Any]] = []
            for path in candidates:
                if not os.path.exists(path):
                    continue
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                        fh.seek(0, os.SEEK_END)
                        size = fh.tell()
                        fh.seek(max(0, size - min(size, _TAIL_BYTES)))
                        chunk = fh.read()
                    file_events = 0
                    for line in reversed(chunk.splitlines()):
                        line = line.strip()
                        if not line:
                            continue
                        if any(n in line for n in _NOISE_MARKERS):
                            continue
                        if any(k in line for k in _QUEUE_MARKERS):
                            ts_match = _TS_RE.search(line)
                            created_at = None
                            if ts_match:
                                # ISO-ish (T separator) so the browser parses
                                # it reliably in every engine.
                                created_at = ts_match.group(1).replace(" ", "T")
                            collected.append({
                                "created_at": created_at,
                                "event_type": "info",
                                "message": line,
                                "item_id": None,
                                "details": {},
                            })
                            file_events += 1
                        if len(collected) >= limit:
                            break
                    # Only fall through to rotated/older files when the newest
                    # file has no queue lines — mixing in old logs makes the
                    # viewer look out of date.
                    if file_events > 0:
                        break
                except Exception:
                    continue
                if len(collected) >= limit:
                    break
            return collected

        events = _collect(queue_candidates or fallback_candidates)
        if not events and queue_candidates:
            # queue.log exists but had no matching lines — try the shared
            # scan logs once so a just-started install is not blank.
            events = _collect(fallback_candidates)

        return events[:limit]
    except Exception as exc:
        logger.debug("[queue_diagnostics] Log-file fallback failed: %s", exc)
        return []


def _within_last_hour(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only events whose timestamp falls within the last 60 minutes.

    ⚠️ This belongs to the LOG-EXPORT buttons, whose labels really do say
    "Download Last Hour". It must NOT gate the monitor page's events viewer.

    It used to be applied to ``queue_events()`` as well — the endpoint the
    viewer reads — so an idle queue rendered as "No queue events yet" even
    though ``queue.log`` still held the history, while the card header
    advertised "(Last 50 events)", a COUNT. A queue only has to be quiet for
    an hour for the log to look broken, which is the reported "Download Queue
    Events Log is empty".
    """
    if not events:
        return events

    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    kept: list[dict[str, Any]] = []
    for event in events:
        raw = event.get("timestamp") or event.get("created_at")
        if not raw:
            continue
        try:
            text = str(raw).replace("Z", "+00:00")
            ts = datetime.fromisoformat(text)
            if ts.tzinfo is None:
                # Log-file timestamps are written in the host's local time —
                # treat naive stamps as local time rather than UTC.
                ts = ts.astimezone()
            else:
                ts = ts.astimezone(timezone.utc)
            if ts >= cutoff:
                kept.append(event)
        except Exception:
            # Unparseable timestamp — keep the row (better than dropping data).
            kept.append(event)
    return kept


def queue_events(
    args: Mapping[str, Any],
):
    try:
        limit = _safe_int(
            args.get("limit"),
            100,
        )

        event_type = (
            str(args.get("type"))
            if args.get("type")
            else None
        )

        events = get_queue_events(
            limit=limit,
            event_type=event_type,
        )

        # Fallback: when the in-memory store is empty (separate queue worker
        # process or after restart), surface recent queue-related log lines
        # so the viewer is never blank (legacy behaviour).
        if not events:
            events = _tail_queue_log(limit=limit)

        # ⚠️ NO time-window filter here — this endpoint backs the monitor's
        # "Download Queue Events Log", whose header promises "(Last 50
        # events)", a COUNT. Applying ``_within_last_hour`` emptied that viewer
        # whenever the queue had been quiet for an hour, hiding events that
        # ``queue.log`` still held. The window is for the log-EXPORT buttons
        # ("Download Last Hour"), which call ``_within_last_hour`` directly.
        return _ok(
            events=events,
            total=len(events),
        )

    except Exception as exc:
        logger.exception(
            "queue_events failed"
        )

        return _fail(
            str(exc),
            500,
        )


def _tail_search_log(limit: int = 100) -> list[dict[str, Any]]:
    """Read the tail of ``search.log`` as a fallback for search events.

    Used when the ``slskd_search_logs`` DB table is unavailable (e.g. during
    database backoff) so the monitor's Soulseek Search Log is never blank.
    """
    _TAIL_BYTES = 256 * 1024
    _TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    try:
        from helpers.logging_config import resolve_log_dir
        import glob as _glob

        log_dir = resolve_log_dir()
        candidates = sorted(
            _glob.glob(os.path.join(log_dir, "search.log") + "*"),
            key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
            reverse=True,
        )
        events: list[dict[str, Any]] = []
        for path in candidates:
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    fh.seek(0, os.SEEK_END)
                    size = fh.tell()
                    fh.seek(max(0, size - min(size, _TAIL_BYTES)))
                    chunk = fh.read()
                file_events = 0
                for line in reversed(chunk.splitlines()):
                    line = line.strip()
                    if not line:
                        continue
                    if any(n in line for n in (
                        "APScheduler", "registered ", "job store", "parameters:",
                        "job_state", "next_run_time", "Binary object",
                    )):
                        continue
                    ts_match = _TS_RE.search(line)
                    created_at = None
                    if ts_match:
                        created_at = ts_match.group(1).replace(" ", "T")
                    events.append({
                        "created_at": created_at,
                        "timestamp": created_at,
                        "search_type": "unknown",
                        "query": line,
                        "result_count": 0,
                        "event_type": "search",
                        "message": line,
                    })
                    file_events += 1
                    if len(events) >= limit:
                        break
                if file_events > 0:
                    break
            except Exception:
                continue
            if len(events) >= limit:
                break
        return events[:limit]
    except Exception as exc:
        logger.debug("[queue_diagnostics] Search-log fallback failed: %s", exc)
        return []


def queue_search_events(
    args: Mapping[str, Any],
):
    """Return recent Soulseek search events.

    Reads from the persistent ``slskd_search_logs`` table (written by
    ``download_pipeline_service``/``log_slskd_search``), which is the
    canonical source the diagnostics UI and ``log_service`` consume.  The
    in-memory queue-event store only carries general queue events and is not
    where search events are recorded.
    """
    try:
        query = str(
            args.get("q")
            or args.get("query")
            or ""
        ).strip().lower()

        limit = _safe_int(
            args.get("limit"),
            100,
        )
        limit = max(1, min(limit, 200))

        from db.repositories.search_logs import get_slskd_search_logs
        logs = get_slskd_search_logs(limit=max(limit * 3, 50))

        if not logs:
            # DB unavailable/empty — fall back to the dedicated search.log file
            # so the viewer is never blank during database backoff.
            logs = _tail_search_log(limit=max(limit * 3, 50))

        matches = [
            event
            for event in logs
            if not query or query in str(event).lower()
        ]

        # The monitor page only shows the last hour of activity; the full
        # search history is available under the /logs page.
        matches = _within_last_hour(matches)

        return _ok(
            events=matches[:limit],
            total=len(matches),
            query=query,
        )

    except Exception as exc:
        logger.exception(
            "queue_search_events failed"
        )

        return _fail(
            str(exc),
            500,
        )


# =============================================================================
# COLLECTION DIAGNOSTICS
# =============================================================================

def queue_check_collection_batch(
    data: Mapping[str, Any],
):
    try:

        items = data.get("items") or []

        if not isinstance(items, list):
            return _fail(
                "items must be a list",
                400,
            )

        results = []

        for item in items:

            artist = str(
                item.get("artist") or ""
            ).strip()

            title = str(
                item.get("title") or ""
            ).strip()

            album = item.get("album")

            if not artist or not title:
                results.append(
                    {
                        "item": item,
                        "found": False,
                        "error": (
                            "artist and title are required"
                        ),
                    }
                )
                continue

            track = find_library_track(
                artist=artist,
                title=title,
                album=album,
            )

            results.append(
                {
                    "item": item,
                    "found": track is not None,
                    "collection_path": (
                        track.get("file_path")
                        if track
                        else None
                    ),
                }
            )

        return _ok(
            results=results,
            total=len(results),
        )

    except Exception as exc:
        logger.exception(
            "queue_check_collection_batch failed"
        )

        return _fail(
            str(exc),
            500,
        )


# =============================================================================
# SLSKD DIAGNOSTICS
# =============================================================================

def queue_slskd_eligibility_diagnostics():
    try:
        return _ok(
            migrated=False,
            counts=get_queue_status_counts(),
            message=(
                "slskd eligibility diagnostics "
                "not implemented"
            ),
        )

    except Exception as exc:
        logger.exception(
            "queue_slskd_eligibility_diagnostics failed"
        )

        return _fail(
            str(exc),
            500,
        )