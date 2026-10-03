"""Report 2 guard: a failed/skipped tag write during download import must be
SURFACED, not swallowed.

The reported bug: *"when albums are downloaded from soulseek, the metadata on
the file isn't updated prior to moving it to the music directory.  Its using
the metadata from the downloaded file when copying"* — observed in Navidrome.

The automatic path itself writes the queue metadata before the move (pinned by
``test_download_import_matches_mb_release`` + an end-to-end probe of
``check_completed_downloads``).  What was broken is the FAILURE HANDLING: the
return value of ``update_file_metadata`` was dropped and exceptions were
logged at DEBUG (one caller used a bare ``except: pass``), so a write refused
by the tagging config gates, an unsupported format, or a writer failure moved
the file while it still carried the DOWNLOADED file's own tags — and the queue
logged a normal "imported" line with no trace.

What is pinned here:

* ``download_completion_service._apply_stored_metadata`` returns the write
  outcome and warns (never DEBUG-only) when it is False or raises;
* ``download_organize_service._apply_stored_metadata`` returns the outcome and
  ``organize_track`` surfaces a False/exception instead of ``pass``;
* ``organize_group_sync`` warns on a failed source write AND writes the
  metadata onto a PRE-EXISTING target — the copy is skipped there, so the
  library file previously kept whatever tags it already had;
* ``process_completed_queue_item`` warns on a failed write.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.downloads import download_completion_service as dcs
from services.downloads import download_organize_service as dos
from services.metadata import tag_file_service as tfs
from services.queue import queue_processing_service as qps


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------

class _RecLogger:
    """Structlog-shaped recorder: collects ``(level, event, kwargs)``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):  # pragma: no cover - trivial shim
        def _log(event, *args, **kw):
            self.calls.append((str(level), str(event), kw))
        return _log

    def warnings(self, contains: str = "") -> list[tuple[str, str, dict]]:
        return [
            c for c in self.calls
            if c[0] == "warning" and contains in c[1]
        ]


def _queue_item(**overrides) -> dict:
    item = {
        "id": 7,
        "artist": "Queued Band",
        "title": "Queued Track Title",
        "album": "Queued Album Name",
        "album_artist": "Queued Band",
        "year": "1999",
        "track_number": "1",
        "metadata": json.dumps({"musicbrainz_genres": "Thrash Metal"}),
        # No release MBID: keeps the album-level refresh OFFLINE in tests.
        "release_mbid": None,
        "release_id": None,
        "recording_mbid": None,
    }
    item.update(overrides)
    return item


def _patch_update(monkeypatch, *, result=None, exc=None) -> list:
    """Patch the tag writer; return the recorded ``(path, meta)`` calls."""
    calls: list[tuple[str, dict]] = []

    def _fake(path, meta):
        calls.append((path, meta))
        if exc is not None:
            raise exc
        return True if result is None else result

    monkeypatch.setattr(tfs, "update_file_metadata", _fake)
    return calls


def _make_mp3(path: Path, *, artist: str) -> None:
    from mutagen.id3 import ID3, TIT2, TPE1

    path.parent.mkdir(parents=True, exist_ok=True)
    frame = bytes([0xFF, 0xFB, 0x90, 0x00]) + bytes(413)
    path.write_bytes(frame * 20)
    id3 = ID3()
    id3.add(TIT2(encoding=3, text=["Peer File Title"]))
    id3.add(TPE1(encoding=3, text=[artist]))
    id3.save(str(path))


# ---------------------------------------------------------------------------
# 1. download_completion_service._apply_stored_metadata — the automatic path
# ---------------------------------------------------------------------------

class TestCompletionApplyStoredMetadata:
    def test_failed_write_returns_false_and_warns(self, monkeypatch):
        _patch_update(monkeypatch, result=False)
        rec = _RecLogger()
        monkeypatch.setattr(dcs, "logger", rec)

        ok = dcs._apply_stored_metadata(_queue_item(), "/music/Queued Band/01.flac")

        assert ok is False
        assert rec.warnings("keeps its downloaded tags"), (
            "a refused tag write must be logged at WARNING — it used to be a "
            "dropped return value with only a DEBUG line, so the import moved "
            "the file with the downloaded file's own tags and said nothing"
        )

    def test_raising_write_returns_false_and_warns(self, monkeypatch):
        _patch_update(monkeypatch, exc=OSError("disk full"))
        rec = _RecLogger()
        monkeypatch.setattr(dcs, "logger", rec)

        ok = dcs._apply_stored_metadata(_queue_item(), "/music/x.flac")

        assert ok is False
        assert rec.warnings("Could not apply stored metadata to file")

    def test_successful_write_returns_true_without_failure_warning(self, monkeypatch):
        calls = _patch_update(monkeypatch, result=True)
        rec = _RecLogger()
        monkeypatch.setattr(dcs, "logger", rec)

        ok = dcs._apply_stored_metadata(_queue_item(), "/music/x.flac")

        assert ok is True
        assert calls, "the writer must be invoked"
        assert not rec.warnings("keeps its downloaded tags")

    def test_writer_receives_the_queued_values(self, monkeypatch):
        calls = _patch_update(monkeypatch, result=True)
        dcs._apply_stored_metadata(_queue_item(), "/music/x.flac")

        path, meta = calls[0]
        assert path == "/music/x.flac"
        assert meta["title"] == "Queued Track Title"
        assert meta["album"] == "Queued Album Name"
        assert meta["musicbrainz_genres"] == "Thrash Metal"


