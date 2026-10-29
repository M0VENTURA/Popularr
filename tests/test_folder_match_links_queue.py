"""Regression: confirming a folder match must link the moved file back to its
download_queue item.

The old system's watcher (``mark_queue_item_matched_from_torrent``) marked a
downloaded file's queue item as matched/completed with its file paths.  The
current ``match_folder_to_release`` moved files to the library but never
touched ``download_queue`` — so a release could appear in Matched Folders and
be organized, yet the queue item stayed orphaned in 'downloading'.  This test
pins ``_link_moved_file_to_queue_item`` (the new linking step).
"""

from __future__ import annotations

import pytest


class TestLinkMovedFileToQueueItem:
    def test_links_by_track_number(self, monkeypatch):
        """A queue item with the same track number is matched and imported."""
        from services.downloads import download_folder_service as dfs
        import db.repositories.queue as queue_mod

        queue_rows = [
            {"id": 1, "title": "BiiiG", "track_number": "1", "artist": "BIGBANG",
             "album": "BiiiG", "album_artist": "BIGBANG", "status": "downloading"},
        ]
        monkeypatch.setattr(queue_mod, "get_album_queue_tracks", lambda artist, album: list(queue_rows))
        updates: dict[int, dict] = {}

        def _update(qid, **kwargs):
            updates[qid] = kwargs
            return {"id": qid, **kwargs}

        monkeypatch.setattr(queue_mod, "update_queue_item", _update)

        ok, _err = dfs._link_moved_file_to_queue_item(
            queue_artist="BIGBANG",
            queue_album="BiiiG",
            file_title="BiiiG",
            track_number="1",
            target_path="/music/BIGBANG/BiiiG/01 - BiiiG.flac",
            release_mbid="mbid-123",
        )
        assert ok is True
        assert 1 in updates
        assert updates[1]["status"] == "imported"
        assert updates[1]["music_file_path"] == "/music/BIGBANG/BiiiG/01 - BiiiG.flac"
        assert updates[1]["release_mbid"] == "mbid-123"

    def test_links_by_normalized_title(self, monkeypatch):
        """When track numbers differ, an exact normalized-title match works
        via the artist-scoped active-queue fallback (single-track download
        whose queue album differs from the resolved MB release)."""
        from services.downloads import download_folder_service as dfs
        import db.repositories.queue as queue_mod

        queue_rows = [
            {"id": 2, "title": "BiiiG", "track_number": "", "artist": "BIGBANG",
             "album": "BiiiG (FLAC)", "album_artist": "BIGBANG", "status": "queued"},
        ]
        monkeypatch.setattr(queue_mod, "get_album_queue_tracks", lambda a, b: [])
        monkeypatch.setattr(queue_mod, "get_active_queue", lambda limit=500: list(queue_rows))
        updates: dict[int, dict] = {}

        def _update(qid, **kwargs):
            updates[qid] = kwargs
            return {"id": qid, **kwargs}

        monkeypatch.setattr(queue_mod, "update_queue_item", _update)

        ok, _err = dfs._link_moved_file_to_queue_item(
            queue_artist="BIGBANG",
            queue_album="BiiiG (FLAC) [24-Bit 48.0-kHz]",
            file_title="BiiiG",
            track_number="01",
            target_path="/music/BIGBANG/BiiiG/01 - BiiiG.flac",
            release_mbid="mbid-456",
        )
        assert ok is True
        assert 2 in updates
        assert updates[2]["status"] == "imported"
        assert updates[2]["release_mbid"] == "mbid-456"

    def test_no_match_returns_false(self, monkeypatch):
        from services.downloads import download_folder_service as dfs
        import db.repositories.queue as queue_mod

        monkeypatch.setattr(queue_mod, "get_album_queue_tracks", lambda a, b: [])
        monkeypatch.setattr(queue_mod, "get_active_queue", lambda limit=500: [])
        ok, _err = dfs._link_moved_file_to_queue_item(
            queue_artist="Nobody", queue_album="Nothing",
            file_title="Nope", track_number="99",
            target_path="/music/x.flac", release_mbid="m",
        )
        assert ok is False
