"""Cross-process leader election for background workers.

WHY THIS EXISTS
---------------
``app.py`` calls ``initialize_app_services(app)`` in every process that imports
the app — and hypercorn runs ``--workers 4`` (``entrypoint.sh``).  The guard
inside ``initialize_app_services`` only consulted the ``ENABLE_BACKGROUND_WORKERS``
environment variable, which **defaults to "true" and is the same for every
worker of a container**, so a multi-worker deployment started **one APScheduler
per worker**: four copies of every periodic job (library sync, popularity scan,
upcoming releases, the queue processor) all running at once.

That is not just wasted CPU.  Each copy makes its own external API calls, and
each process has its own in-process rate limiter, so the real request rate
against MusicBrainz is multiplied by the worker count — the reported 503s and
the circuit breaker opening mid-scan.

HOW LEADERSHIP IS DECIDED
-------------------------
An exclusive, **non-blocking** ``flock`` on a lock file in the shared state
directory.  The first process to take it is the leader and holds it for its
lifetime; ``flock`` is released by the kernel when the holder exits, so a
crashed worker can never wedge leadership.  Late starters simply skip the
background services, which is exactly what we want: one scheduler per
deployment.

``ENABLE_BACKGROUND_WORKERS=false`` still force-disables the background workers
for every process (the previous escape hatch, e.g. for running ``app.py`` in a
debugger next to a live deployment).
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

try:  # POSIX (the Linux container)
    import fcntl
except ImportError:  # pragma: no cover - Windows development/tests
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

#: The lock handle is held for the LIFE OF THE PROCESS — releasing it would hand
#: leadership to another worker and reintroduce duplicate schedulers.
_LEADER_HANDLE: Any = None
_LEADER_STATE: bool | None = None


def _lock_path() -> str:
    from helpers.config_helpers import get_state_directory

    return os.path.join(get_state_directory(), "background_workers.lock")


def _try_take_lock(path: str) -> Any | None:
    """Return a held file handle if we won leadership, else ``None``."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handle = open(path, "a+b")
    except Exception as exc:
        logger.warning("[LEADER] could not open the leadership lock; assuming leader", error=str(exc))
        return None

    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt is not None:  # pragma: no cover - Windows
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - no locking primitive at all
            logger.warning("[LEADER] no file-locking primitive available; assuming leader")
            return handle
    except OSError:
        # Someone else holds it: another worker is already the leader.
        with contextlib.suppress(Exception):
            handle.close()
        return None

    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n".encode("ascii"))
        handle.flush()
    except Exception:
        pass
    return handle


def acquire_background_worker_leadership() -> bool:
    """True when THIS process should start the background services.

    Called once from ``initialize_app_services``.  Idempotent per process: the
    verdict is cached, so repeated calls (tests, reloads) cannot hand leadership
    to the same process twice or drop it.
    """
    global _LEADER_HANDLE, _LEADER_STATE

    if os.environ.get("ENABLE_BACKGROUND_WORKERS", "true").lower() == "false":
        logger.debug("[LEADER] ENABLE_BACKGROUND_WORKERS=false — background workers disabled")
        return False

    if _LEADER_STATE is not None:
        return _LEADER_STATE

    path = _lock_path()
    handle = _try_take_lock(path)
    if handle is None:
        _LEADER_STATE = False
        logger.info(
            "[LEADER] another worker holds the background-worker lock — background services stay off in this process",
            lock=path,
            pid=os.getpid(),
        )
        return False

    _LEADER_HANDLE = handle
    _LEADER_STATE = True
    logger.info("[LEADER] this process owns the background workers", lock=path, pid=os.getpid())
    return True


def release_background_worker_leadership() -> None:
    """Give up leadership (tests / clean shutdown).  Not called at runtime."""
    global _LEADER_HANDLE, _LEADER_STATE
    handle, _LEADER_HANDLE = _LEADER_HANDLE, None
    _LEADER_STATE = None
    if handle is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover - Windows
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception:
        pass
    with contextlib.suppress(Exception):
        handle.close()


def leadership_state() -> bool | None:
    """``True``/``False`` once decided, ``None`` before the first attempt."""
    return _LEADER_STATE
