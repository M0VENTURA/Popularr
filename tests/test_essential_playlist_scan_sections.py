"""Regression tests: Essential Collection .m3u creation per-artist scan section.

The runner must create/refresh an artist's essential collection at the END of
EVERY artist's section during a scan — not once at the end of the whole run.
Previously only artists that appeared in ``finalise_scan``'s ``results`` got a
collection, so a full/combined scan whose albums were mostly SKIPPED (inside
the skip window) never created or updated those artists' playlists.

Covers:
- ``_close_artist_essential_section`` creates the collection once per new
  artist, reuses the once-per-scan feat-track pool, and records the artist so
  ``finalise_scan`` doesn't re-do them.
- skipped/metadata-only scans still close out every artist section.
- ``finalise_scan`` skips artists the runner already handled
  (``options["_essential_playlists_done"]``) but still creates the rest.
"""

from __future__ import annotations

import pytest


class TestCloseArtistEssentialSection:
    """The per-artist section-close helper in scan_stage_runner."""

    def _patch(self, monkeypatch):
        import services.popularity.scan_stage_runner as srr

        created = []
        monkeypatch.setattr(
            srr, "_sync_essential_playlist",
            lambda artist, featured_rows=None: created.append((artist, featured_rows)),
        )
        monkeypatch.setattr(srr, "_essential_playlists_enabled", lambda options: True)
        fetches = []
        monkeypatch.setattr(
            srr, "_fetch_essential_featured_rows",
            lambda: fetches.append(True) or [{"artist": "A feat. B"}],
        )
        return srr, created, fetches

    def test_creates_for_new_artist_and_returns_done(self, monkeypatch):
        srr, created, fetches = self._patch(monkeypatch)

        done, rows = srr._close_artist_essential_section(
            "Poppy", {}, set(), None,
        )

        assert done == {"poppy"}
        assert rows == [{"artist": "A feat. B"}]
        assert created == [("Poppy", [{"artist": "A feat. B"}])]
        assert len(fetches) == 1

    def test_featured_pool_fetched_once_across_artists(self, monkeypatch):
        srr, created, fetches = self._patch(monkeypatch)

        done, rows = srr._close_artist_essential_section("Poppy", {}, set(), None)
        done, rows = srr._close_artist_essential_section(
            "Lord of the Lost", {}, done, rows,
        )

        # The library-wide feat pool is fetched ONCE per scan, never per artist.
        assert len(fetches) == 1
        assert created == [
            ("Poppy", [{"artist": "A feat. B"}]),
            ("Lord of the Lost", [{"artist": "A feat. B"}]),
        ]

    def test_skips_artist_already_handled(self, monkeypatch):
        srr, created, fetches = self._patch(monkeypatch)

        done, rows = srr._close_artist_essential_section(
            "Poppy", {}, {"poppy"}, [{"artist": "A feat. B"}],
        )

        assert created == []
        assert fetches == []
        assert done == {"poppy"}

    def test_metadata_only_scan_never_writes(self, monkeypatch):
        srr, created, fetches = self._patch(monkeypatch)

        done, rows = srr._close_artist_essential_section(
            "Poppy", {"metadata_only": True}, set(), None,
        )

        assert created == []
        assert fetches == []
        assert done == set()

    def test_disabled_via_config_never_writes(self, monkeypatch):
        import services.popularity.scan_stage_runner as srr

        created = []
        monkeypatch.setattr(
            srr, "_sync_essential_playlist",
            lambda artist, featured_rows=None: created.append(artist),
        )
        monkeypatch.setattr(srr, "_essential_playlists_enabled", lambda options: False)

        done, rows = srr._close_artist_essential_section("Poppy", {}, set(), None)

        assert created == []
        assert done == set()
        assert rows is None

    def test_failure_is_isolated_and_artist_retried_next_section(self, monkeypatch):
        import services.popularity.scan_stage_runner as srr

        calls = []

        def flaky(artist, featured_rows=None):
            calls.append(artist)
            if len(calls) == 1:
                raise RuntimeError("db down")

        monkeypatch.setattr(srr, "_sync_essential_playlist", flaky)
        monkeypatch.setattr(srr, "_essential_playlists_enabled", lambda options: True)
        monkeypatch.setattr(srr, "_fetch_essential_featured_rows", lambda: [])

        done, rows = srr._close_artist_essential_section("Poppy", {}, set(), None)
        # First attempt failed → artist NOT marked done, so a later section can
        # retry (e.g. a skipped artist who later gets scanned in the same run).
        assert done == set()
        assert calls == ["Poppy"]


class TestFinaliseSkipsRunnerHandledArtists:
    """finalise_scan must not re-create collections the runner already wrote
    per-artist-section, but must still create collections for any artist it
    didn't cover."""

    def _run(self, monkeypatch, done_set, options=None):
        from services.popularity.stages import finalise_stage as fs
        import db.engine as db_engine
        from contextlib import contextmanager

        class _FakeResult:
            def fetchall(self):
                return []

            def fetchone(self):
                return None

        class _Session:
            def __init__(self):
                self.executed = []

            def execute(self, sql, params=None):
                self.executed.append((sql, params))
                return _FakeResult()

            def commit(self):
                pass

            def rollback(self):
                pass

        session = _Session()

        @contextmanager
        def _cm():
            yield session

        monkeypatch.setattr(db_engine, "db_session", _cm)

        created = []
        monkeypatch.setattr(
            fs, "_sync_essential_playlist",
            lambda artist, **kwargs: created.append((artist, kwargs.get("featured_rows"))),
        )
        monkeypatch.setattr(fs, "_essential_playlists_enabled", lambda opts: True)
        monkeypatch.setattr(fs, "_genre_playlists_active", lambda: False)
        monkeypatch.setattr(fs, "_new_music_playlist_enabled", lambda: False)
        monkeypatch.setattr(fs, "_sync_isrc_popularity", lambda: 0)
        monkeypatch.setattr(fs, "log_unified", lambda msg: None)
        monkeypatch.setattr(
            fs, "post_album_star_ratings",
            lambda **kw: {"star_ratings": 0, "navidrome_synced": 0},
        )

        results = [
            {
                "track_id": "t1",
                "artist": "Artist A",
                "album_artist": "Artist A",
                "album": "Album A",
                "title": "Song",
                "popularity_score": 60.0,
                "final_score": 60.0,
                "lastfm_listeners": 100,
                "listenbrainz_listens": 200,
                "lastfm_score": 4.0,
                "listenbrainz_score": 4.0,
                "is_single": False,
                "single_confidence": "low",
            },
            {
                "track_id": "t2",
                "artist": "Artist B",
                "album_artist": "Artist B",
                "album": "Album B",
                "title": "Song",
                "popularity_score": 70.0,
                "final_score": 70.0,
                "lastfm_listeners": 100,
                "listenbrainz_listens": 200,
                "lastfm_score": 4.0,
                "listenbrainz_score": 4.0,
                "is_single": False,
                "single_confidence": "low",
            },
        ]
        opts = dict(options or {})
        opts["_essential_playlists_done"] = done_set
        fs.finalise_scan(results=results, options=opts)
        return created

    def test_skips_runner_handled_artist(self, monkeypatch):
        created = self._run(monkeypatch, {"artist a"})

        # Artist A was already handled at the end of its scan section → finalise
        # skips it; Artist B (not covered by the runner) still gets created.
        assert [a for a, _ in created] == ["Artist B"]

    def test_creates_all_when_none_handled(self, monkeypatch):
        created = self._run(monkeypatch, set())

        assert [a for a, _ in created] == ["Artist A", "Artist B"]
