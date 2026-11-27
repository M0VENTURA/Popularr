"""Exportify "import as album" and the safe DB cleanup.

Covers the requested behaviour:
  * a CSV queued as ONE new album, under a single artist or Various Artists
  * the "do not search album release" intent carried on the queue rows
  * the safe cleanup of orphaned album art and old queue tombstones
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from db.engine import db_session
from routes import playlists as playlists_route
from services.maintenance import db_cleanup_service
from tests.queue_schema_helpers import ensure_queue_table


def _track(artist, title, *, album_artist="", album="", duration=200, year="2026"):
    return {
        "artist": artist,
        "title": title,
        "album_artist": album_artist or artist,
        "album": album,
        "duration_s": duration,
        "year": year,
    }


@pytest.fixture
def captured(monkeypatch):
    """Capture the items handed to ``queue_add_batch``."""
    calls = {"items": None, "payload": None}

    def _fake(payload):
        calls["payload"] = payload
        calls["items"] = payload.get("items") or []
        return {
            "success": True,
            "added": len(calls["items"]),
            "skipped": 0,
            "failed": 0,
        }

    import services.queue.queue_processing_service as qps

    monkeypatch.setattr(qps, "queue_add_batch", _fake)
    return calls


class TestImportAsAlbum:
    def test_a_single_artist_csv_becomes_that_artists_album(self, captured):
        result = playlists_route._queue_csv_tracks_as_album(
            [_track("INXS", "Never Tear Us Apart"), _track("INXS", "New Sensation")],
            "Kick",
        )
        assert result["album"] == "Kick"
        assert result["album_artist"] == "INXS"
        assert result["is_compilation"] is False
        assert result["queued"] == 2
        assert {i["album_artist"] for i in captured["items"]} == {"INXS"}

    def test_mixed_artists_become_various_artists(self, captured):
        result = playlists_route._queue_csv_tracks_as_album(
            [_track("INXS", "Never Tear Us Apart"), _track("AC/DC", "Thunderstruck")],
            "Triple J Hottest 100",
        )
        assert result["album_artist"] == "Various Artists"
        assert result["is_compilation"] is True
        assert {i["album_artist"] for i in captured["items"]} == {"Various Artists"}

    def test_each_row_keeps_its_own_performer(self, captured):
        playlists_route._queue_csv_tracks_as_album(
            [_track("INXS", "Never Tear Us Apart"), _track("AC/DC", "Thunderstruck")],
            "Mix",
        )
        performers = {i["title"]: i["artist"] for i in captured["items"]}
        assert performers["Never Tear Us Apart"] == "INXS"
        assert performers["Thunderstruck"] == "AC/DC"

    def test_every_row_forces_the_one_album(self, captured):
        playlists_route._queue_csv_tracks_as_album(
            [_track("INXS", "A", album="Kick"), _track("AC/DC", "B", album="Back in Black")],
            "Mix",
        )
        assert {i["album"] for i in captured["items"]} == {"Mix"}

    def test_rows_are_songs_not_release_requests(self, captured):
        """``import_type='album'`` would make the pipeline try to expand it."""
        playlists_route._queue_csv_tracks_as_album([_track("INXS", "A")], "Kick")
        assert {i["import_type"] for i in captured["items"]} == {"song"}

    def test_the_skip_lookup_intent_is_carried(self, captured):
        """'Do not search album release' is applied once tracks exist."""
        playlists_route._queue_csv_tracks_as_album([_track("INXS", "A")], "Kick")
        for item in captured["items"]:
            assert item["metadata"]["album_lookup_skip"] is True

    def test_a_generic_album_artist_is_various_artists(self, captured):
        result = playlists_route._queue_csv_tracks_as_album(
            [_track("Someone", "A", album_artist="Various Artists")], "Comp"
        )
        assert result["album_artist"] == "Various Artists"
        assert result["is_compilation"] is True

    def test_rows_are_grouped_under_one_import_group(self, captured):
        playlists_route._queue_csv_tracks_as_album(
            [_track("INXS", "A"), _track("INXS", "B")], "Kick"
        )
        assert len({i["import_group"] for i in captured["items"]}) == 1
        assert captured["payload"]["import_group"]


class TestAlbumRoute:
    @pytest.mark.asyncio
    async def test_mode_album_queues_the_csv_as_one_album(self, client, captured):
        import io
        from werkzeug.datastructures import FileStorage

        csv = (
            "Track Name,Artist Name(s),Album Name,Duration (ms)\n"
            "Never Tear Us Apart,INXS,Kick,184586\n"
            "New Sensation,INXS,Kick,220000\n"
        )
        upload = FileStorage(
            stream=io.BytesIO(csv.encode("utf-8")),
            filename="x.csv",
            content_type="text/csv",
        )
        resp = await client.post(
            "/api/playlist/import/csv",
            files={"file": upload},
            form={"playlist_name": "Kick", "mode": "album"},
        )
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["mode"] == "album"
        assert body["album"] == "Kick"
        assert body["album_artist"] == "INXS"
        assert body["queued"] == 2
        # Duration was parsed from the CSV and carried into the queue item.
        assert any(i.get("duration") == 185 for i in captured["items"])


# ---------------------------------------------------------------------------
# Safe DB cleanup
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class TestSafeCleanup:
    @pytest.fixture()
    def seeded(self):
        import uuid

        ensure_queue_table()
        live_album = f"Kick-{uuid.uuid4().hex[:8]}"
        gone_album = f"Gone-{uuid.uuid4().hex[:8]}"
        track_id = f"t-{uuid.uuid4().hex[:8]}"
        old = _iso(datetime.now(timezone.utc) - timedelta(days=90))
        with db_session() as session:
            # Isolate: this shared in-memory DB carries rows from earlier tests.
            session.execute(text("DELETE FROM album_art"))
            session.execute(text("DELETE FROM download_queue"))
            # A live album (tracks row) vs an orphan art row.
            session.execute(
                text("INSERT INTO tracks (id, artist, album_artist, album, file_path) "
                     "VALUES (:id, 'INXS', 'INXS', :album, '/music/kick.mp3')"),
                {"id": track_id, "album": live_album},
            )
            session.execute(
                text("INSERT INTO album_art (artist_name, album_name) VALUES ('INXS', :a)"),
                {"a": live_album},
            )
            session.execute(
                text("INSERT INTO album_art (artist_name, album_name) VALUES ('Gone', :a)"),
                {"a": gone_album},
            )
            # A tombstone (old) and an imported row (must survive).
            session.execute(
                text("INSERT INTO download_queue (artist, title, album, status, updated_at) "
                     "VALUES ('INXS', 'A', :a, 'deleted', :old)"),
                {"a": live_album, "old": old},
            )
            session.execute(
                text("INSERT INTO download_queue (artist, title, album, status, updated_at) "
                     "VALUES ('INXS', 'B', :a, 'imported', :old)"),
                {"a": live_album, "old": old},
            )
            session.commit()
        yield live_album

    def test_preview_counts_without_writing(self, seeded):
        preview = db_cleanup_service.preview_cleanup()
        assert preview["success"] is True
        assert preview["orphan_album_art"] == 1
        assert preview["queue_tombstones"] == 1
        # Nothing removed yet.
        with db_session() as session:
            assert session.execute(text("SELECT COUNT(*) FROM album_art")).scalar() == 2

    def test_run_removes_orphans_and_tombstones_only(self, seeded):
        live_album = seeded
        result = db_cleanup_service.run_cleanup()
        assert result["success"] is True
        assert result["removed"]["orphan_album_art"] == 1
        assert result["removed"]["queue_tombstones"] == 1
        with db_session() as session:
            art = {r[0] for r in session.execute(
                text("SELECT album_name FROM album_art")
            ).fetchall()}
            assert art == {live_album}, "the live album's art must survive"
            statuses = {r[0] for r in session.execute(
                text("SELECT status FROM download_queue")
            ).fetchall()}
            assert statuses == {"imported"}, "imported rows must never be deleted"

    def test_imported_rows_are_never_touched(self, seeded):
        db_cleanup_service.run_cleanup(tombstone_days=0)
        with db_session() as session:
            n = session.execute(
                text("SELECT COUNT(*) FROM download_queue WHERE status = 'imported'")
            ).scalar()
        assert n == 1


class TestCleanupRoute:
    @pytest.mark.asyncio
    async def test_preview_is_a_dry_run(self, client):
        ensure_queue_table()
        resp = await client.get("/api/maintenance/cleanup")
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["success"] is True
        assert data["dry_run"] is True
        assert "orphan_album_art" in data and "queue_tombstones" in data

    @pytest.mark.asyncio
    async def test_run_returns_what_was_removed(self, client):
        ensure_queue_table()
        resp = await client.post(
            "/api/maintenance/cleanup", json={"dry_run": False}
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["success"] is True
        assert data["dry_run"] is False
        assert "orphan_album_art" in data["removed"]
