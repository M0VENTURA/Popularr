"""Genre pooling, the three-genre write, and the weekly playlist rotation.

Three reported problems, each pinned separately:

1. **"It was adjusted to remove Alternative Metal, Navidrome doesn't show
   Alternative Metal, but it's still on the Alternative Metal Playlist."**
   The pool used to be built from the per-PROVIDER columns, which keep whatever
   the provider originally returned — so a genre removed from a track's metadata
   lived on in ``lastfm_tags``/``musicbrainz_genres`` and kept the track in a
   playlist its own genre list no longer claimed. The pool now comes from the
   track's FINAL ``genres`` value.
2. **"The finalisation scan should select the top 3 genres from the most used
   across sources."** The write paths passed ``max_genres=2``, overriding the
   configured ceiling. Filter markers are extras ON TOP of that count.
3. **A weekly change of order without a change of content.**
"""

from __future__ import annotations

import inspect
import re
from datetime import datetime
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


def gas():
    return pytest.importorskip("services.enrichment.genre_aggregation_service")


def fs():
    return pytest.importorskip("services.popularity.stages.finalise_stage")


def row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "t1",
        "title": "Song",
        "artist": "The Offspring",
        "album_artist": "The Offspring",
        "album": "Conspiracy of One",
        "stars": 5,
        "genres": "",
        "lastfm_tags": "",
        "musicbrainz_genres": "",
        "discogs_genres": "",
        "listenbrainz_genres": "",
        "spotify_genres": "",
        "essentia_genres": "",
        "manual_genres": "",
        "navidrome_genres": "",
        "audiodb_genres": "",
        "wikidata_genres": "",
        "popularity_score": 70.0,
        "lastfm_listeners": 1000,
        "listenbrainz_listens": 1000,
        "is_live": 0,
        "is_compilation": 0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. Pooling uses the FINAL genres
# ---------------------------------------------------------------------------


class TestPlaylistPoolGenres:
    def test_performance_markers_are_not_pool_keys(self) -> None:
        """Cover/Live/… must never name a playlist."""
        genres = gas().playlist_pool_genres("Punk Rock, Cover, Live, Remastered")
        assert genres == ["Punk Rock"]

    def test_the_holiday_family_is_not_a_pool_key(self) -> None:
        """Seasonal placement is decided by ``is_christmas_track`` instead."""
        for token in ("Christmas", "Holiday", "Xmas", "Noel"):
            assert gas().playlist_pool_genres(f"Pop, {token}") == ["Pop"]

    def test_musical_genres_survive_in_order_and_dedupe(self) -> None:
        assert gas().playlist_pool_genres("Punk, Rock, punk, Alternative Metal") == [
            "Punk",
            "Rock",
            "Alternative Metal",
        ]

    def test_a_limit_counts_only_musical_genres(self) -> None:
        """The markers are extras — they must not consume a slot."""
        genres = gas().playlist_pool_genres(
            "Punk, Cover, Rock, Live, Alternative Metal, Acoustic", limit=2
        )
        assert genres == ["Punk", "Rock"]

    def test_json_and_delimited_shapes_both_parse(self) -> None:
        assert gas().playlist_pool_genres('["Punk", "Cover", "Rock"]') == ["Punk", "Rock"]
        assert gas().playlist_pool_genres("Punk; Rock/Alternative") == ["Punk", "Rock", "Alternative"]

    def test_orchestral_and_soundtrack_still_pool(self) -> None:
        """⚠️ Only PERFORMANCE markers are excluded, not every filter tag."""
        assert gas().playlist_pool_genres("Orchestral, Soundtrack") == [
            "Orchestral",
            "Soundtrack",
        ]

    def test_empty_input_is_empty(self) -> None:
        for value in (None, "", "[]", "{}", "null"):
            assert gas().playlist_pool_genres(value) == []


