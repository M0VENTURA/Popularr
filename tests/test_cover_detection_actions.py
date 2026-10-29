"""Manual cover-detection actions on the track, album and artist pages.

``detect_covers_for_album`` already ran inside the scan pipeline, but there was
no way to ask for it on its own: the track page's "Detect" only searches
MusicBrainz for an original artist, and the album/artist menus had no cover
action at all. These tests pin the three scopes (track / album / artist), the
row filtering each one uses, and the endpoints the page menus call.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from db.engine import db_session
from services.enrichment import cover_detection_service as cds


class _FakeDetector:
    """Records the album-context calls the service makes."""

    calls: list[dict] = []

    def __init__(self, db_connection=None):
        self.db_connection = db_connection

    def detect_covers_for_album(self, album, artist, tracks, force=False):
        _FakeDetector.calls.append({
            "album": album,
            "artist": artist,
            "ids": [track["id"] for track in tracks],
            "force": force,
        })
        return [{
            "track_id": tracks[0]["id"],
            "title": tracks[0].get("title") or "",
            "is_cover": True,
            "original_artist": "Original Artist",
            "reason": "test",
        }]


@pytest.fixture(autouse=True)
def _isolated_tracks(monkeypatch):
    _FakeDetector.calls = []
    monkeypatch.setattr(cds, "CoverDetector", _FakeDetector)

    def _wipe() -> None:
        with db_session() as session:
            session.execute(text("DELETE FROM tracks"))

    _wipe()
    yield
    _wipe()


def _seed() -> None:
    rows = [
        ("t1", "Album Artist", "Album One", "Song One"),
        ("t2", "Album Artist", "Album One", "Song Two"),
        ("t3", "Album Artist", "Album Two", "Song Three"),
        ("t4", "Other Artist", "Album One", "Somebody Else"),
    ]
    with db_session() as session:
        for track_id, artist, album, title in rows:
            session.execute(
                text(
                    "INSERT INTO tracks "
                    "(id, artist, album, album_artist, title, file_path, recording_mbid) "
                    "VALUES (:id, :artist, :album, :artist, :title, :path, :mbid)"
                ),
                {
                    "id": track_id,
                    "artist": artist,
                    "album": album,
                    "title": title,
                    "path": f"{artist}/{album}/{title}.flac",
                    "mbid": f"rec-{track_id}",
                },
            )


class TestAlbumScope:
    def test_only_the_requested_album_and_artist_are_checked(self):
        _seed()

        summary = cds.detect_covers_for_album_name("Album One", "Album Artist")

        assert summary["track_count"] == 2
        assert summary["cover_count"] == 1
        assert len(_FakeDetector.calls) == 1
        call = _FakeDetector.calls[0]
        assert call["album"] == "Album One"
        assert call["artist"] == "Album Artist"
        assert call["ids"] == ["t1", "t2"]
        assert call["force"] is True

    def test_an_unknown_album_reports_nothing_without_calling_the_detector(self):
        _seed()

        summary = cds.detect_covers_for_album_name("No Such Album", "Album Artist")

        assert summary["track_count"] == 0
        assert summary["cover_count"] == 0
        assert _FakeDetector.calls == []


class TestTrackScope:
    def test_only_the_requested_track_is_checked_and_the_verdict_is_returned(self):
        _seed()

        verdict = cds.detect_covers_for_track("t2")

        assert verdict == {
            "track_id": "t2",
            "is_cover": True,
            "original_artist": "Original Artist",
            "reason": "test",
        }
        assert len(_FakeDetector.calls) == 1
        assert _FakeDetector.calls[0]["ids"] == ["t2"]
        assert _FakeDetector.calls[0]["force"] is True

    def test_a_missing_track_returns_none(self):
        _seed()

        assert cds.detect_covers_for_track("nope") is None
        assert _FakeDetector.calls == []


class TestArtistScope:
    def test_every_album_of_the_artist_is_checked(self):
        _seed()

        summary = cds.detect_covers_for_artist_report("Album Artist")

        assert summary["track_count"] == 3
        assert summary["cover_count"] == 2
        assert sorted(
            track_id
            for call in _FakeDetector.calls
            for track_id in call["ids"]
        ) == ["t1", "t2", "t3"]

    def test_the_legacy_count_wrapper_still_returns_an_int(self):
        _seed()

        assert cds.detect_covers_for_artist("Album Artist") == 2


class TestEndpoints:
    async def test_track_endpoint_returns_the_verdict(self, client, monkeypatch):
        monkeypatch.setattr(
            cds,
            "detect_covers_for_track",
            lambda track_id, conn=None, force=True: {
                "track_id": track_id,
                "is_cover": True,
                "original_artist": "Original Artist",
                "reason": "test",
            },
        )

        resp = await client.post("/api/track/t1/cover-detection")
        assert resp.status_code == 200
        payload = json.loads(await resp.get_data(as_text=True))
        assert payload["success"] is True
        assert payload["is_cover"] is True
        assert payload["original_artist"] == "Original Artist"

    async def test_track_endpoint_404s_for_an_unknown_track(self, client, monkeypatch):
        monkeypatch.setattr(cds, "detect_covers_for_track", lambda *a, **k: None)

        resp = await client.post("/api/track/nope/cover-detection")
        assert resp.status_code == 404

    async def test_album_endpoint_starts_the_background_pass(self, client, monkeypatch):
        calls: list[tuple] = []

        def _fake(scope, **kwargs):
            calls.append((scope, kwargs))
            return {"started": True}

        monkeypatch.setattr(cds, "start_cover_detection", _fake)

        resp = await client.post(
            "/api/album/cover-detection",
            json={"artist": "Album Artist", "album": "Album One"},
        )
        assert resp.status_code == 200
        payload = json.loads(await resp.get_data(as_text=True))
        assert payload["success"] is True
        assert calls == [(
            "album",
            {"artist": "Album Artist", "album": "Album One", "force": True},
        )]

    async def test_album_endpoint_requires_both_names(self, client):
        resp = await client.post("/api/album/cover-detection", json={"artist": "A"})
        assert resp.status_code == 400

    async def test_artist_endpoint_starts_the_background_pass(self, client, monkeypatch):
        calls: list[tuple] = []

        def _fake(scope, **kwargs):
            calls.append((scope, kwargs))
            return {"started": True}

        monkeypatch.setattr(cds, "start_cover_detection", _fake)

        resp = await client.post(
            "/api/artist/cover-detection", json={"artist": "Album Artist"},
        )
        assert resp.status_code == 200
        assert calls == [("artist", {"artist": "Album Artist", "force": True})]

    async def test_a_second_pass_is_refused_with_409(self, client, monkeypatch):
        monkeypatch.setattr(
            cds,
            "start_cover_detection",
            lambda *a, **k: {"started": False, "error": "Cover detection is already running"},
        )

        resp = await client.post(
            "/api/artist/cover-detection", json={"artist": "Album Artist"},
        )
        assert resp.status_code == 409
