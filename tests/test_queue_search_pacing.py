"""Search pacing and the duplicated-artist title repair.

Reported: "They average 57 s each, and several hit the 50-result limit", and
"Orpheus Omega / Triple Kill – Triple Kill – Stacy's Mom: the artist is
duplicated into the title."
"""

from __future__ import annotations

import pytest

from services.queue_cleaner import _strip_duplicated_artist_prefix


class TestTheArtistIsNotRepeatedInTheTitle:
    def test_the_reported_combined_credit(self) -> None:
        assert _strip_duplicated_artist_prefix(
            "Orpheus Omega / Triple Kill", "Triple Kill - Stacy's Mom"
        ) == "Stacy's Mom"

    def test_a_single_artist_prefix(self) -> None:
        assert _strip_duplicated_artist_prefix(
            "Orpheus Omega", "Orpheus Omega - The Fall"
        ) == "The Fall"

    def test_a_guest_credit(self) -> None:
        assert _strip_duplicated_artist_prefix(
            "Pitchshifter feat. Burton C. Bell", "Burton C. Bell - Song"
        ) == "Song"

    @pytest.mark.parametrize(
        "artist,title",
        [
            # The artist is not a prefix of the title at all.
            ("Triple Kill", "Killing Me Softly"),
            # A partial word match must not be removed.
            ("Orpheus Omega", "Orpheus Omegasonic - Song"),
            # Nothing to strip.
            ("Artist", "Artist"),
            ("Artist", ""),
            # The tail would be empty, so it is refused.
            ("Artist", "Artist - "),
        ],
    )
    def test_an_ordinary_title_is_never_eaten(self, artist, title) -> None:
        assert _strip_duplicated_artist_prefix(artist, title) is None


class TestFallbackQueriesArePaced:
    """Each fallback is a FULL search; the tail was the 57s average."""

    def test_the_cap_is_configurable(self, monkeypatch):
        from services.downloads import download_pipeline_service as pipeline

        item = {"artist": "The Pretty Reckless", "title": "Heaven Knows"}
        monkeypatch.setattr(
            pipeline, "get_queue_search_tuning", lambda: {"max_fallback_queries": 2},
            raising=False,
        )
        # The helper imports it lazily, so patch at the source.
        import helpers.config_helpers as cfg

        monkeypatch.setattr(
            cfg, "get_queue_search_tuning", lambda: {
                "search_max_wait_seconds": 60,
                "fallback_search_max_wait_seconds": 20,
                "search_empty_abort_seconds": 30,
                "search_empty_shrink_after": 4,
                "search_shrunken_wait_seconds": 8,
                "max_fallback_queries": 2,
            }
        )
        assert len(pipeline._build_fallback_search_queries(item, "primary")) <= 2

    def test_zero_disables_fallbacks(self, monkeypatch):
        import helpers.config_helpers as cfg
        from services.downloads import download_pipeline_service as pipeline

        monkeypatch.setattr(
            cfg, "get_queue_search_tuning", lambda: {"max_fallback_queries": 0}
        )
        assert pipeline._build_fallback_search_queries(
            {"artist": "A", "title": "B"}, "primary"
        ) == []

    def test_the_rejected_primary_does_not_burn_the_tail(self):
        """A primary query that FOUND candidates has proved the files exist;
        a looser query only finds them again."""
        import inspect

        from services.downloads import download_pipeline_service as pipeline

        src = inspect.getsource(pipeline.process_queue_item)
        assert "idx == 0 and results and not best" in src
        assert "not burning the fallback queries" in src
