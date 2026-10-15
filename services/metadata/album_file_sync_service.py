"""Run an album save's DISK phase in the background.

The reported problem
--------------------
> When saving an album that has a lot of tracks, it times out due to cloudflare
> time out, can this be added to a background service so the app can continue and
> not wait for the save to complete

Cloudflare proxies the app and gives an origin **100 seconds** to respond; past
that it returns **524**. An album save is a synchronous form POST, so the browser
waits for the redirect and the whole save must finish inside that window.

The save is already decomposed, and earlier rounds of this problem moved its
network and disk work off the event loop. What remains is simply *volume*: the
disk phase touches every audio file of the album, and a large album costs more
wall clock than the window allows however well it is parallelised.

Why the FILE half is the half that moves
---------------------------------------
The save has two halves with very different costs:

* the **database** half — the album's fields and each track's row. Fast, and it is
  the half that makes the edit *visible*: reload the page and the new values are
  there.
* the **disk** half — rewrite each audio file's tags, embed the cover art into
  each file, then tell Navidrome to re-scan. This is N file passes, and it is the
  half that outlives the timeout.

So only the disk half moves. The database half stays in the request, which means a
backgrounded save is still a **durable** save: the user's edit is committed before
they are redirected, and a later failure costs the file tags, not the edit. That
matters, because the alternative — backgrounding the whole save — would leave the
user unable to tell a slow save from a lost one.

⚠️ THE RESCAN MOVES WITH IT, AND THAT IS NOT OPTIONAL
----------------------------------------------------
``request_rescan`` exists because Navidrome serves its own database and only
refreshes when it re-scans (see ``services/scanning/navidrome_rescan_service``).
If the file writes were backgrounded while the rescan stayed in the request,
Navidrome would scan **before** the corrected tags were on disk and import the
pre-save values — the exact "the next import puts the old data back" bug that
service was written to fix. The rescan is therefore the job's LAST step, after the
tags and the cover art are written.

Nothing here is a new writer
----------------------------
The job calls the same runner the inline path uses, so the tag-write policy gates
(``write_tags_to_file``, ``fill_missing_only``, …) apply exactly as they do for a
small album. The runner is **injected** in the payload rather than imported: it
lives in ``routes/ui_routes.py`` alongside the phase helpers, and a service
importing a route would invert the layering.

Coalescing
----------
One job per album. A save arriving while that album's job is running does not start
a second job — it replaces the payload the running thread picks up next, so the
LATEST save wins and two threads never rewrite the same files from two different
payloads. This mirrors ``navidrome_rescan_service``'s ``pending`` flag.
"""

from __future__ import annotations

import threading
from time import monotonic
from typing import Any, Callable

import structlog

logger = structlog.get_logger(__name__)

#: How long a job may run before the status reports it as stuck. A job past this
#: is NOT killed — a thread mid-write is left to finish, because abandoning an
#: atomic temp+replace would be worse than a slow one. This only decides when the
#: status starts saying "running longer than expected".
STALE_JOB_SECONDS = 900.0

#: The payload key holding the callable that performs the work. Injected so this
#: module never imports a route.
RUNNER_KEY = "runner"

#: A bounded tail of completed runs, newest first, so a failure is discoverable
#: after the fact — the user has already navigated away by then.
_RECENT_LIMIT = 20


class _AlbumJob:
    """One album's pending disk work. A later save replaces ``payload`` in place."""

    __slots__ = ("artist", "album", "payload", "started_at", "runs", "reason")

    def __init__(self, artist: str, album: str, payload: dict[str, Any], reason: str) -> None:
        self.artist = artist
        self.album = album
        self.payload: dict[str, Any] | None = payload
        self.started_at = monotonic()
        self.runs = 0
        self.reason = reason


_state_lock = threading.Lock()

#: album key -> its job (running or waiting to run).
_jobs: dict[tuple[str, str], _AlbumJob] = {}

#: album key -> the thread serving it.
_threads: dict[tuple[str, str], threading.Thread] = {}

_recent: list[dict[str, Any]] = []


def _key(artist: str, album: str) -> tuple[str, str]:
    return (str(artist or "").casefold(), str(album or "").casefold())


def _record_completion(entry: dict[str, Any]) -> None:
    with _state_lock:
        _recent.insert(0, entry)
        del _recent[_RECENT_LIMIT:]


def get_album_file_sync_state() -> dict[str, Any]:
    """Snapshot of what is running, what is queued, and what just finished."""
    now = monotonic()
    running: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    with _state_lock:
        for key, job in _jobs.items():
            thread = _threads.get(key)
            entry = {
                "artist": job.artist,
                "album": job.album,
                "reason": job.reason,
                "runs": job.runs,
                "queued_seconds": round(now - job.started_at, 1),
            }
            if thread is not None and thread.is_alive():
                entry["stale"] = entry["queued_seconds"] > STALE_JOB_SECONDS
                running.append(entry)
            else:
                pending.append(entry)
        recent = [dict(r) for r in _recent]

    return {
        "running_count": len(running),
        "pending_count": len(pending),
        "running": running,
        "pending": pending,
        "recent": recent,
    }


