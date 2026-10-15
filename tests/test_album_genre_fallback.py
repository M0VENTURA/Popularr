"""Tests for the album top-genres fallback.

Requirement: "If tracks don't have 3 genres on them it should use the top
Genres from the album."  A sparse track (few/no tags of its own) inherits
the album's top genres so every track carries a full genre identity.
"""

from __future__ import annotations

import pytest


class TestAlbumTopGenres:
    def _helper(self):
        from services.popularity.stages.track_stage import _album_top_genres
        return _album_top_genres

    def test_empty_album_returns_empty(self, monkeypatch):
        assert self._helper()(None) == []
        assert self._helper()([]) == []

    def test_aggregates_sibling_genre_columns(self, monkeypatch):
        from helpers import config_helpers
        monkeypatch.setattr(config_helpers, "get_config", lambda: {"genres": {"min_weight": 0.0}})

        album_tracks = [
            {
                "title": "Song A",
                "musicbrainz_genres": '["nu metal", "alternative metal"]',
                "lastfm_tags": '["nu-metal"]',
                "discogs_genres": '["NuMetal"]',
            },
            {
                "title": "Song B",
                "musicbrainz_genres": '["alternative metal"]',
                "lastfm_tags": '["alternative metal"]',
            },
        ]
        result = self._helper()(album_tracks, max_genres=3)
        # nu metal (mb 0.40 + lf 0.10 + discogs 0.25 = 0.75) ranks above
        # alternative metal (mb 0.40 + mb 0.40 + lf 0.10 = 0.90) — wait:
        # alternative metal appears in TWO mb rows → 0.80 + lf 0.10 = 0.90.
        #
        # ⚠️ Compared case-insensitively: the canonical engine writes the
        # vocabulary's display name ("Alternative Metal") where the legacy engine
        # echoed the source's spelling. Every consumer lowercases before
        # comparing, so the case is cosmetic.
        lowered = {g.casefold() for g in result}
        assert "alternative metal" in lowered
        assert "nu metal" in lowered

    def test_sparse_sibling_still_contributes(self, monkeypatch):
        from helpers import config_helpers
        monkeypatch.setattr(config_helpers, "get_config", lambda: {"genres": {"min_weight": 0.0}})

        album_tracks = [
            {"title": "A", "musicbrainz_genres": '["rock"]'},
            {"title": "B", "navidrome_genres": "Metal"},
        ]
        result = self._helper()(album_tracks, max_genres=3)
        assert "rock" in result
        assert "Metal".lower() in {g.lower() for g in result}

    def test_junk_siblings_filtered(self, monkeypatch):
        from helpers import config_helpers
        monkeypatch.setattr(config_helpers, "get_config", lambda: {"genres": {"min_weight": 0.0}})

        album_tracks = [
            {"title": "A", "lastfm_tags": '["2014", "beautiful", "nu metal"]'},
        ]
        result = self._helper()(album_tracks, max_genres=3)
        assert "nu metal" in result
        assert "2014" not in result
        assert "beautiful" not in result

    def test_dict_tag_entries_parsed(self, monkeypatch):
        from helpers import config_helpers
        monkeypatch.setattr(config_helpers, "get_config", lambda: {"genres": {"min_weight": 0.0}})

        album_tracks = [
            {"title": "A", "musicbrainz_genres": '[{"name": "progressive metal", "count": 2}]'},
        ]
        result = self._helper()(album_tracks, max_genres=3)
        # Case-insensitive: the canonical engine writes the vocabulary's display
        # name. What this test is about is that a DICT-shaped tag entry is parsed
        # at all, not the spelling it resolves to.
        assert [g.casefold() for g in result] == ["progressive metal"]

    def test_essentia_genres_do_not_vote(self, monkeypatch):
        """Essentia is a mood provider — its text tags must NEVER vote in
        genre consensus (blueprint Phase 1: isolate Essentia)."""
        from helpers import config_helpers
        monkeypatch.setattr(config_helpers, "get_config", lambda: {"genres": {"min_weight": 0.0}})

        album_tracks = [
            {
                "title": "A",
                "essentia_genres": '["Nu Metal", "Korn", "Rock---Heavy Metal"]',
            },
        ]
        result = self._helper()(album_tracks, max_genres=3)
        assert result == []  # essentia_genres alone yields no genres

    def test_admin_siblings_filtered(self, monkeypatch):
        from helpers import config_helpers
        monkeypatch.setattr(config_helpers, "get_config", lambda: {"genres": {"min_weight": 0.0}})

        album_tracks = [
            {"title": "A", "musicbrainz_genres": '["cover", "live", "nu metal"]'},
        ]
        result = self._helper()(album_tracks, max_genres=3)
        assert "nu metal" in result
        assert "cover" not in result
        assert "live" not in result


class TestArtistDominantGenres:
    def _helper(self):
        from services.popularity.stages.track_stage import _artist_dominant_genres
        return _artist_dominant_genres

    def test_aggregates_artist_catalog(self, monkeypatch):
        """Artist-level safety net: inherit the artist's dominant genres."""
        from helpers import config_helpers
        monkeypatch.setattr(config_helpers, "get_config", lambda: {"genres": {"min_weight": 0.0}})

        from services.popularity.stages import track_stage as ts
        import db.engine as db_engine

        class _Result:
            def fetchall(self):
                return [
                    {"musicbrainz_genres": '["nu metal"]', "discogs_genres": '["NuMetal"]',
                     "lastfm_tags": '["nu-metal"]', "listenbrainz_genres": None,
                     "spotify_genres": None, "navidrome_genres": None},
                    {"musicbrainz_genres": '["nu metal"]', "discogs_genres": None,
                     "lastfm_tags": None, "listenbrainz_genres": None,
                     "spotify_genres": None, "navidrome_genres": None},
                ]

        class _Session:
            def execute(self, sql, params=None):
                return _Result()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(db_engine, "db_session", lambda: _Session())
        result = ts._artist_dominant_genres("Korn")
        assert "nu metal" in result
