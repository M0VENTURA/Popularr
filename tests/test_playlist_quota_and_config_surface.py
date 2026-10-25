"""The per-artist quota, the Config page's playlist surface, and the collection CSV.

Three things are pinned here, each because it was reported as missing or was
silently wrong:

1. **``Max Tracks Per Artist`` is a HARD quota.** ``_ordered_playlist_rows``
   ranks, then caps, then interleaves — so the limit still holds after the list
   is spread across artists and after the playlist cap slices it. A future
   reorder that interleaves before capping, or that defers overflow instead of
   dropping it, breaks the promise the Config page makes.
2. **Every playlist setting the backend reads is on the Config page and is
   collected by it.** ``save_config`` REPLACES config.yaml wholesale (only
   top-level sections are carried forward), so a key the collector does not
   emit is DELETED on save — the setting silently reverts to its code default.
   Eight optional playlist families and ``essential_max_tracks`` had no input
   in one or both trees, so their toggles could not work.
3. **The collection-wide popularity CSV** exists, shares the artist report's
   columns and ordering, and is reachable from the Config page.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

TREES = {
    "live": (
        REPO_ROOT / "templates/pages/config.html",
        REPO_ROOT / "static/js/config.js",
    ),
    "test_site": (
        REPO_ROOT / "test_site/templates/Pages/config.html",
        REPO_ROOT / "test_site/static/js/pages/config.js",
    ),
}


def row(artist: str, title: str, *, stars: int = 5, listeners: int = 1000) -> dict[str, Any]:
    return {
        "id": f"{artist}::{title}",
        "artist": artist,
        "album_artist": artist,
        "title": title,
        "stars": stars,
        "lastfm_listeners": listeners,
        "listenbrainz_listens": 0,
        "popularity_score": 50.0,
        "final_score": 50.0,
    }


def _finalise():
    return pytest.importorskip("services.popularity.stages.finalise_stage")


def _big_pool() -> list[dict[str, Any]]:
    """1 artist x 80 + 4 artists x 40 = 240 rows, dominant artist strongest."""
    pool = [row("Dominant", f"D{i:03d}", listeners=100_000 - i) for i in range(80)]
    for artist in ("Alpha", "Beta", "Gamma", "Delta"):
        pool += [row(artist, f"{artist}{i:03d}", listeners=50_000 - i) for i in range(40)]
    return pool


# ---------------------------------------------------------------------------
# 1. The per-artist quota
# ---------------------------------------------------------------------------


class TestPerArtistQuotaIsHard:
    def test_no_artist_exceeds_the_quota(self) -> None:
        fs = _finalise()
        for cap in (1, 3, 25):
            out = fs._ordered_playlist_rows(
                _big_pool(), order_mode="prominence", max_per_artist=cap, interleave=False
            )
            counts: dict[str, int] = {}
            for r in out:
                counts[r["artist"]] = counts.get(r["artist"], 0) + 1
            assert max(counts.values()) <= cap, (cap, counts)

    def test_the_quota_still_holds_after_interleaving(self) -> None:
        """⭐ THE REGRESSION THIS EXISTS FOR.

        Interleaving must not be able to push an artist back over the quota.
        When the pipeline interleaved BEFORE capping, the cap became advisory:
        a 600-track pool with ``max_per_artist=25`` yielded 60 tracks from the
        dominant artist.
        """
        fs = _finalise()
        out = fs._ordered_playlist_rows(
            _big_pool(), order_mode="prominence", max_per_artist=4, interleave=True
        )
        counts: dict[str, int] = {}
        for r in out:
            counts[r["artist"]] = counts.get(r["artist"], 0) + 1
        assert counts == {"Dominant": 4, "Alpha": 4, "Beta": 4, "Gamma": 4, "Delta": 4}

    def test_the_quota_holds_after_the_playlist_cap_slices_it(self) -> None:
        fs = _finalise()
        out = fs._ordered_playlist_rows(
            _big_pool(), order_mode="prominence", max_per_artist=3, interleave=True
        )[:7]
        counts: dict[str, int] = {}
        for r in out:
            counts[r["artist"]] = counts.get(r["artist"], 0) + 1
        assert max(counts.values()) <= 3

    def test_the_quota_keeps_each_artist_strongest_tracks(self) -> None:
        """Capping before spreading is what makes this true — and it must stay."""
        fs = _finalise()
        out = fs._ordered_playlist_rows(
            _big_pool(), order_mode="prominence", max_per_artist=2, interleave=False
        )
        dominant = [r for r in out if r["artist"] == "Dominant"]
        assert [r["title"] for r in dominant] == ["D000", "D001"]
        assert [r["lastfm_listeners"] for r in dominant] == [100_000, 99_999]

    def test_zero_means_no_quota(self) -> None:
        fs = _finalise()
        out = fs._ordered_playlist_rows(
            _big_pool(), order_mode="prominence", max_per_artist=0, interleave=False
        )
        assert len(out) == len(_big_pool())

    def test_the_help_text_does_not_claim_overflow_is_kept(self) -> None:
        """The old text said overflow "moves to the end" — the cap drops it."""
        for tree, (template, _js) in TREES.items():
            text = template.read_text(encoding="utf-8")
            if 'for="playlists_max_per_artist"' not in text:
                pytest.skip(f"{tree} has no per-artist input")
            block = text[text.index('for="playlists_max_per_artist"'):]
            block = block[: block.index("</div>")]
            assert "move to the end" not in block, (
                f"{tree}: the cap DROPS overflow, so the help text must not say otherwise"
            )
            assert "hard quota" in block.lower()


# ---------------------------------------------------------------------------
# 2. The Config page surface
# ---------------------------------------------------------------------------

#: (config key, input id) for every playlist setting the backend reads.
#: The id is not derivable from the key (``decade_playlists_enabled`` is
#: rendered as ``playlists_decade_enabled``), so both are spelled out.
PLAYLIST_SETTINGS: tuple[tuple[str, str], ...] = (
    ("essential_playlists_enabled", "playlists_essential_enabled"),
    ("essential_include_featured", "playlists_essential_include_featured"),
    ("essential_max_tracks", "playlists_essential_max_tracks"),
    ("essential_name_template", "playlists_essential_name_template"),
    ("genre_playlists_enabled", "playlists_genre_playlists_enabled"),
    ("genre_playlists_delete_enabled", "playlists_genre_delete_enabled"),
    ("genre_playlists_create_threshold", "playlists_genre_create_threshold"),
    ("genre_playlists_delete_threshold", "playlists_genre_delete_threshold"),
    ("genre_playlists_min_stars", "playlists_genre_min_stars"),
    ("genre_playlists_max_genres", "playlists_genre_max_genres"),
    ("genre_playlists_max_tracks", "playlists_genre_max_tracks"),
    ("genre_playlists_max_per_artist", "playlists_max_per_artist"),
    ("genre_playlists_name_template", "playlists_genre_name_template"),
    ("playlist_order_mode", "playlists_order_mode"),
    ("playlist_interleave_artists", "playlists_interleave_artists"),
    ("new_music_playlist_enabled", "playlists_new_music_enabled"),
    ("decade_playlists_enabled", "playlists_decade_enabled"),
    ("decade_playlists_name_template", "playlists_decade_name_template"),
    ("decade_playlists_min_tracks", "playlists_decade_min_tracks"),
    ("decade_playlists_max_tracks", "playlists_decade_max_tracks"),
    ("hit_singles_playlist_enabled", "playlists_hit_singles_enabled"),
    ("hit_singles_playlist_name", "playlists_hit_singles_name"),
    ("hit_singles_max_tracks", "playlists_hit_singles_max_tracks"),
    ("hidden_gems_playlist_enabled", "playlists_hidden_gems_enabled"),
    ("hidden_gems_playlist_name", "playlists_hidden_gems_name"),
    ("hidden_gems_max_tracks", "playlists_hidden_gems_max_tracks"),
    ("new_releases_playlist_enabled", "playlists_new_releases_enabled"),
    ("new_releases_playlist_name", "playlists_new_releases_name"),
    ("new_releases_max_tracks", "playlists_new_releases_max_tracks"),
    ("covers_playlist_enabled", "playlists_covers_enabled"),
    ("covers_playlist_name", "playlists_covers_name"),
    ("covers_max_tracks", "playlists_covers_max_tracks"),
    ("acoustic_playlist_enabled", "playlists_acoustic_enabled"),
    ("acoustic_playlist_name", "playlists_acoustic_name"),
    ("acoustic_max_tracks", "playlists_acoustic_max_tracks"),
    ("high_energy_playlist_enabled", "playlists_high_energy_enabled"),
    ("high_energy_playlist_name", "playlists_high_energy_name"),
    ("high_energy_max_tracks", "playlists_high_energy_max_tracks"),
    ("mood_playlists_enabled", "playlists_mood_enabled"),
    ("mood_playlists_name_template", "playlists_mood_name_template"),
    ("mood_playlists_min_tracks", "playlists_mood_min_tracks"),
    ("mood_playlists_max_tracks", "playlists_mood_max_tracks"),
    ("exclude_christmas_from_playlists", "playlists_exclude_christmas"),
    ("christmas_playlist_marker", "playlists_christmas_marker"),
)


class TestEveryPlaylistSettingIsOnTheConfigPage:
    @pytest.mark.parametrize("tree", sorted(TREES))
    def test_every_setting_has_an_input(self, tree: str) -> None:
        template, _js = TREES[tree]
        text = template.read_text(encoding="utf-8")
        missing = [
            input_id
            for _key, input_id in PLAYLIST_SETTINGS
            if f'id="{input_id}"' not in text
        ]
        assert not missing, (
            f"{tree} Config page does not render {missing} — the setting cannot be "
            "changed, and an ordinary save DELETES it from config.yaml"
        )

    @pytest.mark.parametrize("tree", sorted(TREES))
    def test_every_setting_is_collected_by_the_page_script(self, tree: str) -> None:
        """A rendered input nothing collects cannot be saved either."""
        _template, js = TREES[tree]
        source = js.read_text(encoding="utf-8")
        missing = [
            input_id
            for _key, input_id in PLAYLIST_SETTINGS
            if f"'{input_id}'" not in source
        ]
        assert not missing, f"{tree} config.js never reads {missing}"

    @pytest.mark.parametrize("tree", sorted(TREES))
    def test_every_key_the_backend_reads_is_collected(self, tree: str) -> None:
        """The ratchet: a NEW backend key must be added to the page too.

        Derived from ``get_playlists_config()`` rather than a hand list, so this
        cannot go stale when the getter grows a key.
        """
        from helpers.config_helpers import get_playlists_config

        _template, js = TREES[tree]
        source = js.read_text(encoding="utf-8")
        emitted = set(re.findall(r"^\s*([a-z0-9_]+):", source, re.M))

        keys = set(get_playlists_config())
        # ``genre_exclusions`` is a nested block collected separately below its
        # own header, so it is not emitted as a flat key.
        keys.discard("genre_exclusions")
        missing = sorted(k for k in keys if k not in emitted)
        assert not missing, (
            f"{tree} config.js does not emit {missing}; save_config replaces the whole "
            "playlists section, so those keys would be dropped on save"
        )

    @pytest.mark.parametrize("tree", sorted(TREES))
    def test_no_collector_reads_an_input_the_page_never_renders(self, tree: str) -> None:
        """The mirror image, and the more dangerous direction.

        ``getChecked(id, default)`` returns the DEFAULT when the element is
        missing, so a collector with no input does not merely fail — it
        force-writes its default over the saved value on every save.
        """
        template, js = TREES[tree]
        text = template.read_text(encoding="utf-8")
        ids = set(re.findall(r'id="(playlists_[A-Za-z0-9_]+)"', text))
        collected = set(
            re.findall(
                r"""(?:getChecked|getValue)\('(playlists_[A-Za-z0-9_]+)'""",
                js.read_text(encoding="utf-8"),
            )
        )
        orphans = sorted(collected - ids)
        assert not orphans, (
            f"{tree} config.js reads {orphans} but no input has that id, so the "
            "collector writes its default over the stored value"
        )


class TestTheRenderedToggleMatchesTheEffectiveDefault:
    """A checkbox that shows OFF while the backend defaults ON is a lie."""

    @pytest.mark.parametrize("tree", sorted(TREES))
    @pytest.mark.parametrize(
        ("input_id", "config_key"),
        [
            ("playlists_decade_enabled", "decade_playlists_enabled"),
            ("playlists_hit_singles_enabled", "hit_singles_playlist_enabled"),
        ],
    )
    def test_default_matches_the_getter(self, tree: str, input_id: str, config_key: str) -> None:
        from helpers.config_helpers import get_playlists_config

        effective = get_playlists_config()[config_key]
        template, _js = TREES[tree]
        text = template.read_text(encoding="utf-8")
        block = text[text.index(f'id="{input_id}"'):]
        block = block[: block.index(">")]

        rendered = "True" if "True" in block else "False"
        assert (rendered == "True") is effective, (
            f"{tree}: the {input_id} checkbox defaults to {rendered} while "
            f"{config_key} is effectively {effective}"
        )


# ---------------------------------------------------------------------------
# 3. The collection-wide popularity report
# ---------------------------------------------------------------------------


def _report_service():
    return pytest.importorskip("services.popularity.popularity_report_service")


class TestCollectionPopularityReport:
    def test_the_collection_scope_uses_the_whole_table(self) -> None:
        svc = _report_service()
        predicate, params = svc._scope(artist=None, album=None)
        assert predicate == "1 = 1"
        assert params == {}

    def test_the_artist_scope_is_unchanged(self) -> None:
        svc = _report_service()
        predicate, params = svc._scope(artist="Anti-Flag", album=None)
        assert predicate == svc._ARTIST_PREDICATE
        assert params == {"artist": "Anti-Flag"}

    def test_the_collection_report_reuses_the_one_query(self) -> None:
        svc = _report_service()
        source = inspect.getsource(svc.build_collection_popularity_report)
        body = "\n".join(line for line in source.splitlines() if not line.strip().startswith("#"))
        assert "build_popularity_report" in body
        assert "SELECT" not in body, (
            "the collection report must delegate; a second SELECT would let the "
            "columns or the ordering drift from the artist report"
        )

    def test_the_filename_names_the_scope(self) -> None:
        svc = _report_service()
        assert svc.popularity_report_filename() == (
            "Popularr - collection popularity report.csv"
        )
        assert svc.popularity_report_filename(artist="Anti-Flag") == (
            "Anti-Flag - popularity report.csv"
        )
        assert svc.popularity_report_filename(artist="Anti-Flag", album="Mobilize") == (
            "Anti-Flag - Mobilize - popularity report.csv"
        )

    def test_the_sort_chain_is_shared_by_every_scope(self) -> None:
        """The WHERE clause changes; the ORDER BY must not."""
        svc = _report_service()
        sql = svc._POPULARITY_REPORT_SELECT
        assert "{where}" in sql
        assert "raw_score" in sql, "the cross-album number must lead the ordering"

    def test_the_route_exists_and_offloads_the_work(self) -> None:
        """An `async def` reading the whole table must not block the loop."""
        routes = (REPO_ROOT / "routes/misc_routes.py").read_text(encoding="utf-8")
        assert '@misc_api_bp.route("/popularity/report")' in routes
        start = routes.index("async def api_collection_popularity_report")
        body = routes[start:start + 2500]
        assert "asyncio.to_thread" in body, (
            "the collection report reads every track, so it must run off the event loop"
        )

    @pytest.mark.parametrize("tree", sorted(TREES))
    def test_the_config_page_links_to_the_export(self, tree: str) -> None:
        template, _js = TREES[tree]
        text = template.read_text(encoding="utf-8")
        assert 'id="popularity-export"' in text
        assert "api_collection_popularity_report" in text, (
            f"{tree}: the export card must point at the route by ENDPOINT, not a "
            "hard-coded URL, so a prefix change cannot silently break it"
        )


class TestTheCsvIsWellFormed:
    def test_the_header_is_the_shared_column_list(self) -> None:
        svc = _report_service()
        assert svc.POPULARITY_REPORT_COLUMNS[0] == "rank"
        for column in ("title", "artist", "album", "stars", "lastfm_listeners",
                       "listenbrainz_listens", "is_single"):
            assert column in svc.POPULARITY_REPORT_COLUMNS

    def test_every_selected_column_is_also_reported(self) -> None:
        """A column read but never written is a silent data loss in the export."""
        svc = _report_service()
        select_block = svc._POPULARITY_REPORT_SELECT.split("FROM tracks")[0]
        selected = {
            name.strip()
            for name in select_block.replace("SELECT", "").split(",")
            if name.strip()
        }
        assert selected <= set(svc.POPULARITY_REPORT_COLUMNS), (
            selected - set(svc.POPULARITY_REPORT_COLUMNS)
        )
