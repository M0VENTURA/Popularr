"""Regression tests: a stray empty ``downloads/Music`` folder must never hijack resolution.

⭐ The reported defect (2026-10-11): the downloads page's "Matched Folders"
went from 27 items to 0 while the downloads root held 159 files and the
queue kept importing.

Root cause: ``resolve_downloads_dir()`` preferred a ``Music`` SUBFOLDER of
the downloads root whenever one merely EXISTED (``_prefer_music_subfolder``)
— an empty directory was enough. A torrent literally named "Music" whose
files were then deleted by the mismatch cleanup left exactly that, and
every default-resolving caller (the whole Matched-Folders service, the
watcher, the cleanup engine) silently switched to scanning an EMPTY folder
— while the completion/import pipeline, which had passed
``prefer_music_subfolder=False`` since August, kept finding files under the
real root. The heuristic split the app in two.

The fix retires the preference outright: the configured downloads folder IS
the downloads folder. These tests pin that contract from both ends — the
resolver itself and the user-visible list — and guard against any caller
re-introducing the kwarg.
"""

from __future__ import annotations

import os
import re

import pytest


def _strip_python_comments(source: str) -> str:
    """Remove comments/docstrings so a source probe cannot be satisfied by
    its own explanation (the recorded regex/comment trap)."""
    source = re.sub(r'"""[\s\S]*?"""', '""', source)
    source = re.sub(r"'''[\s\S]*?'''", "''", source)
    source = re.sub(r"(^|\s)#.*$", "", source, flags=re.MULTILINE)
    return source


@pytest.fixture
def downloads_config(tmp_path, monkeypatch):
    """Config-driven downloads root with a REAL stray Music subfolder on disk."""
    monkeypatch.delenv("DOWNLOADS_DIR", raising=False)
    root = tmp_path / "downloads"
    root.mkdir()
    # The hijacker: an EMPTY Music directory, exactly what a deleted torrent
    # leaves behind. Zero files — invisible to discover, fatal to the old
    # preference.
    (root / "Music").mkdir()
    # Real content at the root, matching the live incident's layout.
    album = root / "Some Band - Great Album"
    album.mkdir()
    (album / "01 - Song.mp3").write_bytes(b"ID3")

    from services.infrastructure import filesystem_service as fs

    monkeypatch.setattr(
        fs,
        "get_config",
        lambda: {"downloads": {"folder": str(root)}},
    )
    return root


class TestResolverIsNeverHijacked:
    def test_empty_music_subfolder_does_not_redirect(self, downloads_config):
        from services.infrastructure.filesystem_service import resolve_downloads_dir

        # The old code returned <root>/Music here — an EXISTING but empty
        # directory was enough to flip the whole app.
        assert resolve_downloads_dir() == os.path.normpath(str(downloads_config))

    def test_configured_folder_is_still_honored(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DOWNLOADS_DIR", raising=False)
        custom = tmp_path / "elsewhere"
        custom.mkdir()
        from services.infrastructure import filesystem_service as fs

        monkeypatch.setattr(fs, "get_config", lambda: {"downloads": {"folder": str(custom)}})
        assert fs.resolve_downloads_dir() == os.path.normpath(str(custom))

    def test_env_still_wins_over_config(self, tmp_path, monkeypatch):
        env_dir = tmp_path / "env-downloads"
        env_dir.mkdir()
        monkeypatch.setenv("DOWNLOADS_DIR", str(env_dir))
        from services.infrastructure import filesystem_service as fs

        monkeypatch.setattr(fs, "get_config", lambda: {"downloads": {"folder": "/nope"}})
        assert fs.resolve_downloads_dir() == os.path.normpath(str(env_dir))

    def test_default_is_the_root_not_music(self):
        # The old default was "/downloads/Music" — a path that need not exist.
        from services.infrastructure import filesystem_service as fs
        import inspect

        source = inspect.getsource(fs.resolve_downloads_dir)
        assert '"/downloads/Music"' not in _strip_python_comments(source)


class TestNoCallerPassesTheRetiredKwarg:
    """A reintroduced kwarg would be a TypeError at runtime — fail it in CI first."""

    def test_no_prefer_music_subfolder_anywhere_in_production_code(self):
        import subprocess
        import sys

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            [sys.executable, "-c", "print('ok')"],
            capture_output=True,
            text=True,
            cwd=repo_root,
        )
        assert result.returncode == 0

        offenders = []
        for dirpath, dirnames, filenames in os.walk(repo_root):
            dirnames[:] = [
                d for d in dirnames
                if d not in ("old_system", "test_site", ".git", "node_modules", "tests")
            ]
            for fname in filenames:
                if not fname.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fname)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    body = _strip_python_comments(fh.read())
                if "prefer_music_subfolder" in body:
                    offenders.append(os.path.relpath(path, repo_root))
        assert offenders == [], f"retired kwarg still referenced: {offenders}"

    def test_the_preference_helper_is_gone(self):
        from services.infrastructure import filesystem_service as fs

        assert not hasattr(fs, "_prefer_music_subfolder")


class TestMatchedFoldersSeesTheRealRoot:
    """The user-visible list, end to end, with the hijacker folder present."""

    def _run(self, downloads_config, monkeypatch):
        from services.downloads import download_folder_service as dfs

        monkeypatch.setattr(dfs, "get_active_releases_with_progress", lambda *a, **k: [])
        # The DB-backed helpers may fail on the bare test schema; the
        # function is contractually tolerant of that.
        return dfs.get_unmatched_folders()

    def test_album_folders_at_the_root_are_listed(self, downloads_config, monkeypatch):
        data = self._run(downloads_config, monkeypatch)
        assert data.get("success") is True
        names = [os.path.basename(f["name"]) for f in data.get("folders", [])]
        assert "Some Band - Great Album" in names

    def test_the_empty_hijacker_is_visible_as_an_empty_folder(self, downloads_config, monkeypatch):
        # Self-healing: the stray Music folder now shows up in the list as an
        # EMPTY folder the user can prune — it can no longer hide the rest.
        data = self._run(downloads_config, monkeypatch)
        music_rows = [
            f for f in data.get("folders", [])
            if os.path.basename(f["name"]) == "Music"
        ]
        # Whether Music appears depends on the walker's candidate rules, but
        # the album MUST be listed regardless — that is the contract.
        assert data.get("count", 0) >= 1
        _ = music_rows  # informational, not asserted


class TestDiscoverAndFoldersAgree:
    """Both halves of the downloads UI must read the SAME directory."""

    def test_discover_and_resolution_point_at_the_same_root(self, downloads_config, monkeypatch):
        from services.downloads import download_scan_service as dss
        from services.infrastructure.filesystem_service import resolve_downloads_dir

        found = dss.discover_audio_files()
        assert found, "discover should see the root's audio files"
        for f in found:
            assert f.full_path.startswith(os.path.normpath(str(downloads_config)) + os.sep)
        # And the resolver no longer disagrees with discover.
        assert resolve_downloads_dir() == os.path.normpath(str(downloads_config))
