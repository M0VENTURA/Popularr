"""Bounded waiting for abandoned scan workers.

One abandoned worker is survivable; a growing pile is not. Every straggler
still holds a DB-pool connection and a slot on the shared HTTP rate limiter,
which is exactly how a single slow provider used to turn into a cascade that
stalled the artists that came after it.

This lives apart from the two callers so neither has to import the other:

* ``services.scanning.pipelines.popularity_pipeline`` drains after an ARTIST
  is abandoned;
* ``services.popularity.scan_stage_runner`` drains after an ALBUM phase is
  abandoned — the same exposure one level down, where ``_bounded_album_phase``
  gives up at the per-album budget and hands back a worker that is *still
  running*.

The wait is a GRACE PERIOD, never a join: cancellation is cooperative, so a
worker blocked in a syscall may never observe it, and waiting indefinitely
would reintroduce the frozen scan the budget exists to prevent. Termination is
guaranteed by two independent bounds (a deadline AND an iteration ceiling), so
neither a pathological clock nor an indifferent worker can hang the caller.
"""
from __future__ import annotations

import time
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: How long to wait for an abandoned worker to notice its cancellation before
#: moving on, in seconds.  Bounded on purpose — see the module docstring.
ABANDONED_GRACE_SECONDS = 5.0

#: Hard ceiling on ANY single drain wait, regardless of configuration. The
#: drain must never be able to stall the scan, so this is NOT configurable.
ABANDONED_MAX_WAIT_SECONDS = 60.0

#: Default cap on simultaneously-live abandoned workers. One worker still
#: winding down is harmless; a growing pile contends for the DB pool and the
#: shared rate limiter.
DEFAULT_MAX_LIVE_ABANDONED = 1


def resolve_max_live_abandoned() -> int:
    """Read the abandoned-worker cap from config (``features`` block).

    Clamped to 0-16.  ``0`` means "never wait" — every abandoned worker is left
    to unwind on its own, which is the pre-fix behaviour available for a
    deliberately unattended/oversubscribed host.
    """
    try:
        from helpers.config_helpers import get_feature

        value = int(
            get_feature(
                "full_scan_max_abandoned_workers",
                DEFAULT_MAX_LIVE_ABANDONED,
            )
            or 0
        )
    except Exception:
        value = DEFAULT_MAX_LIVE_ABANDONED
    return max(0, min(value, 16))


def live_abandoned_workers(workers: list[Any]) -> list[Any]:
    """Drop finished workers in place and return the still-running ones."""
    workers[:] = [w for w in workers if getattr(w, "is_alive", lambda: False)()]
    return workers


def effective_grace_seconds(grace_seconds: float) -> float:
    """Clamp a requested grace period to the hard ceiling.

    Factored out so the clamp can be asserted directly and cheaply: verifying
    it by TIMING a 1-hour wait would make the suite take an hour.
    """
    try:
        requested = float(grace_seconds)
    except (TypeError, ValueError):
        return ABANDONED_GRACE_SECONDS
    if requested < 0:
        return 0.0
    return min(requested, ABANDONED_MAX_WAIT_SECONDS)


def drain_abandoned_workers(
    workers: list[Any],
    position: int,
    max_live: int,
    *,
    grace_seconds: float = ABANDONED_GRACE_SECONDS,
    label: str = "[FULL_SCAN]",
) -> dict[str, int]:
    """Wait (briefly, bounded) until few enough abandoned workers remain.

    Called after a unit of work is abandoned and asked to cancel.  Returns
    ``{"live": n, "finished": m, "forced": k}`` where ``forced`` counts workers
    abandoned while still running because the cap could not be met.
    """
    stats = {"live": 0, "finished": 0, "forced": 0}

    if max_live <= 0:
        # Cap disabled: never wait.  Report the true outstanding count so the
        # condition stays visible in the log rather than being hidden.
        live = live_abandoned_workers(workers)
        stats["live"] = len(live)
        stats["forced"] = len(live)
        if live:
            logger.warning(
                f"{label} Abandoned workers still running (cap disabled)",
                live=len(live),
                workers=[getattr(w, "name", "?") for w in live],
            )
        return stats

    effective_grace = effective_grace_seconds(grace_seconds)
    deadline = time.monotonic() + effective_grace
    max_iterations = int(effective_grace / 0.25) + 4
    iterations = 0

    while iterations < max_iterations:
        iterations += 1
        if len(live_abandoned_workers(workers)) <= max_live:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)

    live = live_abandoned_workers(workers)
    stats["live"] = len(live)

    if len(live) > max_live:
        # The grace period expired with stragglers left.  Log loudly: this is
        # the case where the cascade could still resume, so it must be visible
        # rather than silently tolerated.
        stats["forced"] = len(live) - max_live
        logger.warning(
            f"{label} Abandoned workers still running after grace period — "
            "these can slow the next unit of work",
            live=len(live),
            cap=max_live,
            grace_seconds=effective_grace,
            workers=[getattr(w, "name", "?") for w in live],
        )
    else:
        logger.debug(
            f"{label} Abandoned workers drained to cap",
            live=len(live),
            cap=max_live,
            position=position,
        )

    return stats
