"""A curated playlist must SHRINK when its members stop qualifying.

Reported:

> "The playlist thats created for Popular Covers doesn't seem to update after a
> scan. It adds tracks, but doesn't remove tracks that have had the cover
> detection removed."

Two independent defects produced that:

1. ``_sync_single_curated_playlist`` returned early on an empty pool, so a
   covers playlist whose remaining covers had all been cleared kept every stale
   entry. An empty pool must now EMPTY the playlist (the same update path that
   removes entries), not leave the old contents in place.
2. Curated playlists were only reconciled by the global rebuild (complete full
   scan / daily job). Artist, album and metadata scans reach
   ``run_popularity_scan`` instead, so the scan that changed ``is_cover`` never
   refreshed the playlist. ``refresh_curated_playlists`` now runs at the end of
   a targeted scan and is a no-op while the full-scan orchestrator owns it.

``_fetch_curated_playlist_rows`` returning ``[]`` (nothing qualifies) vs
``None`` (the query failed) is load-bearing: a transient database error must
never be read as "the library is empty" and wipe every curated playlist.
"""

from __future__ import annotations

from typing import Any

import pytest

from services.popularity.stages import finalise_stage as fs


def _cover_row(track_id: str, *, is_cover: int = 1, stars: int = 5) -> dict[str, Any]:
    return {
        "id": track_id,
        "title": f"Track {track_id}",
        "album": "Album",
        "artist": f"Artist {track_id}",
        "album_artist": f"Artist {track_id}",
        "stars": stars,
        "popularity_score": 50.0,
        "final_score": 50.0,
        "is_live": 0,
        "is_compilation": 0,
        "is_cover": is_cover,
        "is_single": 0,
        "is_acoustic": 0,
    }


CFG = {
    "covers_playlist_enabled": True,
    "covers_playlist_name": "Popular Covers",
    "covers_max_tracks": 150,
    "playlist_order_mode": "final_score",
    "playlist_interleave_artists": False,
    "genre_playlists_max_per_artist": 0,
}


@pytest.fixture
def synced(monkeypatch) -> list[tuple[str, list[str]]]:
    calls: list[tuple[str, list[str]]] = []

    def fake_sync(name, song_ids, **kwargs):
        calls.append((name, list(song_ids)))
        return {"updated": 1, "skipped": 0}

    monkeypatch.setattr(fs, "_sync_playlist_to_navidrome", fake_sync)
    monkeypatch.setattr(fs, "log_unified", lambda *a, **k: None)
    return calls


class TestRemovalsReachThePlaylist:
    def test_a_track_whose_cover_verdict_was_cleared_is_not_requested(
        self, monkeypatch, synced
    ) -> None:
        rows = [
            _cover_row("keep-1"),
            _cover_row("removed", is_cover=0),
            _cover_row("keep-2"),
        ]
        monkeypatch.setattr(fs, "_fetch_curated_playlist_rows", lambda **k: rows)

        assert fs._create_covers_playlist(CFG) is True

        assert synced == [("Popular Covers", ["keep-1", "keep-2"])], (
            "the cleared cover verdict stayed in the requested playlist"
        )

    def test_an_empty_pool_clears_the_playlist(self, monkeypatch, synced) -> None:
        """⭐⭐ THE REPORTED CASE: every remaining track lost its cover verdict."""
        rows = [
            _cover_row("removed-1", is_cover=0),
            _cover_row("removed-2", is_cover=0),
        ]
        monkeypatch.setattr(fs, "_fetch_curated_playlist_rows", lambda **k: rows)

        assert fs._create_covers_playlist(CFG) is True

        assert synced == [("Popular Covers", [])], (
            "an empty cover pool must empty the playlist, not leave it stale"
        )

    def test_a_failed_fetch_never_clears_the_playlist(self, monkeypatch, synced) -> None:
        """``None`` is "unreadable", NOT "empty" — the playlist must survive."""
        monkeypatch.setattr(fs, "_fetch_curated_playlist_rows", lambda **k: None)

        assert fs._create_covers_playlist(CFG) is False
        assert synced == [], "a database failure wiped the playlist"


class TestTheEndpointDispatch:
    def test_a_covers_name_dispatches_to_the_covers_builder(self, monkeypatch) -> None:
        called: list[str] = []

        def fake_builder(cfg):
            called.append("covers")
            return True

        import helpers.config_helpers as ch

        monkeypatch.setattr(
            ch,
            "get_playlists_config",
            lambda: {"covers_playlist_name": "Popular Covers"},
        )
        monkeypatch.setattr(fs, "_create_covers_playlist", fake_builder)

        result = fs.regenerate_managed_playlist("Popular Covers", "curated")

        assert result["ok"] is True
        assert result["synced"] is True
        assert called == ["covers"]

    def test_an_unknown_playlist_is_refused(self) -> None:
        result = fs.regenerate_managed_playlist("My Own Mix")

        assert result["ok"] is False
        assert "not generated" in result["error"]

    def test_a_genre_name_targets_only_that_genre(self, monkeypatch) -> None:
        seen: list[Any] = []

        def fake_builder(prune_only=False, only_genres=None, **kwargs):
            seen.append(only_genres)
            return 1

        import helpers.config_helpers as ch

        monkeypatch.setattr(
            ch,
            "get_playlists_config",
            lambda: {
                "genre_playlists_name_template": "{genre} - Top Tracks",
                "playlist_order_mode": "final_score",
            },
        )
        monkeypatch.setattr(fs, "_create_genre_top_track_playlists", fake_builder)

        result = fs.regenerate_managed_playlist("Metalcore - Top Tracks", "genre")

        assert result["ok"] is True
        assert seen == [["Metalcore"]], "the whole genre family was rebuilt"


class TestTheScanEndRefresh:
    def test_it_is_deferred_during_a_full_scan(self, monkeypatch) -> None:
        ran: list[int] = []
        monkeypatch.setattr(fs, "_run_curated_playlists", lambda **k: ran.append(1))

        import services.popularity.scan_scope as scope

        monkeypatch.setattr(scope, "global_playlists_deferred", lambda: True)

        assert fs.refresh_curated_playlists(reason="test") == {}
        assert ran == [], "a full scan's artists must not each rebuild curated lists"

    def test_it_runs_after_a_targeted_scan(self, monkeypatch) -> None:
        ran: list[int] = []
        monkeypatch.setattr(fs, "_run_curated_playlists", lambda **k: ran.append(1))

        import services.popularity.scan_scope as scope

        monkeypatch.setattr(scope, "global_playlists_deferred", lambda: False)

        fs.refresh_curated_playlists(reason="test")
        assert ran == [1]


class TestTheListReportsOwnership:
    def test_an_indexed_type_wins_over_the_name_rules(self) -> None:
        from routes import playlists as pl

        assert pl._managed_type_for(
            "Popular Covers", {"popular covers": "curated"}
        ) == "curated"

    def test_an_unindexed_generated_name_falls_back_to_the_rules(self) -> None:
        from routes import playlists as pl

        assert pl._managed_type_for("New Music", {}) == "new_music"

    def test_a_user_playlist_has_no_managed_type(self) -> None:
        from routes import playlists as pl

        assert pl._managed_type_for("My Own Mix", {}) is None
