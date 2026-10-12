"""The per-album scan budget scales with the album's track count.

Reported (2026-10-12): scan albums with no MusicBrainz match "fail at the end
before hitting the finalise stage". The log:

    [SCAN] section abandoned (timeout) section='album_track_phase'
        elapsed_s=900.001 budget_seconds=900.0 artist='Various Artists'
        album='Alternative 90s'
    [POPULARITY] Album '...' skipped (exceeded 900.0s budget)
    Popularity Scan - no tracks processed: 1 album(s) exceeded their
        per-album time budget
    [FINALISE_STAGE] Finalising scan — 0 tracks processed

An 80-track compilation with no release-group MBID does a FULL per-track
online lookup (MusicBrainz 1 req/s + Last.fm + Discogs + per-track cover
detection), so it needs ~20 minutes of wall-clock. It was cut at 15 min
*mid-progress* (still logging ``[TRACK]`` lines) by the flat 900 s cap, and
the runner then discarded every finished track's star rating — "0 tracks
processed", no stars for the album.

Fix: the configured ``album_timeout_seconds`` is the FLOOR of the track-phase
budget; albums whose ``track_count * 20s`` exceeds it get the scaled
allowance. This keeps the budget a *hung-call* safety net instead of an
accidental size limit. ``0`` (disabled) and the enrich/post-singles phases
are unchanged.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

SCANNER = REPO_ROOT / "services" / "popularity" / "scan_stage_runner.py"


def _source() -> str:
    return SCANNER.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. The pure scaling helper
# ---------------------------------------------------------------------------

class TestTheBudgetScalesWithTrackCount:

    def test_small_album_keeps_the_base_budget(self):
        import services.popularity.scan_stage_runner as runner

        # 20 tracks * 20s = 400s < 900s floor → the configured value stands.
        assert runner._scale_album_budget(900.0, 20) == 900.0

    def test_empty_album_keeps_the_base_budget(self):
        import services.popularity.scan_stage_runner as runner

        assert runner._scale_album_budget(900.0, 0) == 900.0

    def test_large_album_scales_up(self):
        import services.popularity.scan_stage_runner as runner

        # THE reported album: 80 tracks → 1600s > 900s.
        assert runner._scale_album_budget(900.0, 80) == 1600.0

    def test_the_configured_value_is_the_floor(self):
        import services.popularity.scan_stage_runner as runner

        # A low configured budget still yields the scaled allowance for a
        # large album (the floor never drops below the album's need).
        assert runner._scale_album_budget(300.0, 80) == 1600.0
        # ...but a small album keeps the low configured value.
        assert runner._scale_album_budget(300.0, 5) == 300.0

    def test_disabled_budget_passes_through(self):
        import services.popularity.scan_stage_runner as runner

        assert runner._scale_album_budget(None, 80) is None, (
            "album_timeout_seconds=0 must keep meaning 'never skip'"
        )

    def test_scaling_is_monotonic_in_track_count(self):
        import services.popularity.scan_stage_runner as runner

        values = [runner._scale_album_budget(900.0, n) for n in (1, 10, 45, 46, 80, 200)]
        assert values == sorted(values), "a bigger album must never get less budget"

    def test_boundary_track_count(self):
        import services.popularity.scan_stage_runner as runner

        # 45 * 20 = 900 == floor → base; 46 * 20 = 920 → scaled.
        assert runner._scale_album_budget(900.0, 45) == 900.0
        assert runner._scale_album_budget(900.0, 46) == 920.0


# ---------------------------------------------------------------------------
# 2. The wiring — only the track phase scales
# ---------------------------------------------------------------------------

class TestTheTrackPhaseUsesTheScaledBudget:

    def test_the_track_phase_call_scales(self):
        source = _source()
        assert "seconds=_scale_album_budget(_album_budget, len(_track_jobs))" in source, (
            "the track phase must scale its budget by the album's track count"
        )

    def test_the_enrich_phase_keeps_the_base_budget(self):
        source = _source()
        idx = source.index('section="enrich_album"')
        window = source[idx: idx + 400]
        assert "seconds=_album_budget," in window, (
            "enrich_album is fast — it must keep the configured base budget"
        )
        assert "seconds=_scale_album_budget(" not in window, (
            "enrich_album must NOT scale (its cost is not per-track)"
        )

    def test_the_budget_is_still_resolved_once_per_scan(self):
        # Existing pin in test_album_scan_budget — restated so this suite
        # fails loudly if the single resolution is ever duplicated.
        assert "_album_budget = _resolve_album_budget()" in _source()

    def test_all_bounded_album_phases_still_pass_seconds(self):
        source = _source()
        calls = [
            call
            for call in re.findall(
                r"(?<!def )_bounded_album_phase\((?:[^()]|\([^()]*\))*?\)",
                source,
                re.S,
            )
            if "Callable[..., T]" not in call
        ]
        assert calls, "no bounded album phase CALLS found"
        for call in calls:
            assert "seconds=" in call, f"a bounded album phase call lacks seconds=:\n{call[:200]}"

    def test_the_per_track_constant_is_documented(self):
        source = _source()
        assert "_ALBUM_BUDGET_PER_TRACK_SECONDS = 20.0" in source
        # The scaling factor must be referenced by the helper (not an orphan
        # constant that the math accidentally ignores).
        assert "_ALBUM_BUDGET_PER_TRACK_SECONDS" in source.split("def _scale_album_budget")[1]


# ---------------------------------------------------------------------------
# 3. The end-to-end effect on the reported album
# ---------------------------------------------------------------------------

class TestTheReportedAlbumWouldNoLongerBeCut:

    def test_an_80_track_album_gets_more_than_the_flat_900s(self):
        import services.popularity.scan_stage_runner as runner

        scaled = runner._scale_album_budget(runner._resolve_album_budget(), 80)
        assert scaled is not None and scaled > 900.0, (
            "the reported 80-track no-MBID album must get more than the flat "
            "900s that cut it mid-progress"
        )

    def test_a_genuinely_stuck_album_is_still_bounded(self):
        import services.popularity.scan_stage_runner as runner

        # Even a large album has a finite ceiling — the budget still bounds a
        # HANG, it just no longer doubles as a size limit.
        assert runner._scale_album_budget(900.0, 80) == 1600.0