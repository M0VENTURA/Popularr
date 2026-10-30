"""A queued track owned on another album is re-imported, not downloaded.

REQUEST: *"if a track exists in the collection on another album, that it copies
that track to the downloads folder, then reimports it into the collection
rather than going through soulseek? But that track still needs to obey the
quality and track length match"*

``try_library_copy`` copies the file into the downloads folder and marks the
queue row ``downloading`` with ``found_filename``/``file_path``, so the existing
completion hook moves and tags it for the target album. It returns ``None`` when
no copy qualifies, which is what keeps the Soulseek search as the fallback.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import text

from db.engine import db_session
from queue_schema_helpers import ensure_queue_table
from services.downloads import library_copy_service as lcs
from services.queue import queue_processing_service as qps

_ALBUM = "2002 - MTV2 Handpicked"
_SOURCE_ALBUM = "Parachutes"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    ensure_queue_table()
    monkeypatch.setenv("DOWNLOADS_DIR", str(tmp_path / "downloads"))

    def _wipe() -> None:
        with db_session() as session:
            session.execute(text("DELETE FROM tracks"))
            session.execute(text("DELETE FROM download_queue"))

    _wipe()
    yield
    _wipe()


def _seed_track(
    tmp_path: Path,
    *,
    album: str = _SOURCE_ALBUM,
    artist: str = "Coldplay",
    album_artist: str = "Various Artists",
    title: str = "Yellow",
    duration: float = 240.0,
    extension: str = ".mp3",
) -> str:
    folder = tmp_path / "library" / album
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"05. {artist} - {title}{extension}"
    path.write_bytes(b"not real audio")
    with db_session() as session:
        session.execute(
            text(
                "INSERT INTO tracks (id, title, artist, album, album_artist, "
                "track_number, disc_number, file_path, duration) "
                "VALUES (:id, :title, :artist, :album, :album_artist, '5', '1', "
                ":file_path, :duration)"
            ),
            {
                "id": f"lib-{title}-{album}",
                "title": title,
                "artist": artist,
                "album": album,
                "album_artist": album_artist,
                "file_path": str(path),
                "duration": duration,
            },
        )
    return str(path)


def _item(**overrides) -> dict:
    item = {
        "id": 5,
        "artist": "Coldplay",
        "title": "Yellow",
        "album": _ALBUM,
        "album_artist": "Various Artists",
        "track_number": "5",
        "duration": 240,
        "source": "musicbrainz",
        "status": "searching",
    }
    item.update(overrides)
    return item


def _queue_row(queue_id: int = 5) -> dict:
    with db_session() as session:
        row = session.execute(
            text("SELECT * FROM download_queue WHERE id = :id"), {"id": queue_id},
        ).fetchone()
    return dict(row._mapping) if row else {}


def _insert_queue_row(item: dict) -> None:
    with db_session() as session:
        session.execute(
            text(
                "INSERT INTO download_queue (id, title, artist, album, "
                "album_artist, track_number, duration, source, status) "
                "VALUES (:id, :title, :artist, :album, :album_artist, :tn, "
                ":duration, :source, :status)"
            ),
            {
                "id": item["id"],
                "title": item["title"],
                "artist": item["artist"],
                "album": item["album"],
                "album_artist": item["album_artist"],
                "tn": item["track_number"],
                "duration": item["duration"],
                "source": item["source"],
                "status": item["status"],
            },
        )


class TestLibraryCopy:
    def test_copies_a_track_owned_on_another_album(self, tmp_path):
        source = _seed_track(tmp_path)
        item = _item()
        _insert_queue_row(item)

        result = lcs.try_library_copy(item)

        assert result is not None
        destination = result["file_path"]
        assert os.path.isfile(destination)
        assert Path(destination).read_bytes() == Path(source).read_bytes()
        assert os.path.isfile(source), "the library copy must be COPIED, not moved"
        assert destination != source

        row = _queue_row()
        assert row["status"] == "downloading"
        assert row["file_path"] == destination
        assert row["found_filename"]

    def test_the_copied_filename_identifies_the_target_track(self, tmp_path):
        _seed_track(tmp_path)
        item = _item()
        _insert_queue_row(item)

        result = lcs.try_library_copy(item)

        name = os.path.basename(result["file_path"])
        assert name.startswith("5. Coldplay - Yellow")
        assert name.endswith(".mp3")

    def test_a_track_already_on_the_target_album_is_not_copied(self, tmp_path):
        _seed_track(tmp_path, album=_ALBUM)
        item = _item()
        _insert_queue_row(item)

        assert lcs.try_library_copy(item) is None
        assert _queue_row()["status"] == "searching"

    def test_an_unknown_track_is_not_copied(self, tmp_path):
        _seed_track(tmp_path)
        item = _item(title="Trouble")
        _insert_queue_row(item)

        assert lcs.try_library_copy(item) is None

    def test_a_different_artist_is_not_copied(self, tmp_path):
        _seed_track(tmp_path)
        item = _item(artist="Oasis")
        _insert_queue_row(item)

        assert lcs.try_library_copy(item) is None

    def test_a_local_source_is_not_copied(self, tmp_path):
        _seed_track(tmp_path)
        item = _item(source="local")
        _insert_queue_row(item)

        assert lcs.try_library_copy(item) is None


class TestGates:
    def test_quality_format_gate_rejects_the_copy(self, tmp_path, monkeypatch):
        _seed_track(tmp_path, extension=".mp3")
        item = _item()
        _insert_queue_row(item)
        monkeypatch.setattr(
            "helpers.config_helpers.get_search_quality_config",
            lambda: {"allowed_formats": {"flac"}},
        )

        assert lcs.try_library_copy(item) is None
        assert _queue_row()["status"] == "searching"

    def test_length_mismatch_rejects_the_copy(self, tmp_path):
        _seed_track(tmp_path, duration=300.0)
        item = _item(duration=240)  # 60s difference
        _insert_queue_row(item)

        assert lcs.try_library_copy(item) is None
        assert _queue_row()["status"] == "searching"

    def test_length_within_tolerance_is_copied(self, tmp_path):
        _seed_track(tmp_path, duration=242.0)
        item = _item(duration=240)
        _insert_queue_row(item)

        assert lcs.try_library_copy(item) is not None

    def test_an_expected_length_with_no_file_duration_is_rejected(self, tmp_path):
        _seed_track(tmp_path, duration=None)
        item = _item(duration=240)
        _insert_queue_row(item)

        assert lcs.try_library_copy(item) is None

    def test_no_expected_length_is_not_copied(self, tmp_path):
        """The length is part of the identity — without one, don't guess."""
        _seed_track(tmp_path)
        item = _item(duration=None)
        _insert_queue_row(item)

        assert lcs.try_library_copy(item) is None

    def test_a_live_album_does_not_satisfy_a_studio_queue_row(self, tmp_path):
        _seed_track(tmp_path, album="Live at Wembley")
        item = _item(album="Parachutes")
        _insert_queue_row(item)

        assert lcs.try_library_copy(item) is None

    def test_metadata_rejection_skips_the_copy(self, tmp_path, monkeypatch):
        _seed_track(tmp_path)
        item = _item()
        _insert_queue_row(item)
        # The filename matcher would accept it; force the metadata path to say no.
        import services.downloads.download_completion_service as dcs

        monkeypatch.setattr(
            dcs, "_file_matches_queue_item",
            lambda *a, **k: (False, "metadata"),
        )

        assert lcs.try_library_copy(item) is None


class TestProcessorShortCircuit:
    def test_the_copy_runs_before_requiring_soulseek(self, tmp_path, monkeypatch):
        _seed_track(tmp_path)
        item = _item()
        _insert_queue_row(item)

        called: list[str] = []
        monkeypatch.setattr(
            qps, "get_slskd_client",
            lambda: called.append("slskd") or None,
        )

        result = qps.process_queue_item(dict(item))

        assert result["success"] is True
        assert result["source"] == "library_copy"
        assert called == [], "Soulseek must not be required for a library copy"
        assert _queue_row()["status"] == "downloading"

    def test_without_a_copy_it_falls_through_to_soulseek(self, tmp_path, monkeypatch):
        item = _item(title="Not In Library")
        _insert_queue_row(item)
        monkeypatch.setattr(qps, "get_slskd_client", lambda: None)
        # ``mark_failed`` uses PostgreSQL interval/power SQL; the fall-through
        # assertion is about the RESULT, not the retry bookkeeping.
        monkeypatch.setattr(qps, "mark_failed", lambda *a, **k: None)

        result = qps.process_queue_item(dict(item))

        assert result["error"] == "soulseek_unavailable"
        assert _queue_row()["status"] != "downloading"
