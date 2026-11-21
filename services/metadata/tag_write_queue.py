"""Write file tags in the background, off the scan path.

The reported problem
--------------------
> When writing genres to files, this is taking a long time and also stopping
> another scan from running at the same time. Could this run as a separate
> background task that doesn't stop the popularity scan running.

Two separate costs were being paid in the wrong place:

1. **Volume.** A tag write is a read-modify-write of a whole audio file
   (``tag_file_service._write_tags_atomic`` copies the file, rewrites it and
   compares it before swapping). On a network mount that is 3–4 full file
   passes *per track*. The scan ran that synchronously inside the album loop, so
   the popularity scan's wall clock was dominated by disk I/O it does not need
   in order to finish.

2. **Mutual exclusion.** ``genre_playlist_resync_service.apply_genre_playlist_resync``
   took the cross-process scan lock (``try_acquire_scan_start_lock``) for its
   WHOLE run, including its file-write phase. While genres were being written,
   every scan start was refused with "A scan is running".

So the writes move here. This queue:

* runs on its own daemon threads, with a small bounded concurrency so it cannot
  swamp the disks a scan is also reading;
* **never** takes the scan-start lock — that lock exists to stop two *scans*
  colliding, and a tag write is not a scan;
* coalesces per album, so a re-scan of the same album mid-write does not create
  a second thread rewriting the same files from an older payload.

Why the payload must be plain data
----------------------------------
A job outlives the request (or the scan pass) that created it, so it may hold no
session, ORM object or route reference. The album's rows are already plain dicts
by the time the caller has them, and the runner is looked up by name inside this
module rather than passed in — unlike ``album_file_sync_service``, whose runner
lives in a route and must be injected. Keeping the runner here means this module
has no import cycle with the scan runner that calls it.

⚠️ THE RESCAN IS THE JOB'S LAST STEP
------------------------------------
Navidrome serves its own database and only refreshes when it re-scans. If it
scanned before the new genres were on disk it would import the OLD values, so the
rescan is requested once, after every file of the album has been written.
"""

from __future__ import annotations

import threading
from time import monotonic
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: A job past this is reported as stuck. It is NOT killed — a thread mid-write is
#: left to finish, because abandoning an atomic temp+replace is worse than a slow
#: one. This only decides when the status starts saying so.
STALE_JOB_SECONDS = 1800.0

#: Concurrent write threads. Deliberately small: the queue competes with a scan
#: for the same disks, and the point of the move is that it does not starve it.
DEFAULT_CONCURRENCY = 2

#: A bounded tail of completed runs, newest first. The user is looking at the
#: scan's progress, not this queue, so a failure has to be discoverable later.
_RECENT_LIMIT = 40

#: ``(artist, album)`` -> pending payload. A repeat enqueue replaces the payload
#: in place so the LATEST genres win.
_pending: dict[tuple[str, str], dict[str, Any]] = {}

#: ``(artist, album)`` -> the thread serving it.
_threads: dict[tuple[str, str], threading.Thread] = {}

#: Queue-level bookkeeping for the worker pool.
_queue: list[tuple[str, str]] = []
_slots = threading.BoundedSemaphore(DEFAULT_CONCURRENCY)

_lock = threading.Lock()
_recent: list[dict[str, Any]] = []


def _key(artist: str, album: str) -> tuple[str, str]:
    return (str(artist or "").casefold(), str(album or "").casefold())


def _record_completion(entry: dict[str, Any]) -> None:
    with _lock:
        _recent.insert(0, entry)
        del _recent[_RECENT_LIMIT:]


def _serve(key: tuple[str, str]) -> None:
    """Write one album's pending files, then release the key.

    Looping is what makes coalescing work: an enqueue arriving mid-write replaces
    the payload, and this same thread picks it up rather than spawning a second
    one — so the files are never written by two threads from two payloads.

    The concurrency slot is taken INSIDE the thread, so a queued album waits here
    instead of the enqueue call blocking. Only a bounded number of threads are
    ever writing at once, which is what keeps the queue from starving the scan of
    the same disks.
    """
    with _slots:
        _serve_loop(key)


