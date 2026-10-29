"""``updatePlaylist`` retries Navidrome's transient SQLite "database is locked".

REPORTED LOG::

    WARNING api_clients.navidrome updatePlaylist songs rejected
        playlist='Nu Metal - Top Tracks' removed=300 added=300
        response={'status': 'failed', ... 'error': {'code': 0,
                  'message': 'Internal Server Error: database is locked'}}

Navidrome's SQLite backend answers a write with "database is locked" (over
HTTP 200) while its own scanner — or another write — holds the lock. The
rejection is transient, and a playlist REPLACE is idempotent, so it is retried
with backoff instead of leaving the playlist stale.
"""

from __future__ import annotations

from api_clients.navidrome import (
    _PLAYLIST_WRITE_ATTEMPTS,
    NavidromeClient,
)


_LOCKED = {
    "status": "failed",
    "error": {"code": 0, "message": "Internal Server Error: database is locked"},
}


class _Response:
    def __init__(self, payload: dict):
        self.status_code = 200
        self._payload = payload
        self.content = b"{}"
        self.text = "{}"

    def raise_for_status(self):
        return None

    def json(self):
        return {"subsonic-response": self._payload}


class _Session:
    """Answers each POST from a queue, remembering how many were made."""

    def __init__(self, payloads: list[dict]):
        self._payloads = list(payloads)
        self.posted: list[dict] = []

    def post(self, url, data=None, **kwargs):
        self.posted.append(dict(data or {}))
        payload = self._payloads.pop(0) if self._payloads else {"status": "ok"}
        return _Response(payload)

    def get(self, url, params=None, **kwargs):  # pragma: no cover - must not GET
        raise AssertionError("update_playlist_songs must not issue a GET here")


class _Client(NavidromeClient):
    def __init__(self, session: _Session):
        super().__init__(base_url="http://navidrome:4533", username="u", password="p")
        self.session = session


def _no_sleep(monkeypatch) -> list[float]:
    import api_clients.navidrome as nav

    waits: list[float] = []
    monkeypatch.setattr(nav.time, "sleep", lambda seconds: waits.append(seconds))
    return waits


class TestDatabaseLockIsRetried:
    def test_a_locked_write_is_retried_until_it_succeeds(self, monkeypatch):
        waits = _no_sleep(monkeypatch)
        session = _Session([_LOCKED, _LOCKED, {"status": "ok"}])
        client = _Client(session)

        ok = client.update_playlist_songs("pl-1", ["a", "b"], current_count=0)

        assert ok is True
        assert len(session.posted) == 3
        assert waits == [0.5, 1.0], (
            "retries must back off exponentially, not hammer the locked server"
        )

    def test_it_gives_up_after_the_attempt_limit(self, monkeypatch):
        _no_sleep(monkeypatch)
        session = _Session([_LOCKED] * _PLAYLIST_WRITE_ATTEMPTS)
        client = _Client(session)

        ok = client.update_playlist_songs("pl-1", ["a"], current_count=0)

        assert ok is False
        assert len(session.posted) == _PLAYLIST_WRITE_ATTEMPTS

    def test_a_non_lock_failure_is_not_retried(self, monkeypatch):
        _no_sleep(monkeypatch)
        denied = {"status": "failed", "error": {"code": 50, "message": "Not authorized"}}
        session = _Session([denied, {"status": "ok"}])
        client = _Client(session)

        ok = client.update_playlist_songs("pl-1", ["a"], current_count=0)

        assert ok is False
        assert len(session.posted) == 1, (
            "only the transient database lock is idempotent-safe to retry; an "
            "authorization failure must be reported, not retried"
        )

    def test_a_successful_first_write_makes_one_request(self, monkeypatch):
        _no_sleep(monkeypatch)
        session = _Session([{"status": "ok"}])
        client = _Client(session)

        assert client.update_playlist_songs("pl-1", ["a"], current_count=0) is True
        assert len(session.posted) == 1
