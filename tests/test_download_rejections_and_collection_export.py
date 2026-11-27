"""The reported false rejections, and the slow collection export.

Reported:

1. *"Different edition — the file is 'non album bonus track' but the queue item
   has none"* for Britney Spears — (I've Just Begun) Having My Fun. The file was
   tagged "(Non-Album Bonus Track)"; the MusicBrainz queue title carries
   "(Non-album)". Both name the SAME recording.

2. *"Wrong length — the file is 4:24 but the queue item is 4:29 (5.0s apart,
   tolerance 5s)"*, with *"5 seconds and under should be approved"*.

3. *"collection csv seems to take a long time to complete"*.
"""

from __future__ import annotations

import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. A bonus / non-album annotation does not denote a different recording
# ---------------------------------------------------------------------------

class TestTheNonAlbumBonusTrackIsNotADifferentEdition:
    FILE_TITLE = "(I've Just Begun) Having My Fun (Non-Album Bonus Track)"
    QUEUE_TITLE = "(I've Just Begun) Having My Fun (Non-album)"

    def test_the_reported_pair_is_compatible(self):
        from helpers.normalization_service import edition_annotations_compatible

        assert edition_annotations_compatible(self.FILE_TITLE, self.QUEUE_TITLE) is True

    def test_the_file_annotation_is_still_recognised(self):
        """It must be SEEN, then judged neutral — not silently missed."""
        from helpers.normalization_service import extract_edition_annotation

        assert extract_edition_annotation(self.FILE_TITLE) == "non album bonus track"

    @pytest.mark.parametrize(
        "title",
        ["Song (Bonus Track)", "Song (Non-Album Bonus Track)", "Song (Bonus)"],
    )
    def test_bonus_forms_are_neutral(self, title):
        from helpers.normalization_service import edition_annotations_compatible

        assert edition_annotations_compatible(title, "Song") is True

    @pytest.mark.parametrize(
        "title",
        [
            "Song (Epic Edition)",
            "Song (Deluxe Edition)",
            "Song (Remastered)",
            "Song (Limited Edition)",
            "Song (Anniversary Edition)",
        ],
    )
    def test_a_real_edition_still_refuses_to_collapse(self, title):
        """The gate must keep working — this is what stops a wrong import."""
        from helpers.normalization_service import edition_annotations_compatible

        assert edition_annotations_compatible(title, "Song") is False


# ---------------------------------------------------------------------------
# 2. A difference that DISPLAYS as within tolerance must be accepted
# ---------------------------------------------------------------------------

class TestTheLengthVerdictMatchesTheNumberItPrints:
    def test_the_verdict_uses_the_rounded_difference(self):
        """`diff > tolerance` on the raw float contradicted its own sentence."""
        source = _src("services/queue/queue_metadata_matcher.py")
        start = source.index("_tolerance = _length_tolerance_seconds()")
        block = source[start:start + 2200]
        assert "_reported_diff = round(float(diff), 1)" in block
        assert "if _reported_diff > _tolerance:" in block, (
            "comparing the unrounded diff rejected a 5.02s gap that the message "
            "printed as '5.0s apart, tolerance 5s'"
        )
        assert "length_difference_seconds=_reported_diff" in block, (
            "the reported number and the compared number must be the same"
        )

    @pytest.mark.parametrize(
        "diff, tolerance, accepted",
        [
            (5.0, 5.0, True),    # exactly at the tolerance
            (5.02, 5.0, True),   # prints "5.0s" -> must pass
            (5.04, 5.0, True),   # still prints "5.0s"
            (5.06, 5.0, False),  # prints "5.1s" -> genuinely over
            (4.0, 5.0, True),
        ],
    )
    def test_boundary_arithmetic(self, diff, tolerance, accepted):
        """Mirror the exact comparison, so the rule is pinned."""
        reported = round(float(diff), 1)
        assert (reported <= tolerance) is accepted


# ---------------------------------------------------------------------------
# 3. The collection export must not wait for a playlist sync
# ---------------------------------------------------------------------------

class TestTheCollectionExportIsNotBlockedByThePlaylistSync:
    def _worker(self) -> str:
        source = _src("services/popularity/popularity_report_export_service.py")
        start = source.index("def _worker(")
        return source[start:source.index("def create_report_job(")]

    def test_the_refresh_is_conditional_on_an_empty_index(self):
        body = self._worker()
        assert "if not playlist_index_has_entries():" in body, (
            "a populated index is enough to build the CSV; a full Navidrome "
            "re-sync of every changed playlist must not gate the file"
        )

    def test_the_csv_is_written_before_the_trailing_refresh(self):
        body = self._worker()
        write_at = body.index('handle.write(report)')
        done_at = body.index('"status": "done"')
        refresh_at = body.index("Refreshing the playlist index")
        assert write_at < done_at < refresh_at, (
            "the file must be downloadable before housekeeping starts"
        )

    def test_the_phase_is_reported(self):
        body = self._worker()
        assert 'job["phase"]' in body
        source = _src("test_site/static/js/pages/config.js")
        assert "job.phase" in source, "the UI must be able to show the phase"

    def test_the_empty_index_probe_exists(self):
        source = _src("services/popularity/popularity_report_service.py")
        assert "def playlist_index_has_entries()" in source
        assert "FROM playlist_entries LIMIT 1" in source

    def test_the_redundant_body_copy_is_gone(self):
        body = self._worker()
        assert 'body = "\\ufeff" + ' not in body
        assert 'handle.write("\\ufeff")' in body
        assert "os.path.getsize(path)" in body, (
            "encoding the whole CSV again just to measure it is wasted work"
        )

    def test_the_job_still_carries_its_path(self):
        """report_file(job_id) resolves the download from this field."""
        body = self._worker()
        assert '"path": path' in body
