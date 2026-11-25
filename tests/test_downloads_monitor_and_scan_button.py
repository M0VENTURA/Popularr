"""Two reported faults: downloads folders not listed, and the Start Scan button.

REPORT

> "The download monitor page is still not showing the albums found under the
>  downloads directory"

> "Also when a Navidrome import is happening, the start scan button on the
>  dashboard isn't available to press"

The first had THREE separate causes, all silent:

1. ``download_folder_service`` kept its own hardcoded audio-extension set that
   had drifted from ``config_helpers.get_supported_audio_formats``. It omitted
   ``.opus``, ``.mp4``, ``.ape``, ``.wv``, ``.dsf`` and others, so an album of
   those files contained "no audio" and was dropped — or listed as an empty
   folder.
2. An album sitting DIRECTLY in the downloads root was never a candidate: the
   collector was only ever called on the root's subdirectories.
3. The frontend treated a FAILED read as "no folders": the card was hidden and
   nothing was said, so a backend error was indistinguishable from an empty
   directory.

The second is a state-machine bug: ``navidrome_scan`` is in
``progress_service.SCAN_TYPES``, so a running IMPORT published an
``active_scans`` entry with ``is_running: true`` — and the dashboard swapped the
Start button out for the "scanning" panel for the whole import. That also made
the scan pre-flight unreachable, which exists precisely to offer "cancel and
start the new one".
"""

from __future__ import annotations

import inspect
import os
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MONITOR_JS = REPO_ROOT / "test_site/static/js/pages/monitor.js"
DASH_JS = REPO_ROOT / "test_site/static/js/pages/dashboard.js"
LIVE_DASH_JS = REPO_ROOT / "test_site/static/js/pages/dashboard.js"


@pytest.fixture()
def dl_service(tmp_path, monkeypatch):
    """The downloads service with its roots pointed at a temp tree."""
    from services.downloads import download_folder_service as svc

    archive = tmp_path / "_archive"
    monkeypatch.setattr(svc, "resolve_downloads_dir", lambda: str(tmp_path))
    monkeypatch.setattr(svc, "resolve_original_archive_dir", lambda: str(archive))
    return svc, tmp_path


def _make(root, rel, name="01.flac"):
    p = root / rel
    p.mkdir(parents=True, exist_ok=True)
    f = p / name
    f.write_bytes(b"x" * 32)
    return p


