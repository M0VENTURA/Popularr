"""Scan-scope and curated-batch speed fixes.

Covers:

* ``load_candidates`` builds the artist→album→track structure from ONE bulk
  query and honours an explicit artist scope (letter scans), instead of an
  N+1 of ``get_all_artists`` + per-album ``get_tracks_for_album``.
* an EMPTY artist scope never falls through to "the whole library".
* ``_run_curated_playlists`` fetches the curated rows ONCE and derives the 4★
  pool from the 3★ superset, instead of running one full-library query per
  builder.
"""

from __future__ import annotations

from typing import Any

from db.repositories import library as library_repo
from services.popularity.stages import finalise_stage as fs
from services.popularity.stages import load_stage


def _row(artist: str, album: str, title: str, *, stars: int = 5) -> dict[str, Any]:
    return {
        "artist": artist,
        "album_artist": artist,
        "album": album,
        "title": title,
        "stars": stars,
        "musicbrainz_albumtype": "album",
        "spotify_album_type": None,
    }


class TestBulkCandidateLoading:
    def _patch(self, monkeypatch, rows):
        calls: list[dict[str, Any]] = []

        def fake_get_scan_tracks(**kwargs):
            calls.append(kwargs)
            return rows

        monkeypatch.setattr(load_stage, "get_scan_tracks", fake_get_scan_tracks)
        return calls

    def test_one_query_groups_every_artist_and_album(self, monkeypatch):
        rows = [
            _row("A", "One", "a1"),
            _row("A", "One", "a2"),
            _row("B", "Two", "b1"),
        ]
        calls = self._patch(monkeypatch, rows)

        candidates = load_stage.load_candidates({})

        assert calls == [{}], "the loader must run ONE query, not one per album"
        assert [(c["artist"], c["album"], len(c["tracks"])) for c in candidates] == [
            ("A", "One", 2),
            ("B", "Two", 1),
        ]

    def test_an_explicit_artist_scope_is_passed_as_one_query(self, monkeypatch):
        rows = [_row("A", "One", "a1"), _row("B", "Two", "b1")]
        calls = self._patch(monkeypatch, rows)

        candidates = load_stage.load_candidates({"artists": ["A", "B"]})

        assert calls == [{"artists": ["A", "B"]}]
        assert [c["album"] for c in candidates] == ["One", "Two"]

    def test_an_empty_scope_never_means_the_whole_library(self, monkeypatch):
        """The repository short-circuits before opening a session."""
        assert library_repo.get_scan_tracks(artists=[]) == []

    def test_resume_from_is_pushed_into_the_query(self, monkeypatch):
        """A resumed scan must not LOAD the artists it will skip."""
        rows = [
            _row("Alpha", "One", "a1"),
            _row("Beta", "Two", "b1"),
            _row("Gamma", "Three", "g1"),
        ]
        calls = self._patch(monkeypatch, rows)
        monkeypatch.setattr(
            load_stage, "get_all_artists", lambda: ["Alpha", "Beta", "Gamma"]
        )

        candidates = load_stage.load_candidates({"resume_from": "Beta"})

        assert calls == [{"artists": ["Beta", "Gamma"]}], (
            "the resume point must scope the bulk query, not just the loop"
        )
        assert [c["artist"] for c in candidates] == ["Beta", "Gamma"]

    def test_album_filter_uses_the_shared_tolerant_match(self, monkeypatch):
        rows = [_row("A", "One", "a1"), _row("A", "Two", "a2")]
        calls = self._patch(monkeypatch, rows)

        candidates = load_stage.load_candidates(
            {"artist_filter": "A", "album_filter": "one"}
        )

        assert calls == [{"artist": "A"}]
        assert [c["album"] for c in candidates] == ["One"]


class TestScanScopeWiring:
    """Pin the routing fixes that are one line away from silent regression."""

    def _read(self, relative: str) -> str:
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        return (root / relative).read_text(encoding="utf-8")

    def test_track_rescan_is_album_scoped_and_forced(self) -> None:
        src = self._read("routes/scan_routes/artist_album.py")
        start = src.index("async def scan_track_rescan")
        end = src.find("\n@scans_bp.route", start + 10)
        body = src[start: end if end != -1 else len(src)]

        assert "guard_and_launch_popularity_scan(" in body
        assert "run_album_pipeline" in body
        assert "run_async(run_artist_pipeline" not in body, (
            "one track must not drag the whole artist through the pipeline"
        )
        assert "force=True" in body, (
            "a non-forced rescan can be skipped by the album's freshness window"
        )

    def test_letter_scan_passes_the_letters_artist_list(self) -> None:
        src = self._read("routes/scan_routes/popularity.py")

        assert "_resolve_letter_artists" in src
        assert "artists=letter_artists or None" in src, (
            "the letter must be an explicit artist scope, not a resume point "
            "that runs to the end of the library"
        )

    def test_full_scan_verifies_navidrome_freshness_once(self) -> None:
        src = self._read("services/scanning/pipelines/popularity_pipeline.py")

        assert "ensure_navidrome_import_freshness" in src
        assert "freshness_verified=_freshness_verified" in src

    def test_essentia_pass_honours_the_feature_flag(self) -> None:
        src = self._read("services/scanning/pipeline.py")

        window = src[src.index('get_feature("run_essentia"'):]
        window = window[: window.index("save_artist_scan_checkpoint")]
        assert "if _essentia_enabled:" in window, (
            "the artist pipeline must not run Essentia when the feature is off"
        )


class TestCuratedRowsAreFetchedOnce:
    def test_one_row_query_feeds_every_builder(self, monkeypatch):
        import helpers.config_helpers as ch

        monkeypatch.setattr(
            ch,
            "get_playlists_config",
            lambda: {
                "decade_playlists_enabled": True,
                "hit_singles_playlist_enabled": True,
                "hidden_gems_playlist_enabled": True,
                "new_releases_playlist_enabled": True,
                "covers_playlist_enabled": True,
                "acoustic_playlist_enabled": True,
                "high_energy_playlist_enabled": True,
                "mood_playlists_enabled": True,
            },
        )
        monkeypatch.setattr(fs, "_navidrome_clients", lambda: [])

        fetches: list[int] = []

        def fake_fetch(min_stars: int = 4):
            fetches.append(min_stars)
            return [
                {"id": "mature", "stars": 5, "title": "T", "is_single": 1},
                {"id": "cover", "stars": 3, "title": "C", "is_cover": 1},
            ]

        monkeypatch.setattr(fs, "_fetch_curated_playlist_rows", fake_fetch)

        seen: dict[str, Any] = {}

        def _fake_builder(name):
            def _build(cfg, rows=None, *, playlist_listing=None):
                seen[name] = (rows, playlist_listing)
                return True

            return _build

        builders = (
            "_create_decade_playlists",
            "_create_hit_singles_playlist",
            "_create_hidden_gems_playlist",
            "_create_new_releases_playlist",
            "_create_covers_playlist",
            "_create_acoustic_playlist",
            "_create_high_energy_playlist",
            "_create_mood_playlists",
        )
        for name in builders:
            monkeypatch.setattr(fs, name, _fake_builder(name))

        fs._run_curated_playlists()

        assert fetches == [3], (
            "every builder must share ONE 3★ query; the 4★ pool is derived"
        )
        assert set(seen) == set(builders)
        mature, _listing = seen["_create_hit_singles_playlist"]
        covers, _listing = seen["_create_covers_playlist"]
        assert [r["id"] for r in mature] == ["mature"]
        assert [r["id"] for r in covers] == ["mature", "cover"]
