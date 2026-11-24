"""The shared search breaker: one outage, one wait, one probe.

Reported: 2,277 "Waiting for search service" events while slskd was down — each
queue item discovered the outage on its own. These tests pin the process-wide
breaker and its growing backoff.
"""

from __future__ import annotations

import time

import pytest

from services.downloads import download_pipeline_service as pipeline


@pytest.fixture(autouse=True)
def clean_breaker():
    pipeline.reset_empty_search_streak()
    yield
    pipeline.reset_empty_search_streak()


class TestTheBreaker:
    def test_three_infrastructure_failures_pause_everything(self):
        for _ in range(pipeline._INFRA_FAILURE_LIMIT):
            pipeline._note_infra_failure("no route to host")

        assert pipeline._search_service_looks_down() is True
        assert pipeline._search_service_retry_seconds() > 0

    def test_one_success_closes_it_and_resets_the_ladder(self):
        for _ in range(pipeline._INFRA_FAILURE_LIMIT):
            pipeline._note_infra_failure("disconnected")
        assert pipeline._search_service_looks_down() is True

        pipeline._bump_empty_streak(True)  # a probe returned results

        assert pipeline._search_service_looks_down() is False
        assert pipeline._search_backoff_level == 0
        assert pipeline._infra_failure_streak == 0

    def test_the_ladder_grows_and_caps_at_fifteen_minutes(self):
        windows: list[float] = []
        for _ in range(6):
            pipeline._search_unavailable_until = 0.0  # simulate the probe failing
            before = time.time()
            pipeline._trip_search_breaker("test")
            windows.append(round(pipeline._search_unavailable_until - before))

        assert windows[0] == 30
        assert windows[1] == 120
        assert windows[2] == 300
        assert windows[3] == 600
        assert windows[4] == 900
        assert windows[5] == 900  # capped

    def test_an_open_breaker_is_not_escalated_per_item(self):
        pipeline._trip_search_breaker("test")
        first = pipeline._search_unavailable_until
        for _ in range(10):
            pipeline._note_infra_failure("disconnected")
        assert pipeline._search_unavailable_until == first

    def test_an_empty_search_streak_trips_the_same_breaker(self):
        for _ in range(pipeline._EMPTY_STREAK_LIMIT):
            pipeline._bump_empty_streak(False)
        assert pipeline._search_service_looks_down() is True
        assert pipeline._search_unavailable_until > 0


class TestTheCandidateBudget:
    def test_the_overall_ceiling_is_200_not_the_per_query_50(self):
        assert pipeline._MAX_UNIQUE_CANDIDATES == 200

    def test_the_dedupe_key_is_peer_plus_full_path(self):
        a = {"username": "Peer", "filename": "Music\\Song.flac"}
        b = {"username": "peer", "filename": "music\\song.flac"}
        c = {"username": "peer", "filename": "music\\other.flac"}
        assert pipeline._candidate_key(a) == pipeline._candidate_key(b)
        assert pipeline._candidate_key(a) != pipeline._candidate_key(c)
