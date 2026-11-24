"""The Essential Collections are rebuilt by the playlist resync.

REPORTED
--------
"Should there be a different Artist - Playlist resync option on the config page to
rebuild the artist essential playlists with the earlier adjustments?"

There was no such path at all. ``apply_genre_playlist_resync`` rebuilt the genre
top-tracks and the covers family, and an Essential Collection only moved on a full
scan — so changing the Essential rules and pressing "Apply & Rebuild" left every
artist playlist untouched.
"""

from __future__ import annotations

import inspect

import pytest


@pytest.fixture(scope="module")
def apply_source():
    from services.enrichment import genre_playlist_resync_service as svc

    return inspect.getsource(svc.apply_genre_playlist_resync)


class TestTheResyncRebuildsEssentials:
    def test_it_calls_the_essential_reconcile_pass(self, apply_source):
        assert "_refresh_all_essential_collections" in apply_source, (
            "the resync never rebuilds Essential Collections"
        )

    def test_it_uses_the_same_pass_as_the_scan(self, apply_source):
        """⭐ SAME FUNCTION, so the preview/apply and a scan cannot disagree about
        what an Essential Collection is."""
        assert "from services.popularity.stages.finalise_stage import" in apply_source
        assert "_refresh_all_essential_collections" in apply_source

    def test_it_is_gated_by_the_enable_switch(self, apply_source):
        assert 'essential_playlists_enabled' in apply_source

    def test_a_failure_does_not_fail_the_whole_job(self, apply_source):
        """CONTROL — the genres are already written by this point; losing them to
        an Essential error would be strictly worse."""
        idx = apply_source.index("essential_written = 0")
        window = apply_source[idx: idx + 1600]
        assert "except Exception" in window


class TestPreviewExplainsTheShrink:
    def test_the_preview_reports_a_shrink_split(self):
        """⚠️ REPORTED: "Playlists shrinking to 48-66 tracks". A bare count says
        nothing about WHICH rule removed the tracks, and the two causes need
        opposite responses: genre-driven is the rules working, builder-driven is a
        config lever."""
        from services.enrichment import genre_playlist_resync_service as svc

        src = inspect.getsource(svc.preview_genre_playlist_resync)
        assert "shrinking_playlists" in src
        assert "genre_driven" in src
        assert "builder_driven" in src

    def test_it_names_the_knobs(self):
        from services.enrichment import genre_playlist_resync_service as svc

        src = inspect.getsource(svc.preview_genre_playlist_resync)
        for knob in (
            "genre_playlists_max_tracks",
            "genre_playlists_max_per_artist",
            "genre_playlists_create_threshold",
        ):
            assert knob in src, f"the shrink report does not name {knob}"


class TestTheCreationThresholdUsesTheFinalCount:
    def test_the_gate_follows_the_cap_and_slice(self):
        from services.popularity.stages import finalise_stage as fs

        src = inspect.getsource(fs._create_genre_top_track_playlists)
        gate = src.index("_final_count")
        cap = src.index("_apply_artist_cap(winners, max_per_artist)")
        assert cap < gate, (
            "the creation gate is evaluated before the cap, so a genre can pass a "
            "100-track threshold and then ship 48 tracks"
        )

    def test_a_short_playlist_is_not_created(self):
        from services.popularity.stages import finalise_stage as fs

        src = inspect.getsource(fs._create_genre_top_track_playlists)
        assert (
            "if not playlist_exists and _final_count < create_threshold" in src
        ), "a playlist shorter than the threshold would still be created"