class TestTheTrackPoolsByItsFinalGenres:
    def test_a_removed_genre_no_longer_pools(self) -> None:
        """⭐⭐ THE REPORTED OFFSPRING CASE.

        The provider columns still carry "Alternative Metal" because Last.fm
        says so; the track's metadata no longer does. The playlist must follow
        the metadata.
        """
        track = row(
            genres="Punk Rock, Alternative Rock",
            lastfm_tags="alternative metal, punk rock",
            musicbrainz_genres="alternative metal",
            discogs_genres="alternative metal",
        )
        genres, owned = fs()._resolve_playlist_genres(track, max_genres=3)
        assert owned is True
        assert "Alternative Metal" not in genres
        assert genres == ["Punk Rock", "Alternative Rock"]

    def test_the_final_value_wins_over_a_longer_provider_list(self) -> None:
        track = row(
            genres="Punk Rock",
            lastfm_tags="punk rock, alternative metal, hardcore, rock, punk",
        )
        genres, _owned = fs()._resolve_playlist_genres(track, max_genres=3)
        assert genres == ["Punk Rock"]

    def test_a_track_with_no_final_genres_falls_back_to_its_own_sources(self) -> None:
        """Never silently drop a track that simply has not been aggregated yet.

        ⚠️ The source columns are normalised to lower case by
        ``iter_genre_tokens``, so the fallback returns the provider's spelling
        as stored. Only the ANSWER matters here, not its casing — the final
        ``genres`` path preserves the display casing.
        """
        track = row(genres="", musicbrainz_genres="Punk Rock")
        genres, owned = fs()._resolve_playlist_genres(track, max_genres=3)
        assert owned is True
        assert [g.casefold() for g in genres] == ["punk rock"]

    def test_a_track_with_nothing_at_all_pools_nowhere(self) -> None:
        genres, owned = fs()._resolve_playlist_genres(row(genres=""), max_genres=3)
        assert genres == []
        assert owned is False

    def test_the_config_ceiling_applies_to_the_pool(self) -> None:
        track = row(genres="A, B, C, D, E")
        genres, _ = fs()._resolve_playlist_genres(track, max_genres=3)
        assert genres == ["A", "B", "C"]


class TestTheChristmasPoolStillExists:
    def test_a_purely_seasonal_track_lands_in_the_holiday_pool(self) -> None:
        """Markers are not pool keys, so the seasonal branch needs its own fill."""
        source = inspect.getsource(fs()._create_genre_top_track_playlists)
        assert "_HOLIDAY_POOL_DISPLAY" in source
        assert "if not track_genres and is_christmas" in source

    def test_a_seasonal_track_pools_under_a_christmas_prefix(self) -> None:
        """A Christmas rock track belongs in "Christmas Rock", not "Rock"."""
        source = inspect.getsource(fs()._create_genre_top_track_playlists)
        assert 'f"christmas {norm_key}"' in source


# ---------------------------------------------------------------------------
# 2. Three musical genres are written
# ---------------------------------------------------------------------------


