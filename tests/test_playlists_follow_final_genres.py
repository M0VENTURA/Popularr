"""Playlist membership must follow the cleaned Final Genres.

REPORT

> "the playlists are still not consistently using the cleaned Final Genres …
> four selected tracks do not have Alternative Rock in their matched final
> genres … The Metal Covers playlist has not changed … the artist cap still
> looks like five."

FOUR DEFECTS

1. **Source-tag fallback leaked ineligible tracks into genre playlists.**
   ``_resolve_playlist_genres`` fell back to the provider columns whenever the
   final ``genres`` value was empty. A track whose Final Genres are only
   ``Cover`` therefore pooled by its stale ``musicbrainz_genres`` — exactly the
   Placebo / Paramore / Cake / Ugly Kid Joe rows in the report.

2. **Curated (covers) playlists were never rebuilt by the genre resync.** The
   apply rebuilt only ``_create_genre_top_track_playlists``, so "Metal - Great
   Covers" kept its old membership — hence "exactly the same 150 file paths, in
   the same order".

3. **The Config page defaulted the artist cap to 25.** The template, the Python
   resolver and the help text all say 4; the JS reader said ``'25'`` and would
   persist it back on any save.

4. **Rock/metal conflicts: every remaining producer already arbitrates.** The
   residual 499 rows are pre-existing data, cleared by a resync.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from services.enrichment.genre_aggregation_service import (
    aggregate_genres,
    playlist_pool_genres,
)
from services.popularity.stages.finalise_stage import (
    _resolve_playlist_genres,
    _resolve_max_per_artist,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
FINALISE = REPO_ROOT / "services" / "popularity" / "stages" / "finalise_stage.py"
RESYNC = REPO_ROOT / "services" / "enrichment" / "genre_playlist_resync_service.py"


class TestAMarkerOnlyTrackPoolsIntoNoPlaylist:
    """The four reported Alternative Rock leaks, and the metal-less covers."""

    @pytest.mark.parametrize(
        "final_genres",
        ["Cover", "Remix", "Live", "Cover, Live", "Remaster"],
    )
    def test_a_marker_only_final_genre_yields_no_pool(self, final_genres: str) -> None:
        assert playlist_pool_genres(final_genres) == []

    @pytest.mark.parametrize(
        "final_genres",
        ["Cover", "Remix", "Acoustic"],
    )
    def test_stale_source_tags_cannot_resurrect_it(self, final_genres: str) -> None:
        """The exact Placebo case: Final Genres 'Cover', MB still 'Alternative Rock'."""
        row = {
            "genres": final_genres,
            "musicbrainz_genres": '["Alternative Rock", "Indie Rock"]',
            "lastfm_tags": '["alternative rock"]',
            "navidrome_genres": '["Alternative Rock"]',
            "discogs_genres": '["Rock"]',
        }
        pools, owned = _resolve_playlist_genres(row, max_genres=3)
        assert pools == [], (
            f"a track whose Final Genres are only {final_genres!r} pooled into "
            f"{pools} — this is the reported genre-playlist leak"
        )
        assert owned is False

    def test_a_musical_final_genre_still_pools(self) -> None:
        row = {"genres": "Alternative Rock"}
        pools, owned = _resolve_playlist_genres(row, max_genres=3)
        assert pools == ["Alternative Rock"]
        assert owned is True

    def test_a_marker_beside_a_real_genre_keeps_the_real_one(self) -> None:
        row = {"genres": "Hard Rock; Cover"}
        pools, _owned = _resolve_playlist_genres(row, max_genres=3)
        assert pools == ["Hard Rock"]


class TestMetalCoversRequiresARealMetalOrRockGenre:
    """Neue Deutsche Härte must resolve by taxonomy, never by substring."""

    def test_neue_deutsche_haerte_is_metal(self) -> None:
        from services.playlists.main_genres import classify_main_genre

        assert classify_main_genre(["Neue Deutsche Härte"])["id"] == "metal"

    def test_a_cover_with_no_musical_genre_buckets_nowhere(self) -> None:
        from services.playlists.main_genres import classify_main_genre

        assert classify_main_genre(playlist_pool_genres("Cover")) is None

    def test_a_hard_rock_cover_buckets_as_rock_not_metal(self) -> None:
        from services.playlists.main_genres import classify_main_genre

        # "Hard Rock" contains no "metal" substring; it must not become Metal.
        assert classify_main_genre(["Hard Rock"])["id"] == "rock"


class TestTheGenreResyncRebuildsCuratedPlaylists:
    def test_the_apply_rebuilds_the_covers_family(self) -> None:
        src = RESYNC.read_text(encoding="utf-8")
        start = src.index("def apply_genre_playlist_resync")
        body = src[start:]
        assert "_create_covers_playlist" in body, (
            "curated covers playlists are genre-derived but were never rebuilt, "
            "so Metal Covers kept its stale membership"
        )

    def test_a_covers_failure_cannot_fail_the_genre_job(self) -> None:
        src = RESYNC.read_text(encoding="utf-8")
        start = src.index("covers_written = 0")
        block = src[start:start + 1400]
        assert "except Exception" in block


class TestTheArtistCapDefaultsToFour:
    def test_the_python_resolver_defaults_to_four(self) -> None:
        assert _resolve_max_per_artist({}) == 4

    @pytest.mark.parametrize(
        "path",
        [
            "test_site/static/js/pages/config.js",
            "static/js/pages/config.js",
            "static/js/config.js",
        ],
    )
    def test_the_config_page_fallback_is_four(self, path: str) -> None:
        src = (REPO_ROOT / path).read_text(encoding="utf-8")
        # The line that reads the input and its numeric fallback.
        block_start = src.index("genre_playlists_max_per_artist:")
        block = src[block_start:block_start + 320]
        assert "'25'" not in block, (
            f"{path} still falls back to 25 for the artist cap, which a save "
            "then persists — the reported 'cap looks like five'"
        )
        assert "'4'" in block

    def test_zero_still_means_unlimited(self) -> None:
        assert _resolve_max_per_artist({"genre_playlists_max_per_artist": 0}) is None


class TestTheResidualRockMetalConflictsAreNotProduced:
    """Every producer must arbitrate; residual rows are pre-existing data."""

    @pytest.mark.parametrize(
        "source_map",
        [
            {"musicbrainz@track": ["Alternative Rock", "Alternative Metal"]},
            {"musicbrainz@track": ["Alternative Metal"], "lastfm@track": ["Alternative Rock"]},
            {"musicbrainz@track": ["Alternative Rock"], "discogs@track": ["Alternative Metal"]},
        ],
    )
    def test_alternative_rock_and_metal_never_both_survive(self, source_map) -> None:
        got = aggregate_genres(source_map, max_genres=4)
        assert not (
            {"alternative rock", "alternative metal"} <= {g.casefold() for g in got}
        ), f"both rock and metal survived: {got}"
