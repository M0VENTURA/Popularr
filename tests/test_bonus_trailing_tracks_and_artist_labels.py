"""Bonus/trailing missing tracks, artist labels, and the permissive guest fold.

Reported:

* A compilation's missing bonus tracks at the END of the release were shown as
  corrections. They should be a GREEN "Bonus Tracks to Download" label and must
  not put the artist in "needs fixing".
* Every correction type should be a label next to the artist.
* A track artist with a featured credit still raised "duplicate artists sharing
  an MBID".
"""

from __future__ import annotations

from sqlalchemy import create_engine, text

from services.metadata.album_missing_service import is_bonus_missing_track


class TestBonusClassification:
    def test_a_track_after_the_last_library_track_is_bonus(self) -> None:
        assert is_bonus_missing_track(
            {"title": "Outtake", "track_number": "12", "disc_number": 1},
            {1: 10},
        )

    def test_a_gap_before_the_last_track_is_not_bonus(self) -> None:
        assert not is_bonus_missing_track(
            {"title": "Track 5", "track_number": "5", "disc_number": 1},
            {1: 10},
        )

    def test_an_explicit_bonus_title_is_bonus_without_positions(self) -> None:
        assert is_bonus_missing_track(
            {"title": "Song (Bonus Track)", "track_number": "4"}, {}
        )

    def test_no_positions_and_no_marker_is_not_bonus(self) -> None:
        assert not is_bonus_missing_track(
            {"title": "Song", "track_number": "4"}, {}
        )

    def test_a_disc_without_a_library_position_is_not_trailing(self) -> None:
        assert not is_bonus_missing_track(
            {"title": "Song", "track_number": "4", "disc_number": 2}, {1: 10}
        )


class TestTheArtistMissingMbAggregate:
    def _rows(self):
        engine = create_engine("sqlite://")
        from routes.artist_routes import ARTIST_MISSING_MB_SQL

        with engine.connect() as conn:
            conn.execute(
                text(
                    "CREATE TABLE missing_album_tracks ("
                    "artist_name TEXT, is_bonus BOOLEAN, ignored BOOLEAN)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO missing_album_tracks "
                    "(artist_name, is_bonus, ignored) VALUES "
                    "('Bonus Only', 1, 0),"
                    "('Bonus Only', 1, 0),"
                    "('Gap Artist', 1, 0),"
                    "('Gap Artist', 0, 0),"
                    "('Gap Artist', 0, 0),"
                    "('Ignored', 1, 1)"
                )
            )
            conn.commit()
            return {
                row._mapping["artist_key"]: dict(row._mapping)
                for row in conn.execute(text(ARTIST_MISSING_MB_SQL)).fetchall()
            }

    def test_bonus_and_core_counts_are_split(self) -> None:
        rows = self._rows()
        assert rows["bonus only"]["bonus_tracks_count"] == 2
        assert rows["bonus only"]["missing_mb_tracks_count"] == 0
        assert rows["gap artist"]["bonus_tracks_count"] == 1
        assert rows["gap artist"]["missing_mb_tracks_count"] == 2

    def test_ignored_rows_are_excluded(self) -> None:
        assert "ignored" not in self._rows()


class TestTheMergeMakesBonusNonFlagging:
    class _Row:
        def __init__(self, mapping):
            self._mapping = mapping

    def _entry(self, **mapping):
        from routes.artist_routes import _merge_artist_missing_mb_counts

        out: dict = {}
        _merge_artist_missing_mb_counts(out, [self._Row(mapping)])
        return out[str(mapping["artist_key"])]

    def test_bonus_only_artist_is_not_needs_fixing(self) -> None:
        entry = self._entry(
            artist_key="bonus only",
            artist_name="Bonus Only",
            bonus_tracks_count=3,
            missing_mb_tracks_count=0,
        )
        assert entry["needs_correction"] is False
        assert entry["bonus_tracks_count"] == 3

    def test_a_real_gap_still_needs_fixing(self) -> None:
        entry = self._entry(
            artist_key="gap artist",
            artist_name="Gap Artist",
            bonus_tracks_count=1,
            missing_mb_tracks_count=2,
        )
        assert entry["needs_correction"] is True
        assert entry["missing_mb_tracks_count"] == 2


class TestTheArtistListLabels:
    @staticmethod
    def _js() -> str:
        from pathlib import Path

        return (
            Path(__file__).resolve().parents[1]
            / "test_site/static/js/pages/artists.js"
        ).read_text(encoding="utf-8")

    def test_every_correction_type_has_a_label(self) -> None:
        js = self._js()
        for label in (
            "duplicate track",
            "disc number inconsistency",
            "missing MusicBrainz ID",
            "missing audio file",
            "missing track",
        ):
            assert label in js

    def test_bonus_is_a_green_separate_badge(self) -> None:
        js = self._js()
        assert "artist-bonus-tracks" in js
        assert "bg-success-subtle" in js
        assert "to download" in js
        # It must NOT be inside PROBLEM_LABELS: that object is the filter
        # predicate, and a bonus track must not flag the artist.
        block = js[js.index("const PROBLEM_LABELS"): js.index("const BONUS_LABEL")]
        assert "bonus_tracks_count" not in block

    def test_the_template_has_a_label_container(self) -> None:
        from pathlib import Path

        html = (
            Path(__file__).resolve().parents[1]
            / "test_site/templates/Pages/artist_list.html"
        ).read_text(encoding="utf-8")
        assert "artist-correction-labels" in html


class TestFeaturedCreditsDoNotRaiseADuplicate:
    def test_a_permissive_fold_covers_and_and_with(self) -> None:
        from routes.misc_routes import _collapse_duplicate_artist_rows

        rows = [
            {"artist": "Powerwolf", "track_count": 10},
            {"artist": "Powerwolf & Unleash the Archers", "track_count": 1},
            {"artist": "Powerwolf with Alissa White-Gluz", "track_count": 1},
        ]

        narrow = _collapse_duplicate_artist_rows(rows)
        permissive = _collapse_duplicate_artist_rows(rows, permissive=True)

        assert len(narrow) == 3, "the merge list must stay credit-aware"
        assert len(permissive) == 1
        assert permissive[0]["artist"] == "Powerwolf"

    def test_the_endpoint_uses_the_permissive_fold_for_the_decision(self) -> None:
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1] / "routes/misc_routes.py"
        ).read_text(encoding="utf-8")
        assert "permissive=True" in source
        assert "if _has_duplicate_artist_variants(decision_data):" in source
