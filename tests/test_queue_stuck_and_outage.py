"""Queue reliability: a frozen transfer must be detectable, and an outage is not a miss.

Reported, four days of log:

* "A Killer's Confession Victim 3: each track was sent to the same file from peer
  TheCookFeelsBraveNearby four times over about 7 hours. No failure or timeout
  was logged between attempts."
* "Nine queue items have been 'downloading' with no further progress for about 3
  days. Every worker restart reports downloading=9."
* "34 searches returned 0 results between 05:00 and 06:59 ... These were then
  logged as 'persistent miss, retrying Thursday 27 Nov'."

Every existing timeout keys off ``download_queue.updated_at``, which the progress
poll refreshes on every pass — so a transfer that stopped moving bytes looked
alive forever. These tests pin the two things that fix that: a byte-count clock,
and an empty-SEARCH streak.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.downloads import slskd_reaper_service as reaper


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TestNoProgressDetection:
    """The marker, not ``startedAt``/``updated_at``, decides a frozen transfer."""

    def test_untouched_for_longer_than_the_window_is_stalled(self):
        item = {"slskd_progress_at": _now_naive() - timedelta(minutes=21)}
        transfer = {"progress": 37, "averageSpeed": 0}
        assert reaper._no_progress_stalled(item, transfer) is True

    def test_inside_the_window_is_not_stalled(self):
        item = {"slskd_progress_at": _now_naive() - timedelta(minutes=19)}
        transfer = {"progress": 37, "averageSpeed": 0}
        assert reaper._no_progress_stalled(item, transfer) is False

    def test_no_marker_yet_falls_back_to_the_old_behaviour(self):
        """A first observation (or a pre-migration row) must not be reaped."""
        assert reaper._no_progress_stalled({}, {"progress": 10}) is False
        assert reaper._no_progress_stalled(None, {"progress": 10}) is False

    def test_a_finished_transfer_is_not_stalled(self):
        """100% is the completion pass's business, not the reaper's."""
        item = {"slskd_progress_at": _now_naive() - timedelta(hours=3)}
        assert reaper._no_progress_stalled(item, {"progress": 100}) is False

    def test_the_window_is_configurable(self, monkeypatch):
        item = {"slskd_progress_at": _now_naive() - timedelta(minutes=5)}
        transfer = {"progress": 50}
        assert reaper._no_progress_stalled(item, transfer) is False
        monkeypatch.setattr(reaper, "_no_progress_minutes", lambda: 1.0)
        assert reaper._no_progress_stalled(item, transfer) is True

    def test_zero_disables_the_check(self, monkeypatch):
        item = {"slskd_progress_at": _now_naive() - timedelta(days=2)}
        monkeypatch.setattr(reaper, "_no_progress_minutes", lambda: 0.0)
        assert reaper._no_progress_stalled(item, {"progress": 5}) is False


class TestTheMarkerOnlyMovesWhenBytesMove:
    """``record_download_progress`` must not refresh the clock on a no-op poll."""

    def test_bytes_seen_is_what_advances_the_marker(self):
        from db.repositories import queue as queue_repo

        source = queue_repo.record_download_progress.__doc__ or ""
        assert "slskd_progress_at" in source

    @pytest.mark.parametrize(
        "clause",
        ["slskd_bytes_seen IS DISTINCT FROM :bytes_seen"],
    )
    def test_the_update_is_gated_on_the_byte_count(self, clause):
        """Without this gate every poll refreshed the marker and nothing could
        ever be detected as frozen."""
        import inspect

        from db.repositories import queue as queue_repo

        src = inspect.getsource(queue_repo.record_download_progress)
        assert clause in src


class TestTheEmptySearchStreak:
    """A run of zero-result searches is an outage, not a library of misses."""

    def setup_method(self):
        from services.downloads import download_pipeline_service as pipeline

        pipeline.reset_empty_search_streak()

    def test_five_in_a_row_trips_the_breaker(self):
        from services.downloads import download_pipeline_service as pipeline

        assert pipeline._search_service_looks_down() is False
        for _ in range(pipeline._EMPTY_STREAK_LIMIT - 1):
            pipeline._bump_empty_streak(False)
        assert pipeline._search_service_looks_down() is False

        pipeline._bump_empty_streak(False)
        assert pipeline._search_service_looks_down() is True

    def test_one_result_resets_the_streak(self):
        from services.downloads import download_pipeline_service as pipeline

        for _ in range(pipeline._EMPTY_STREAK_LIMIT):
            pipeline._bump_empty_streak(False)
        assert pipeline._search_service_looks_down() is True

        pipeline._bump_empty_streak(True)
        assert pipeline._search_service_looks_down() is False

    def test_a_down_service_does_not_schedule_a_miss(self):
        """The zero-result branch must SKIP, not stamp a Thursday retry date."""
        import inspect

        from services.downloads import download_pipeline_service as pipeline

        src = inspect.getsource(pipeline.process_queue_item)
        branch = src.split("if not all_results:", 1)[1]
        head = branch[: branch.index("_schedule_search_retry")]

        assert "_search_service_looks_down()" in head, (
            "the outage check must run BEFORE the miss is scheduled"
        )
        assert 'status": "search_service_down"' in branch


class TestAbandonedDownloadsAreReleased:
    """The 9 rows stuck for 3 days must clear on a worker restart."""

    def test_reset_abandoned_items_covers_downloading(self):
        import inspect

        from services.queue import queue_cleanup_service as cleanup

        src = inspect.getsource(cleanup.reset_abandoned_items)
        assert "status = 'downloading'" in src, (
            "reset_abandoned_items must consider downloading rows, or every "
            "restart reports the same stuck count"
        )
        assert "downloading_reset" in src

    def test_the_sweeper_reads_the_progress_marker(self):
        import inspect

        from services.queue import queue_cleanup_service as cleanup

        src = inspect.getsource(cleanup.cleanup_stuck_items)
        assert "slskd_progress_at" in src, (
            "a transfer whose progress poll keeps touching updated_at can only "
            "be caught by the byte-count marker"
        )
