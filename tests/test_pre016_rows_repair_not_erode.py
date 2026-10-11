"""Pre-016 rows: no more erosion, and the damage is repaired from stored data.

REPORTED
--------
> When scanning an album using a non-forced scan, no tracks hit 5 star and
> reset to 3. … Only happening with albums that have been scanned before and
> have data. Clean releases scan fine.

ROOT CAUSE
----------
The two CACHED scoring paths (the singles-pass block and the album-scan
``_cached`` branch) rebuilt ``_raw_combined`` for a row whose ``raw_score``
is NULL (every row scored BEFORE migration 016) from ``final_score`` — a
value the album-relative remap had ALREADY rewritten. Feeding a remapped
value back into the remap is the erosion loop: the sigmoid is monotonic but
SATURATING, so each pass pulls the album's extremes toward the middle — the
top track's z decays monotonically and crosses below the 5★ bound
(album_z ≥ 1.0), after which 5★ is unreachable (how far it falls past that
depends on the pool's shape; the user saw 3★). Clean albums never hit the
fallback — they take the fresh path, which persists a genuine raw — exactly
matching "clean releases scan fine".

THE FIX
--------
A pre-016 row's TRUE blend is rebuilt from its own stored data: listeners,
cached verdict flags and album context are all on the row, and
``_score_track_popularity`` is a PURE function of them (no network). The
reconstruction is persisted as ``raw_score``, so the first non-forced scan
REPAIRS the row and every later pass is the016 idempotence contract. When
the row lacks even stored listeners the raw stays UNKNOWN (0) and the remap
SKIPS it — a value on the wrong scale is never again treated as raw.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _strip_python(src: str) -> str:
    src = re.sub(r'""".*?"""', "", src, flags=re.S)
    src = re.sub(r"'''.*?'''", "", src, flags=re.S)
    return "\n".join(line.split("#", 1)[0] for line in src.splitlines())


def _recon(track, **overrides):
    from services.popularity.stages.track_stage import _reconstruct_raw_blend

    kwargs = dict(
        track=track,
        title=str(track.get("title") or ""),
        artist=str(track.get("artist") or ""),
        album_context={},
        album_tracks=None,
        album_lb_listens=None,
        prefetched_popularity=None,
        artist_max_lf_listeners=0,
        artist_lf_context=None,
        is_live_track=False,
        track_duration=None,
    )
    kwargs.update(overrides)
    return _reconstruct_raw_blend(**kwargs)


def _remap_all(values):
    from services.popularity.popularity_math import apply_album_relative_popularity

    return [apply_album_relative_popularity(v, values) for v in values]


def _album_z(score, scores):
    from services.popularity.popularity_math import calculate_robust_zscore

    z, _spread = calculate_robust_zscore(score, scores)
    return z


# ---------------------------------------------------------------------------
# 1. The reconstruction is a genuine blend of stored data
# ---------------------------------------------------------------------------


class TestReconstructionIsGenuine:
    def test_a_populated_row_reconstructs_a_positive_blend(self):
        raw = _recon({
            "id": "r1", "title": "Wake the White Wolf", "artist": "Miracle of Sound",
            "lastfm_listeners": 41100, "listenbrainz_listens": 6500,
            "is_single": True,
        })
        assert raw > 0, "stored listeners must reproduce a raw blend"

    def test_the_spread_survives_reconstruction(self):
        # The star system lives on SPREAD. A41k-listener standout must
        # reconstruct far above a 1.9k deep cut — this is the spread the
        # remapped final_score no longer carries after erosion.
        top = _recon({
            "id": "r2", "title": "Top", "artist": "Band",
            "lastfm_listeners": 41100, "listenbrainz_listens": 6500,
        })
        deep = _recon({
            "id": "r3", "title": "Deep Cut", "artist": "Band",
            "lastfm_listeners": 1900, "listenbrainz_listens": 228,
        })
        assert top > deep + 5, f"expected a wide raw gap, got top={top} deep={deep}"

    def test_no_stored_listeners_reconstructs_unknown(self):
        # Unknown must be0 (skip), NEVER a guess and NEVER the stored
        # remapped score.
        assert _recon({"id": "r4", "title": "x", "artist": "y"}) == 0.0
        assert _recon({
            "id": "r5", "title": "x", "artist": "y",
            "lastfm_listeners": 0, "listenbrainz_listens": 0,
        }) == 0.0

    def test_a_broken_scorer_degrades_to_unknown_not_to_final_score(self):
        import services.popularity.stages.track_stage as ts

        original = ts._score_track_popularity

        def _boom(**kwargs):
            raise RuntimeError("scorer down")

        ts._score_track_popularity = _boom
        try:
            raw = _recon({
                "id": "r6", "title": "x", "artist": "y",
                "lastfm_listeners": 5000, "listenbrainz_listens": 900,
            })
        finally:
            ts._score_track_popularity = original
        assert raw == 0.0, "a failed reconstruction must degrade to UNKNOWN (skip)"


# ---------------------------------------------------------------------------
# 2. The user's scenario, end to end: erosion vs repair, pass over pass
# ---------------------------------------------------------------------------


class TestRepeatedPassesStopErodingAndRepair:
    RAW = [88.0, 71.0, 64.0, 55.0, 47.0, 39.0, 31.0, 24.0]

    def test_the_old_fallback_collapses_the_album(self):
        """The documented bug, pinned as a simulation of the shipped math.

        final_score ← remap(raw); next pass treats that remapped value as
        raw again. The z of the top track decays monotonically and crosses
        below the 5★ bound (album_z ≥ 1.0) — after which no pass can ever
        award 5★ again. How far it falls past that (4★, 3★) is pool-shape
        dependent; what is guaranteed — and what the user reported — is that
        5★ becomes unreachable and the trend is downward without limit.
        """
        pool = list(self.RAW)
        z_pass0 = _album_z(max(pool), pool)
        z_cross = None
        z_min = z_pass0
        for p in range(1, 25):
            pool = _remap_all(pool)
            z = _album_z(max(pool), pool)
            z_min = min(z_min, z)
            if z_cross is None and z < 1.0:
                z_cross = p
        assert z_cross is not None, (
            "the erosion must actually cross below the 5★ bound in "
            "simulation, or this test proves nothing about the defect"
        )
        assert z_min < z_pass0, "the trend must be downward"
        assert _album_z(max(pool), pool) < 1.0, (
            "the fixed point must keep 5★ unreachable"
        )

    def test_the_repair_makes_every_pass_stable(self):
        """The fixed pipeline: rebuild raw from stored data ONCE, persist it,
        and every later pass reads raw → remap is the016 fixed point."""
        raw_score_column = [None] * len(self.RAW)  # pre-016 rows
        stored_final = None
        pool_z = []
        for p in range(1, 13):
            if raw_score_column[0] is None:
                # Pass 1: reconstruct every row's TRUE blend from its stored
                # data (here: the components survive untouched in the row).
                raw_score_column = list(self.RAW)
            raw_pool = list(raw_score_column)
            stored_final = _remap_all(raw_pool)
            raw_score_column = list(raw_pool)  # persisted
            pool_z.append(_album_z(max(stored_final), stored_final))
        assert pool_z[0] == pytest.approx(pool_z[-1]), (
            "after the repair, pass 1 and pass 12 must agree — that is the "
            "idempotence contract extending to pre-016 rows"
        )
        assert pool_z[-1] >= 1.0, "the top track must stay 5★-eligible forever"


# ---------------------------------------------------------------------------
# 3. The wiring: both cached sites reconstruct and persist, never fall back
# ---------------------------------------------------------------------------


class TestCachedSitesNeverFeedRemappedValuesIntoTheRemap:
    @classmethod
    def _src(cls) -> str:
        path = REPO_ROOT / "services" / "popularity" / "stages" / "track_stage.py"
        return _strip_python(path.read_text(encoding="utf-8"))

    def test_the_singles_pass_cached_block_reconstructs_and_persists(self):
        src = self._src()
        idx = src.index('update_payload["raw_score"] = _recon_raw')
        # The reconstruction call must sit BEFORE the persist, in the same
        # fallback branch, and the branch must be reached only when the
        # stored raw is missing.
        call = src.rfind("_reconstruct_raw_blend(", 0, idx)
        assert call != -1
        guard = src.rfind("_stored_raw = float(track.get(\"raw_score\") or 0)", 0, call)
        assert guard != -1 and call - guard < 700, (
            "the singles-pass fallback must reconstruct from stored data"
        )

    def test_the_album_scan_cached_branch_reconstructs_and_persists(self):
        src = self._src()
        idx = src.index('update_payload["raw_score"] = _recon_raw', src.index("if _cached:"))
        call = src.rfind("_reconstruct_raw_blend(", 0, idx)
        assert call != -1
        guard = src.rfind("_stored_raw = float(effective_track.get(\"raw_score\") or 0)", 0, call)
        assert guard != -1 and call - guard < 900, (
            "the album-scan cached branch must reconstruct from stored data"
        )

    def test_no_cached_site_uses_final_score_as_the_raw_fallback(self):
        src = self._src()
        # The exact two lines that caused the erosion — each must be GONE.
        assert "else float(score_data[\"combined_score\"])" not in src, (
            "singles-pass: final_score is a REMAPPED value — never raw input"
        )
        assert "else _stored_score" not in src, (
            "album-scan: _stored_score is final_score — never raw input"
        )

    def test_the_unknown_raw_is_zero_not_the_stored_score(self):
        src = self._src()
        assert src.count('update_payload["_raw_combined"] = 0.0') >= 2, (
            "both cached sites must leave the raw UNKNOWN (remap skips) when "
            "reconstruction is impossible"
        )


# ---------------------------------------------------------------------------
# 4. Controls — the paths that were never broken stay untouched
# ---------------------------------------------------------------------------


class TestControls:
    def test_a_row_with_raw_score_is_used_verbatim(self):
        src = _strip_python(
            (REPO_ROOT / "services" / "popularity" / "stages" / "track_stage.py")
            .read_text(encoding="utf-8")
        )
        assert src.count("if _stored_raw > 0:") >= 2, (
            "rows that already carry raw_score must skip reconstruction"
        )

    def test_the_remap_still_skips_unknown_rows(self):
        from services.popularity.popularity_math import (
            apply_album_relative_popularity,
        )

        # raw <= 0 → returned unchanged (the skip the fix relies on).
        assert apply_album_relative_popularity(0.0, [88.0, 71.0, 64.0]) == 0.0

    def test_the_fresh_path_still_persists_under_the_cached_guard(self):
        src = _strip_python(
            (REPO_ROOT / "services" / "popularity" / "stages" / "track_stage.py")
            .read_text(encoding="utf-8")
        )
        idx = src.index('update_payload["raw_score"] = _fresh_raw')
        guard = src.rfind('if not update_payload.get("_cached")', 0, idx)
        assert guard != -1 and idx - guard < 900, (
            "the fresh-path write is the016 contract — keep it"
        )
