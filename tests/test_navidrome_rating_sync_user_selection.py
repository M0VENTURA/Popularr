"""Rating sync must target a USABLE Navidrome account.

REPORTED (side note)
--------------------
Every ``setRating`` aborted after three consecutive failures while playlist
writes to the same server succeeded — "Navidrome unreachable?" was wrong;
Navidrome was reachable and the playlist path was using ``moventura``.

WHY
---
``get_rating_sync_clients`` sliced ``users[:1]`` BEFORE checking the entry.
A setup-wizard stub (or any row with an empty user/password) sitting first
became the rating target: the client was built with no credentials, so every
``setRating`` was rejected. The playlist paths iterate and SKIP unusable rows,
which is why they reached the real account.

A row without ``base_url``/``user``/``pass`` is not an account and must never
be selected.
"""
from __future__ import annotations

from api_clients.navidrome import NavidromeClient
from services.navidrome import rating_sync_service as rss


STUB = {"base_url": "http://192.168.1.200:4533", "user": "", "pass": ""}
REAL = {"base_url": "http://192.168.1.200:4533", "user": "moventura", "pass": "secret"}
SECOND = {"base_url": "http://192.168.1.200:4533", "user": "listener", "pass": "secret2"}


def _patch(monkeypatch, users, *, all_users=False):
    monkeypatch.setattr(rss, "load_navidrome_users_from_config", lambda: list(users))
    monkeypatch.setattr(
        rss, "is_sync_ratings_to_all_users_enabled", lambda: all_users,
    )


class TestThePrimaryTarget:
    def test_a_credential_less_first_row_is_skipped(self, monkeypatch):
        _patch(monkeypatch, [STUB, REAL])

        clients = rss.get_rating_sync_clients()

        assert len(clients) == 1, (
            "the empty stub became the rating target, so every setRating "
            "carried no credentials"
        )
        assert clients[0].username == "moventura"

    def test_a_stub_after_the_primary_changes_nothing(self, monkeypatch):
        _patch(monkeypatch, [REAL, STUB])
        clients = rss.get_rating_sync_clients()
        assert [c.username for c in clients] == ["moventura"]


class TestAllUsersMode:
    def test_usable_accounts_are_all_kept(self, monkeypatch):
        _patch(monkeypatch, [STUB, REAL, SECOND], all_users=True)
        clients = rss.get_rating_sync_clients()
        assert [c.username for c in clients] == ["moventura", "listener"]

    def test_only_stubs_yields_no_clients(self, monkeypatch):
        _patch(monkeypatch, [STUB], all_users=True)
        assert rss.get_rating_sync_clients() == []


class TestNoConfiguration:
    def test_empty_config_yields_no_clients(self, monkeypatch):
        _patch(monkeypatch, [])
        assert rss.get_rating_sync_clients() == []


def test_the_clients_point_at_the_configured_server(monkeypatch):
    _patch(monkeypatch, [STUB, REAL])
    client = rss.get_rating_sync_clients()[0]
    assert isinstance(client, NavidromeClient)
    assert client.base_url == "http://192.168.1.200:4533"
    assert client.password == "secret"


class TestSetRatingReportsTheRealRejection:
    @staticmethod
    def _client(response):
        client = NavidromeClient("http://192.168.1.200:4533", "moventura", "secret")
        client._get_subsonic_response = lambda endpoint, **kwargs: response
        return client

    def test_a_failed_status_is_false(self):
        client = self._client({
            "status": "failed",
            "error": {"code": 70, "message": "The requested data was not found"},
        })
        assert client.set_rating("stale-id", 4) is False

    def test_a_successful_status_is_true(self):
        client = self._client({"status": "ok"})
        assert client.set_rating("song-1", 4) is True
