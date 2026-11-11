"""A redownload-for-a-better-version deletes the old library version.

REPORTED
--------
"When a track is selected to download a better version when the track length
doesn't match, can we have it so it deletes the old version automatically when
the new version is imported?"

The queue row carries ``replace_track_id`` (set only by the Redownload correct
version button). After the new file verifies and imports, the old file and its
``tracks`` row are removed. The cleanup is deliberately conservative.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from db.engine import db_session
from services.downloads import download_completion_service as dcs


@pytest.fixture(autouse=True)
def _wipe_tracks():
    def _wipe():
        with db_session() as session:
            session.execute(text("DELETE FROM tracks"))

    _wipe()
    yield
    _wipe()


def _seed_track(track_id: str, file_path: str) -> None:
    with db_session() as session:
        session.execute(
            text(
                "INSERT INTO tracks (id, artist, album, title, file_path) "
                "VALUES (:id, 'Artist', 'Album', 'Song', :path)"
            ),
            {"id": track_id, "path": file_path},
        )


def _track_exists(track_id: str) -> bool:
    with db_session() as session:
        row = session.execute(
            text("SELECT 1 FROM tracks WHERE CAST(id AS TEXT) = :id"),
            {"id": track_id},
        ).first()
    return row is not None


def test_the_old_file_and_row_are_removed_after_replacement(tmp_path, monkeypatch):
    old_file = tmp_path / "old.mp3"
    new_file = tmp_path / "new.mp3"
    old_file.write_bytes(b"old")
    new_file.write_bytes(b"new")
    _seed_track("old-1", str(old_file))
    monkeypatch.setattr(dcs, "_music_delete_roots", lambda: [str(tmp_path)])

    dcs._delete_replaced_track(
        {"id": 10, "replace_track_id": "old-1"},
        str(new_file),
    )

    assert not old_file.exists()
    assert new_file.exists()
    assert not _track_exists("old-1")


def test_the_imported_file_is_never_deleted_when_it_reused_the_path(
    tmp_path, monkeypatch
):
    shared = tmp_path / "same.mp3"
    shared.write_bytes(b"audio")
    _seed_track("old-1", str(shared))
    monkeypatch.setattr(dcs, "_music_delete_roots", lambda: [str(tmp_path)])

    dcs._delete_replaced_track(
        {"id": 10, "replace_track_id": "old-1"},
        str(shared),
    )

    assert shared.exists()
    assert _track_exists("old-1")


def test_an_adopted_row_is_kept_but_the_old_file_is_still_removed(
    tmp_path, monkeypatch
):
    """The import can adopt the old row by recording MBID and repoint its
    file_path to the new file. The old file must still go; the row is the live
    track now and must stay."""
    old_file = tmp_path / "old.mp3"
    new_file = tmp_path / "new.mp3"
    old_file.write_bytes(b"old")
    new_file.write_bytes(b"new")
    # The row was adopted: its CURRENT path is the new file.
    _seed_track("old-1", str(new_file))
    monkeypatch.setattr(dcs, "_music_delete_roots", lambda: [str(tmp_path)])

    dcs._delete_replaced_track(
        {"id": 10, "replace_track_id": "old-1"},
        str(new_file),
        replace_path=str(old_file),
    )

    assert not old_file.exists()
    assert new_file.exists()
    assert _track_exists("old-1"), "the adopted row is the live track"


def test_a_file_outside_the_music_roots_is_not_deleted(tmp_path, monkeypatch):
    old_file = tmp_path / "old.mp3"
    old_file.write_bytes(b"old")
    _seed_track("old-1", str(old_file))
    monkeypatch.setattr(dcs, "_music_delete_roots", lambda: [str(tmp_path / "other")])

    dcs._delete_replaced_track(
        {"id": 10, "replace_track_id": "old-1"},
        str(tmp_path / "new.mp3"),
    )

    assert old_file.exists()
    assert _track_exists("old-1")


def test_queue_add_persists_the_replacement_target():
    from services.queue.queue_processing_service import queue_add
    from tests.queue_schema_helpers import ensure_queue_table

    ensure_queue_table()
    result = queue_add({
        "artist": "Replacement Artist",
        "title": "Replacement Song",
        "album": "Replacement Album",
        "replace_track_id": "old-1",
    })

    assert result.get("success") is True, result
    assert result.get("inserted") is True, result
    item = result.get("item") or {}
    assert item.get("replace_track_id") == "old-1"

    if item.get("id") is not None:
        with db_session() as session:
            session.execute(
                text("DELETE FROM download_queue WHERE id = :id"),
                {"id": item["id"]},
            )


def test_the_import_replaces_a_same_path_old_version(tmp_path, monkeypatch):
    """The new version frequently resolves to the OLD file's exact path.

    The old duplicate check treated that as "already imported" and left the
    wrong version in place. It must back the old file up and replace it.
    """
    from services.downloads import download_organize_helpers as doh

    old_file = tmp_path / "Artist" / "Album" / "01 - Song.mp3"
    old_file.parent.mkdir(parents=True)
    old_file.write_bytes(b"old-version")
    new_file = tmp_path / "downloads" / "new.mp3"
    new_file.parent.mkdir(parents=True)
    new_file.write_bytes(b"new-version")

    monkeypatch.setattr(doh, "_build_target_path", lambda *a, **k: str(old_file))
    monkeypatch.setattr(
        doh, "_read_download_conversion_settings", lambda: {"enabled": False}
    )

    result = doh.move_track_to_library(
        {
            "file_path": str(new_file),
            "artist": "Artist",
            "title": "Song",
            "track_number": 1,
        },
        {"album_artist": "Artist", "album": "Album", "year": 1990},
        str(tmp_path),
        replace_path=str(old_file),
    )

    assert result.get("success") is True, result
    assert old_file.read_bytes() == b"new-version"
    assert not new_file.exists()
    assert not list(old_file.parent.glob("*.replace-*"))


def test_a_failed_replacement_restores_the_old_version(tmp_path, monkeypatch):
    from services.downloads import download_organize_helpers as doh

    old_file = tmp_path / "Artist" / "Album" / "01 - Song.mp3"
    old_file.parent.mkdir(parents=True)
    old_file.write_bytes(b"old-version")
    new_file = tmp_path / "downloads" / "new.mp3"
    new_file.parent.mkdir(parents=True)
    new_file.write_bytes(b"new-version")

    monkeypatch.setattr(doh, "_build_target_path", lambda *a, **k: str(old_file))
    monkeypatch.setattr(
        doh, "_read_download_conversion_settings", lambda: {"enabled": False}
    )

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(doh.shutil, "move", _boom)

    result = doh.move_track_to_library(
        {
            "file_path": str(new_file),
            "artist": "Artist",
            "title": "Song",
            "track_number": 1,
        },
        {"album_artist": "Artist", "album": "Album", "year": 1990},
        str(tmp_path),
        replace_path=str(old_file),
    )

    assert result.get("success") is False
    assert old_file.read_bytes() == b"old-version", "the old version was lost"
    assert not list(old_file.parent.glob("*.replace-*"))


def test_an_ordinary_download_does_nothing(tmp_path, monkeypatch):
    old_file = tmp_path / "old.mp3"
    old_file.write_bytes(b"old")
    _seed_track("old-1", str(old_file))
    monkeypatch.setattr(dcs, "_music_delete_roots", lambda: [str(tmp_path)])

    dcs._delete_replaced_track({"id": 10}, str(tmp_path / "new.mp3"))

    assert old_file.exists()
    assert _track_exists("old-1")