def album_file_sync_enqueued(artist: str, album: str) -> bool:
    """Is there a job for this album (running or waiting)?"""
    with _state_lock:
        return _key(artist, album) in _jobs


def _worker(key: tuple[str, str]) -> None:
    """Serve one album until nothing is left, then release the slot.

    Looping is what makes coalescing work: a save arriving mid-run replaces the
    payload and is picked up by this same thread rather than spawning a second one,
    so the files are never written by two threads from two payloads.

    ⚠️ The exit decision and ``enqueue``'s "is a thread already serving this?"
    decision are made under the SAME lock, and the exit REMOVES the key. Without
    that, a save landing exactly as the worker exited would see a live thread,
    coalesce onto it, and be stranded when the thread returned.
    """
    while True:
        with _state_lock:
            job = _jobs.get(key)
            if job is None:
                _threads.pop(key, None)
                return
            payload = job.payload
            job.payload = None
            job.runs += 1
            job.started_at = monotonic()
            artist, album, reason = job.artist, job.album, job.reason

            if payload is None:
                # Nothing waiting, so this thread's work is done. Removing the key
                # here is what lets a concurrent enqueue start a fresh thread
                # instead of coalescing onto one that is about to die.
                _jobs.pop(key, None)
                _threads.pop(key, None)
                return

        runner: Callable[[dict[str, Any]], dict[str, Any]] | None = payload.get(RUNNER_KEY)
        entry: dict[str, Any] = {
            "artist": artist,
            "album": album,
            "reason": reason,
            "tracks": len(payload.get("file_jobs") or []),
        }

        started = monotonic()
        if not callable(runner):
            # A payload without a runner is a programming error, not a runtime
            # condition — report it loudly rather than silently doing nothing.
            entry.update({"success": False, "error": "no runner in payload"})
            logger.error(
                "Album file sync payload has no runner — nothing was written",
                artist=artist, album=album,
            )
        else:
            try:
                entry.update(runner(payload) or {})
                entry["success"] = True
            except Exception as exc:
                entry.update({"success": False, "error": str(exc)})
                logger.error(
                    "Background album file sync FAILED",
                    artist=artist, album=album, error=str(exc), exc_info=True,
                )
                try:
                    from helpers.logging_config import log_unified

                    log_unified(
                        f"[ALBUM_SAVE] Background tagging FAILED for {artist} - {album}: "
                        f"{exc} (the database WAS saved; the audio files were not retagged)"
                    )
                except Exception:
                    pass

        entry["duration_ms"] = round((monotonic() - started) * 1000.0, 1)
        _record_completion(entry)


def enqueue_album_file_sync(
    *,
    artist: str,
    album: str,
    payload: dict[str, Any],
    reason: str = "album save",
) -> dict[str, Any]:
    """Queue the album's disk phase. Returns immediately.

    ``payload`` is everything the disk phase needs and nothing it does not, and it
    must be plain data plus the pre-built per-track job tuples and the injected
    runner — the job runs after the request that created it has ended, so it can
    hold no request or session objects.

    A second call for the same album REPLACES the payload rather than starting a
    second thread. The running thread picks the replacement up when it finishes the
    current pass, so the newest save is the one that lands last.
    """
    key = _key(artist, album)
    tracks = len(payload.get("file_jobs") or [])

    with _state_lock:
        existing = _jobs.get(key)
        if existing is not None:
            thread = _threads.get(key)
            if thread is not None and thread.is_alive():
                existing.payload = payload
                existing.artist = artist
                existing.album = album
                existing.reason = reason
                logger.info(
                    "Album file sync already running — queued the newer save behind it",
                    artist=artist, album=album, tracks=tracks,
                )
                return {"queued": True, "coalesced": True, "running": True, "tracks": tracks}
            # The thread is gone but its slot survived (a hard crash mid-run, or a
            # thread that died without unwinding). Replace it rather than leaving
            # the album permanently unprocessable.
            logger.warning(
                "Album file sync had a dead worker — restarting it",
                artist=artist, album=album,
            )

        _jobs[key] = _AlbumJob(artist, album, payload, reason)
        thread = threading.Thread(
            target=_worker,
            args=(key,),
            name="album-file-sync",
            daemon=True,
        )
        _threads[key] = thread
        thread.start()

    logger.info(
        "Album file sync queued in the background",
        artist=artist, album=album, tracks=tracks, reason=reason,
    )
    return {"queued": True, "coalesced": False, "running": False, "tracks": tracks}


def wait_for_album_file_sync(artist: str, album: str, timeout: float = 30.0) -> bool:
    """Block until this album's job finishes. For tests, and for a manual drain.

    Returns True when nothing was left to run, False on timeout. Polls rather than
    joining once, because a coalesced follow-up extends the work.
    """
    key = _key(artist, album)
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        with _state_lock:
            thread = _threads.get(key)
            alive = thread is not None and thread.is_alive()
        if not alive:
            return True
        thread.join(timeout=0.1)
    return False


def reset_album_file_sync_state() -> None:
    """Drop all bookkeeping. Tests only — it does NOT stop a running thread."""
    with _state_lock:
        _jobs.clear()
        _threads.clear()
        _recent.clear()
