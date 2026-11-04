"""Subsonic code 70 on ``setRating`` is a STALE SONG ID, not an outage.

Reported log:

    Navidrome rejected setRating track_id='CPE3bqK58XDXEwMxvNjonh' rating=4
        code=70 message='data not found' username='moventura'
    ...
    Aborting Navidrome rating sync after consecutive failures — Navidrome unreachable? failures=3
    Navidrome rating syncs failed — check credentials

The server is healthy. Code 70 means the song id no longer exists in Navidrome
(a renamed/moved file, or Navidrome re-encoded ids on 0.64). Counting it as an
outage aborted the artist's whole sync after three tracks and blamed
credentials. A stale id must be skipped, not counted, so the rest of the album
still syncs.
"""

from __future__ import annotations

from api_clients.navidrome import NavidromeClient
from services.navidrome.rating_sync_service import rating_status_for_track
from services.popularity.stages import finalise_stage as fs


class _Client:
    """A rating client returning a fixed status."""

    def __init__(self, status: str) -> None:
        self.status = status

    def set_rating_status(self, track_id: str, stars: int) -> str:
        return self.status


def _nav_client(monkeypatch, envelope: dict) -> NavidromeClient:
    client = NavidromeClient(base_url="http://nav", username="u", password="p")
    monkeypatch.setattr(
        client, "_get_subsonic_response", lambda *a, **k: envelope
    )
    return client


class TestTheClientClassifiesTheOutcome:
    def test_code_70_is_not_found(self, monkeypatch):
        client = _nav_client(
            monkeypatch,
            {"status": "failed", "error": {"code": 70, "message": "data not found"}},
        )

        assert client.set_rating_status("stale", 4) == "not_found"
        assert client.set_rating("stale", 4) is False

    def test_ok(self, monkeypatch):
        client = _nav_client(monkeypatch, {"status": "ok"})

        assert client.set_rating_status("t", 4) == "ok"
        assert client.set_rating("t", 4) is True

    def test_auth_is_distinct_from_not_found(self, monkeypatch):
        client = _nav_client(
            monkeypatch,
            {"status": "failed", "error": {"code": 40, "message": "wrong credentials"}},
        )

        assert client.set_rating_status("t", 4) == "auth"


class TestAggregationAcrossUsers:
    def test_all_stale_is_not_found(self):
        assert (
            rating_status_for_track(
                [_Client("not_found"), _Client("not_found")], "t", 4
            )
            == "not_found"
        )

    def test_one_success_wins(self):
        assert (
            rating_status_for_track([_Client("not_found"), _Client("ok")], "t", 4)
            == "ok"
        )

    def test_a_real_failure_wins_over_stale(self):
        assert (
            rating_status_for_track([_Client("not_found"), _Client("auth")], "t", 4)
            == "failed"
        )


class TestTheAlbumSyncSeesAStaleId:
    def test_a_stale_id_is_reported_separately(self):
        assert (
            fs._sync_rating_to_navidrome("t", 4, clients=[_Client("not_found")])
            == "not_found"
        )

    def test_a_real_failure_is_still_a_failure(self):
        assert (
            fs._sync_rating_to_navidrome("t", 4, clients=[_Client("failed")])
            == "failed"
        )
