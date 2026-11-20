"""Top genre playlists are 4★/5★ only — the floor is enforced on MEMBERSHIP.

REPORTED

> "Tracks rated only 3 star are showing in top genre playlists"

``_create_genre_top_track_playlists`` selected its rows with

    WHERE COALESCE(stars, star_rating) >= :min_stars

which was the ONLY gate. That holds for a normal global rebuild, but it is not
a property of the BUILDER — several routes hand the loop rows that never went
through that query, and nothing re-checked the tier:

* ``rows_override`` (the genre/playlist resync PREVIEW) substitutes an arbitrary
  row set;
* ``_GENRE_ROWS_CACHE`` serves rows for 120 s keyed on ``min_stars`` alone, so a
  rating DEMOTED to 3★ between the fetch and the rebuild was still pooled at its
  old tier;
* ``COALESCE(stars, star_rating)`` lets a stale second rating column satisfy the
  threshold while the displayed ``stars`` reads 3.

The builder now re-asserts the floor on the row's own ``stars``, and the
self-check names any track that still reaches a playlist.
"""

from __future__ import annotations

import pytest

import helpers.config_helpers as config_helpers
from services.popularity.stages import finalise_stage as fs


def _row(track_id: str, stars: int, *, genre: str = "Metal", artist: str = "A"):
    return {
        "id": track_id,
        "title": f"Song {track_id}",
        "album": "Album",
        "artist": artist,
        "album_artist": artist,
        "file_path": f"/music/{track_id}.mp3",
        "duration": 180,
        "stars": stars,
        "popularity_score": 50.0,
        "final_score": 50.0,
        "is_live": 0,
        "is_compilation": 0,
        "lastfm_listeners": 1000,
        "listenbrainz_listens": 100,
        "genres": genre,
    }


@pytest.fixture()
def env(monkeypatch):
    """The real builder, a stubbed Navidrome, and a configurable star floor."""
    state = {"min_stars": 4, "rows": [], "pushed": [], "existing": []}

    def _cfg():
        return {"playlists": {
            "genre_playlists_enabled": True,
            "genre_playlists_delete_enabled": False,
            "genre_playlists_create_threshold": 1,
            "genre_playlists_delete_threshold": 1,
            "genre_playlists_min_stars": state["min_stars"],
            "genre_playlists_max_genres": 3,
            "genre_playlists_max_tracks": 0,
            "genre_playlists_max_per_artist": 0,
            "playlist_order_mode": "prominence",
            "playlist_interleave_artists": False,
            "exclude_christmas_from_playlists": True,
        }}

    monkeypatch.setattr(config_helpers, "get_config", _cfg)
    monkeypatch.setattr(fs, "_genre_playlists_enabled", lambda: True)
    monkeypatch.setattr(fs, "_genre_playlists_delete_enabled", lambda: False)
    monkeypatch.setattr(fs, "_genre_playlists_active", lambda: True)

    class _Client:
        def fetch_all_playlists(self):
            return list(state["existing"])

    monkeypatch.setattr(fs, "_navidrome_clients", lambda: [_Client()])
    monkeypatch.setattr(fs, "_playlist_already_pushed", lambda *a, **k: False)
    monkeypatch.setattr(fs, "_record_playlist_push", lambda *a, **k: None)

    def _sync(name, song_ids, rows=None, **kw):
        state["pushed"].append((name, list(song_ids)))
        return {"updated": 1, "created": 0, "unchanged": 0, "failed": 0,
                "skipped": 0, "success": True}

    monkeypatch.setattr(fs, "_sync_playlist_to_navidrome", _sync)
    return state


def _run(monkeypatch, env, rows, *, override=False):
    """Drive the real builder, feeding ROWS directly.

    ``override=True`` delivers them through ``rows_override`` — the path the
    resync preview uses, which never touches the row query.
    """
    env["rows"] = rows
    if override:
        return fs._create_genre_top_track_playlists(rows_override=rows)
    monkeypatch.setattr(fs, "_fetch_genre_playlist_rows",
                        lambda min_stars, genres=None: list(rows))
    return fs._create_genre_top_track_playlists()


class TestThreeStarTracksNeverReachATopPlaylist:
    def test_a_three_star_row_is_dropped(self, monkeypatch, env) -> None:
        _run(monkeypatch, env, [_row("t4", 4), _row("t3", 3)])
        pushed = [track_id for _name, ids in env["pushed"] for track_id in ids]
        assert "t4" in pushed
        assert "t3" not in pushed, (
            "a 3★ track reached a Top Tracks playlist"
        )

    def test_the_override_path_is_floored_too(self, monkeypatch, env) -> None:
        """⭐ The reported route: ``rows_override`` bypasses the SQL entirely."""
        _run(monkeypatch, env, [_row("t5", 5), _row("t3", 3)], override=True)
        pushed = [track_id for _name, ids in env["pushed"] for track_id in ids]
        assert pushed == ["t5"], pushed

    def test_a_lower_configured_floor_admits_that_tier(self, monkeypatch, env) -> None:
        """CONTROL — the floor is the CONFIG, not a hard-coded 4."""
        env["min_stars"] = 3
        _run(monkeypatch, env, [_row("t4", 4), _row("t3", 3)])
        pushed = [track_id for _name, ids in env["pushed"] for track_id in ids]
        assert {"t4", "t3"} <= set(pushed), pushed

    def test_an_all_below_floor_pool_writes_nothing(self, monkeypatch, env) -> None:
        written = _run(monkeypatch, env, [_row("t3", 3), _row("t2", 2)])
        assert written == 0
        assert env["pushed"] == []


class TestTheSelfCheckNamesABelowFloorTrack:
    def test_below_floor_is_reported(self) -> None:
        problems = fs._verify_genre_playlist(
            genre="Metal",
            playlist_name="Metal - Top Tracks",
            winners=[{"id": "t3", "title": "Song", "stars": 3, "genres": ["Metal"]}],
            max_tracks=1,
            max_per_artist=4,
            eligible_pool_size=1,
            min_stars=4,
        )
        assert any(p.startswith("BELOW_STAR_FLOOR") for p in problems), problems

    def test_a_compliant_playlist_reports_nothing(self) -> None:
        problems = fs._verify_genre_playlist(
            genre="Metal",
            playlist_name="Metal - Top Tracks",
            winners=[{"id": "t4", "title": "Song", "stars": 4, "genres": ["Metal"]}],
            max_tracks=1,
            max_per_artist=4,
            eligible_pool_size=1,
            min_stars=4,
        )
        assert problems == [], problems

    def test_an_unset_floor_never_flags(self) -> None:
        """CONTROL — a caller that does not pass a floor keeps the old silence."""
        problems = fs._verify_genre_playlist(
            genre="Metal",
            playlist_name="Metal - Top Tracks",
            winners=[{"id": "t1", "title": "Song", "stars": 1, "genres": ["Metal"]}],
            max_tracks=1,
            max_per_artist=4,
            eligible_pool_size=1,
        )
        assert problems == [], problems
