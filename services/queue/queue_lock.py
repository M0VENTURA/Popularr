"""Cross-process mutual exclusion for the download queue cycle.

The standalone ``queue_worker`` process and the APScheduler
``download_queue_processor`` job both dispatch queue items independently.
Without serialisation they can claim and process the same items
concurrently, overlapping DB writes and filesystem moves.

This module provides ``queue_cycle_lock()`` — a best-effort lock that
yields ``True`` when this process acquired the lock and may proceed, or
``False`` when another worker/scheduler cycle already holds it.
"""

from __future__ import annotations

import os
import tempfile
import time
from contextlib import contextmanager
from typing import Iterator

import structlog

logger = structlog.get_logger(__name__)

_LOCK_KEY = "popularr_queue_cycle"

#: One-shot guard: the degradation below is a per-process fact, not an event.
_FCNTL_UNAVAILABLE_WARNED = False


def _using_postgres() -> bool:
    try:
        from db.engine import get_engine
        return str(get_engine().url).startswith("postgresql")
    except Exception:
        return False


@contextmanager
def _pg_advisory_lock(
    key: str,
    max_attempts: int,
    interval: float,
) -> Iterator[bool]:
    """Hold a PostgreSQL advisory lock on a DEDICATED raw connection."""
    from db.utils import get_db_connection_raw

    conn = None
    cursor = None
    acquired = False
    for _ in range(max(1, max_attempts)):
        try:
            conn = get_db_connection_raw(reason="advisory_lock")
            cursor = conn.cursor()
            cursor.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS acquired",
                (key,),
            )
            row = cursor.fetchone()
            if row is not None:
                if hasattr(row, "get"):
                    acquired_flag = bool(row.get("acquired") or row.get("pg_try_advisory_lock"))
                else:
                    acquired_flag = bool(row[0])
            else:
                acquired_flag = False

            if acquired_flag:
                acquired = True
                try:
                    conn.commit()
                except Exception:
                    conn.rollback()
                break
        except Exception as exc:
            logger.debug("Advisory lock error", error=str(exc))
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
                cursor = None
            break

        if conn is not None:
            conn.close()
        conn = None
        cursor = None
        time.sleep(interval)

    try:
        yield acquired
    finally:
        if acquired and conn is not None:
            try:
                if cursor is None:
                    cursor = conn.cursor()
                cursor.execute(
                    "SELECT pg_advisory_unlock(hashtext(%s))",
                    (key,),
                )
                cursor.fetchone()
                try:
                    conn.commit()
                except Exception:
                    conn.rollback()
            except Exception as exc:
                logger.debug("Advisory unlock error", error=str(exc))
            try:
                conn.close()
            except Exception:
                pass


@contextmanager
def _file_lock(
    key: str,
    max_attempts: int,
    interval: float,
) -> Iterator[bool]:
    """Hold an exclusive ``flock`` on a temp lock file (fallback lock).

    ``fcntl`` is POSIX-only.  This is a BEST-EFFORT lock by contract, so on a
    platform without it the lock degrades to "always acquired" with a one-time
    warning rather than raising — raising took the whole queue cycle down with
    ``ModuleNotFoundError`` (``process_cycle`` cannot acquire a lock that cannot
    exist).  Production always takes the PostgreSQL advisory path instead; this
    only affects a non-Postgres, non-POSIX environment.
    """
    global _FCNTL_UNAVAILABLE_WARNED
    try:
        import fcntl
    except ModuleNotFoundError:
        if not _FCNTL_UNAVAILABLE_WARNED:
            _FCNTL_UNAVAILABLE_WARNED = True
            logger.warning(
                "Queue cycle lock degraded — no fcntl on this platform",
                key=key,
                detail="cross-process exclusion unavailable; cycles run unsynchronised",
            )
        yield True
        return

    path = os.path.join(tempfile.gettempdir(), f"popularr_{key}.lock")
    fd: int | None = None
    acquired = False
    for _ in range(max(1, max_attempts)):
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            break
        except OSError:
            if fd is not None:
                os.close(fd)
                fd = None
            time.sleep(interval)

    try:
        yield acquired
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(fd)
            except Exception:
                pass


@contextmanager
def queue_cycle_lock(
    key: str = _LOCK_KEY,
    max_attempts: int = 20,
    attempt_interval: float = 0.5,
) -> Iterator[bool]:
    """Best-effort cross-process mutual exclusion for queue cycles."""
    if _using_postgres():
        with _pg_advisory_lock(key, max_attempts, attempt_interval) as acquired:
            yield acquired
        return
        
    with _file_lock(key, max_attempts, attempt_interval) as acquired:
        yield acquired
