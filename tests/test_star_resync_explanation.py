"""The resync payload must explain itself.

Two additions this session, both pinned here so a refactor cannot quietly drop
them:

  * ``changed_tracks[*].reason`` — WHY each rating moved, read from the verdict
    fields the rules already write;
  * ``star_histogram`` / ``five_star_albums`` — the shape of the library after
    the run, which is the fastest way to tell whether a retune went too harsh or
    too generous.
"""

from __future__ import annotations

import inspect

import pytest

from services.popularity import star_resync_service as svc


class TestTheReasonIsCarried:
    def test_the_row_carries_a_reason(self) -> None:
        source = inspect.getsource(svc._compute_stars_for_album)
        assert '"reason": star_rating_reason(r)' in source, (
            "the preview can only explain a change if the row carries the reason"
        )

    def test_the_reason_comes_from_the_shared_builder(self) -> None:
        """One explanation implementation, not a second copy in the service."""
        source = inspect.getsource(svc._compute_stars_for_album)
        assert "from services.popularity.stages.finalise_stage import star_rating_reason" in source


class TestTheHistogramIsCarried:
    def test_the_summary_has_a_histogram(self) -> None:
        source = inspect.getsource(svc.resync_star_ratings)
        assert '"star_histogram"' in source
        assert '"before": star_histogram_before' in source
        assert '"after": star_histogram_after' in source

    def test_the_summary_lists_the_five_star_albums(self) -> None:
        source = inspect.getsource(svc.resync_star_ratings)
        assert '"five_star_albums"' in source

    def test_it_counts_every_rated_track_not_just_changed_ones(self) -> None:
        """A histogram over the DIFF would describe the change, not the library."""
        source = inspect.getsource(svc.resync_star_ratings)
        loop = source.split("for row in rows:", 1)[1].split("five_star_albums.sort", 1)[0]
        assert "star_histogram_before[" in loop
        assert "star_histogram_after[" in loop
        # ...and the change filter is separate from the counting.
        assert 'if row["before"] != row["after"]:' in loop


class TestTheFrontEndRendersThem:
    @pytest.mark.parametrize(
        "path",
        ["test_site/static/js/pages/config.js", "static/js/config.js"],
    )
    def test_both_collectors_render_the_histogram(self, path: str) -> None:
        import pathlib

        src = pathlib.Path(path).read_text(encoding="utf-8")
        assert "function starHistogramHtml" in src, f"{path} cannot render the histogram"
        assert "starHistogramHtml(data.star_histogram" in src, f"{path} never calls it"

    @pytest.mark.parametrize(
        "path",
        ["test_site/static/js/pages/config.js", "static/js/config.js"],
    )
    def test_both_collectors_render_the_reason(self, path: str) -> None:
        import pathlib

        src = pathlib.Path(path).read_text(encoding="utf-8")
        assert "t.reason" in src, f"{path} ignores the per-track reason"
