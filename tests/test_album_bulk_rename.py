"""Album page: select multiple tracks → rename them all (config format,
FLAC→MP3 conversion when enabled).

Request: *"I want the album page to have an option to select multiple tracks
and select to rename them all. The rename would use the format set in the
config, converting flac to mp3 if that is set."*

What exists and what was missing:

* The rename service already renders ``downloads.file_name_format`` and
  already converts FLAC→MP3 when ``downloads.conversion.enabled`` (mode
  ``flac_to_mp3``) — but it only ever ran for a WHOLE album (Actions ▸
  Rename Files), and **neither album template rendered track checkboxes**,
  so both trees' selection machinery (``track-checkbox``,
  ``selectAllTracks``, ``bulkActionsToolbar``) was dead code.

Pinned here:

* service: ``track_ids`` narrows the run to the selection; an EMPTY
  selection renames NOTHING (never everything); no ``track_ids`` keeps the
  album-wide contract; the configured format and the conversion setting both
  drive the outcome;
* route: ``{"track_ids": [...]}`` body accepted, empty/invalid rejected,
  absent body → album-wide;
* wiring: both trees render the checkboxes + toolbar + button and ship
  ``renameSelectedTracks``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from sqlalchemy import text

import helpers.config_helpers as config_helpers
import routes.album_routes as album_routes
from db.engine import db_session
from db.models import Track


@pytest.fixture(autouse=True)
def _isolated_tracks():
    def _wipe() -> None:
        with db_session() as session:
            session.execute(text("DELETE FROM tracks"))

    _wipe()
    yield
    _wipe()


def _cfg(music_root: Path, *, conversion: bool, bitrate: int = 320) -> dict:
    return {
        "downloads": {
            # Deliberately simple so the expected target is arithmetic.
            "file_name_format": "{album_artist}/{album}/{track_number}. {title}",
            "conversion": {
                "enabled": conversion,
                "mode": "flac_to_mp3",
                "mp3_bitrate_kbps": bitrate,
            },
        },
        "music": {"root": str(music_root)},
    }


def _seed(track_id: str, *, title: str, file_path: Path,
          track_number: str = "1") -> None:
    with db_session() as session:
        session.execute(
            text(
                "INSERT INTO tracks (id, artist, album, album_artist, title, "
                "year, track_number, file_path) "
                "VALUES (:id, :artist, :album, :album_artist, :title, "
                ":year, :track_number, :file_path)"
            ),
            {
                "id": track_id,
                "artist": "Sel Artist",
                "album": "Sel Album",
                "album_artist": "Sel Artist",
                "title": title,
                "year": "2001",
                "track_number": track_number,
                "file_path": str(file_path),
            },
        )


def _write(path: Path, suffix: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"ID3" + bytes(64) if suffix == ".mp3" else b"fLaC" + bytes(64))
    return path


def _target(music_root: Path, track_number: str, title: str, ext: str) -> Path:
    return music_root / "Sel Artist" / "Sel Album" / f"{track_number}. {title}{ext}"


# ---------------------------------------------------------------------------
# 1. Service: selection, format, conversion
# ---------------------------------------------------------------------------

class TestRenameServiceSelection:
    def test_only_selected_tracks_are_renamed(self, monkeypatch, tmp_path):
        root = tmp_path / "music"
        src1 = _write(root / "incoming" / "One.flac", ".flac")
        src2 = _write(root / "incoming" / "Two.flac", ".flac")
        src3 = _write(root / "incoming" / "Three.flac", ".flac")
        _seed("r1", title="One", file_path=src1)
        _seed("r2", title="Two", file_path=src2, track_number="2")
        _seed("r3", title="Three", file_path=src3, track_number="3")
        monkeypatch.setattr(config_helpers, "get_config", lambda: _cfg(root, conversion=False))

        from services.metadata.album_service import rename_album_files_service
        result = rename_album_files_service("Sel Artist", "Sel Album", track_ids=["r1", "r3"])

        assert result["success"] is True
        assert result["renamed_count"] == 2
        assert _target(root, "01", "One", ".flac").is_file(), "selected track must move to the formatted path"
        assert _target(root, "03", "Three", ".flac").is_file()
        assert src2.is_file(), "an UNSELECTED track must not be touched"
        assert not src1.exists() and not src3.exists()

    def test_empty_selection_renames_nothing(self, monkeypatch, tmp_path):
        root = tmp_path / "music"
        src = _write(root / "incoming" / "One.flac", ".flac")
        _seed("r1", title="One", file_path=src)
        monkeypatch.setattr(config_helpers, "get_config", lambda: _cfg(root, conversion=False))

        from services.metadata.album_service import rename_album_files_service
        result = rename_album_files_service("Sel Artist", "Sel Album", track_ids=[])

        assert result["success"] is False
        assert src.is_file(), "an empty selection must NEVER rename everything"

    def test_no_track_ids_keeps_the_album_wide_contract(self, monkeypatch, tmp_path):
        root = tmp_path / "music"
        src1 = _write(root / "incoming" / "One.flac", ".flac")
        src2 = _write(root / "incoming" / "Two.flac", ".flac")
        _seed("r1", title="One", file_path=src1)
        _seed("r2", title="Two", file_path=src2, track_number="2")
        monkeypatch.setattr(config_helpers, "get_config", lambda: _cfg(root, conversion=False))

        from services.metadata.album_service import rename_album_files_service
        result = rename_album_files_service("Sel Artist", "Sel Album")

        assert result["renamed_count"] == 2
        assert _target(root, "01", "One", ".flac").is_file()
        assert _target(root, "02", "Two", ".flac").is_file()

    def test_selection_outside_the_album_is_an_error(self, monkeypatch, tmp_path):
        root = tmp_path / "music"
        src = _write(root / "incoming" / "One.flac", ".flac")
        _seed("r1", title="One", file_path=src)
        monkeypatch.setattr(config_helpers, "get_config", lambda: _cfg(root, conversion=False))

        from services.metadata.album_service import rename_album_files_service
        result = rename_album_files_service("Sel Artist", "Sel Album", track_ids=["nope"])

        assert result["success"] is False
        assert src.is_file()


class TestRenameServiceConversion:
    @pytest.fixture()
    def fake_convert(self, monkeypatch):
        calls: list[str] = []

        def _convert(flac_path: str, bitrate: str = "320k") -> str:
            calls.append(bitrate)
            mp3_path = os.path.splitext(flac_path)[0] + ".mp3"
            Path(mp3_path).write_bytes(b"ID3" + bytes(64))
            os.remove(flac_path)          # mirrors the real helper's contract
            return mp3_path

        import services.metadata.tag_file_service as tfs
        monkeypatch.setattr(tfs, "convert_flac_to_mp3", _convert)
        return calls

    def test_flac_converts_when_conversion_enabled(self, monkeypatch, tmp_path, fake_convert):
        root = tmp_path / "music"
        src = _write(root / "incoming" / "One.flac", ".flac")
        _seed("r1", title="One", file_path=src)
        monkeypatch.setattr(
            config_helpers, "get_config",
            lambda: _cfg(root, conversion=True, bitrate=192),
        )

        from services.metadata.album_service import rename_album_files_service
        result = rename_album_files_service("Sel Artist", "Sel Album", track_ids=["r1"])

        assert result["success"] is True
        assert fake_convert == ["192k"], "the configured bitrate must be used"
        assert _target(root, "01", "One", ".mp3").is_file(), (
            "the formatted target must be an MP3 when conversion is enabled"
        )
        assert not src.exists(), "the source FLAC is consumed by the conversion"
        assert not _target(root, "01", "One", ".flac").exists()

    def test_conversion_off_keeps_the_flac(self, monkeypatch, tmp_path, fake_convert):
        root = tmp_path / "music"
        src = _write(root / "incoming" / "One.flac", ".flac")
        _seed("r1", title="One", file_path=src)
        monkeypatch.setattr(config_helpers, "get_config", lambda: _cfg(root, conversion=False))

        from services.metadata.album_service import rename_album_files_service
        result = rename_album_files_service("Sel Artist", "Sel Album", track_ids=["r1"])

        assert result["success"] is True
        assert fake_convert == [], "conversion must not run when it is disabled"
        assert _target(root, "01", "One", ".flac").is_file()

    def test_failed_conversion_reports_an_error_not_a_move(self, monkeypatch, tmp_path):
        import services.metadata.tag_file_service as tfs

        root = tmp_path / "music"
        src = _write(root / "incoming" / "One.flac", ".flac")
        _seed("r1", title="One", file_path=src)
        monkeypatch.setattr(config_helpers, "get_config", lambda: _cfg(root, conversion=True))
        monkeypatch.setattr(tfs, "convert_flac_to_mp3", lambda *a, **k: None)  # ffmpeg missing

        from services.metadata.album_service import rename_album_files_service
        result = rename_album_files_service("Sel Artist", "Sel Album", track_ids=["r1"])

        assert result["success"] is False
        assert any("conversion failed" in e for e in result["errors"])
        assert src.is_file(), "a failed conversion must leave the source in place"


# ---------------------------------------------------------------------------
# 2. Route: the track_ids body
# ---------------------------------------------------------------------------

class TestRenameFilesRoute:
    URL = "/api/album/Sel%20Artist/Sel%20Album/rename-files"

    async def test_body_narrows_to_the_selection(self, monkeypatch, client):
        seen: dict = {}

        def _fake(artist, album, track_ids=None):
            seen.update(artist=artist, album=album, track_ids=track_ids)
            return {"success": True, "renamed_count": 2}

        monkeypatch.setattr(album_routes, "rename_album_files_service", _fake)

        resp = await client.post(self.URL, json={"track_ids": ["r1", "r2"]})

        assert resp.status_code == 200
        assert seen["artist"] == "Sel Artist" and seen["album"] == "Sel Album"
        assert seen["track_ids"] == ["r1", "r2"]

    async def test_no_body_stays_album_wide(self, monkeypatch, client):
        seen: dict = {}

        def _fake(artist, album, track_ids=None):
            seen.update(track_ids=track_ids)
            return {"success": True, "renamed_count": 9}

        monkeypatch.setattr(album_routes, "rename_album_files_service", _fake)

        resp = await client.post(self.URL, json={})

        assert resp.status_code == 200
        assert seen["track_ids"] is None, "an absent track_ids must keep the album-wide behaviour"

    @pytest.mark.parametrize("bad_body", [{"track_ids": []}, {"track_ids": "r1"}])
    async def test_invalid_selection_is_rejected(self, client, bad_body):
        resp = await client.post(self.URL, json=bad_body)
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 3. Wiring: both trees render the selection UI and ship the handler
# ---------------------------------------------------------------------------

class TestAlbumBulkRenameWiring:
    TEMPLATES = [
        "templates/pages/album_detail.html",
        "test_site/templates/Pages/album_detail.html",
    ]
    SCRIPTS = [
        "static/js/album_detail.js",
        "test_site/static/js/pages/album.js",
    ]
    ROOT = Path(__file__).resolve().parent.parent

    @pytest.mark.parametrize("rel", TEMPLATES)
    def test_template_renders_the_selection_ui(self, rel):
        html = (self.ROOT / rel).read_text(encoding="utf-8")
        assert 'id="selectAllTracksCheckbox"' in html, "header select-all is missing"
        assert 'class="track-checkbox' in html, "per-row checkboxes are missing"
        assert 'id="bulkActionsToolbar"' in html, "the bulk toolbar is missing"
        assert 'onclick="renameSelectedTracks(this)"' in html, "the Rename Selected button is missing"
        # The checkbox must live INSIDE an existing cell — a new column would
        # shift every colspan the injected sub-rows rely on.
        assert html.count('data-track-id="{{ track.id }}"') >= 2  # row attr + checkbox

    @pytest.mark.parametrize("rel", TEMPLATES)
    def test_template_checkbox_does_not_add_a_column(self, rel):
        html = (self.ROOT / rel).read_text(encoding="utf-8")
        assert '<td class="text-center text-muted">' in html, (
            "the checkbox must be prepended to the # cell, not a new column"
        )

    @pytest.mark.parametrize("rel", SCRIPTS)
    def test_script_ships_rename_selected(self, rel):
        source = (self.ROOT / rel).read_text(encoding="utf-8")
        assert "renameSelectedTracks" in source
        assert "/rename-files" in source
        assert "track_ids: ids" in source or "track_ids: ids" in source.replace("'", '"')
        # One track must never be acted on twice.
        assert "[...new Set(" in source
