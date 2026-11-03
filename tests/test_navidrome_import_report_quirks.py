"""Navidrome import report: skipped album visibility, burst load, composer bloat.

REPORTED
--------
* Deftones imported albums 1,2,3,5,6 — ``album_index=4`` vanished with no
  ``Importing album`` line and no skip/error, so a filter/dedupe outcome was
  indistinguishable from a crash.
* 4 outer artist workers × 4 album-tracklist workers = up to 16 concurrent
  requests; Navidrome's ``Fetched X album(s)`` went from 0.0s to 6.7–9.8s.
* ``composer`` accumulated producer/engineer/band names and repeated names
  ("Terry Date", "Sam Hofstedt", "Deftones", `"A • A • B"`).
* An empty local ``disc_number``/``albumversion`` was protected against
  Navidrome's real value just because the row had a title correction.

These tests pin the four fixes: visible skip lines, a low outer worker count,
a composer credit that excludes non-composers and dedupes, and "an empty local
value is not a correction".
"""
from __future__ import annotations

import json
from pathlib import Path

from services.metadata.album_tag_sync_service import _db_tag_candidates
from services.scanning.metadata_extractor import extract_track_metadata
from services.scanning.reconciliation import local_authority_conflicts


# ===========================================================================
# 1. Composer: dedupe, and keep non-composers out
# ===========================================================================
class TestComposerIsAComposerCredit:
    def test_display_composer_loses_producers_engineers_and_the_band(self):
        """The reported `displayComposer` bloat."""
        extracted = extract_track_metadata({
            "id": "t1",
            "title": "Simple Man",
            "artist": "Deftones",
            "albumArtist": "Deftones",
            "displayComposer": (
                "Gary Rossington, Ronnie Van Zant • Gary Rossington • "
                "Ronnie Van Zant • Joe Fraulob • Deftones"
            ),
            "producer": "Terry Date",
            "engineer": "Sam Hofstedt",
        })
        assert extracted["composer"] == (
            "Gary Rossington, Ronnie Van Zant, Joe Fraulob"
        ), (
            "repeated names must collapse and producer/engineer/band credits "
            "must not land in composer"
        )

    def test_an_explicit_composer_tag_is_deduped(self):
        extracted = extract_track_metadata({
            "id": "t1",
            "title": "Song",
            "artist": "A Perfect Circle",
            "composer": "Billy Howerdel, Billy Howerdel; Maynard James Keenan",
            "producer": "Billy Howerdel",
        })
        assert extracted["composer"] == "Billy Howerdel, Maynard James Keenan"

    def test_tag_sync_does_not_copy_the_mixed_writer_store(self):
        """``writer`` holds producers/engineers too; COMPOSER must not."""
        candidates = _db_tag_candidates({
            "title": "Simple Man",
            "artist": "Deftones",
            "album_artist": "Deftones",
            "composer": "",
            "writer": json.dumps([
                "Gary Rossington", "Ronnie Van Zant", "Terry Date",
                "Deftones", "Joe Fraulob",
            ]),
            "producer": "Terry Date",
        })
        assert candidates["composer"] == (
            "Gary Rossington, Ronnie Van Zant, Joe Fraulob"
        ), (
            "the COMPOSER tag is being built from writer credits, which include "
            "producers and the performing band"
        )

    def test_tag_sync_prefers_the_composer_column(self):
        candidates = _db_tag_candidates({
            "title": "Song",
            "artist": "A Perfect Circle",
            "album_artist": "A Perfect Circle",
            "composer": "Billy Howerdel, Maynard James Keenan",
            "writer": json.dumps(["Terry Date", "A Perfect Circle"]),
            "producer": "Terry Date",
        })
        assert candidates["composer"] == "Billy Howerdel, Maynard James Keenan"