# ---------------------------------------------------------------------------
# 2. download_organize_service — per-track Transfer / organize routes
# ---------------------------------------------------------------------------

class TestOrganizeApplyStoredMetadata:
    def test_returns_the_writer_outcome(self, monkeypatch):
        _patch_update(monkeypatch, result=False)
        assert dos._apply_stored_metadata(_queue_item(), "/music/x.mp3") is False

        _patch_update(monkeypatch, result=True)
        assert dos._apply_stored_metadata(_queue_item(), "/music/x.mp3") is True

    def test_empty_metadata_is_reported_not_written(self, monkeypatch):
        calls = _patch_update(monkeypatch, result=True)
        ok = dos._apply_stored_metadata({"id": 1}, "/music/x.mp3")
        assert ok is False
        assert not calls

    def test_organize_track_surfaces_failed_write(self, monkeypatch, tmp_path):
        downloads = tmp_path / "downloads"
        music = tmp_path / "music"
        src = downloads / "peer.flac"
        _make_mp3(src, artist="Queued Band")

        row = _queue_item(file_path=str(src), status="completed")
        monkeypatch.setattr(
            "db.repositories.queue.get_queue_item", lambda _qid: row
        )
        monkeypatch.setattr(
            dos, "get_config",
            lambda: {"downloads": {"folder": str(downloads)}},
        )
        moved: list[dict] = []

        def _move_to_library(**kw):
            moved.append(kw)
            return {"success": True, "target_path": str(music / "out.flac")}

        monkeypatch.setattr(
            dos, "get_infra",
            lambda: SimpleNamespace(fs=SimpleNamespace(
                music_root=music, move_to_library=_move_to_library,
            )),
        )
        _patch_update(monkeypatch, result=False)
        rec = _RecLogger()
        monkeypatch.setattr(dos, "logger", rec)

        result = dos.organize_track(7, {})

        assert moved, "the move still runs (tag failure must not block it)"
        assert rec.warnings("File tags not written before move"), (
            "organize_track used to swallow the failure with a bare "
            "'except Exception: pass'"
        )
        assert result.get("success") is True

    def test_organize_track_surfaces_raising_write(self, monkeypatch, tmp_path):
        downloads = tmp_path / "downloads"
        music = tmp_path / "music"
        src = downloads / "peer.flac"
        _make_mp3(src, artist="Queued Band")

        row = _queue_item(file_path=str(src), status="completed")
        monkeypatch.setattr(
            "db.repositories.queue.get_queue_item", lambda _qid: row
        )
        monkeypatch.setattr(
            dos, "get_config",
            lambda: {"downloads": {"folder": str(downloads)}},
        )
        monkeypatch.setattr(
            dos, "get_infra",
            lambda: SimpleNamespace(fs=SimpleNamespace(
                music_root=music,
                move_to_library=lambda **kw: (
                    {"success": True, "target_path": str(music / "out.flac")}
                ),
            )),
        )
        _patch_update(monkeypatch, exc=RuntimeError("mutagen blew up"))
        rec = _RecLogger()
        monkeypatch.setattr(dos, "logger", rec)

        dos.organize_track(7, {})

        assert rec.warnings("Stored metadata not applied before move")


# ---------------------------------------------------------------------------
# 3. organize_group_sync — group copy, incl. the pre-existing-target branch
# ---------------------------------------------------------------------------

