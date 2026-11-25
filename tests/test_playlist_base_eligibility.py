"""One base eligibility for every generated playlist family.

The audit of the collection report found a gap between the families:

* genre Top Tracks already dropped live/excluded rows;
* decade playlists admitted live and ``exclude_from_stats`` tracks;
* covers admitted sub-four-star rows because the batch pass shared a 3★ pool
  and the builder never re-applied the configured floor;
* Essentials admitted ``exclude_from_stats`` rows and unscored rows.

These tests pin the shared floor: not live, not excluded, and scored.
"""

from __future__ import annotations

import inspect

from services.popularity.stages import finalise_stage as fs


class TestTheTrackEligibilityGate:
    def test_a_scoreless_row_is_not_a_candidate(self):
        assert not fs._track_is_playlist_eligible(
            {"title": "Song", "raw_final_score": None}
        )

    def test_a_scored_row_is_eligible(self):
        assert fs._track_is_playlist_eligible(
            {"title": "Song", "raw_final_score": 74.0}
        )

    def test_rows_without_the_key_are_not_newly_excluded(self):
        """Hand-built rows / older callers keep working."""
        assert fs._track_is_playlist_eligible({"title": "Song"})


class TestTheCuratedPoolUsesTheSameFloor:
    def test_the_shared_query_runs(self, db_session):
        """The added WHERE column must not break the shared fetch."""
        assert fs._fetch_curated_playlist_rows(min_stars=4) is not None

    def test_the_shared_query_drops_live_excluded_and_unscored(self):
        sql = " ".join(fs._CURATED_ROWS_SQL.split())
        assert "COALESCE(is_live, 0) = 0" in sql
        assert "COALESCE(exclude_from_stats, FALSE) = FALSE" in sql
        assert "final_score IS NOT NULL" in sql

    def test_the_genre_query_exposes_the_raw_score(self):
        sql = " ".join(fs._GENRE_ROWS_SQL.split())
        assert "final_score AS raw_final_score" in sql, (
            "without the raw column the eligibility gate cannot tell a scored "
            "row from one whose COALESCE fallback is 0"
        )


class TestEssentialsUseTheSameFloor:
    def test_the_builder_filters_excluded_and_unscored_rows(self):
        source = inspect.getsource(fs._sync_essential_playlist)
        assert "COALESCE(exclude_from_stats, FALSE) = FALSE" in source
        assert "final_score IS NOT NULL" in source
        assert "raw_final_score" in source, (
            "the Python merge filter must also drop merged featured rows that "
            "were never scored"
        )


class TestCoversReapplyTheConfiguredFloor:
    def test_a_three_star_cover_is_dropped_from_the_batch_pool(self, monkeypatch):
        captured: list[list[dict]] = []

        def _fake_sync(name, pool, **kwargs):
            captured.append(list(pool))
            return True

        monkeypatch.setattr(fs, "_sync_single_curated_playlist", _fake_sync)
        monkeypatch.setattr(
            fs, "_sweep_orphaned_covers_genre_playlists", lambda *a, **k: 0
        )

        rows = [
            {"id": "a", "title": "Cover A", "is_cover": 1, "stars": 5,
             "original_cover_artist": "X"},
            {"id": "b", "title": "Cover B", "is_cover": 1, "stars": 3,
             "original_cover_artist": "Y"},
        ]
        fs._create_covers_playlist(
            {"covers_playlist_enabled": True, "covers_min_stars": 4},
            rows=rows,
        )

        ids = [row["id"] for row in captured[0]]
        assert ids == ["a"], (
            "the batch pass shares a 3★ pool; the builder must re-apply the "
            "configured covers floor"
        )

    def test_zero_still_disables_the_floor(self, monkeypatch):
        captured: list[list[dict]] = []

        def _fake_sync(name, pool, **kwargs):
            captured.append(list(pool))
            return True

        monkeypatch.setattr(fs, "_sync_single_curated_playlist", _fake_sync)
        monkeypatch.setattr(
            fs, "_sweep_orphaned_covers_genre_playlists", lambda *a, **k: 0
        )

        rows = [
            {"id": "b", "title": "Cover B", "is_cover": 1, "stars": 3,
             "original_cover_artist": "Y"},
        ]
        fs._create_covers_playlist(
            {"covers_playlist_enabled": True, "covers_min_stars": 0},
            rows=rows,
        )

        assert [row["id"] for row in captured[0]] == ["b"]
