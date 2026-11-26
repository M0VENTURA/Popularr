"""The search breaker must describe the CONNECTION, not the tracks.

Reported from a live log:

    ... Search for 'Adele - One and Only' completed with 0 responses
    ... Search for 'Adele - and Only' completed with 0 responses
    ... Search for 'Adele - One Only' completed with 0 responses
    ... Search for 'Adele - One and' completed with 0 responses
    ... Search for 'Britney Spears - Sometimes' completed with 251 responses
    ... Download ... sometimes.mp3 ... Completed, Succeeded @ 112.2 KB/s

    Soulseek search service is unreachable
    Reason: search service disconnected

slskd was healthy throughout. Two distinct defects produced that banner:

1. the empty-result streak was bumped INSIDE the per-query loop, so one
   unfindable track burned the whole allowance (every fallback query variant
   counted separately);
2. a tripped streak called the INFRASTRUCTURE path, opening the same persisted
   breaker a real disconnection uses — and the dashboard renders that as an
   outage.
"""

from __future__ import annotations

import importlib
import inspect

import pytest


@pytest.fixture()
def pipeline():
    module = importlib.import_module("services.downloads.download_pipeline_service")
    module.reset_empty_search_streak()
    yield module
    module.reset_empty_search_streak()


class TestOneTrackCountsOnce:
    def test_the_streak_is_not_bumped_inside_the_query_loop(self, pipeline):
        """The bump must happen ONCE per track, after every variant ran."""
        src = inspect.getsource(pipeline.process_queue_item)
        # Everything from the query loop to the all_results branch.
        loop = src.split("for idx, q in enumerate(", 1)[1]
        loop = loop.split("if not all_results:", 1)[0]

        assert "_bump_empty_streak" not in loop, (
            "each fallback query variant must not count as its own empty track "
            "(one Adele track tripped the breaker on its own)"
        )

        branch = src.split("if not all_results:", 1)[1]
        head = branch[: branch.index("_schedule_search_retry")]
        assert "_bump_empty_streak(False)" in head


class TestAnEmptyStreakIsNotAnOutage:
    def test_a_full_streak_leaves_the_service_marked_available(self, pipeline):
        for _ in range(pipeline._EMPTY_STREAK_LIMIT):
            pipeline._bump_empty_streak(False)

        assert pipeline._search_service_looks_down() is False
        assert pipeline._search_unavailable_until == 0.0

    def test_a_full_streak_does_not_publish_an_outage(self, pipeline, monkeypatch):
        """The dashboard reads the persisted state, so it must stay clean."""
        published: list[tuple] = []
        state = importlib.import_module("services.downloads.search_service_state")
        monkeypatch.setattr(
            state, "mark_search_unavailable",
            lambda *a, **k: published.append((a, k)),
        )

        for _ in range(pipeline._EMPTY_STREAK_LIMIT):
            pipeline._bump_empty_streak(False)

        assert published == [], (
            "empty results must never be published as a service outage"
        )

    def test_a_real_infrastructure_failure_still_trips_it(self, pipeline):
        for _ in range(pipeline._INFRA_FAILURE_LIMIT):
            pipeline._note_infra_failure("search service disconnected")

        assert pipeline._search_service_looks_down() is True
        assert pipeline._search_unavailable_until > 0


class TestTheReaderSelfHeals:
    def test_a_reachable_transport_clears_a_stale_breaker(self, tmp_path, monkeypatch):
        state = importlib.import_module("services.downloads.search_service_state")
        monkeypatch.setenv(
            "SEARCH_SERVICE_STATE_FILE", str(tmp_path / "state.json")
        )
        state.mark_search_unavailable("disconnected", pause_seconds=900, tier=2)
        monkeypatch.setattr(state, "_probe_transport_alive", lambda: True)

        payload = state.read_search_service_state()

        assert payload["available"] is True
        assert payload["reason"] == ""

    def test_an_unreachable_transport_leaves_it_reported(self, tmp_path, monkeypatch):
        state = importlib.import_module("services.downloads.search_service_state")
        monkeypatch.setenv(
            "SEARCH_SERVICE_STATE_FILE", str(tmp_path / "state.json")
        )
        state.mark_search_unavailable("disconnected", pause_seconds=900, tier=2)
        monkeypatch.setattr(state, "_probe_transport_alive", lambda: False)

        payload = state.read_search_service_state()

        assert payload["available"] is False
