"""Songs on single releases are no longer hard-capped at 3 stars.

Reported: *"Songs on single releases seem to be scoring low."*

Root cause — three interacting mechanisms all keyed on the ALBUM pool size:

1. ``_apply_album_relative_normalization`` skips albums with fewer than 3
   tracks (a single release: the title track + an instrumental), so the raw
   score is never album-relative remapped.
2. ``_album_z_band_star`` returns ``min(3, max_stars)`` when the album pool
   has fewer than 3 valid scores — a hard 3★ ceiling for every single
   release, however popular the song.
3. ``_compute_album_z`` needs ``min_count=3``, so a 1-2 track album's z is
   0.0 by construction and ``is_standout`` (the 5★ path) can never fire.

The compilation branch already documents the fix: "there is no separate
album pool that means anything" and rates against the credited artist's
catalogue. A 1-2 track single is the same situation. Both the top-of-function
z-scores (``_assign_stars``) and the base band (``_album_z_band_star``) now
fall back to the ARTIST catalogue when the album pool is too small. The era
caps (catalogue-top cutoff / top-N / 5★ slots) still gate the 5★, so a
mid-catalogue single cannot inflate past its true position.
"""

from __future__ import annotations

import pytest

import services.popularity.stages.finalise_stage as fs


def _band_star(
    score: float,
    album_pool: list[float],
    artist_scores: list[float] | None = None,
    max_stars: int = 5,
) -> int:
    return fs._album_z_band_star(
        score,
        album_pool,
        artist_scores=list(artist_scores or []),
        max_stars=max_stars,
    )


# A realistic single release: the title track + the instrumental.
SINGLE_POOL = [68.9, 27.0]
# The artist's catalogue (NCT Dream-shaped: a wide, healthy distribution).
ARTIST_POOL = [94.7, 77.9, 77.8, 68.9, 62.3, 61.1, 58.7, 53.2, 51.4, 50.0,
               49.7, 49.2, 48.6, 46.8, 41.7, 40.1, 39.9, 36.3, 31.7, 28.8,
               27.0, 26.9]


class TestAlbumZBandStarFallsBackToArtistCatalogue:
    def test_a_popular_single_can_now_reach_four_stars(self):
        """Before the fix: ``len(valid) < 3 -> min(3)`` — a hard 3★ cap."""
        stars = _band_star(68.9, SINGLE_POOL, ARTIST_POOL)
        assert stars == 4, (
            "a genuinely popular single (artist-catalogue top) must not be "
            "capped at 3★ just because its release holds one track "
            f"(got {stars})"
        )

    def test_a_mid_single_stays_three_stars(self):
        stars = _band_star(49.2, SINGLE_POOL, ARTIST_POOL)
        assert stars == 3, (
            "a mid-catalogue single stays 3★ — the fallback must not inflate "
            "a merely-average song"
        )

    def test_a_weak_single_drops_below_three(self):
        stars = _band_star(27.0, SINGLE_POOL, ARTIST_POOL)
        assert stars <= 2

    def test_no_artist_catalogue_keeps_the_old_cap(self):
        """No artist scores (or too few) -> the 3★ cap remains (honest)."""
        assert _band_star(68.9, SINGLE_POOL, None) == 3
        assert _band_star(68.9, SINGLE_POOL, [90.0, 85.0, 80.0, 75.0]) == 3

    def test_the_max_stars_ceiling_still_applies(self):
        # A live album passes max_stars=3 — the fallback must respect it.
        assert _band_star(68.9, SINGLE_POOL, ARTIST_POOL, max_stars=3) == 3

    def test_a_normal_album_is_unaffected(self):
        """CONTROL: a 4+ track album still rates on its OWN pool."""
        album = [90.0, 85.0, 80.0, 75.0, 70.0]
        # The top track of a strong album clears star4_album_z (0.5) vs the
        # album median (~80) with a real spread -> 4★.
        stars = _band_star(90.0, album, ARTIST_POOL)
        assert stars == 4


class TestAssignStarsUsesArtistCatalogueForSmallAlbums:
    def _assign(self, monkeypatch, *, score, album_pool, artist_scores, title="Single"):
        """Drive ``_assign_stars`` with the small-album inputs."""
        track = {
            "track_id": "s1",
            "title": title,
            "popularity_score": score,
            "final_score": score,
            "lastfm_listeners": 31459,
            "listenbrainz_listens": 495,
            "single_confidence": "high",
        }
        return fs._assign_stars(
            track,
            album_pool,
            artist_scores,
            popularity_only=False,
            album_model={"era": "peak", "has_benchmark": False},
        )

    def test_top_of_function_z_uses_the_artist_pool(self, monkeypatch):
        """When the album pool is < 3, both z-scores share the artist pool."""
        seen: dict = {}

        def _capture_artist_z(score, pool):
            seen["pool"] = list(pool)
            return 1.5, 5.0

        monkeypatch.setattr(fs, "_compute_artist_z", _capture_artist_z)

        self._assign(monkeypatch, score=68.9, album_pool=SINGLE_POOL,
                     artist_scores=ARTIST_POOL)
        assert seen["pool"] == ARTIST_POOL

    def test_a_popular_single_gets_four_stars(self):
        """68.9 in a 26-track catalogue is top-quartile — 4★, not a 3★ cap."""
        stars = self._assign(
            None,
            score=68.9,
            album_pool=SINGLE_POOL,
            artist_scores=ARTIST_POOL,
        )
        # Band path uses the real z; NCT-DREAM-shaped pool -> 4★ base.
        assert stars >= 4, f"popular single release capped below 4★ (got {stars})"

    def test_a_normal_album_still_uses_its_own_z(self, monkeypatch):
        """CONTROL: >= 3 track album keeps album-relative z (no fallback)."""
        seen: dict = {}

        def _capture_album_z(score, pool):
            seen["album_pool"] = list(pool)
            return 0.4, 8.0

        monkeypatch.setattr(fs, "_compute_album_z", _capture_album_z)
        monkeypatch.setattr(fs, "_compute_artist_z", lambda s, p: (0.0, 1.0))

        self._assign(monkeypatch, score=80.0,
                     album_pool=[80.0, 75.0, 70.0, 65.0, 60.0],
                     artist_scores=ARTIST_POOL)
        assert seen["album_pool"] == [80.0, 75.0, 70.0, 65.0, 60.0]

    def test_an_instrumental_single_stays_low(self):
        """Inst. tracks on singles still rate by catalogue (weak = low)."""
        stars = self._assign(
            None,
            score=27.0,
            album_pool=SINGLE_POOL,
            artist_scores=ARTIST_POOL,
            title="Single (Inst.)",
        )
        assert stars <= 3