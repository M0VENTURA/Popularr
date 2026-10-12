"""Starless-but-scored albums self-heal on the next non-forced scan.

A run interrupted between per-album score persistence and the DEFERRED
artist-level star flush (``_flush_artist_star_ratings`` runs at the artist
boundary, not per album) leaves every track of that artist's completed albums
SCORED but with NULL stars. The skip gate then skips those albums on the next
non-forced scan ("no changes detected" — all tracks already carry
``final_score``), so their stars stay NULL forever. Reported: *"when an
artist was rescanned with a non forced scan later on, it runs the finalize
pass on it and no tracks are getting 5 star in the scan."*

This suite pins the self-heal: ``_album_scored_but_never_rated`` recognises
the interrupted signature, and the skip gate forces those albums through on
non-metadata passes (stored scores reused, no re-score, no forced metadata)
so ``_post_album_stars`` finally assigns them.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.popularity import scan_stage_runner as runner  # noqa: E402



def _track(*, score: float = 50.0, stars: int = 0, excluded: bool = False,
           live: bool = False) -> dict:
    return {
        "title": "Track",
        "final_score": score,
        "stars": stars,
        "exclude_from_stats": excluded,
        "is_live": live,
    }


class TestAlbumScoredButNeverRated:

    def test_scored_but_starless_is_the_interrupted_signature(self):
        assert runner._album_scored_but_never_rated([
            _track(score=40.0), _track(score=60.0),
        ]) is True, (
            "the signature of a run interrupted before the star flush: all "
            "tracks scored, none rated"
        )

    def test_a_rated_album_is_untouched(self):
        assert runner._album_scored_but_never_rated([
            _track(score=40.0, stars=3), _track(score=60.0, stars=5),
        ]) is False, "an album that already has stars must not re-run"

    def test_a_partially_rated_album_is_untouched(self):
        # One track rated, one not — a different story; never force it.
        assert runner._album_scored_but_never_rated([
            _track(score=40.0, stars=4), _track(score=60.0, stars=0),
        ]) is False

    def test_an_unscored_album_is_untouched(self):
        assert runner._album_scored_but_never_rated([
            _track(score=0.0), _track(score=0.0),
        ]) is False, "an album never scored is not 'waiting for a flush'"

    def test_an_all_excluded_album_is_untouched(self):
        assert runner._album_scored_but_never_rated([
            _track(score=50.0, excluded=True), _track(score=50.0, excluded=True),
        ]) is False, "excluded tracks are ignored on both sides — never force it forever"

    def test_empty_album_is_untouched(self):
        assert runner._album_scored_but_never_rated([]) is False
        assert runner._album_scored_but_never_rated(None) is False

    def test_live_album_can_heal(self):
        # A live album stopped before the flush is also starless; it must be
        # forced through (its stars are just capped at the live max).
        assert runner._album_scored_but_never_rated([
            _track(score=60.0, live=True), _track(score=55.0, live=True),
        ]) is True


class TestTheSkipGateHealsStarlessAlbums:
    """Wiring — the skip gate must force starless albums through on passes
    that RATE, and keep skipping them on metadata-only passes."""

    def test_the_starless_heal_is_in_the_skip_gate(self):
        source = (REPO_ROOT / "services" / "popularity" / "scan_stage_runner.py").read_text(encoding="utf-8")
        assert "_album_scored_but_never_rated(tracks)" in source
        assert "scored but never rated " in source
        assert "(no stars) — running it to assign them" in source
        # And it must NEVER force a metadata-only pass (metadata passes never
        # rate, so forcing them would re-run the albums forever).
        assert 'not options.get("metadata_only") and _album_scored_but_never_rated(tracks)' in source

    def test_the_heal_does_not_set_force_metadata(self):
        # Forcing metadata would re-run the whole metadata pipeline; a
        # starless album only needs stored scores re-posted. The heal must
        # NOT set ``force_metadata_for_this_album`` — only the completeness
        # override (a genuinely metadata-incomplete album) may.
        source = (REPO_ROOT / "services" / "popularity" / "scan_stage_runner.py").read_text(encoding="utf-8")
        # The heal CONDITION is the last thing before the skip; it clears the
        # skip but does not touch force_metadata_for_this_album afterwards.
        cond = source.index("if skip_album and not options.get(\"metadata_only\") and _album_scored_but_never_rated(tracks):")
        window = source[cond: cond + 500]
        assert "force_metadata_for_this_album" not in window

    def test_starless_albums_reach_the_star_phase_with_stored_scores(self):
        # process_track's CACHED path (a normal non-forced pass) reuses the
        # stored final_score + raw_score — the anti-erosion contract that makes
        # the heal cheap and correct. Pin the contract so the heal can rely on it.
        import services.popularity.stages.track_stage as ts
        src = Path(ts.__file__).read_text(encoding="utf-8")
        assert "should_use_cached_score" in src
        assert "_stored_raw = float(effective_track.get(\"raw_score\") or 0)" in src