"""A zero popularity source must not be frozen into a cached score.

Reported: a track ("The Infection") kept showing ``LB: 0.0`` on every
non-forced scan even though its Last.fm side was healthy.

Root cause: ``should_use_cached_score`` only checks the freshness TIMESTAMP,
and the cached branch's ``_has_credible_data`` accepts EITHER source
(``lf >= 25 or lb >= 25``). So a track with ``lastfm_listeners=21000`` and
``listenbrainz_listens=0`` passed both gates, reused its stale
``final_score``, and the zeroed ListenBrainz source was never re-queried —
only the FRESH path re-fetches zeroed sources.

``popularity_sources_are_complete`` is the missing half of the gate: the
cached path now declines unless BOTH sources carry a non-zero count, unless
the track is explicitly user-frozen (in which case the stored score is a
deliberate choice and must not be disturbed).

These tests pin the helper directly, the full reconstructed decision, and the
wiring in ``track_stage``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

from services.popularity.popularity_cache_policy import (  # noqa: E402
    popularity_sources_are_complete,
    should_use_cached_score,
)


def _fresh() -> dict:
    """A track with a just-written freshness stamp (the cache's only clock)."""
    return {"lastfm_last_updated": datetime.now(timezone.utc)}


def _cached_branch(track: dict, *, force: bool = False, frozen: bool = False) -> bool:
    """Mirror of the cached-branch expression in ``track_stage``.

    Written EXACTLY like the production rule (see
    ``TestTheWiringSurvives``) so the two cannot silently diverge. The
    behavioural tests below exercise this mirror against the real helpers.
    """
    credible = (
        int(track.get("lastfm_listeners") or 0) >= 25
        or int(track.get("listenbrainz_listens") or 0) >= 25
    )
    return (
        not force
        and (frozen or should_use_cached_score(track))
        and bool(track.get("final_score") and credible)
        and (frozen or popularity_sources_are_complete(track))
    )


# ===========================================================================
# 1. The helper
# ===========================================================================
class TestPopularitySourcesAreComplete:
    def test_both_sources_present_is_complete(self):
        assert popularity_sources_are_complete(
            {"lastfm_listeners": 21000, "listenbrainz_listens": 40}
        ) is True

    def test_zero_listenbrainz_is_incomplete(self):
        """THE REPORTED SHAPE — a healthy Last.fm side must not mask LB=0."""
        assert popularity_sources_are_complete(
            {"lastfm_listeners": 21000, "listenbrainz_listens": 0}
        ) is False

    def test_zero_lastfm_is_incomplete(self):
        assert popularity_sources_are_complete(
            {"lastfm_listeners": 0, "listenbrainz_listens": 5000}
        ) is False

    def test_both_zero_is_incomplete(self):
        assert popularity_sources_are_complete(
            {"lastfm_listeners": 0, "listenbrainz_listens": 0}
        ) is False

    def test_missing_keys_are_incomplete(self):
        assert popularity_sources_are_complete({}) is False

    def test_string_counts_are_parsed_like_the_scoring_path(self):
        assert popularity_sources_are_complete(
            {"lastfm_listeners": "120", "listenbrainz_listens": "7"}
        ) is True

    def test_none_count_is_incomplete(self):
        assert popularity_sources_are_complete(
            {"lastfm_listeners": None, "listenbrainz_listens": 9}
        ) is False

    def test_one_listener_is_present(self):
        # The gate is "present vs absent", NOT the >= 25 credibility floor.
        assert popularity_sources_are_complete(
            {"lastfm_listeners": 1, "listenbrainz_listens": 1}
        ) is True

    def test_garbage_counts_are_incomplete(self):
        assert popularity_sources_are_complete(
            {"lastfm_listeners": "many", "listenbrainz_listens": "lots"}
        ) is False


# ===========================================================================
# 2. The full decision
# ===========================================================================
class TestTheReconstructedDecision:
    def test_the_reported_track_falls_through_to_the_fresh_path(self):
        # The exact reported shape: fresh timestamp, credible Last.fm count,
        # a stored score, and LB frozen at zero.
        track = {
            **_fresh(),
            "lastfm_listeners": 21000,
            "listenbrainz_listens": 0,
            "final_score": 70.0,
        }

        assert should_use_cached_score(track) is True, (
            "the timestamp gate alone says 'fresh' — that is the trap this "
            "fix closes"
        )
        assert _cached_branch(track) is False, (
            "a track with LB=0 must NOT reuse the cache, or LB is never "
            "re-queried"
        )

    def test_a_complete_track_still_reuses_the_cache(self):
        track = {
            **_fresh(),
            "lastfm_listeners": 21000,
            "listenbrainz_listens": 400,
            "final_score": 70.0,
        }
        assert _cached_branch(track) is True

    def test_a_user_frozen_track_keeps_its_stored_score(self):
        # The escape hatch: an explicitly frozen track is a deliberate
        # choice, so even a zero source must not force a re-query.
        track = {
            **_fresh(),
            "lastfm_listeners": 21000,
            "listenbrainz_listens": 0,
            "final_score": 70.0,
        }
        assert _cached_branch(track, frozen=True) is True

    def test_a_forced_pass_ignores_the_cache_entirely(self):
        track = {
            **_fresh(),
            "lastfm_listeners": 21000,
            "listenbrainz_listens": 400,
            "final_score": 70.0,
        }
        assert _cached_branch(track, force=True) is False

    def test_no_stored_score_means_no_cache(self):
        track = {
            **_fresh(),
            "lastfm_listeners": 21000,
            "listenbrainz_listens": 400,
            "final_score": 0,
        }
        assert _cached_branch(track) is False

    def test_neither_source_credible_means_no_cache(self):
        track = {
            **_fresh(),
            "lastfm_listeners": 20,
            "listenbrainz_listens": 20,
            "final_score": 70.0,
        }
        assert _cached_branch(track) is False


# ===========================================================================
# 3. Wiring — the production branch keeps the rule
# ===========================================================================
class TestTheWiringSurvives:
    def test_track_stage_imports_the_completeness_helper(self):
        src = (
            REPO_ROOT / "services" / "popularity" / "stages" / "track_stage.py"
        ).read_text(encoding="utf-8")
        assert "popularity_sources_are_complete," in src

    def test_track_stage_uses_the_helper_in_the_cached_branch(self):
        src = (
            REPO_ROOT / "services" / "popularity" / "stages" / "track_stage.py"
        ).read_text(encoding="utf-8")
        assert "popularity_sources_are_complete(effective_track)" in src
        assert "(frozen_track or _sources_complete)" in src, (
            "the cached branch dropped the zero-source requirement — a "
            "source stuck at 0 would freeze again"
        )