class TestThreeGenresAreWritten:
    def test_the_default_is_three(self) -> None:
        from helpers.config_helpers import GENRE_TRACK_LIMIT_DEFAULT

        assert GENRE_TRACK_LIMIT_DEFAULT == 3

    def test_the_getter_honours_the_configured_ceiling(self, monkeypatch) -> None:
        import helpers.config_helpers as ch

        monkeypatch.setattr(ch, "get_config", lambda: {"genres": {"max_genres_per_track": 4}})
        assert ch.get_genre_track_limit() == 4

    def test_the_getter_falls_back_to_the_selection_ceiling(self, monkeypatch) -> None:
        """An install that only tuned ``genres.selection.max_genres`` still works."""
        import helpers.config_helpers as ch

        monkeypatch.setattr(
            ch, "get_config", lambda: {"genres": {"selection": {"max_genres": 5}}}
        )
        assert ch.get_genre_track_limit() == 5

    def test_a_nonsense_value_is_clamped(self, monkeypatch) -> None:
        import helpers.config_helpers as ch

        for raw, expected in ((0, 1), (-3, 1), (99, 10), ("abc", 3)):
            monkeypatch.setattr(
                ch, "get_config", lambda raw=raw: {"genres": {"max_genres_per_track": raw}}
            )
            assert ch.get_genre_track_limit() == expected

    def test_the_track_stage_no_longer_hardcodes_two(self) -> None:
        """⭐ THE ACTUAL DEFECT: the write paths overrode the configured value."""
        module = pytest.importorskip("services.popularity.stages.track_stage")
        source = inspect.getsource(module.process_track)
        assert "max_genres=_genre_track_limit()" in source, (
            "the track's own genre write must use the configured limit"
        )
        assert "max_genres=2" not in source

    def test_the_album_blend_uses_the_same_limit(self) -> None:
        module = gas()
        source = inspect.getsource(module.get_track_recommendations)
        assert "max_genres=_genre_track_limit()" in source
        assert "max_genres=2" not in source

    def test_the_various_artists_path_uses_the_same_limit(self) -> None:
        module = gas()
        source = inspect.getsource(module.sync_various_artists_track_genres)
        assert "max_genres=_genre_track_limit()" in source
        assert "max_genres=2" not in source

    def test_the_aggregator_appends_markers_on_top_of_the_limit(self) -> None:
        """Filter tags are EXTRAS — the count is a ceiling on musical genres.

        ⚠️ Three UNRELATED genres are used on purpose. "Punk Rock" and "Rock"
        are parent/child in the vocabulary and get condensed into one, so a
        nested fixture would measure condensation rather than the limit.
        """
        result = gas().aggregate_genres(
            {"musicbrainz@track": ["Punk Rock", "Ska", "Hardcore"]},
            max_genres=3,
            context_title="Song",
        )
        musical = [g for g in result if not gas().is_track_affiliation_tag(g)]
        assert len(musical) == 3

    def test_a_cover_track_keeps_its_marker_beyond_the_limit(self) -> None:
        result = gas().aggregate_genres(
            {"musicbrainz@track": ["Punk Rock", "Rock", "Alternative Metal", "Cover"]},
            max_genres=3,
            context_title="Song",
        )
        assert "Cover" in result, "the marker must survive on top of the three"


# ---------------------------------------------------------------------------
# 3. The weekly rotation
# ---------------------------------------------------------------------------


def tracks(n: int = 25) -> list[dict[str, Any]]:
    return [
        {
            "id": f"t{i}",
            "title": f"Track {i:02d}",
            "artist": f"Artist {i % 5}",
            "stars": 5,
            "lastfm_listeners": 100_000 - i,
            "listenbrainz_listens": 0,
            "popularity_score": 90.0 - i,
            "score": 90.0 - i,
        }
        for i in range(n)
    ]


WEEK_1 = datetime(2026, 1, 6)
WEEK_2 = datetime(2026, 1, 13)


