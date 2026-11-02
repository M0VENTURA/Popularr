"""Regression: the track-level genre aggregation must include
``navidrome_genres`` as a low-authority fallback source.

Previously the aggregation block only built ``source_map`` from the external
sources (MB/Discogs/Last.fm/LB/Spotify).  A track whose external columns were
empty (no MBID → no MB genres, no Discogs token, etc.) produced an empty
source_map, so ``genres`` was never updated during a metadata scan — the
album page then showed every genre attributed to "Navidrome" because only the
import-time ``navidrome_genres`` column had data.  Including navidrome in the
source map means the aggregated ``genres`` is always recomputed (navidrome
weight 0.30), and a real external genre outranks it (MB 0.40 / Discogs 0.25).
"""

from __future__ import annotations


class TestTrackGenreAggregationIncludesNavidrome:
    def test_navidrome_only_track_gets_genres(self, monkeypatch):
        """A track with ONLY navidrome_genres (no external source data) must
        still produce an aggregated genre set (not lose its genre field)."""
        import helpers.config_helpers as ch
        monkeypatch.setattr(ch, "get_config", lambda: {"genres": {"min_weight": 0.0}})

        from services.enrichment.genre_aggregation_service import aggregate_genres

        result = aggregate_genres(
            {"navidrome": ["Electronic", "Industrial", "Rock"]},
            max_genres=3,
        )
        assert "Electronic" in result or "electronic" in result
        assert len(result) >= 1

    def test_online_sources_win_when_they_produce_a_genre(self, monkeypatch):
        """Navidrome must not change an answer the online sources made."""
        import helpers.config_helpers as ch
        monkeypatch.setattr(ch, "get_config", lambda: {"genres": {"min_weight": 0.0}})

        from services.enrichment.genre_aggregation_service import aggregate_genres

        result = aggregate_genres(
            {
                "musicbrainz": ["nu metal"],
                "navidrome": ["Electronic", "Industrial"],
            },
            max_genres=5,
        )
        assert any(g.lower() == "nu metal" for g in result)
        assert not any(
            g.lower() in ("electronic", "industrial") for g in result
        ), "Navidrome must only vote when online evidence is empty"

    def test_navidrome_is_used_when_online_evidence_is_empty(self, monkeypatch):
        import helpers.config_helpers as ch
        monkeypatch.setattr(ch, "get_config", lambda: {"genres": {"min_weight": 0.0}})

        from services.enrichment.genre_aggregation_service import aggregate_genres

        result = aggregate_genres(
            {"musicbrainz": [], "navidrome": ["Electronic"]},
            max_genres=3,
        )
        assert any(g.lower() == "electronic" for g in result)

    def test_track_stage_builds_source_map_with_navidrome(self, monkeypatch):
        """The track_stage aggregation loop must treat navidrome_genres as a
        source (the exact regression)."""
        import inspect
        import re
        from services.popularity.stages import track_stage as ts

        src = inspect.getsource(ts)

        # ⚠️ The assertion allows an ``@entity`` SUFFIX on the source key. The
        # canonical selection engine (2026-10-15) weights evidence by entity
        # level, so the loop now declares ``"navidrome@track"`` rather than a
        # bare ``"navidrome"``. Pinning the exact literal would make a legitimate
        # change to the weighting model look like a regression — what this test
        # is about is that navidrome IS a source, not how it is labelled.
        assert re.search(
            r'"navidrome_genres"\s*,\s*"navidrome(?:@\w+)?"', src
        ), "navidrome_genres is no longer in the track-stage source map"
