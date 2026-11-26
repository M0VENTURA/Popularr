"""Release requests must be EXPANDED, not searched as one track.

Reported: ``Fuel - Pendulum`` (an album title) entered the track pipeline and
produced a run of ``title_mismatch`` rejections.
"""

from __future__ import annotations

from services.downloads import download_pipeline_service as pipeline


class TestDetection:
    def test_an_album_typed_row_is_a_release_request(self):
        assert pipeline._is_release_request(
            {"import_type": "album", "artist": "Fuel", "title": "Pendulum",
             "album": "Pendulum", "track_number": 1, "duration": 200}
        ) is True

    def test_an_album_titled_row_without_track_identity_is_a_release_request(self):
        assert pipeline._is_release_request(
            {"artist": "Fuel", "title": "Pendulum", "album": "Pendulum"}
        ) is True

    def test_a_real_track_sharing_the_album_name_is_not_a_release(self):
        assert pipeline._is_release_request(
            {"artist": "Fuel", "title": "Pendulum", "album": "Pendulum",
             "track_number": 1, "duration": 215}
        ) is False

    def test_an_ordinary_track_is_not_a_release(self):
        assert pipeline._is_release_request(
            {"artist": "Fuel", "title": "Hemorrhage", "album": "Something Like Human",
             "track_number": 2}
        ) is False


class TestHandling:
    def test_it_expands_the_tracklist_and_retires_the_album_row(self, monkeypatch):
        updates: list[tuple] = []
        monkeypatch.setattr(
            pipeline, "update_queue_item",
            lambda qid, **kw: updates.append((qid, kw)) or {"id": qid},
        )
        monkeypatch.setattr(
            pipeline, "start_release_download",
            lambda *a, **k: {"success": True, "queue_items_created": 12},
        )

        result = pipeline._process_release_request(
            {"artist": "Fuel", "album": "Pendulum",
             "release_mbid": "11111111-1111-1111-1111-111111111111"},
            77,
        )

        assert result["status"] == "release_expanded"
        assert result["queue_items_created"] == 12
        assert updates[-1][1]["status"] == "removed"
        assert "12 track" in updates[-1][1]["failure_reason"]

    def test_without_a_release_id_it_waits_instead_of_searching(self, monkeypatch):
        """It must PARK with a real retry time, not just flip the status.

        ``update_queue_item(status=...)`` left ``next_retry_at`` NULL, and the
        ready query treats NULL as DUE — so this row was re-fetched on every
        queue cycle (the reported "left pending_release" line every ~35s for
        hours) and its attempt count never moved.
        """
        scheduled: list[tuple] = []
        monkeypatch.setattr(
            "db.repositories.queue.schedule_queue_retry",
            lambda qid, status, next_retry_at, reason="": (
                scheduled.append((qid, status, next_retry_at, reason))
                or {"success": True, "status": status}
            ),
            raising=False,
        )
        called = {"download": False}
        monkeypatch.setattr(
            pipeline, "start_release_download",
            lambda *a, **k: called.__setitem__("download", True) or {},
        )

        result = pipeline._process_release_request(
            {"artist": "Fuel", "album": "Pendulum"}, 78
        )

        assert result["status"] == "pending_release"
        assert called["download"] is False
        assert scheduled, "the row must be scheduled, not left immediately due"
        qid, status, next_retry_at, _reason = scheduled[-1]
        assert (qid, status) == (78, "pending_release")
        assert next_retry_at, "a NULL next_retry_at re-fetches the row every cycle"

    def test_an_expansion_failure_keeps_the_row_pending(self, monkeypatch):
        updates: list[tuple] = []
        monkeypatch.setattr(
            pipeline, "update_queue_item",
            lambda qid, **kw: updates.append((qid, kw)) or {"id": qid},
        )
        monkeypatch.setattr(
            pipeline, "start_release_download",
            lambda *a, **k: {"success": True, "queue_items_created": 0,
                             "error": "no tracklist"},
        )

        result = pipeline._process_release_request(
            {"artist": "Fuel", "album": "Pendulum",
             "release_mbid": "11111111-1111-1111-1111-111111111111"},
            79,
        )

        assert result["status"] == "pending_release"
        assert updates[-1][1]["status"] == "pending_release"
        assert "no tracklist" in updates[-1][1]["failure_reason"]
