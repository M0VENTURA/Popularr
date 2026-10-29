"""Per-genre Great Covers playlists.

REPORT: *"For the creation of the playlist Great Covers, can we also adjust so
that it will create a different one for different Genre's, but limit it to the
main genres such as Rock, Pop, Punk, Metal, etc and not include sub genres."*

What is pinned here:

* Only vocabulary ROOT genres are offered and generated — ``Nu Metal`` folds
  into Metal, ``Grunge`` into Rock, ``Pop Punk`` into Punk.
* The split is opt-in; with it off only the library-wide ``Great Covers``
  playlist is built and stale per-genre lists are swept.
* A genre is created only at or above its minimum and capped at its maximum,
  mirroring Essential collections (below-minimum playlists are deleted).
* The Config page exposes every setting and reads it back on save.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers.config_helpers import get_playlists_config
from services.playlists import main_genres
from services.playlists.playlist_taxonomy import (
    CURATED,
    DECADE,
    GENRE,
    classify_managed_playlist,
)
from services.popularity.stages import finalise_stage as fs

REPO = Path(__file__).resolve().parent.parent
CONFIG_TEMPLATE = REPO / "test_site" / "templates" / "Pages" / "config.html"
CONFIG_JS = REPO / "test_site" / "static" / "js" / "pages" / "config.js"


class TestMainGenreTaxonomy:
    def test_subgenres_fold_into_their_main_genre(self):
        assert main_genres.classify_main_genre(["Nu Metal"])["id"] == "metal"
        assert main_genres.classify_main_genre(
            ["Grunge", "Alternative Rock"]
        )["id"] == "rock"
        assert main_genres.classify_main_genre(["Pop Punk"])["id"] == "punk"
        assert main_genres.classify_main_genre(["Synthpop"])["id"] == "electronic"

    def test_non_genres_never_vote(self):
        assert main_genres.classify_main_genre(
            ["dark", "Australian", "live"]
        ) is None

    def test_choices_are_root_genres_only(self):
        from services.enrichment.genre_vocabulary import get_vocabulary

        vocab = get_vocabulary()
        choices = main_genres.main_genre_choices()
        ids = {choice["id"] for choice in choices}

        assert {"rock", "pop", "punk", "metal"} <= ids
        assert all(vocab.genres[genre_id].specificity == 0 for genre_id in ids)
        assert "nu_metal" not in ids
        assert "grunge" not in ids

    def test_display_names_are_the_vocabulary_displays(self):
        choices = {c["id"]: c["display"] for c in main_genres.main_genre_choices()}
        assert choices["r_and_b"] == "R&B"
        assert choices["hip_hop"] == "Hip-Hop"


class TestConfig:
    def test_defaults_keep_great_covers_as_the_default_shape(self, monkeypatch):
        import helpers.config_helpers as ch

        monkeypatch.setattr(ch, "get_config", lambda: {})
        cfg = get_playlists_config()

        assert cfg["covers_playlist_enabled"] is False
        assert cfg["covers_genre_playlists_enabled"] is False
        assert cfg["covers_genre_playlists_name_template"] == "{genre} - Great Covers"
        assert cfg["covers_genre_playlists_genres"] == [
            "rock", "pop", "punk", "metal",
        ]
        assert cfg["covers_genre_playlists_min_tracks"] == 12
        assert cfg["covers_genre_playlists_max_tracks"] == 100

    def test_selection_is_normalised(self, monkeypatch):
        import helpers.config_helpers as ch

        monkeypatch.setattr(
            ch,
            "get_config",
            lambda: {"playlists": {
                "covers_genre_playlists_genres": ["Rock", "R&B", "rock"],
            }},
        )
        assert get_playlists_config()["covers_genre_playlists_genres"] == [
            "rock", "r_and_b",
        ]

    def test_a_hand_written_subgenre_becomes_its_main_genre(self, monkeypatch):
        import helpers.config_helpers as ch

        monkeypatch.setattr(
            ch, "get_config",
            lambda: {"playlists": {
                "covers_genre_playlists_genres": ["Nu Metal", "Grunge"],
            }},
        )
        assert get_playlists_config()["covers_genre_playlists_genres"] == [
            "metal", "rock",
        ]

    def test_an_explicit_empty_selection_is_a_real_choice(self, monkeypatch):
        import helpers.config_helpers as ch

        monkeypatch.setattr(
            ch, "get_config",
            lambda: {"playlists": {"covers_genre_playlists_genres": []}},
        )
        assert get_playlists_config()["covers_genre_playlists_genres"] == []


class TestTaxonomyOwnership:
    def test_a_genre_covers_name_is_curated(self):
        cfg = {"covers_genre_playlists_name_template": "{genre} - Great Covers"}
        assert classify_managed_playlist("Metal - Great Covers", config=cfg) == CURATED

    def test_a_decade_name_is_still_a_decade(self):
        cfg = {"covers_genre_playlists_name_template": "{genre} - Great Covers"}
        assert classify_managed_playlist("1980s - Top Tracks", config=cfg) == DECADE

    def test_a_colliding_template_does_not_steal_genre_ownership(self):
        cfg = {"covers_genre_playlists_name_template": "{genre} - Top Tracks"}
        assert classify_managed_playlist("Rock - Top Tracks", config=cfg) == GENRE


def _row(track_id: str, title: str, genres: str, *, is_cover: int = 1) -> dict:
    return {
        "id": track_id,
        "title": title,
        "artist": "Artist",
        "album_artist": "Artist",
        "genres": genres,
        "stars": 4,
        "is_cover": is_cover,
        "is_live": 0,
        "is_compilation": 0,
        "score": 1.0,
        "popularity_score": 1.0,
        "final_score": 1.0,
    }


def _cfg(**overrides) -> dict:
    cfg = {
        "covers_playlist_enabled": True,
        "covers_playlist_name": "Great Covers",
        "covers_max_tracks": 150,
        "covers_genre_playlists_enabled": True,
        "covers_genre_playlists_name_template": "{genre} - Great Covers",
        "covers_genre_playlists_genres": ["rock", "pop", "punk", "metal"],
        "covers_genre_playlists_min_tracks": 12,
        "covers_genre_playlists_max_tracks": 100,
        "genre_playlists_max_genres": 3,
        "playlist_order_mode": "final_score",
        "playlist_interleave_artists": True,
    }
    cfg.update(overrides)
    return cfg


class TestGenreCoversBuilder:
    @pytest.fixture()
    def capture(self, monkeypatch):
        synced: list[tuple[str, int, int | None]] = []
        deleted: list[str] = []
        swept: list[set[str]] = []

        def _fake_sync(name, pool, *, max_tracks, cfg, **kwargs):
            synced.append((name, len(pool), max_tracks))
            return True

        monkeypatch.setattr(fs, "_sync_single_curated_playlist", _fake_sync)
        monkeypatch.setattr(
            fs, "_delete_playlist_from_navidrome",
            lambda name: deleted.append(name) or True,
        )
        monkeypatch.setattr(
            fs, "_sweep_orphaned_covers_genre_playlists",
            lambda keep, cfg=None: swept.append(set(keep)) or 0,
        )
        return synced, deleted, swept

    def _rows(self) -> list[dict]:
        rows = [
            _row(f"m{i}", f"Metal Song {i}", '["Nu Metal"]') for i in range(13)
        ]
        rows += [
            _row(f"r{i}", f"Rock Song {i}", '["Grunge"]') for i in range(5)
        ]
        rows += [
            # Synthpop belongs to the ELECTRONIC family, which is not selected.
            _row(f"e{i}", f"Electronic Song {i}", '["Synthpop"]') for i in range(20)
        ]
        rows.append(_row("not-a-cover", "Ordinary Song", '["Rock"]', is_cover=0))
        return rows

    def test_creates_one_playlist_per_selected_main_genre_at_or_above_min(
        self, capture,
    ):
        synced, deleted, swept = capture

        fs._create_covers_playlist(_cfg(), rows=self._rows())

        names = {name: (count, cap) for name, count, cap in synced}

        # The library-wide playlist still exists — the split is ADDITIONAL.
        # It holds every cover, including genres not selected for a split.
        assert names["Great Covers"][0] == 38
        assert names["Metal - Great Covers"] == (13, 100)

        # Rock has only 5 covers, below the 12-track floor.
        assert "Rock - Great Covers" not in names
        assert "Rock - Great Covers" in deleted

        # Electronic was not selected, so no bucket is created at all.
        assert not any("Electronic" in name for name in names)

        # The sweep keeps exactly what this pass produced.
        assert swept == [{"Metal - Great Covers"}]

    def test_the_maximum_cap_is_passed_to_the_sync(self, capture):
        synced, _deleted, _swept = capture

        fs._create_covers_playlist(
            _cfg(covers_genre_playlists_max_tracks=7), rows=self._rows(),
        )

        caps = {name: cap for name, _count, cap in synced}
        assert caps["Great Covers"] == 150  # single-playlist cap unchanged
        assert caps["Metal - Great Covers"] == 7

    def test_split_off_builds_only_the_single_playlist_and_sweeps_genres(
        self, capture,
    ):
        synced, _deleted, swept = capture

        fs._create_covers_playlist(
            _cfg(covers_genre_playlists_enabled=False), rows=self._rows(),
        )

        assert [name for name, _count, _cap in synced] == ["Great Covers"]
        assert swept == [set()]

    def test_an_empty_selection_creates_no_genre_playlists(self, capture):
        synced, _deleted, swept = capture

        fs._create_covers_playlist(
            _cfg(covers_genre_playlists_genres=[]), rows=self._rows(),
        )

        assert [name for name, _count, _cap in synced] == ["Great Covers"]
        assert swept == [set()]

    def test_regenerating_a_genre_covers_name_runs_the_family_builder(
        self, monkeypatch,
    ):
        calls: list[dict] = []
        monkeypatch.setattr(
            fs, "_create_covers_playlist",
            lambda cfg, *a, **k: calls.append(cfg) or True,
        )

        ok = fs._regenerate_curated_playlist("Metal - Great Covers", _cfg())

        assert ok is True
        assert len(calls) == 1


class TestConfigSurface:
    async def test_the_page_renders_the_main_genre_checkboxes(self, client):
        resp = await client.get("/config")
        assert resp.status_code == 200
        html = await resp.get_data(as_text=True)

        for genre_id in ("rock", "pop", "punk", "metal"):
            assert f'id="playlists_covers_genre_{genre_id}"' in html, genre_id
        assert 'id="playlists_covers_genre_min_tracks"' in html

    def test_template_exposes_every_setting(self):
        html = CONFIG_TEMPLATE.read_text(encoding="utf-8")
        for element_id in (
            "playlists_covers_genre_enabled",
            "playlists_covers_genre_min_tracks",
            "playlists_covers_genre_max_tracks",
            "playlists_covers_genre_name_template",
        ):
            assert f'id="{element_id}"' in html, element_id
        assert "playlist-cover-genre" in html

    def test_js_collects_every_setting(self):
        js = CONFIG_JS.read_text(encoding="utf-8")
        for key in (
            "covers_genre_playlists_enabled",
            "covers_genre_playlists_name_template",
            "covers_genre_playlists_min_tracks",
            "covers_genre_playlists_max_tracks",
            "covers_genre_playlists_genres",
        ):
            assert f"{key}:" in js, key
        assert ".playlist-cover-genre:checked" in js
