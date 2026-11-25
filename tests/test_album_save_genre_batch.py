"""The album genre fill is ONE statement, not one commit per track.

A 100-track compilation paid 100 sessions/commits inside the save request —
the reported timeout. All filled tracks receive the same list.
"""

from __future__ import annotations

from routes import ui_routes


class _Result:
    rowcount = 3


class _Session:
    def __init__(self):
        self.calls = []

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        return _Result()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_many_tracks_share_one_update(monkeypatch):
    session = _Session()
    from db import engine
    monkeypatch.setattr(engine, "db_session", lambda: session)

    rows, failed = ui_routes._apply_album_track_genres_batch(
        ["1", "2", "3"], "Rock, Pop"
    )

    assert (rows, failed) == (3, 0)
    assert len(session.calls) == 1, "the genre fill still commits per track"
    sql, params = session.calls[0]
    assert "IN (:g0, :g1, :g2)" in sql
    assert "manual_genres" not in sql
    assert params["genres"] == "Rock, Pop"


def test_no_tracks_is_a_no_op(monkeypatch):
    session = _Session()
    monkeypatch.setattr(ui_routes, "db_session", lambda: session)
    assert ui_routes._apply_album_track_genres_batch([], "Rock") == (0, 0)
    assert session.calls == []
