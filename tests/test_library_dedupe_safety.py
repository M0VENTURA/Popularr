"""Library deduplication must never delete a different track.

REPORTED
--------
``dedupe_library_folder`` removed

    05. Stray Kids - BEWARE.mp3

and kept

    02. Stray Kids - Double Knot -Japanese ver.-.mp3

Those are DIFFERENT tracks. The old key was ``artist::tag-title``; a shared or
wrong tag was enough to pair them and ``os.remove`` made the loss permanent.

The new rule requires agreement on artist, TAG title, FILENAME title and
duration, and the loser is renamed to ``*.duplicate`` (quarantine), never
deleted.
"""

from __future__ import annotations

import os

from services.downloads import download_organize_helpers as doh


def _metadata(artist: str, title: str, duration: float, size: int = 1000, quality: int = 100):
    return artist, title, duration, size, quality


def _patch_metadata(monkeypatch, values: dict[str, tuple]):
    def _read(path: str):
        return values[os.path.basename(path)]

    monkeypatch.setattr(doh, "_read_dedupe_metadata", _read)


def test_different_filename_titles_are_never_quarantined(monkeypatch, tmp_path):
    """A shared wrong TAG cannot pair two differently named songs."""
    beware = tmp_path / "05. Stray Kids - BEWARE.mp3"
    double_knot = tmp_path / "02. Stray Kids - Double Knot -Japanese ver.-.mp3"
    beware.write_bytes(b"a")
    double_knot.write_bytes(b"b")

    _patch_metadata(monkeypatch, {
        beware.name: _metadata("Stray Kids", "Double Knot", 180.0),
        double_knot.name: _metadata("Stray Kids", "Double Knot", 180.5),
    })

    result = doh.dedupe_library_folder(str(tmp_path))

    assert result["quarantined"] == 0
    assert beware.exists(), "a differently NAMED track was taken out of the library"
    assert double_knot.exists()


def test_unknown_duration_is_never_a_duplicate(monkeypatch, tmp_path):
    first = tmp_path / "01 - Song.mp3"
    second = tmp_path / "Song.mp3"
    first.write_bytes(b"a")
    second.write_bytes(b"b")

    _patch_metadata(monkeypatch, {
        first.name: _metadata("Artist", "Song", 0.0),
        second.name: _metadata("Artist", "Song", 0.0),
    })

    result = doh.dedupe_library_folder(str(tmp_path))

    assert result["quarantined"] == 0
    assert first.exists() and second.exists()


def test_a_real_duplicate_is_quarantined_not_deleted(monkeypatch, tmp_path):
    keep = tmp_path / "Song.mp3"
    duplicate = tmp_path / "01 - Song.mp3"
    keep.write_bytes(b"high")
    duplicate.write_bytes(b"low")

    _patch_metadata(monkeypatch, {
        keep.name: _metadata("Artist", "Song", 200.0, quality=200_000),
        duplicate.name: _metadata("Artist", "Song", 200.2, quality=10),
    })

    result = doh.dedupe_library_folder(str(tmp_path))

    assert result["quarantined"] == 1
    assert keep.exists(), "the better copy must be kept"
    assert not duplicate.exists()
    quarantined = tmp_path / (duplicate.name + doh._DEDUPE_QUARANTINE_SUFFIX)
    assert quarantined.exists(), "the loser must be recoverable, not deleted"
    assert quarantined.read_bytes() == b"low"


def test_cleanup_disabled_does_nothing(monkeypatch, tmp_path):
    from services.downloads import download_scan_service as dss

    first = tmp_path / "01 - Song.mp3"
    second = tmp_path / "Song.mp3"
    first.write_bytes(b"a")
    second.write_bytes(b"b")

    monkeypatch.setattr(dss, "_duplicate_cleanup_config", lambda *a, **k: False)
    _patch_metadata(monkeypatch, {
        first.name: _metadata("Artist", "Song", 200.0, quality=1),
        second.name: _metadata("Artist", "Song", 200.0, quality=2),
    })

    result = doh.dedupe_library_folder(str(tmp_path))

    assert result.get("skipped") is True
    assert first.exists() and second.exists()
