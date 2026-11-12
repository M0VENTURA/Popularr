"""A playlist must be CLEARED when its genre no longer exists on any track.

REPORTED
--------
"But some of the playlists are being generated from tracks that have had genres
removed, but the tracks are still on the playlist."

ROOT CAUSE
----------
``_create_genre_top_track_playlists`` builds ``pools`` — a map of genre → tracks
— and then loops ``for norm_key, tracks in pools.items()``. A genre whose tracks
have ALL lost it produces NO ENTRY in that map, so the loop never visited it and
its Navidrome playlist was never rewritten. The tracks were re-tagged correctly;
the playlist simply kept the old contents because nothing looked at its key.

This is the normal outcome of a genre MOVE (Alternative Rock → Post-Grunge) and
of the metal-over-rock policy (Folk Rock suppressed by Folk Metal).

The affected-genre set is already computed by the callers and passed down as
``only_genres``, so an affected genre with no tracks left is an EMPTY pool that
must be synced down to nothing.

SAFETY
------
The row query is LIBRARY-WIDE (no artist/album restriction) and filters on the
final ``genres`` column, so a genre absent from ``pools`` genuinely has no
qualifying track anywhere — clearing cannot delete a live playlist's contents.
The clear also passes ``create_if_missing=False``, so it can only EMPTY a
playlist that already exists, never conjure one.
"""

from __future__ import annotations

import pytest

TITLES = {"t1": "One", "t2": "Two", "t3": "Three"}

# No track carries Alternative Rock any more — the genre MOVED off every track
# (or was suppressed by the metal policy). `pools` therefore has no entry for
# it, which is exactly the reported case.
ROWS = [
    {"id": "t1", "title": "One", "album": "A", "artist": "X",
     "album_artist": "X", "file_path": "", "duration": 100,
     "stars": 5, "final_score": 9.0, "is_live": 0, "is_compilation": 0,
     "lastfm_listeners": 0, "listenbrainz_listens": 0,
     "genres": "Post-Grunge"},
    {"id": "t2", "title": "Two", "album": "A", "artist": "X",
     "album_artist": "X", "file_path": "", "duration": 100,
     "stars": 5, "final_score": 8.0, "is_live": 0, "is_compilation": 0,
     "lastfm_listeners": 0, "listenbrainz_listens": 0,
     "genres": "Alternative Metal"},
]


@pytest.fixture
def builder(monkeypatch):
    """Run the REAL builder with a stubbed Navidrome, capturing every call."""
    import helpers.config_helpers as config_helpers
    from services.popularity.stages import finalise_stage as fs

    def _run(*, only_genres=None, existing=("Alternative Rock - Top Tracks",
                                            "Alternative Metal - Top Tracks")):
        calls: list[tuple[str, list[str], bool]] = []

        monkeypatch.setattr(fs, "_fetch_genre_playlist_rows",
                            lambda min_stars, genres=None: ROWS)
        monkeypatch.setattr(fs, "_genre_playlists_enabled", lambda: True)
        monkeypatch.setattr(fs, "_genre_playlists_delete_enabled", lambda: False)
        monkeypatch.setattr(fs, "_genre_playlists_active", lambda: True)

        class _Client:
            def fetch_all_playlists(self):
                return [{"name": name, "id": "id-" + name} for name in existing]

        monkeypatch.setattr(fs, "_navidrome_clients", lambda: [_Client()])

        def _sync(name, song_ids, rows=None, existing_playlist_id=None,
                  server_confirmed_exists=None, create_if_missing=True, **_kw):
            calls.append((name, list(song_ids), create_if_missing))
            return {"updated": 1, "created": 0, "unchanged": 0, "failed": 0}

        monkeypatch.setattr(fs, "_sync_playlist_to_navidrome", _sync)
        monkeypatch.setattr(config_helpers, "get_config", lambda: {
            "playlists": {
                "genre_playlists_create_threshold": 1,
                "genre_playlists_min_stars": 1,
                "genre_playlists_max_genres": 4,
                "genre_playlists_max_tracks": 0,
                "genre_playlists_max_per_artist": 0,
                "playlist_interleave_artists": False,
            }
        })

        fs._create_genre_top_track_playlists(
            only_genres=only_genres, cleanup_orphans=False
        )
        return calls

    return _run


class TestAnEmptiedGenreClearsItsPlaylist:
    def test_a_genre_with_no_tracks_left_has_its_playlist_cleared(self, builder):
        """The reported bug: the genre moved away and the playlist kept the
        tracks."""
        calls = builder(only_genres={"Alternative Rock", "Alternative Metal"})
        by_name = {name: ids for name, ids, _create in calls}

        assert "Alternative Rock - Top Tracks" in by_name, (
            "the emptied genre's playlist was never visited, so it keeps the "
            f"tracks that no longer carry the genre: {sorted(by_name)}"
        )
        assert by_name["Alternative Rock - Top Tracks"] == [], (
            "Alternative Rock is gone from every track, so its playlist must be "
            f"emptied, not left populated: {by_name['Alternative Rock - Top Tracks']}"
        )

    def test_the_still_populated_genre_is_kept(self, builder):
        calls = builder(only_genres={"Alternative Rock", "Alternative Metal"})
        by_name = {name: ids for name, ids, _create in calls}
        assert by_name.get("Alternative Metal - Top Tracks") == ["t2"], (
            "a genre that still has qualifying tracks must keep them"
        )
        # ``only_genres`` bounds the pass, so an unrelated genre (Post-Grunge)
        # is correctly NOT synced here.
        assert "Post-Grunge - Top Tracks" not in by_name

    def test_the_clear_never_creates_a_playlist(self, builder):
        """Only EMPTY one that already exists — never conjure it."""
        calls = builder(only_genres={"Alternative Rock", "Alternative Metal"})
        for name, ids, create_if_missing in calls:
            if ids == []:
                assert create_if_missing is False, (
                    f"{name!r} was cleared with create_if_missing=True, which "
                    "would CREATE an empty playlist on a server that had none"
                )

    def test_a_genre_with_no_playlist_on_the_server_is_skipped(self, builder):
        """No playlist means nothing to clear — a sync call would be waste."""
        calls = builder(
            only_genres={"Alternative Rock"},
            existing=(),  # server has neither playlist
        )
        assert calls == [], f"unexpected sync calls: {calls}"

    def test_an_unaffected_genre_is_not_touched(self, builder):
        """``only_genres`` bounds the pass: unrelated genres stay out."""
        calls = builder(only_genres={"Alternative Metal"})
        names = {name for name, _ids, _c in calls}
        assert "Alternative Rock - Top Tracks" not in names

    def test_a_full_rebuild_does_not_clear_every_absent_genre(self, builder):
        """A full rebuild (``only_genres=None``) visits only live pools.

        Every genre in the library is present in ``pools`` by definition, and
        the orphan sweep owns removal there — this guard keeps the new clear
        path from firing on a full pass.
        """
        calls = builder(only_genres=None)
        cleared = [name for name, ids, _c in calls if ids == []]
        assert cleared == [], (
            f"a full rebuild emptied a playlist without the sweep deciding: {cleared}"
        )