def _serve_loop(key: tuple[str, str]) -> None:
    try:
        while True:
            with _lock:
                payload = _pending.get(key)
                if payload is None:
                    # Removing the key under the SAME lock enqueue uses is what
                    # lets a concurrent enqueue start a fresh thread instead of
                    # coalescing onto one that is about to die.
                    _pending.pop(key, None)
                    _threads.pop(key, None)
                    return
                _pending[key] = None  # claimed; a later enqueue replaces it

            artist = str(payload.get("artist") or "")
            album = str(payload.get("album") or "")
            tracks = payload.get("tracks") or []
            entry: dict[str, Any] = {
                "artist": artist,
                "album": album,
                "reason": str(payload.get("reason") or "scan"),
                "tracks": len(tracks),
            }
            started = monotonic()
            try:
                from services.metadata.album_tag_sync_service import (
                    run_deferred_tag_write,
                )

                entry.update(run_deferred_tag_write(payload) or {})
                entry["success"] = True
            except Exception as exc:  # pragma: no cover - defensive
                entry.update({"success": False, "error": str(exc)})
                logger.error(
                    "Background file tag write FAILED",
                    artist=artist, album=album, error=str(exc), exc_info=True,
                )
                try:
                    from helpers.logging_config import log_unified

                    log_unified(
                        f"[TAG_WRITE] Background tagging FAILED for {artist} - "
                        f"{album}: {exc} (the database WAS written; the audio "
                        f"files were not retagged)"
                    )
                except Exception:
                    pass
            entry["duration_ms"] = round((monotonic() - started) * 1000.0, 1)
            _record_completion(entry)
    finally:
        with _lock:
            _pending.pop(key, None)
            _threads.pop(key, None)


def enqueue_tag_tracks(
    *,
    artist: str,
    album: str,
    tracks: list[dict[str, Any]],
    reason: str = "scan",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Queue one album's tag writes. Returns immediately.

    ``tracks`` must be plain dicts (DB rows), plus the per-album context in
    ``extra`` (album year, perfect-match flag, …). A second call for the same
    album REPLACES the queued payload so the newest genres are the ones written.
    """
    if not tracks:
        return {"queued": False, "tracks": 0}

    key = _key(artist, album)
    payload = {
        "artist": artist,
        "album": album,
        "reason": reason,
        "tracks": list(tracks),
        **(extra or {}),
    }

    with _lock:
        existing = _pending.get(key)
        thread = _threads.get(key)
        if existing is not None and thread is not None and thread.is_alive():
            _pending[key] = payload
            logger.info(
                "File tag write already running — queued the newer payload behind it",
                artist=artist, album=album, tracks=len(tracks),
            )
            return {"queued": True, "coalesced": True, "tracks": len(tracks)}

        # A payload already sitting in the queue (its thread has not claimed it
        # yet) is replaced rather than duplicated.
        _pending[key] = payload
        if thread is not None and thread.is_alive():
            return {"queued": True, "coalesced": True, "tracks": len(tracks)}

        new_thread = threading.Thread(
            target=_serve,
            args=(key,),
            name="tag-write",
            daemon=True,
        )
        _threads[key] = new_thread
        new_thread.start()

    logger.info(
        "File tag write queued in the background",
        artist=artist, album=album, tracks=len(tracks), reason=reason,
    )
    return {"queued": True, "coalesced": False, "tracks": len(tracks)}


def get_tag_write_state() -> dict[str, Any]:
    """Snapshot of what is running and what just finished."""
    now = monotonic()
    running: list[dict[str, Any]] = []
    with _lock:
        for key, payload in _pending.items():
            thread = _threads.get(key)
            if payload is None:
                continue
            running.append({
                "artist": payload.get("artist"),
                "album": payload.get("album"),
                "reason": payload.get("reason"),
                "tracks": len(payload.get("tracks") or []),
                "alive": bool(thread is not None and thread.is_alive()),
            })
        recent = [dict(r) for r in _recent]
    return {
        "running_count": sum(1 for r in running if r["alive"]),
        "queued_count": len(running),
        "running": running,
        "recent": recent,
        "checked_at": round(now, 3),
    }


def wait_for_tag_writes(timeout: float = 60.0) -> bool:
    """Block until the queue drains. For tests and for a manual drain."""
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        with _lock:
            alive = [
                t for t in _threads.values() if t is not None and t.is_alive()
            ]
        if not alive:
            return True
        for thread in alive:
            thread.join(timeout=0.1)
    return False


def reset_tag_write_state() -> None:
    """Drop all bookkeeping. Tests only — it does NOT stop a running thread."""
    with _lock:
        _pending.clear()
        _threads.clear()
        _recent.clear()
