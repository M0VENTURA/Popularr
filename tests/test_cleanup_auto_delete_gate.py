"""Back-to-back cleanup passes must not repeat the slow auto-delete scan.

REPORTED
--------
Cleanup runs with ``scanned=0`` still spent 177–211 s in ``auto_delete_ms``.
Discovery is interval-gated; auto-delete now uses the same interval, so a pass
that is too soon does no filesystem work at all.
"""

from __future__ import annotations

import time

from services.downloads import cleanup_engine_service as ces


def test_auto_delete_is_skipped_before_the_interval(monkeypatch):
    now = time.time()
    monkeypatch.setattr(ces, "_discovery_last_run_at", now)
    monkeypatch.setattr(ces, "_auto_delete_last_run_at", now)

    called: list[int] = []
    monkeypatch.setattr(
        "services.downloads.download_folder_service.auto_delete_imported_folders",
        lambda: called.append(1) or 0,
    )

    result = ces.cleanup_stale_downloads()

    assert not called, "auto-delete ran again before its interval elapsed"
    assert result["timings_ms"]["total_ms"] < 5000


def test_auto_delete_runs_when_its_interval_elapsed(monkeypatch):
    monkeypatch.setattr(ces, "_discovery_last_run_at", time.time())
    monkeypatch.setattr(ces, "_auto_delete_last_run_at", 0.0)

    called: list[int] = []
    monkeypatch.setattr(
        "services.downloads.download_folder_service.auto_delete_imported_folders",
        lambda: called.append(1) or 0,
    )

    ces.cleanup_stale_downloads()

    assert called == [1]
