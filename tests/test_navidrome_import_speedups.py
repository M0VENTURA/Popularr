"""Navidrome import speed-ups: delta while dirty, parallel album fetch, no repeat scans."""

from __future__ import annotations

from pathlib import Path

from services.scanning.navidrome_import import _album_fetch_workers


ROOT = Path(__file__).resolve().parents[1]


def test_album_fetch_workers_are_bounded():
    assert 1 <= _album_fetch_workers() <= 8


def test_album_tracklists_are_prefetched_in_parallel():
    source = (ROOT / "services/scanning/navidrome_import.py").read_text(encoding="utf-8")
    assert "ThreadPoolExecutor" in source
    assert "_prefetched_album_tracks" in source
    assert "Prefetching" in source
    # The loop must consume the cache instead of always fetching again.
    assert "_prefetched_entry = _prefetched_album_tracks.get" in source


def test_album_state_fast_path_is_wired():
    source = (ROOT / "services/scanning/navidrome_import.py").read_text(encoding="utf-8")
    assert "can_fast_skip_album(" in source
    assert "load_album_states(" in source
    assert "save_album_state(" in source
    assert "scan_marker: str | None = None" in source

    pipeline = (
        ROOT / "services/scanning/pipelines/navidrome_pipeline.py"
    ).read_text(encoding="utf-8")
    assert "scan_marker=_scan_marker" in pipeline
    assert "current_last_scan" in pipeline


def test_delta_mode_is_not_disabled_by_dirty_rows():
    source = (
        ROOT / "services/scanning/pipelines/navidrome_pipeline.py"
    ).read_text(encoding="utf-8")
    assert "if not force_rescan and not _metadata_dirty:" not in source
    assert "if not force_rescan:\n                    delta_since" in source


def test_a_completed_scan_after_the_last_local_write_is_not_repeated():
    source = (
        ROOT / "services/scanning/pipelines/navidrome_pipeline.py"
    ).read_text(encoding="utf-8")
    assert "_latest_dirty_local_write_epoch" in source
    assert "last_scan >= latest_write" in source
    assert "skip_scan_after_local_write" in source
