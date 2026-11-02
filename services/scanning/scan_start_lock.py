"""Cross-process guard for starting a scan.

The per-process ``runtime_state`` registry is enough when one worker handles
both clicks, but hypercorn runs several workers. Two clicks can then land in
different processes, both pass the runtime/DB checks before either writes the
shared progress row, and both scans run — the reported double Navidrome import.

This lock is held for the lifetime of the scan and released by its ``finally``.
The OS releases it automatically if the worker dies, so a crash cannot leave a
permanent block.
"""

from __future__ import annotations

import os
import tempfile
import threading
from typing import Any

_LOCK_NAME = "popularr_scan_active.lock"
_handles: dict[str, Any] = {}
_guard = threading.Lock()


def _lock_dir() -> str:
    configured = str(os.environ.get("SPTNR_CONFIG_DIR") or "").strip()
    if configured and os.path.isdir(configured) and os.access(configured, os.W_OK):
        return configured
    if os.path.isdir("/config") and os.access("/config", os.W_OK):
        return "/config"
    return tempfile.gettempdir()


def _lock_path() -> str:
    return os.path.join(_lock_dir(), _LOCK_NAME)


def try_acquire_scan_start_lock() -> bool:
    """Try to become THE running scan. False means another process owns it."""
    with _guard:
        if _handles:
            return False
        path = _lock_path()
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return False
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                os.close(fd)
            except OSError:
                pass
            return False
        _handles["fd"] = fd
        return True


def release_scan_start_lock() -> None:
    """Release the cross-process scan lock. Safe to call more than once."""
    with _guard:
        fd = _handles.pop("fd", None)
        if fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
