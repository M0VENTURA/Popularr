"""Queue search: early termination, wait shrinking, and the persistent-miss slot.

Covers the 2026-10-12 queue/search review:

1. A hopeless track used to burn ~9 minutes of searching (150s primary + up
   to 20 fallbacks x 20s) to conclude ``no_results``.  New behaviour:
   - ``search_and_filter`` cancels a search that has streamed ZERO results for
     ``search_empty_abort_seconds`` instead of waiting out the whole ceiling
     (slskd keeps empty searches ``InProgress`` until they time out);
   - the pipeline shrinks the wait of remaining queries after N consecutive
     EMPTY queries (``_search_query_wait``);
   - each finished search is cancelled when the queue is done polling it, so
     searches do not linger until the 180s stale-search cleaner has to cancel
     them ("cleanup working too hard").
2. Persistent misses used to retry every 24h forever.  New behaviour: after
   the escalating backoff tiers are spent, the retry lands on ONE calendar
   slot per week — the evening before the release day (Thursday 18:00 UTC by
   default) — while the item is never parked or abandoned.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from db.engine import db_session  # noqa: F401  (fixture compatibility)


# ---------------------------------------------------------------------------
# 1. _search_query_wait — per-query wait ceiling
# ---------------------------------------------------------------------------

class TestSearchQueryWait:

    def _wait(self, **overrides):
        from services.downloads.download_pipeline_service import _search_query_wait
        kwargs = dict(
            idx=0,
            consecutive_empty=0,
            long_first_wait=True,
            primary_wait=150,
            fallback_wait=20,
            shrink_after=4,
            shrunken_wait=8,
        )
        kwargs.update(overrides)
        return _search_query_wait(**kwargs)

    def test_primary_query_gets_the_long_wait(self):
        assert self._wait(idx=0, long_first_wait=True) == 150

    def test_second_bitrate_tier_primary_is_not_long(self):
        # The "long first wait" only applies to the FIRST tier — the
        # low-quality fallback tier reuses the fallback ceiling.
        assert self._wait(idx=0, long_first_wait=False) == 20

    def test_fallback_queries_get_the_fallback_wait(self):
        assert self._wait(idx=5) == 20

    def test_shrink_after_consecutive_empties(self):
        assert self._wait(idx=5, consecutive_empty=4) == 8
        # The primary shrinks too once the threshold is reached.
        assert self._wait(idx=0, long_first_wait=True, consecutive_empty=4) == 8

    def test_no_shrink_below_threshold(self):
        assert self._wait(idx=5, consecutive_empty=2) == 20

    def test_shrink_is_disabled_by_zero_threshold(self):
        assert self._wait(idx=5, consecutive_empty=10, shrink_after=0) == 20

    def test_shrunken_wait_is_never_larger_than_the_normal_wait(self):
        # Guard against a mis-set config (shrunken > fallback would INCREASE
        # the wait once the shrink triggers).
        assert self._wait(idx=5, consecutive_empty=4, shrunken_wait=60) == 20

    def test_waits_are_floored_at_one_second(self):
        assert self._wait(idx=5, fallback_wait=0) == 1
        # Shrink disabled (shrunken_wait=0): waits stay on their normal ceiling.
        assert self._wait(idx=0, long_first_wait=True, consecutive_empty=4, shrunken_wait=0) == 150
        assert self._wait(idx=5, consecutive_empty=4, shrunken_wait=0) == 20


# ---------------------------------------------------------------------------
# 2. get_queue_search_tuning — config defaults + overrides
# ---------------------------------------------------------------------------

class TestQueueSearchTuningConfig:

    def test_defaults(self):
        from helpers import config_helpers
        tuning = config_helpers.get_queue_search_tuning()
        assert tuning == {
            "search_max_wait_seconds": 150,
            "fallback_search_max_wait_seconds": 20,
            "search_empty_abort_seconds": 90.0,
            "search_empty_shrink_after": 4,
            "search_shrunken_wait_seconds": 8,
        }

    def test_overrides(self, monkeypatch):
        from helpers import config_helpers
        monkeypatch.setattr(
            config_helpers,
            "get_config",
            lambda: {"queue": {
                "search_max_wait_seconds": 60,
                "fallback_search_max_wait_seconds": "10",
                "search_empty_abort_seconds": 120,
                "search_empty_shrink_after": 3,
                "search_shrunken_wait_seconds": 5,
            }},
        )
        tuning = config_helpers.get_queue_search_tuning()
        assert tuning["search_max_wait_seconds"] == 60
        assert tuning["fallback_search_max_wait_seconds"] == 10
        assert tuning["search_empty_abort_seconds"] == 120.0
        assert tuning["search_empty_shrink_after"] == 3
        assert tuning["search_shrunken_wait_seconds"] == 5

    def test_zero_abort_is_preserved_as_disabled(self, monkeypatch):
        from helpers import config_helpers
        # 0 must stay 0 — the caller reads it as "no early abort".  A `or 90`
        # style default would silently re-enable the abort.
        monkeypatch.setattr(config_helpers, "get_config", lambda: {"queue": {"search_empty_abort_seconds": 0}})
        assert config_helpers.get_queue_search_tuning()["search_empty_abort_seconds"] == 0.0

    def test_zero_shrink_controls_are_preserved(self, monkeypatch):
        from helpers import config_helpers
        monkeypatch.setattr(
            config_helpers,
            "get_config",
            lambda: {"queue": {"search_empty_shrink_after": 0, "search_shrunken_wait_seconds": 0}},
        )
        tuning = config_helpers.get_queue_search_tuning()
        assert tuning["search_empty_shrink_after"] == 0
        assert tuning["search_shrunken_wait_seconds"] == 0


# ---------------------------------------------------------------------------
# 3. search_and_filter — empty-search early abort + eager cancellation
# ---------------------------------------------------------------------------

class _FakeHttp:
    enabled = True


def _make_service() -> tuple[object, list[str]]:
    from services.downloads.slskd_service import SlskdService
    svc = SlskdService(http_client=_FakeHttp())
    calls: list[str] = []
    svc.maybe_clear_stale_searches = lambda budget_seconds=6: False
    svc.start_search = lambda query, timeout=None: "s1"
    svc.cancel_search = lambda search_id, timeout=None: calls.append(str(search_id)) or True
    return svc, calls


class _ResultsFeed:
    """Yields canned (responses, state, is_complete) tuples per poll."""

    def __init__(self, entries: list[tuple[list, str, bool]]):
        self._entries = list(entries)
        self.polls = 0

    def __call__(self, search_id, timeout=None):
        self.polls += 1
        entry = self._entries.pop(0) if self._entries else ([], "InProgress", False)
        return entry


class TestSearchAndFilterEmptyAbort:

    def test_zero_result_search_is_cancelled_early(self):
        svc, calls = _make_service()
        svc.get_search_results = _ResultsFeed([
            ([], "InProgress", False),   # first poll: still nothing
        ])
        # wait_seconds is large; the abort must fire FIRST at ~1s.
        svc.filter_results_by_quality = lambda responses, min_bitrate=192, query="", max_results=None, reject_reasons=None, allowed_formats=None: responses

        results = svc.search_and_filter(
            "artist - title", wait_seconds=600, empty_abort_seconds=1
        )
        assert results == []
        # The search was cancelled by the early abort AND by the eager
        # finally-cleanup — it must not linger until the stale-search cleaner.
        assert calls.count("s1") >= 2

    def test_early_abort_honours_an_offset_late_result(self):
        svc, calls = _make_service()
        svc.get_search_results = _ResultsFeed([
            ([{"username": "peer", "filename": "A - B.flac"}], "InProgress", False),
            ([], "Completed, TimedOut", True),
        ])
        svc.filter_results_by_quality = lambda responses, min_bitrate=192, query="", max_results=None, reject_reasons=None, allowed_formats=None: responses

        results = svc.search_and_filter(
            "artist - title", wait_seconds=600, empty_abort_seconds=1
        )
        # A result arrived before the abort window — no early abort, the
        # terminal state ends the poll, the file comes back.
        assert [r["filename"] for r in results] == ["A - B.flac"]
        # Eager cleanup still cancelled the finished search.
        assert calls.count("s1") >= 1

    def test_abort_disabled_when_zero(self):
        svc, calls = _make_service()
        feed = _ResultsFeed([])  # always ([], "InProgress", False)
        svc.get_search_results = feed
        svc.filter_results_by_quality = lambda responses, min_bitrate=192, query="", max_results=None, reject_reasons=None, allowed_formats=None: responses

        results = svc.search_and_filter(
            "artist - title", wait_seconds=1, empty_abort_seconds=0
        )
        assert results == []
        # No early-abort cancel — only the final eager cancel.
        assert calls.count("s1") == 1

    def test_cancel_when_done_false_leaves_the_search(self):
        svc, calls = _make_service()
        svc.get_search_results = _ResultsFeed([
            ([{"username": "peer", "filename": "A - B.flac"}], "Completed, TimedOut", True),
        ])
        svc.filter_results_by_quality = lambda responses, min_bitrate=192, query="", max_results=None, reject_reasons=None, allowed_formats=None: responses

        svc.search_and_filter("artist - title", wait_seconds=10, cancel_when_done=False)
        assert calls == []


# ---------------------------------------------------------------------------
# 4. _next_weekday_slot / miss-slot scheduling
# ---------------------------------------------------------------------------

_MONDAY = datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc)  # weekday() == 0


class TestNextWeekdaySlot:

    def test_monday_is_index_zero_anchor(self):
        assert _MONDAY.weekday() == 0

    def test_slot_later_today_is_this_week(self):
        from services.downloads.download_pipeline_service import _next_weekday_slot
        thu_morning = _MONDAY + timedelta(days=3, hours=9)   # Thursday 09:00
        slot = _next_weekday_slot(thu_morning, weekday=3, clock="18:00")
        assert slot == _MONDAY + timedelta(days=3, hours=18)  # Thursday 18:00

    def test_slot_already_passed_today_moves_to_next_week(self):
        from services.downloads.download_pipeline_service import _next_weekday_slot
        thu_evening = _MONDAY + timedelta(days=3, hours=20)  # Thursday 20:00 (past 18:00)
        slot = _next_weekday_slot(thu_evening, weekday=3, clock="18:00")
        assert slot == _MONDAY + timedelta(days=10, hours=18)  # next Thursday 18:00

    def test_exact_slot_time_is_in_the_future(self):
        from services.downloads.download_pipeline_service import _next_weekday_slot
        thu = _MONDAY + timedelta(days=3, hours=18, minutes=30)
        slot = _next_weekday_slot(thu, weekday=3, clock="18:00")
        assert slot > thu and slot.weekday() == 3

    def test_clock_override_and_single_digit_hour(self):
        from services.downloads.download_pipeline_service import _next_weekday_slot
        friday = _MONDAY + timedelta(days=4, hours=8)
        slot = _next_weekday_slot(friday, weekday=4, clock="9:30")
        assert slot == _MONDAY + timedelta(days=4, hours=9, minutes=30)

    def test_out_of_range_clock_is_clamped_not_ignored(self):
        from services.downloads.download_pipeline_service import _next_weekday_slot
        monday = _MONDAY + timedelta(hours=8)
        # Ints parse, so the safety clamp bounds them (25:99 -> 23:59) instead
        # of silently resetting the slot; garbage that cannot parse falls back
        # to 06:00 (see the next test).
        slot = _next_weekday_slot(monday, weekday=0, clock="25:99")
        assert slot == _MONDAY + timedelta(hours=23, minutes=59)

    def test_unparseable_clock_falls_back_to_six_am(self):
        from services.downloads.download_pipeline_service import _next_weekday_slot
        monday = _MONDAY + timedelta(hours=8)
        slot = _next_weekday_slot(monday, weekday=0, clock="midnight")
        assert slot == _MONDAY + timedelta(days=7, hours=6)  # next Monday 06:00

    def test_pre_release_slot_delegates_unchanged(self):
        # The refactored pre-release slot must keep the OLD contract exactly:
        # the configured weekday at 06:00 UTC, strict future.
        from services.downloads.download_pipeline_service import _next_pre_release_six_am
        now = _MONDAY + timedelta(hours=10)
        slot = _next_pre_release_six_am(now)
        assert slot.weekday() == 4  # Friday (default)
        assert slot.hour == 6
        assert slot > now


class TestMissRetrySlotConfig:

    def test_defaults_are_thursday_six_pm(self, monkeypatch):
        from services.downloads import download_pipeline_service as dps
        monkeypatch.setattr("helpers.config_helpers.get_config", lambda: {})
        assert dps._miss_retry_weekday() == 3
        assert dps._miss_retry_time() == "18:00"

    def test_config_override(self, monkeypatch):
        from services.downloads import download_pipeline_service as dps
        monkeypatch.setattr(
            "helpers.config_helpers.get_config",
            lambda: {"queue": {"miss_retry_weekday": 4, "miss_retry_time": "23:00"}},
        )
        assert dps._miss_retry_weekday() == 4
        assert dps._miss_retry_time() == "23:00"

    def test_bad_time_falls_back(self, monkeypatch):
        from services.downloads import download_pipeline_service as dps
        monkeypatch.setattr(
            "helpers.config_helpers.get_config",
            lambda: {"queue": {"miss_retry_time": "midnight"}},
        )
        assert dps._miss_retry_time() == "18:00"


# ---------------------------------------------------------------------------
# 5. _schedule_search_retry — tiers then the persistent-miss slot
# ---------------------------------------------------------------------------

class TestScheduleSearchRetryPersistentMiss:

    def _schedule(self, monkeypatch, retry_count):
        from services.downloads import download_pipeline_service as dps
        scheduled: dict = {}
        monkeypatch.setattr(
            "db.repositories.queue.schedule_queue_retry",
            lambda qid, status, next_retry_at, reason="": scheduled.update(
                {"qid": qid, "status": status, "next_retry_at": next_retry_at, "reason": reason}
            ),
        )
        monkeypatch.setattr(dps, "_resolve_item_release_date", lambda item: None)
        monkeypatch.setattr(dps, "log_unified", lambda *a, **k: None)
        monkeypatch.setattr(dps, "_log_queue_event", lambda *a, **k: None)
        dps._schedule_search_retry(
            5, {"artist": "A", "title": "T", "retry_count": retry_count}, "no_results"
        )
        return scheduled

    def test_tiers_still_apply_below_the_ladder_length(self, monkeypatch):
        scheduled = self._schedule(monkeypatch, retry_count=0)
        assert scheduled["status"] == "backed_off"
        eta = datetime.fromisoformat(scheduled["next_retry_at"])
        expected = datetime.now(timezone.utc) + timedelta(hours=4)  # first tier
        assert abs((eta - expected).total_seconds()) < 10
        assert "backing off 4h" in scheduled["reason"]

    def test_persistent_miss_uses_the_weekly_slot(self, monkeypatch):
        from services.downloads import download_pipeline_service as dps
        fixed_slot = datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(dps, "_next_miss_retry_slot", lambda now_utc: fixed_slot)

        scheduled = self._schedule(monkeypatch, retry_count=10)
        assert scheduled["status"] == "backed_off"  # never parked/abandoned
        assert datetime.fromisoformat(scheduled["next_retry_at"]) == fixed_slot
        assert "persistent miss" in scheduled["reason"]
        assert "Thursday" in scheduled["reason"]

    def test_persistent_miss_is_never_a_terminal_status(self, monkeypatch):
        scheduled = self._schedule(monkeypatch, retry_count=99)
        assert scheduled["status"] not in ("failed", "cancelled", "removed")
        assert scheduled["status"] == "backed_off"

    def test_every_defined_miss_still_schedules_a_future_window(self, monkeypatch):
        # The "never abandon" invariant: ANY miss count produces a next
        # window, so the item keeps returning to the queue automatically.
        for count in (0, 1, 2, 3, 10, 100, 10_000):
            scheduled = self._schedule(monkeypatch, retry_count=count)
            assert scheduled["status"] == "backed_off"
            eta = datetime.fromisoformat(scheduled["next_retry_at"])
            assert eta > datetime.now(timezone.utc) - timedelta(seconds=5)


# ---------------------------------------------------------------------------
# 6. Config page surface (both trees) — the Config-page source-of-truth rule
# ---------------------------------------------------------------------------

_SEARCH_TUNING_IDS = (
    "queue_search_max_wait_seconds",
    "queue_fallback_search_max_wait_seconds",
    "queue_search_empty_abort_seconds",
    "queue_search_empty_shrink_after",
    "queue_search_shrunken_wait_seconds",
    "queue_miss_retry_weekday",
    "queue_miss_retry_time",
)


def _read(path: str) -> str:
    import pathlib
    return pathlib.Path(__file__).resolve().parent.parent.joinpath(path).read_text(encoding="utf-8")


def _strip_js_comments(source: str) -> str:
    lines = []
    for line in source.splitlines():
        # Only whole-line `//` comments — `'https://…'` string literals and
        # inline `//` must survive.
        stripped = line.lstrip()
        if stripped.startswith("//"):
            continue
        lines.append(line)
    return "\n".join(lines)


class TestConfigPageSurface:

    @pytest.mark.parametrize("template", [
        "templates/pages/config.html",
        "test_site/templates/Pages/config.html",
    ])
    def test_template_renders_every_tuning_input(self, template):
        src = _read(template)
        for input_id in _SEARCH_TUNING_IDS:
            assert f'id="{input_id}"' in src, f"{template} missing {input_id}"
            assert f'for="{input_id}"' in src, f"{template} missing label for {input_id}"

    @pytest.mark.parametrize("collector", [
        "static/js/config.js",
        "test_site/static/js/pages/config.js",
    ])
    def test_collector_reads_every_tuning_input(self, collector):
        src = _strip_js_comments(_read(collector))
        assert "queue_search_max_wait_seconds" in src
        assert "queue_fallback_search_max_wait_seconds" in src
        assert "queue_search_empty_abort_seconds" in src
        assert "queue_search_empty_shrink_after" in src
        assert "queue_search_shrunken_wait_seconds" in src
        assert "queue_miss_retry_weekday" in src
        assert "queue_miss_retry_time" in src