class TestWeeklyRotation:
    def test_the_same_week_gives_the_same_order(self) -> None:
        """Deterministic, so an unchanged week is never rewritten."""
        a = fs()._weekly_shuffle(tracks(), playlist_key="Rock", enabled=True, when=WEEK_1)
        b = fs()._weekly_shuffle(tracks(), playlist_key="Rock", enabled=True, when=WEEK_1)
        assert [t["id"] for t in a] == [t["id"] for t in b]

    def test_the_seed_is_a_string_seed_not_python_hash(self) -> None:
        """A str seed is SHA-512 derived, so it survives a restart unchanged."""
        first = [t["id"] for t in fs()._weekly_shuffle(
            tracks(), playlist_key="Rock", enabled=True, when=WEEK_1
        )]
        second = [t["id"] for t in fs()._weekly_shuffle(
            tracks(), playlist_key="Rock", enabled=True, when=WEEK_1
        )]
        assert first == second

    def test_a_new_week_changes_the_order(self) -> None:
        a = [t["id"] for t in fs()._weekly_shuffle(
            tracks(), playlist_key="Rock", enabled=True, when=WEEK_1
        )]
        b = [t["id"] for t in fs()._weekly_shuffle(
            tracks(), playlist_key="Rock", enabled=True, when=WEEK_2
        )]
        assert a != b

    def test_the_membership_is_unchanged(self) -> None:
        """⭐⭐ The whole point: SAME tracks, different sequence."""
        source = tracks()
        shuffled = fs()._weekly_shuffle(source, playlist_key="Rock", enabled=True, when=WEEK_1)
        assert sorted(t["id"] for t in shuffled) == sorted(t["id"] for t in source)
        assert len(shuffled) == len(source)

    def test_two_playlists_in_the_same_week_do_not_share_an_order(self) -> None:
        a = [t["id"] for t in fs()._weekly_shuffle(
            tracks(), playlist_key="Rock", enabled=True, when=WEEK_1
        )]
        b = [t["id"] for t in fs()._weekly_shuffle(
            tracks(), playlist_key="Metal", enabled=True, when=WEEK_1
        )]
        assert a != b

    def test_the_playlist_name_is_matched_case_insensitively(self) -> None:
        a = [t["id"] for t in fs()._weekly_shuffle(
            tracks(), playlist_key="Rock", enabled=True, when=WEEK_1
        )]
        b = [t["id"] for t in fs()._weekly_shuffle(
            tracks(), playlist_key="rock", enabled=True, when=WEEK_1
        )]
        assert a == b

    def test_disabled_leaves_the_order_untouched(self) -> None:
        source = tracks()
        out = fs()._weekly_shuffle(source, playlist_key="Rock", enabled=False, when=WEEK_1)
        assert [t["id"] for t in out] == [t["id"] for t in source]

    def test_a_short_list_is_a_no_op(self) -> None:
        for n in (0, 1):
            short = tracks(n)
            assert fs()._weekly_shuffle(
                short, playlist_key="Rock", enabled=True, when=WEEK_1
            ) == short

    def test_the_input_list_is_not_mutated(self) -> None:
        source = tracks()
        before = [t["id"] for t in source]
        fs()._weekly_shuffle(source, playlist_key="Rock", enabled=True, when=WEEK_1)
        assert [t["id"] for t in source] == before

    def test_the_rotation_runs_after_the_track_cap(self) -> None:
        """⭐ THE ORDERING THAT MATTERS.

        Shuffling before the slice would let the cap drop the strongest tracks
        at random. Every rotation call must sit AFTER the slice in its builder.
        """
        fsm = fs()
        builders = {
            "_create_genre_top_track_playlists": "[:max_tracks]",
            "_create_new_music_playlist": "[:_NEW_MUSIC_MAX_TRACKS]",
            "_sync_essential_playlist": "[:max_tracks]",
        }
        checked = 0
        for builder, slice_token in builders.items():
            if not hasattr(fsm, builder):
                continue
            source = inspect.getsource(getattr(fsm, builder))
            if "_weekly_shuffle(" not in source:
                continue
            checked += 1
            slice_pos = source.index(slice_token)
            shuffle_pos = source.index("_weekly_shuffle(")
            assert shuffle_pos > slice_pos, (
                f"{builder}: the rotation must come AFTER {slice_token}"
            )
        assert checked == len(builders), (
            f"only {checked} of {len(builders)} builders rotate their order"
        )

    def test_the_rotation_is_on_by_default_and_configurable(self, monkeypatch) -> None:
        import helpers.config_helpers as ch

        monkeypatch.setattr(ch, "get_config", lambda: {})
        assert ch.get_playlist_shuffle_config()["enabled"] is True

        monkeypatch.setattr(
            ch, "get_config", lambda: {"playlists": {"weekly_shuffle_enabled": False}}
        )
        assert ch.get_playlist_shuffle_config()["enabled"] is False


