"""A star resync must also rebuild the playlists it invalidates."""
from __future__ import annotations

import pytest

from services.popularity import star_resync_service as svc


def _patch_rating(monkeypatch, *, changed: int) -> None:
    """Stub the DB load and the per-album rating so nothing touches Postgres."""

    def _fake_load(**_kwargs):
        return {("A", "B"): [{"track_id": "1", "album": "B", "artist": "A",
                              "album_artist": "A", "stars": 3,
                              "popularity_score": 50.0, "title": "t"}]}

    def _fake_rate(album_results, *, options, artist_scores):
        rows = [
            {"track_id": "1", "title": "t", "before": 3, "after": 5 if changed else 3,
             "lastfm_listeners": 10}
        ]
        return rows, {"tracks": 1, "changed": changed,
                      "star_ratings": 1, "navidrome_synced": 0}

    monkeypatch.setattr(svc, "_load_album_groups", _fake_load)
    monkeypatch.setattr(svc, "_compute_stars_for_album", _fake_rate)
    monkeypatch.setattr(
        "helpers.config_helpers.get_star_resync_config",
        lambda config=None: {"enabled": 1, "write_file_tags": 0,
                             "sync_navidrome": 0, "albums_per_batch": 25},
    )


def _patch_playlists(monkeypatch) -> dict:
    from services.popularity.stages import finalise_stage as fs

    calls: dict = {"invalidated": 0, "essential_all": 0, "essential_one": [],
                   "global": []}

    monkeypatch.setattr(fs, "invalidate_genre_playlist_rows",
                        lambda: calls.__setitem__("invalidated", calls["invalidated"] + 1))

    def _all_essential():
        calls["essential_all"] += 1
        return 7

    def _one_essential(artist):
        calls["essential_one"].append(artist)
        return True

    monkeypatch.setattr(fs, "_refresh_all_essential_collections", _all_essential)
    monkeypatch.setattr(fs, "_sync_essential_playlist", _one_essential)
    monkeypatch.setattr(fs, "_essential_playlists_enabled", lambda options: True)
    monkeypatch.setattr(fs, "_run_global_playlist_rebuild",
                        lambda options, force=False: calls["global"].append(
                            (dict(options), force)))
    return calls


class TestResyncRebuildsWhatItInvalidates:
    def test_a_library_wide_change_rebuilds_every_generated_playlist(self, monkeypatch):
        """Playlists select on stars and order on the score, so a resync that
        changes stars and leaves them alone leaves them on the OLD ordering —
        the reported "essential playlists are not using the new ordering"."""
        _patch_rating(monkeypatch, changed=1)
        calls = _patch_playlists(monkeypatch)

        result = svc.resync_star_ratings(dry_run=False)

        assert calls["invalidated"] == 1, "the genre pool cache must not serve pre-resync stars"
        assert calls["essential_all"] == 1
        assert calls["essential_one"] == []
        assert calls["global"] == [({}, True)], "the global rebuild must be forced"
        assert result["playlists"] == {"essential": 7, "global": True}

    def test_an_artist_scope_rebuilds_only_that_artist(self, monkeypatch):
        _patch_rating(monkeypatch, changed=1)
        calls = _patch_playlists(monkeypatch)

        svc.resync_star_ratings(artist="A", dry_run=False)

        assert calls["essential_one"] == ["A"]
        assert calls["essential_all"] == 0, "a one-artist resync must not rebuild the library"

    def test_a_dry_run_never_touches_a_playlist(self, monkeypatch):
        _patch_rating(monkeypatch, changed=1)
        calls = _patch_playlists(monkeypatch)

        result = svc.resync_star_ratings(dry_run=True)

        assert calls["invalidated"] == 0
        assert calls["essential_all"] == 0
        assert calls["essential_one"] == []
        assert calls["global"] == []
        assert result["playlists"] == {}

    def test_an_unchanged_library_skips_the_rebuild(self, monkeypatch):
        """A rebuild that cannot differ is pure cost (it re-pushes every
        playlist and, with rotation on, rewrites the order)."""
        _patch_rating(monkeypatch, changed=0)
        calls = _patch_playlists(monkeypatch)

        result = svc.resync_star_ratings(dry_run=False)

        assert calls["global"] == []
        assert calls["essential_all"] == 0
        assert result["playlists"] == {}

    def test_a_playlist_failure_never_discards_the_ratings(self, monkeypatch):
        from services.popularity.stages import finalise_stage as fs

        _patch_rating(monkeypatch, changed=1)
        _patch_playlists(monkeypatch)

        def _boom(*_args, **_kwargs):
            raise RuntimeError("navidrome is down")

        monkeypatch.setattr(fs, "_refresh_all_essential_collections", _boom)
        monkeypatch.setattr(fs, "_run_global_playlist_rebuild", _boom)

        result = svc.resync_star_ratings(dry_run=False)

        assert result["ok"] is True
        assert result["changed"] == 1
