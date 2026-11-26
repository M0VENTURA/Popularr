"""Pressing Queue on an UNMATCHED upcoming release must try to match first.

REPORTED

> "I added Tyr by pressing the queue button, but it just added the album name,
> not each track. When I used the search, I was able to find it with the same
> release date in musicbrainz. Maybe if the release isn't matched to the queue
> item, by pressing queue, it should try matching the release in the background
> (with a splash to show what's happening), then start downloading. if no release
> is found, then it should bring up the manual search option."

CAUSE

``/api/downloads/queue-upcoming`` only ran the per-track path when
``release_group_mbid`` was set. An unmatched (or merely candidate) row skipped it
and fell through to a single album-typed row that ``_process_release_request``
parks as ``pending_release`` forever, because it has no release id to expand.

FIX

When there is no release-group id, the route now runs the same matcher the
"match" endpoint uses. A HIGH-confidence match is queued per track; anything else
returns ``needs_manual_search`` so the UI can open the picker — and no dead album
row is inserted.
"""

from __future__ import annotations

import pytest

from routes import downloads as downloads_route


class _FakeRow:
    def __init__(self, mapping: dict):
        self._mapping = mapping


class _FakeResult:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class _RecordingSession:
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


def _release_row(**overrides) -> dict:
    row = {
        "id": 42,
        "artist_name": "Tyr",
        "album_name": "Tyr",
        "release_date": "2026-11-24",
        "release_group_mbid": None,   # ← unmatched: the reported case
        "status": "upcoming",
    }
    row.update(overrides)
    return row


@pytest.fixture
def harness(monkeypatch):
    session = _RecordingSession(_release_row())
    calls = {"start": [], "album_rows": [], "persisted": []}

    monkeypatch.setattr("helpers.app_hooks.needs_setup", lambda: False)
    monkeypatch.setattr(downloads_route, "db_session", lambda *a, **k: session)
    monkeypatch.setattr(downloads_route, "signal_new_item", lambda *a, **k: None)

    # The matcher is imported inside the handler from this module.
    import services.upcoming_releases.matching_service as matching

    def _set_match(result: dict):
        monkeypatch.setattr(matching, "match_to_musicbrainz", lambda *a, **k: result)

    # Persisting would open its own DB session; record instead.
    import routes.upcoming_releases_routes as upcoming_routes

    monkeypatch.setattr(
        upcoming_routes, "_persist_match_result",
        lambda rid, res: calls["persisted"].append((rid, res.get("status"))),
    )

    # Release-group → concrete resolution returns the group id unchanged unless
    # a test says otherwise.
    import services.enrichment.musicbrainz_service as mbs

    monkeypatch.setattr(mbs, "resolve_release_id", lambda rid: rid)

    def _set_start(result: dict):
        def _start(release_id, *a, **k):
            calls["start"].append(release_id)
            return result
        monkeypatch.setattr(downloads_route, "start_release_download", _start)

    # A dead album row must never be inserted on the unmatched path.
    monkeypatch.setattr(
        downloads_route, "insert_queue_item",
        lambda **k: calls["album_rows"].append(k) or {"id": 999, "already_queued": False},
    )

    return session, calls, _set_match, _set_start


def _matched(mbid: str = "rg-tyr") -> dict:
    return {
        "status": "matched", "score": 1.0, "mbid": mbid,
        "title": "Tyr", "artist": "Tyr", "candidates": [{"mbid": mbid, "score": 1.0}],
    }


class TestAnUnmatchedReleaseIsMatchedBeforeQueueing:
    @pytest.mark.asyncio
    async def test_a_high_confidence_match_queues_every_track(self, client, harness):
        _session, calls, set_match, set_start = harness
        set_match(_matched())
        set_start({
            "success": True, "queue_items_created": 12, "queued": True,
            "total_tracks": 12,
        })

        response = await client.post(
            "/api/downloads/queue-upcoming", json={"upcoming_release_id": 42}
        )

        assert response.status_code == 200
        body = await response.get_json()
        assert body["success"] is True
        assert body["queue_shape"] == "tracks"
        assert body["queued_tracks"] == 12
        assert calls["start"], "start_release_download was never called with the matched release"
        assert calls["album_rows"] == [], "no single album row should be inserted"

    @pytest.mark.asyncio
    async def test_the_match_is_persisted(self, client, harness):
        _session, calls, set_match, set_start = harness
        set_match(_matched())
        set_start({"success": True, "queue_items_created": 3, "queued": True})

        await client.post("/api/downloads/queue-upcoming", json={"upcoming_release_id": 42})

        assert calls["persisted"], "the on-demand match must be stored on the release"


class TestNoMatchOpensTheManualSearch:
    @pytest.mark.asyncio
    async def test_an_unmatched_result_asks_for_manual_search(self, client, harness):
        _session, calls, set_match, set_start = harness
        set_match({"status": "unmatched", "score": 0.0, "mbid": None, "candidates": []})

        response = await client.post(
            "/api/downloads/queue-upcoming", json={"upcoming_release_id": 42}
        )

        body = await response.get_json()
        assert body["success"] is False
        assert body["needs_manual_search"] is True
        assert body["artist"] == "Tyr" and body["album"] == "Tyr"
        assert body["error"], "a human sentence is required for the toast"
        assert calls["start"] == [], "nothing should be downloaded without a release"
        assert calls["album_rows"] == [], "a dead album row must not be inserted"

    @pytest.mark.asyncio
    async def test_a_candidate_is_not_auto_queued(self, client, harness):
        """A medium-confidence candidate is the USER's call, not ours."""
        _session, calls, set_match, set_start = harness
        set_match({
            "status": "candidate", "score": 0.6, "mbid": "rg-maybe",
            "candidates": [{"mbid": "rg-maybe", "score": 0.6}],
        })

        response = await client.post(
            "/api/downloads/queue-upcoming", json={"upcoming_release_id": 42}
        )

        body = await response.get_json()
        assert body["needs_manual_search"] is True
        assert calls["start"] == []
        assert calls["album_rows"] == []

    @pytest.mark.asyncio
    async def test_a_matcher_exception_still_asks_for_manual_search(
        self, client, harness, monkeypatch
    ):
        _session, calls, set_match, set_start = harness
        import services.upcoming_releases.matching_service as matching

        def _boom(*a, **k):
            raise RuntimeError("musicbrainz down")

        monkeypatch.setattr(matching, "match_to_musicbrainz", _boom)

        response = await client.post(
            "/api/downloads/queue-upcoming", json={"upcoming_release_id": 42}
        )

        body = await response.get_json()
        assert body["needs_manual_search"] is True
        assert calls["album_rows"] == []


class TestAnAlreadyMatchedReleaseSkipsTheSearch:
    @pytest.mark.asyncio
    async def test_no_match_call_when_a_group_id_exists(
        self, client, harness, monkeypatch
    ):
        _session, calls, set_match, set_start = harness
        # Row already carries a group id.
        session = _RecordingSession(_release_row(release_group_mbid="rg-known"))
        monkeypatch.setattr(downloads_route, "db_session", lambda *a, **k: session)

        called = {"n": 0}
        import services.upcoming_releases.matching_service as matching

        def _spy(*a, **k):
            called["n"] += 1
            return {"status": "unmatched", "mbid": None, "candidates": []}

        monkeypatch.setattr(matching, "match_to_musicbrainz", _spy)
        set_start({"success": True, "queue_items_created": 5, "queued": True})

        await client.post("/api/downloads/queue-upcoming", json={"upcoming_release_id": 42})

        assert called["n"] == 0, "a matched release must not re-run the search"
