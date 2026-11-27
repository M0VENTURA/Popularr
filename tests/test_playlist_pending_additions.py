"""Auto-adding imported tracks to the playlist they were queued for.

"Download Missing" stores ``metadata.playlist_target`` on each queue row; a
maintenance sweep adds the track once it has landed and Navidrome has adopted a
real song id. These tests pin the append helper (never replaces the list) and the
sweep (resolves the real id, is idempotent, retries while the id is provisional).
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

import services.playlists.playlist_pending_additions_service as sweep
import services.playlists.playlist_navidrome_service as nav_playlists
from db.engine import db_session
from tests.queue_schema_helpers import ensure_queue_table


# ---------------------------------------------------------------------------
# The append helper
# ---------------------------------------------------------------------------

class _FakeClient:
    def __init__(self, playlist, current):
        self._playlist = playlist
        self._current = current
        self.seen_pid = None

    def find_playlist_by_name(self, name):
        return self._playlist

    def get_playlist_song_ids(self, pid):
        self.seen_pid = pid
        return self._current


class TestAddTrackToPlaylistByName:
    def test_missing_playlist_is_not_created(self):
        result = nav_playlists.add_track_to_playlist_by_name(
            _FakeClient(None, []), "Mix", "song-1"
        )
        assert result["success"] is False
        assert result["reason"] == "not_found"

    def test_an_existing_song_is_not_duplicated(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(
            nav_playlists, "sync_playlist_by_name",
            lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {"success": True},
        )
        result = nav_playlists.add_track_to_playlist_by_name(
            _FakeClient({"id": "p1"}, ["song-1"]), "Mix", "song-1"
        )
        assert result["success"] is True
        assert result["changed"] is False
        assert called["n"] == 0, "must not rewrite a playlist that already has the song"

    def test_a_new_song_is_appended_not_replaced(self, monkeypatch):
        captured = {}

        def _sync(client, name, ids, **kw):
            captured["ids"] = list(ids)
            return {"success": True, "updated": True}

        monkeypatch.setattr(nav_playlists, "sync_playlist_by_name", _sync)
        result = nav_playlists.add_track_to_playlist_by_name(
            _FakeClient({"id": "p1"}, ["song-a", "song-b"]), "Mix", "song-c"
        )
        assert result == {"success": True, "changed": True, "reason": "added"}
        # ⚠️ The FULL current list plus the new id — never just [new_id], which
        # would replace the playlist down to one track.
        assert captured["ids"] == ["song-a", "song-b", "song-c"]

    def test_deferred_write_is_reported(self, monkeypatch):
        monkeypatch.setattr(
            nav_playlists, "sync_playlist_by_name",
            lambda *a, **k: {"success": True, "skipped": True},
        )
        result = nav_playlists.add_track_to_playlist_by_name(
            _FakeClient({"id": "p1"}, []), "Mix", "song-1"
        )
        assert result["success"] is False
        assert result["reason"] == "import_running"

    def test_unknown_contents_is_not_treated_as_empty(self):
        result = nav_playlists.add_track_to_playlist_by_name(
            _FakeClient({"id": "p1"}, None), "Mix", "song-1"
        )
        assert result["success"] is False
        assert result["reason"] == "unknown_contents"


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

def _seed(queue_meta: dict, *, track_id: str, path: str) -> int:
    ensure_queue_table()
    tag = uuid.uuid4().hex[:8]
    with db_session() as session:
        # Only clear rows carrying a playlist intent, so this fixture cannot
        # disturb other tests' queue rows in the shared in-memory DB.
        session.execute(text(
            "DELETE FROM download_queue "
            "WHERE COALESCE(CAST(metadata AS TEXT), '') LIKE '%playlist_target%'"
        ))
        res = session.execute(
            text(
                "INSERT INTO download_queue "
                "(artist, title, album, status, import_type, music_file_path, metadata, updated_at) "
                "VALUES ('A', 'Song', 'Album', 'imported', 'song', :path, :meta, CURRENT_TIMESTAMP) "
                "RETURNING id"
            ),
            {"path": path, "meta": json.dumps(queue_meta)},
        )
        queue_id = int(res.scalar())
        session.execute(
            text("INSERT INTO tracks (id, artist, album_artist, album, file_path) "
                 "VALUES (:id, 'A', 'A', 'Album', :path)"),
            {"id": f"{track_id}-{tag}", "path": path},
        )
        session.commit()
    return queue_id


def _queue_meta(queue_id: int) -> dict:
    with db_session() as session:
        raw = session.execute(
            text("SELECT metadata FROM download_queue WHERE id = :id"), {"id": queue_id}
        ).scalar()
    return json.loads(raw) if isinstance(raw, str) else (raw or {})


class TestSweep:
    @pytest.fixture
    def no_import(self, monkeypatch):
        import services.navidrome.import_gate as gate

        monkeypatch.setattr(gate, "is_import_running", lambda *a, **k: False)

    def test_a_landed_track_is_added_and_the_intent_cleared(self, no_import, monkeypatch):
        calls = []
        monkeypatch.setattr(sweep, "_client_for", lambda user: object())
        monkeypatch.setattr(
            nav_playlists, "add_track_to_playlist_by_name",
            lambda client, name, tid: calls.append((name, tid)) or {
                "success": True, "changed": True, "reason": "added"
            },
        )
        qid = _seed(
            {"playlist_target": "Mix", "playlist_user": "u"}, track_id="nav", path="/music/a.mp3"
        )

        result = sweep.apply_pending_playlist_additions()

        assert result["added"] == 1
        assert len(calls) == 1 and calls[0][0] == "Mix"
        assert "playlist_target" not in _queue_meta(qid), "the intent must be cleared"

    def test_a_provisional_id_keeps_the_intent(self, no_import, monkeypatch):
        calls = []
        monkeypatch.setattr(sweep, "_client_for", lambda user: object())
        monkeypatch.setattr(
            nav_playlists, "add_track_to_playlist_by_name",
            lambda *a, **k: calls.append(1) or {"success": True, "changed": True},
        )
        qid = _seed(
            {"playlist_target": "Mix"} , track_id="dlimport", path="/music/b.mp3"
        )

        result = sweep.apply_pending_playlist_additions()

        assert result["added"] == 0
        assert result["pending"] == 1
        assert calls == [], "a dlimport- placeholder is not a valid song id"
        assert _queue_meta(qid).get("playlist_target") == "Mix", "retry later"

    def test_it_is_skipped_while_a_navidrome_import_runs(self, monkeypatch):
        import services.navidrome.import_gate as gate

        monkeypatch.setattr(gate, "is_import_running", lambda *a, **k: True)
        _seed({"playlist_target": "Mix"}, track_id="nav", path="/music/c.mp3")
        result = sweep.apply_pending_playlist_additions()
        assert result["skipped"] is True
        assert result["added"] == 0

    def test_a_deferred_write_keeps_the_intent(self, no_import, monkeypatch):
        monkeypatch.setattr(sweep, "_client_for", lambda user: object())
        monkeypatch.setattr(
            nav_playlists, "add_track_to_playlist_by_name",
            lambda *a, **k: {"success": False, "reason": "not_found"},
        )
        qid = _seed({"playlist_target": "Not Created Yet"}, track_id="nav", path="/music/d.mp3")
        result = sweep.apply_pending_playlist_additions()
        assert result["added"] == 0
        assert result["pending"] == 1
        assert _queue_meta(qid).get("playlist_target") == "Not Created Yet"
