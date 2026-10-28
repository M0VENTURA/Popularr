"""Persistent playlist song-ID rejections are scoped and retryable."""

from __future__ import annotations

from services.playlists import playlist_navidrome_service as pns
from services.playlists.song_id_reconciliation import (
    clear_playlist_song_rejections,
    get_rejected_song_ids,
    record_rejected_song_ids,
)


def test_rejections_are_recorded_new_then_suppressed_then_cleared():
    server = "http://nav.example|user"
    playlist = "Christmas Rock - Top Tracks"
    clear_playlist_song_rejections(server)

    assert record_rejected_song_ids(server, playlist, ["bad-1"], {"bad-1": "Blind Channel - Diana"}) == 1
    assert get_rejected_song_ids(server, playlist) == {"bad-1": "Blind Channel - Diana"}

    # Re-recording the same rejection is not "new": the caller must not warn
    # again on every scan.
    assert record_rejected_song_ids(server, playlist, ["bad-1"]) == 0

    assert clear_playlist_song_rejections(server) == 1
    assert get_rejected_song_ids(server, playlist) == {}


def test_rejections_are_not_shared_between_servers():
    clear_playlist_song_rejections("server-a")
    clear_playlist_song_rejections("server-b")
    assert record_rejected_song_ids("server-a", "P", ["bad"]) == 1
    assert get_rejected_song_ids("server-b", "P") == {}
    clear_playlist_song_rejections("server-a")


class _PlaylistClient:
    base_url = "http://nav.example"
    username = "user"

    def __init__(self):
        self.stored = ["old"]
        self.updates = 0

    def fetch_all_playlists(self):
        return [{"id": "pl1", "name": "Christmas Rock - Top Tracks"}]

    def fetch_playlist(self, playlist_id):
        return {"tracks": [{"id": value} for value in self.stored]}

    def update_playlist_songs(self, playlist_id, song_ids, **kwargs):
        self.updates += 1
        self.stored = [value for value in song_ids if value != "bad-1"]
        return True


def test_repeated_scan_does_not_rewrite_for_a_known_rejected_id():
    server = "http://nav.example|user"
    playlist = "Christmas Rock - Top Tracks"
    clear_playlist_song_rejections(server)
    client = _PlaylistClient()

    first = pns.sync_playlist_by_name(client, playlist, ["a", "bad-1", "b"])
    assert first["partial"] is True
    assert first["needs_navidrome_import"] is True
    assert first["dropped_ids"] == ["bad-1"]
    assert client.updates == 1

    second = pns.sync_playlist_by_name(client, playlist, ["a", "bad-1", "b"])
    assert second["partial"] is True
    assert second["reconciliation_complete"] is False
    assert client.updates == 1, "a known rejected ID must not rewrite the playlist again"

    # A verified import/mapping change clears the rejection so the ID is
    # retried rather than permanently suppressed.
    clear_playlist_song_rejections(server)
    client.stored = ["a", "bad-1", "b"]
    third = pns.sync_playlist_by_name(client, playlist, ["a", "bad-1", "b"])
    assert third["unchanged"] is True
    assert third["reconciliation_complete"] is True