class TestRotationIsPerPlaylistFamily:
    """A chart rotates; a ranked best-of must not.

    Reported: "I wanted the top genre tracks to be randomised, but the essential
    artist collections in order."

    One global switch could not express that. Rotation is right for a Top Tracks
    chart (a shopping list that looks stale if it never moves) and wrong for an
    Essential Collection (a best-of where "track 1 is their strongest" is the
    only information the list carries).
    """

    # -- the defaults -----------------------------------------------------

    def test_defaults_per_family(self, monkeypatch) -> None:
        import helpers.config_helpers as ch

        monkeypatch.setattr(ch, "get_config", lambda: {})
        scopes = ch.get_playlist_shuffle_config()["scopes"]
        assert scopes["genre_top_tracks"] is True, "a chart should rotate"
        assert scopes["new_music"] is True
        assert scopes["essential"] is False, (
            "an Essential Collection is a RANKED best-of — rotating it destroys "
            "the ranking that is its whole point"
        )

    # -- the behaviour ----------------------------------------------------

    def test_a_genre_playlist_is_reordered(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "helpers.config_helpers.get_config", lambda: {"playlists": {}}
        )
        source = tracks()
        out = fs()._weekly_shuffle(
            source, playlist_key="Rock - Top Tracks",
            scope="genre_top_tracks", when=WEEK_1,
        )
        assert [t["id"] for t in out] != [t["id"] for t in source]
        assert sorted(t["id"] for t in out) == sorted(t["id"] for t in source)

    def test_an_essential_collection_keeps_its_order(self, monkeypatch) -> None:
        """The regression: the global switch used to scramble every best-of."""
        monkeypatch.setattr(
            "helpers.config_helpers.get_config", lambda: {"playlists": {}}
        )
        source = tracks()
        out = fs()._weekly_shuffle(
            source, playlist_key="The Offspring - Essential Collection",
            scope="essential", when=WEEK_1,
        )
        assert [t["id"] for t in out] == [t["id"] for t in source], (
            "the Essential Collection was reordered — its ranking is information"
        )

    def test_the_master_switch_still_governs_everything(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "helpers.config_helpers.get_config",
            lambda: {"playlists": {"weekly_shuffle_enabled": False}},
        )
        source = tracks()
        for scope in ("genre_top_tracks", "new_music", "essential"):
            out = fs()._weekly_shuffle(
                source, playlist_key="Rock - Top Tracks", scope=scope, when=WEEK_1
            )
            assert [t["id"] for t in out] == [t["id"] for t in source]

    def test_a_family_can_be_turned_on(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "helpers.config_helpers.get_config",
            lambda: {"playlists": {"weekly_shuffle_essential": True}},
        )
        source = tracks()
        out = fs()._weekly_shuffle(
            source, playlist_key="The Offspring - Essential Collection",
            scope="essential", when=WEEK_1,
        )
        assert [t["id"] for t in out] != [t["id"] for t in source]

    def test_a_family_can_be_turned_off(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "helpers.config_helpers.get_config",
            lambda: {"playlists": {"weekly_shuffle_genre_top_tracks": False}},
        )
        source = tracks()
        out = fs()._weekly_shuffle(
            source, playlist_key="Rock - Top Tracks",
            scope="genre_top_tracks", when=WEEK_1,
        )
        assert [t["id"] for t in out] == [t["id"] for t in source]

    def test_an_explicit_enabled_flag_wins_over_the_family(self, monkeypatch) -> None:
        """The unit tests above force the decision; they must keep working."""
        monkeypatch.setattr(
            "helpers.config_helpers.get_config", lambda: {"playlists": {}}
        )
        source = tracks()
        out = fs()._weekly_shuffle(
            source, playlist_key="x", scope="essential", enabled=True, when=WEEK_1
        )
        assert [t["id"] for t in out] != [t["id"] for t in source]

    def test_no_scope_falls_back_to_the_master_switch_only(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "helpers.config_helpers.get_config", lambda: {"playlists": {}}
        )
        source = tracks()
        out = fs()._weekly_shuffle(source, playlist_key="x", when=WEEK_1)
        assert [t["id"] for t in out] != [t["id"] for t in source]

    def test_an_unreadable_config_turns_rotation_off(self, monkeypatch) -> None:
        def boom():
            raise RuntimeError("config unavailable")

        monkeypatch.setattr("helpers.config_helpers.get_config", boom)
        source = tracks()
        out = fs()._weekly_shuffle(
            source, playlist_key="x", scope="genre_top_tracks", when=WEEK_1
        )
        assert [t["id"] for t in out] == [t["id"] for t in source]

    # -- each builder passes the right family -----------------------------

    def test_every_builder_names_its_family(self) -> None:
        fsm = fs()
        expected = {
            "_create_genre_top_track_playlists": 'scope="genre_top_tracks"',
            "_create_new_music_playlist": 'scope="new_music"',
            "_sync_essential_playlist": 'scope="essential"',
        }
        for builder, token in expected.items():
            if not hasattr(fsm, builder):
                pytest.skip(f"{builder} not present")
            source = inspect.getsource(getattr(fsm, builder))
            assert token in source, (
                f"{builder} must declare its playlist family, or the per-family "
                "rotation switch cannot distinguish a chart from a best-of"
            )

    # -- the Config page carries every switch ----------------------------

    @pytest.mark.parametrize("tree", sorted(TREES))
    def test_the_family_switches_are_rendered(self, tree: str) -> None:
        template, _js = TREES[tree]
        text = template.read_text(encoding="utf-8")
        for element_id in (
            "playlists_shuffle_genre",
            "playlists_shuffle_new_music",
            "playlists_shuffle_essential",
        ):
            assert f'id="{element_id}"' in text

    @pytest.mark.parametrize("tree", sorted(TREES))
    def test_the_family_switches_are_collected(self, tree: str) -> None:
        _template, js = TREES[tree]
        source = js.read_text(encoding="utf-8")
        for element_id, key in (
            ("playlists_shuffle_genre", "weekly_shuffle_genre_top_tracks"),
            ("playlists_shuffle_new_music", "weekly_shuffle_new_music"),
            ("playlists_shuffle_essential", "weekly_shuffle_essential"),
        ):
            assert f"'{element_id}'" in source
            assert key in source

    @pytest.mark.parametrize("tree", sorted(TREES))
    def test_the_essential_switch_is_off_by_default_in_the_markup(self, tree: str) -> None:
        """The template default and the Python default must agree."""
        template, _js = TREES[tree]
        text = template.read_text(encoding="utf-8")
        line = next(
            ln for ln in text.splitlines() if 'id="playlists_shuffle_essential"' in ln
        )
        assert "weekly_shuffle_essential', False" in line


