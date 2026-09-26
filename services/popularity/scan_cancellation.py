"""Cooperative cancellation for ONE artist inside a full-library scan.

WHY THIS EXISTS
---------------
``_bounded_call_report`` runs each artist's pipeline on a daemon thread and
``join``s it with a wall-clock budget:

    thread.start()
    thread.join(timeout=seconds)
    if thread.is_alive():
        return {"abandoned": True, ...}   # <-- the thread is STILL RUNNING

The loop then moves to the next artist, so the abandoned worker keeps going.
That is pure downside: the artist is reported as skipped, yet it continues
consuming the DB connection pool, the shared HTTP rate limiter and CPU —
which makes the NEXT artist more likely to time out too. Probed with the real
``_bounded_call_report``: 8 slow artists produced 8 concurrent pipelines with
7 still alive after the loop finished.

WAITING IS NOT THE FIX
----------------------
"A genuinely hung call never returns", so ``join``ing without a timeout would
reintroduce the frozen scan the budget exists to prevent. The fix is to tell
the orphan to STOP at its next safe checkpoint, and to bound how many orphans
can pile up if it never reaches one.

WHY A SEPARATE REGISTRY (and not the existing stop flag)
--------------------------------------------------------
``scan_state.is_stop_requested`` is the USER's Stop button: it halts the whole
scan. Reusing it would make abandoning one artist abort all 58. This registry
is per-artist and internal, so cancelling the straggler never touches the
user's intent.

The check site is the album loop in ``run_scan`` — the same boundary the user
stop flag already uses. That is the coarsest safe point where in-flight work
for the current album has completed and the next album has not started, so
unwinding there cannot corrupt the DB or leave a half-written album.
"""

from __future__ import annotations

import threading

__all__ = [
    "begin_scan",
    "request_cancel",
    "is_cancelled",
    "clear_artist",
    "reset",
    "cancelled_artists",
]

#: Guard for every piece of shared state below.
_LOCK = threading.Lock()

#: Normalised artist keys cancelled in the CURRENT scan.
_cancelled: set[str] = set()

#: Bumped by ``begin_scan``. Cancellations carry the epoch they were raised
#: in, so a straggler from a previous scan cannot cancel a same-named artist
#: in a new one (artist names repeat across runs; a stale event must not leak).
_epoch: int = 0


def _key(artist: str | None) -> str:
    """Normalise an artist for comparison (case/punctuation-insensitive)."""
    return str(artist or "").strip().casefold()


def begin_scan() -> int:
    """Start a new scan generation, clearing any previous cancellations.

    Returns the new epoch. Call once when a full-library scan starts, so
    cancellations from a previous run cannot bleed into this one.
    """
    global _epoch
    with _LOCK:
        _epoch += 1
        _cancelled.clear()
        return _epoch


def request_cancel(artist: str | None, epoch: int | None = None) -> bool:
    """Ask the pipeline for *artist* to stop at its next checkpoint.

    Returns True when the request was recorded. ``epoch`` is the value
    :func:`begin_scan` returned for the scan this artist belongs to; when
    supplied it must still be current, so a late straggler cannot cancel an
    artist in a newer scan. Pass ``None`` to always record.

    This is advisory: it cannot interrupt a blocked syscall, it only makes the
    pipeline stop at the next boundary it checks.
    """
    key = _key(artist)
    if not key:
        return False
    with _LOCK:
        if epoch is not None and epoch != _epoch:
            return False
        _cancelled.add(key)
        return True


def is_cancelled(artist: str | None) -> bool:
    """True when *artist* has been asked to stop in the current scan."""
    key = _key(artist)
    if not key:
        return False
    with _LOCK:
        return key in _cancelled


def clear_artist(artist: str | None) -> None:
    """Forget one artist's cancellation (its worker has unwound)."""
    key = _key(artist)
    if not key:
        return
    with _LOCK:
        _cancelled.discard(key)


def cancelled_artists() -> set[str]:
    """Snapshot of the currently-cancelled artist keys (diagnostics/tests)."""
    with _LOCK:
        return set(_cancelled)


def reset() -> None:
    """Drop all cancellation state and the epoch (tests, hard resets)."""
    global _epoch
    with _LOCK:
        _cancelled.clear()
        _epoch = 0
