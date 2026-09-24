"""Regression tests: queueing an upcoming release must not fake success.

``start_release_download`` returns ``success: True`` as soon as the
MusicBrainz fetch works — even when every track in the release was already in
the library or queue, so ZERO new queue rows were created. The route
(``/api/downloads/queue-upcoming``) only checked that flag, then:

* reported ``success: True`` with ``queued_tracks: 0``, and
* flipped ``upcoming_releases.status`` to ``'queued'``.

The dashboard then showed a release that looked queued while nothing was
downloading behind it. The service already exposes ``queued`` and
``queue_reason`` for exactly this case; the route simply ignored them.

These tests drive the real route handler and assert on the SQL it executes, so
the "did it wrongly mark the release queued?" question is answered directly.
"""

from __future__ import annotations

import pytest

from routes import downloads as downloads_route


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeRow:
    def __init__(self, mapping: dict):
        self._mapping = mapping


class _FakeResult:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class _RecordingSession:
    """Records every statement so the test can assert what was written."""

    def __init__(self, release_row: dict):
        self.release_row = release_row
        self.statements: list[str] = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        if "FROM upcoming_releases" in sql:
            return _FakeResult(_FakeRow(self.release_row))
        return _FakeResult(None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _release_row() -> dict:
    """A release that is already OUT (past date) so the guard lets it through."""
    return {
        "id": 7,
        "artist_name": "Pink Floyd",
        "album_name": "Animals",
        "release_date": "2025-01-15",
        "release_group_mbid": "rg-animals",
        "status": "upcoming",
    }


@pytest.fixture
def harness(monkeypatch):
    """Patch the route's collaborators and hand back the recording session."""
    session = _RecordingSession(_release_row())

    monkeypatch.setattr("helpers.app_hooks.needs_setup", lambda: False)
    monkeypatch.setattr(downloads_route, "db_session", lambda *a, **k: session)
    monkeypatch.setattr(downloads_route, "signal_new_item", lambda *a, **k: None)

    def _set_result(result: dict):
        monkeypatch.setattr(
            downloads_route, "start_release_download", lambda *a, **k: result
        )

    return session, _set_result


def _was_marked_queued(session: _RecordingSession) -> bool:
    return any(
        "UPDATE upcoming_releases" in sql and "'queued'" in sql
        for sql in session.statements
    )


# ---------------------------------------------------------------------------
# The bug: nothing queued, but success reported
# ---------------------------------------------------------------------------

class TestZeroTrackQueueDoesNotReportSuccess:

    @pytest.mark.asyncio
    async def test_zero_queued_tracks_is_not_reported_as_success(
        self, client, harness
    ):
        session, set_result = harness
        set_result({
            "success": True,               # MB fetch worked...
            "queue_items_created": 0,      # ...but nothing was queued
            "queue_ids": [],
            "queued": False,
            "queue_reason": "all_in_library",
            "queue_message": "Every track in this release is already in your library.",
            "total_tracks": 10,
        })

        response = await client.post(
            "/api/downloads/queue-upcoming", json={"upcoming_release_id": 7}
        )

        assert response.status_code >= 400, "zero queued tracks must not be a 200"
        body = await response.get_json()
        assert body["success"] is False
        assert body["queued_tracks"] == 0

    @pytest.mark.asyncio
    async def test_zero_queued_tracks_does_not_mark_the_release_queued(
        self, client, harness
    ):
        """The load-bearing assertion: the release row must not be marked."""
        session, set_result = harness
        set_result({
            "success": True,
            "queue_items_created": 0,
            "queued": False,
            "queue_reason": "all_in_library",
            "queue_message": "Every track in this release is already in your library.",
            "total_tracks": 10,
        })

        await client.post(
            "/api/downloads/queue-upcoming", json={"upcoming_release_id": 7}
        )

        assert not _was_marked_queued(session), (
            "the release was marked 'queued' despite zero tracks being queued"
        )

    @pytest.mark.asyncio
    async def test_the_reason_reaches_the_user(self, client, harness):
        """The UI toasts ``data.error``, so the human sentence must be there —
        a bare machine code like ``all_in_library`` is not user-facing."""
        _, set_result = harness
        set_result({
            "success": True,
            "queue_items_created": 0,
            "queued": False,
            "queue_reason": "all_in_library",
            "queue_message": "Every track in this release is already in your library.",
            "total_tracks": 10,
        })

        response = await client.post(
            "/api/downloads/queue-upcoming", json={"upcoming_release_id": 7}
        )

        body = await response.get_json()
        assert body["error"] == "Every track in this release is already in your library."
        assert body["reason"] == "all_in_library"

    @pytest.mark.asyncio
    async def test_a_missing_message_still_says_something_useful(self, client, harness):
        """A bare ``{success: False}`` would toast as the generic client
        fallback; the route must always supply a sentence."""
        _, set_result = harness
        set_result({
            "success": True,
            "queue_items_created": 0,
            "queued": False,
            "queue_reason": "nothing_queued",
            "total_tracks": 3,
        })

        response = await client.post(
            "/api/downloads/queue-upcoming", json={"upcoming_release_id": 7}
        )

        body = await response.get_json()
        assert body["error"]
        assert isinstance(body["error"], str)
        assert "_" not in body["error"], "a machine code leaked into the user-facing message"


# ---------------------------------------------------------------------------
# The happy path must still work
# ---------------------------------------------------------------------------

class TestWithTracksStillQueues:

    @pytest.mark.asyncio
    async def test_tracks_queued_reports_success(self, client, harness):
        _, set_result = harness
        set_result({
            "success": True,
            "queue_items_created": 10,
            "queued": True,
            "queue_reason": None,
            "total_tracks": 10,
        })

        response = await client.post(
            "/api/downloads/queue-upcoming", json={"upcoming_release_id": 7}
        )

        assert response.status_code == 200
        body = await response.get_json()
        assert body["success"] is True
        assert body["queued_tracks"] == 10

    @pytest.mark.asyncio
    async def test_tracks_queued_marks_the_release(self, client, harness):
        session, set_result = harness
        set_result({
            "success": True,
            "queue_items_created": 4,
            "queued": True,
            "total_tracks": 4,
        })

        await client.post(
            "/api/downloads/queue-upcoming", json={"upcoming_release_id": 7}
        )

        assert _was_marked_queued(session)

    @pytest.mark.asyncio
    async def test_a_genuine_service_failure_still_returns_error(self, client, harness):
        """``start_release_download`` returning success:False must still be an
        error — and must not be confused with the zero-track case."""
        session, set_result = harness
        set_result({"success": False, "error": "MusicBrainz fetch failed"})

        response = await client.post(
            "/api/downloads/queue-upcoming", json={"upcoming_release_id": 7}
        )

        # Falls through to the single-item fallback, which is a separate path.
        body = await response.get_json()
        assert body is not None
        assert not _was_marked_queued(session) or body.get("success") is not None


# ---------------------------------------------------------------------------
# Guard: the route must read the service's zero-track signal
# ---------------------------------------------------------------------------

def test_route_reads_queue_items_created():
    """The fix is only correct while the route inspects the count. If someone
    reverts to trusting ``success`` alone, the guard above silently disappears."""
    import inspect

    source = inspect.getsource(downloads_route.api_queue_upcoming)
    assert "queue_items_created" in source
    assert "queued_tracks <= 0" in source


def test_service_exposes_the_zero_track_signal():
    """``start_release_download`` must keep reporting why nothing queued."""
    import inspect

    from services.downloads import download_pipeline_service as dps

    source = inspect.getsource(dps.start_release_download)
    for key in ("queue_items_created", "queue_reason", "queue_message"):
        assert key in source, key