class TestOrganizeGroupTagging:
    @pytest.fixture()
    def group_env(self, monkeypatch, tmp_path):
        """One completed item + a source MP3 + a PRE-EXISTING library target."""
        downloads = tmp_path / "downloads"
        music = tmp_path / "music"
        src = downloads / "grp" / "Queued Band - Queued Track Title.mp3"
        _make_mp3(src, artist="Queued Band")
        # The exact path build_organize_group_target_path would produce —
        # stubbed below so the test does not depend on the naming format.
        target = music / "Queued Band" / "1999 - Queued Album Name" / (
            "01. Queued Band - Queued Track Title.mp3"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"OLD-LIBRARY-BYTES-WITH-DOWNLOADED-TAGS")

        item = _queue_item(
            id=11,
            file_path=str(src),
            disc_number=None,
            status="completed",
        )

        monkeypatch.setenv("MUSIC_ROOT", str(music))
        monkeypatch.setattr(qps, "get_completed_group_queue_items", lambda _g: [item])

        class _NoMBTables:
            def __enter__(self):
                raise RuntimeError("probe: musicbrainz_release_tracks absent")

            def __exit__(self, *exc):
                return False

        # Avoid touching the shared test DB (its OperationalError would
        # dispose the engine and wipe the in-memory schema).
        monkeypatch.setattr(qps, "db_session", lambda: _NoMBTables())

        queued_updates: list[dict] = []
        monkeypatch.setattr(
            qps, "update_queue_item", lambda _id, **kw: queued_updates.append(kw)
        )
        monkeypatch.setattr(
            qps, "build_organize_group_target_path", lambda **kw: target
        )
        writes: list[tuple[str, dict]] = []

        def _write(path, meta):
            writes.append((path, meta))
            return True

        monkeypatch.setattr(qps, "update_file_metadata", _write)
        rec = _RecLogger()
        monkeypatch.setattr(qps, "logger", rec)
        return SimpleNamespace(
            src=src, target=target, writes=writes, rec=rec,
            queued_updates=queued_updates, item=item,
        )

    def test_preexisting_target_receives_the_metadata(self, group_env):
        """The copy is SKIPPED when the target exists — the target itself must
        be tagged, otherwise the library keeps its old (possibly
        downloaded-file) tags while the row flips to 'imported'."""
        before = group_env.target.read_bytes()

        result = qps.organize_group_sync("grp-1", {})

        assert result["success"] and result["organized"] == 1
        paths = [p for p, _ in group_env.writes]
        assert str(group_env.target) in paths, (
            "a pre-existing target never received the metadata — the skipped "
            "copy left the library file with its old tags (reported bug)"
        )
        assert group_env.target.read_bytes() == before, (
            "the existing target must NOT be clobbered by a copy"
        )
        # The source is still tagged first, before the (skipped) copy.
        assert paths[0] == str(group_env.src)
        assert any(
            u.get("status") == "imported" for u in group_env.queued_updates
        ), "the item must still be marked imported"

    def test_failed_source_write_is_surfaced(self, monkeypatch, group_env):
        def _write(path, meta):
            return False

        monkeypatch.setattr(qps, "update_file_metadata", _write)

        qps.organize_group_sync("grp-1", {})

        assert group_env.rec.warnings("source tag write failed"), (
            "a refused source write must be visible — the copy would carry "
            "the downloaded file's tags into the library silently"
        )
        assert group_env.rec.warnings("existing target tag write failed")

    def test_artist_gate_reads_the_source_file(self, group_env):
        """Control: the shipped gate really reads THIS source file (so the
        fixture cannot pass vacuously on a stubbed reader)."""
        from helpers.metadata_reader import read_mp3_metadata

        meta = read_mp3_metadata(str(group_env.src)) or {}
        assert str(meta.get("artist") or "") == "Queued Band"

        result = qps.organize_group_sync("grp-1", {})
        assert result["organized"] == 1


# ---------------------------------------------------------------------------
# 4. process_completed_queue_item — single completed item
# ---------------------------------------------------------------------------

class TestProcessCompletedSurfacesWriteFailure:
    def test_failed_write_warns_but_still_moves(self, monkeypatch):
        # qps binds update_file_metadata at MODULE level — patch the binding,
        # not the tag_file_service attribute.
        monkeypatch.setattr(qps, "update_file_metadata", lambda path, meta: False)
        moved: list[str] = []
        monkeypatch.setattr(
            qps, "rename_and_move_file",
            lambda path, meta: moved.append(path) or {
                "success": True, "target_path": "/music/out.mp3",
            },
        )
        monkeypatch.setattr(qps, "update_queue_item", lambda *a, **k: None)
        rec = _RecLogger()
        monkeypatch.setattr(qps, "logger", rec)

        result = qps.process_completed_queue_item({
            "id": 3,
            "file_path": "/downloads/x.mp3",
            "artist": "Queued Band",
            "title": "Queued Track Title",
            "album": "Queued Album Name",
        })

        assert result.get("success") is True
        assert moved
        assert rec.warnings("Completed-item tag write failed"), (
            "the completed-item path dropped the writer's return value"
        )
