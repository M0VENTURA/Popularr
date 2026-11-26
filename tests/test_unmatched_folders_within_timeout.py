"""The downloads folder listing must answer inside the client timeout.

Reported: *"Matched & Unmatched folders is showing Could not load folders:
Request timed out after 30s. It should be looking at the /downloads folder set
up in the config page."*

Two separate things:

1. `get_unmatched_folders` opened EVERY audio file in EVERY folder with mutagen
   to derive the artist/album group. On a downloads root with hundreds of files
   that is hundreds of full tag reads per poll, and the monitor polls — so the
   request blew past the client's 30 s timeout.
2. The folder it walks must be the CONFIGURED downloads folder
   (`downloads.folder`, then `downloads.monitor_folder`), not a hardcoded path.
"""

from __future__ import annotations

import importlib
import os
import time

import pytest


@pytest.fixture()
def folder_service():
    return importlib.import_module("services.downloads.download_folder_service")


@pytest.fixture()
def downloads_tree(tmp_path):
    """A downloads root with an album folder holding several audio files."""
    root = tmp_path / "downloads"
    album = root / "Some Artist" / "2001 - Some Album"
    album.mkdir(parents=True)
    for n in range(1, 6):
        (album / f"{n:02d} - Some Artist - Track {n}.mp3").write_bytes(b"x" * 64)
    return root


class TestTheConfiguredFolderIsUsed:
    def test_the_configured_folder_wins_over_the_default(
        self, folder_service, tmp_path, monkeypatch
    ):
        configured = tmp_path / "my-downloads"
        configured.mkdir()
        monkeypatch.delenv("DOWNLOADS_DIR", raising=False)
        monkeypatch.setattr(
            folder_service,
            "resolve_downloads_dir",
            lambda: os.path.normpath(str(configured)),
            raising=True,
        )

        assert folder_service.resolve_downloads_dir() == os.path.normpath(str(configured))


class TestTheTagReadIsBounded:
    def test_a_slow_tag_reader_cannot_exceed_the_budget(
        self, folder_service, downloads_tree, monkeypatch
    ):
        """A blocking tag read must not be able to hang the endpoint."""
        monkeypatch.setattr(
            folder_service, "resolve_downloads_dir", lambda: str(downloads_tree)
        )
        monkeypatch.setattr(folder_service, "resolve_original_archive_dir", lambda: "")
        monkeypatch.setattr(folder_service, "_tracked_monitoring_folders", lambda: set())
        monkeypatch.setattr(folder_service, "_imported_source_paths", lambda: set())
        monkeypatch.setattr(
            folder_service, "get_active_releases_with_progress", lambda: []
        )
        monkeypatch.setattr(folder_service, "_METADATA_READ_BUDGET_SECONDS", 0.0)

        calls: list[str] = []

        def _slow(folder_path, file_name):
            calls.append(file_name)
            return {"artist": "Tagged", "album": "Tagged Album", "title": "Tagged"}

        monkeypatch.setattr(folder_service, "_read_audio_metadata", _slow)

        started = time.monotonic()
        result = folder_service.get_unmatched_folders()
        elapsed = time.monotonic() - started

        assert result["success"] is True
        assert elapsed < 5, f"the endpoint took {elapsed:.1f}s"
        assert calls == [], "tags were read even though the budget was zero"
        assert result["tags_truncated"] is True, (
            "the caller cannot tell that names came from filenames"
        )

    def test_names_are_used_when_the_budget_is_spent(self, folder_service):
        meta = folder_service._metadata_from_name("05 - Some Artist - Track Five.mp3")
        assert meta["artist"] == "Some Artist"
        assert meta["title"] == "Track Five"
        # Never guessed: a filename does not carry the album, and a made-up one
        # would feed the wrong album into folder matching.
        assert meta["album"] == ""

    def test_the_tag_result_is_cached_by_mtime_and_size(
        self, folder_service, downloads_tree, monkeypatch
    ):
        album = next(downloads_tree.rglob("*.mp3"))
        folder = str(album.parent)
        name = album.name

        reads: list[str] = []
        monkeypatch.setattr(
            folder_service, "_read_audio_metadata",
            lambda f, n: reads.append(n) or {"artist": "A", "album": "B", "title": "T"},
        )
        folder_service._metadata_cache.clear()

        first = folder_service._cached_audio_metadata(folder, name)
        second = folder_service._cached_audio_metadata(folder, name)

        assert first == second
        assert reads == [name], "the same unchanged file was read twice"

    def test_a_changed_file_is_re_read(
        self, folder_service, downloads_tree, monkeypatch
    ):
        album = next(downloads_tree.rglob("*.mp3"))
        folder = str(album.parent)
        name = album.name

        reads: list[str] = []
        monkeypatch.setattr(
            folder_service, "_read_audio_metadata",
            lambda f, n: reads.append(n) or {"artist": "A", "album": "B", "title": "T"},
        )
        folder_service._metadata_cache.clear()

        folder_service._cached_audio_metadata(folder, name)
        os.utime(album, (time.time() + 10, time.time() + 10))
        folder_service._cached_audio_metadata(folder, name)

        assert len(reads) == 2, "a re-tagged file was served from the cache"