class TestEverySupportedFormatIsSeenAsAudio:
    """⭐ CAUSE 1: an album of Opus files "contained no audio"."""

    @pytest.mark.parametrize(
        "ext",
        [".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".opus",
         ".wav", ".aac", ".wma", ".aiff", ".ape", ".wv", ".dsf"],
    )
    def test_a_folder_of_this_format_is_discovered(self, dl_service, ext):
        svc, root = dl_service
        _make(root, "Album", f"01{ext}")
        cands = svc._iter_matched_folder_candidates(str(root), str(root / "_archive"))
        assert cands, f"a folder of {ext} files was not discovered"

    def test_the_service_uses_the_canonical_list(self) -> None:
        """⭐ It had its OWN hardcoded set, which is how it drifted."""
        from helpers.config_helpers import get_supported_audio_formats
        from services.downloads import download_folder_service as svc

        assert svc.SUPPORTED_AUDIO_FORMATS == get_supported_audio_formats()

    def test_the_canonical_list_covers_what_the_tag_writer_writes(self) -> None:
        """The writer handles .opus/.mp4; the scan list must agree."""
        from helpers.config_helpers import get_supported_audio_formats

        writeable = {".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".opus"}
        missing = writeable - get_supported_audio_formats()
        assert not missing, f"scan list cannot see formats the writer handles: {missing}"


class TestAudioDirectlyInTheDownloadsRoot:
    """⭐ CAUSE 2: an album dropped flat was invisible."""

    def test_loose_files_at_the_root_are_discovered(self, dl_service) -> None:
        svc, root = dl_service
        (root / "01.flac").write_bytes(b"x" * 32)
        (root / "02.flac").write_bytes(b"x" * 32)
        cands = svc._iter_matched_folder_candidates(str(root), str(root / "_archive"))
        assert any(os.path.normpath(c[0]) == os.path.normpath(str(root)) for c in cands)

    def test_a_root_with_only_subfolders_adds_no_root_entry(self, dl_service) -> None:
        """Guards the check from inventing an empty root album."""
        svc, root = dl_service
        _make(root, "Artist/Album")
        cands = svc._iter_matched_folder_candidates(str(root), str(root / "_archive"))
        assert not any(
            os.path.normpath(c[0]) == os.path.normpath(str(root)) for c in cands
        )
        assert len(cands) == 1

    def test_nested_albums_are_still_found(self, dl_service) -> None:
        svc, root = dl_service
        _make(root, "Artist - Album")
        _make(root, "Artist/Album Two")
        cands = svc._iter_matched_folder_candidates(str(root), str(root / "_archive"))
        assert len(cands) == 2


class TestTheEndpointReturnsTheFolders:
    def test_a_real_tree_is_reported(self, dl_service, monkeypatch) -> None:
        svc, root = dl_service
        _make(root, "Artist - Album")
        monkeypatch.setattr(svc, "get_active_releases_with_progress", lambda: [])
        result = svc.get_unmatched_folders()
        assert result["success"] is True
        assert result["count"] >= 1


class TestAFailureNeverLooksLikeAnEmptyLibrary:
    """⭐ CAUSE 3: the card was hidden on error, with nothing said."""

    def test_the_success_flag_is_checked(self) -> None:
        src = MONITOR_JS.read_text(encoding="utf-8")
        assert "data.success === false" in src, (
            "a failed read is still rendered as 'no folders'"
        )

    def test_the_error_branch_keeps_the_card_visible(self) -> None:
        src = MONITOR_JS.read_text(encoding="utf-8")
        start = src.index("data.success === false")
        block = src[start:start + 700]
        assert "display = 'block'" in block
        assert "Could not read the downloads folder" in block

    def test_a_transport_error_is_reported_to_the_user(self) -> None:
        src = MONITOR_JS.read_text(encoding="utf-8")
        start = src.index("Error loading unmatched folders")
        block = src[start:start + 700]
        assert "Could not load folders" in block


class TestTheStartButtonSurvivesANavidromeImport:
    """⭐ The import was mistaken for a scan and took the button away."""

    @pytest.mark.parametrize("path", [DASH_JS, LIVE_DASH_JS])
    def test_navidrome_scan_is_excluded_from_the_scanning_state(self, path) -> None:
        src = path.read_text(encoding="utf-8")
        assert "IMPORT_ONLY_TYPES" in src, (
            "a Navidrome import still hides the Start Scan button"
        )
        assert "'navidrome_scan'" in src

    @pytest.mark.parametrize("path", [DASH_JS, LIVE_DASH_JS])
    def test_the_idle_state_is_governed_by_real_scans_only(self, path) -> None:
        src = path.read_text(encoding="utf-8")
        start = src.index("IMPORT_ONLY_TYPES")
        block = src[start:start + 1400]
        assert "idleState.classList.toggle('d-none', !!running)" in block
        assert "realScans" in block

    @pytest.mark.parametrize("path", [DASH_JS, LIVE_DASH_JS])
    def test_the_import_still_shows_its_progress(self, path) -> None:
        """The progress block must not vanish with the button."""
        src = path.read_text(encoding="utf-8")
        start = src.index("IMPORT_ONLY_TYPES")
        block = src[start:start + 1600]
        assert "importRunning" in block
        assert "!(running || importRunning)" in block

    @pytest.mark.parametrize("path", [DASH_JS, LIVE_DASH_JS])
    def test_the_import_is_named_in_the_state_label(self, path) -> None:
        src = path.read_text(encoding="utf-8")
        assert "Importing from Navidrome" in src