# ---------------------------------------------------------------------------
# 4. The Config page carries the switch (both trees — they must match)
# ---------------------------------------------------------------------------


class TestTheConfigPage:
    @pytest.mark.parametrize("tree", sorted(TREES))
    def test_the_shuffle_toggle_is_rendered(self, tree: str) -> None:
        template, _js = TREES[tree]
        text = template.read_text(encoding="utf-8")
        assert 'id="playlists_weekly_shuffle"' in text

    @pytest.mark.parametrize("tree", sorted(TREES))
    def test_the_shuffle_toggle_is_collected(self, tree: str) -> None:
        _template, js = TREES[tree]
        source = js.read_text(encoding="utf-8")
        assert "'playlists_weekly_shuffle'" in source
        assert "weekly_shuffle_enabled" in source

    @pytest.mark.parametrize("tree", sorted(TREES))
    def test_the_genre_ceiling_is_on_the_page_and_collected(self, tree: str) -> None:
        """The per-track genre count must stay configurable, not hard-coded."""
        template, js = TREES[tree]
        assert 'id="genre_selection_max"' in template.read_text(encoding="utf-8")
        assert "genre_selection_max" in js.read_text(encoding="utf-8")

    def test_the_two_trees_render_the_same_playlist_settings(self) -> None:
        """⭐ test_site was migrated to live; they must not drift again."""
        live_ids = set(
            re.findall(r'id="(playlists_[A-Za-z0-9_]+)"',
                       TREES["live"][0].read_text(encoding="utf-8"))
        )
        test_ids = set(
            re.findall(r'id="(playlists_[A-Za-z0-9_]+)"',
                       TREES["test_site"][0].read_text(encoding="utf-8"))
        )
        assert live_ids == test_ids, (
            f"only in live: {sorted(live_ids - test_ids)}; "
            f"only in test_site: {sorted(test_ids - live_ids)}"
        )