# ===========================================================================
# 2. An empty local value is not a local correction
# ===========================================================================
class TestEmptyLocalNeverBlocksTheProvider:
    def test_an_empty_local_disc_number_accepts_the_remote_value(self):
        assert local_authority_conflicts(
            {"title": "Edited", "disc_number": ""},
            {"title": "Edited", "disc_number": 1},
        ) == [], (
            "the row-level dirty flag made one title edit freeze every empty "
            "authority field"
        )

    def test_an_empty_local_albumversion_accepts_the_remote_value(self):
        assert local_authority_conflicts(
            {"albumversion": ""},
            {"albumversion": "B-Sides & Rarities"},
        ) == []

    def test_a_real_local_correction_is_still_protected(self):
        assert local_authority_conflicts(
            {"title": "My Correction"},
            {"title": "Different"},
        ) == ["title"]

    def test_the_upsert_sql_has_the_empty_local_branch(self):
        src = Path("db/repositories/popularity_repository.py").read_text(
            encoding="utf-8"
        )
        assert "TRIM(CAST(tracks.{k} AS TEXT)) = ''" in src, (
            "a dirty row must still accept a provider value for a field the "
            "local side never set"
        )


# ===========================================================================
# 3. Bullet-grouped credits compare as a SET
# ===========================================================================
class TestBulletGroupedPeopleCompareAsASet:
    def test_the_same_names_in_another_grouping_are_not_a_conflict(self):
        assert local_authority_conflicts(
            {"composer": "Gary Rossington, Ronnie Van Zant"},
            {"composer": "Gary Rossington • Ronnie Van Zant"},
        ) == []

    def test_a_genuinely_extra_composer_is_still_a_conflict(self):
        assert local_authority_conflicts(
            {"composer": "Gary Rossington • Ronnie Van Zant"},
            {"composer": "Gary Rossington • Ronnie Van Zant • Joe Fraulob"},
        ) == ["composer"]

    def test_a_bloated_composer_equals_the_cleaned_provider_value(self):
        """The stored bloat is healed, not protected forever."""
        local = {
            "composer": (
                "Gary Rossington • Ronnie Van Zant • Terry Date • Deftones"
            ),
            "producer": "Terry Date",
            "artist": "Deftones",
        }
        remote = {
            "composer": "Gary Rossington • Ronnie Van Zant",
            "producer": "Terry Date",
            "artist": "Deftones",
        }
        assert local_authority_conflicts(local, remote) == [], (
            "names credited to producer/artist roles must not keep a bloated "
            "composer marked as a local correction"
        )


# ===========================================================================
# 4. Visible skips + bounded artist concurrency
# ===========================================================================
class TestPrefetchColumnsExist:
    def test_every_prefetch_column_is_a_real_tracks_column(self):
        """The reported crash: ``column "producer" does not exist``.

        ``tracks`` has no producer/engineer/mixer columns (only composer and
        lyricist). Selecting one made EVERY artist scan fail.
        """
        from db.models import Track
        from services.scanning.navidrome_import import _PREFETCH_TRACK_COLUMNS

        columns = {column.name for column in Track.__table__.columns}
        missing = [
            name for name in _PREFETCH_TRACK_COLUMNS if name not in columns
        ]
        assert missing == [], (
            f"prefetch selects non-existent tracks columns: {missing}"
        )


class TestImportVisibilityAndLoad:
    def test_a_skipped_album_index_is_logged(self):
        src = Path("services/scanning/navidrome_import.py").read_text(
            encoding="utf-8"
        )
        for reason in (
            "(album filter / nothing to import)",
            "(no Navidrome album id)",
            "(already imported under another artist view)",
            "track(s) already imported under",
        ):
            assert reason in src, (
                f"an album skipped by {reason!r} still disappears without a "
                "scan-log line"
            )

    def test_the_outer_artist_pool_defaults_to_two(self):
        src = Path(
            "services/scanning/pipelines/navidrome_pipeline.py"
        ).read_text(encoding="utf-8")
        assert '"POPULARLR_IMPORT_WORKERS", "2"' in src, (
            "4 artists × 4 album workers is 16 concurrent Navidrome requests; "
            "the default must stay low"
        )

        from services.scanning.pipelines import navidrome_pipeline as pipeline

        assert 1 <= pipeline._PARALLEL_WORKERS <= 4
