"""The scan start lock must be exclusive and released after the scan."""

from __future__ import annotations


def test_the_scan_start_lock_is_exclusive_and_releasable() -> None:
    from services.scanning.scan_start_lock import (
        release_scan_start_lock,
        try_acquire_scan_start_lock,
    )

    release_scan_start_lock()
    assert try_acquire_scan_start_lock() is True
    assert try_acquire_scan_start_lock() is False
    release_scan_start_lock()
    assert try_acquire_scan_start_lock() is True
    release_scan_start_lock()
