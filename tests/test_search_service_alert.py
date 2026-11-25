"""Soulseek search-service availability must reach the dashboard.

The queue's search breaker already pauses every search when slskd/Soulseek
cannot be reached, but it lived in the worker process's memory — invisible to
the web process that serves the dashboard. The log showed an hour of "search
service unavailable" while every page looked healthy.
"""

from __future__ import annotations

import importlib
import time

import pytest


@pytest.fixture()
def state(tmp_path, monkeypatch):
    module = importlib.import_module("services.downloads.search_service_state")
    monkeypatch.setenv(
        "SEARCH_SERVICE_STATE_FILE", str(tmp_path / "search_service_state.json")
    )
    return module


class TestTheSharedState:
    def test_an_open_breaker_is_reported_unavailable(self, state):
        state.mark_search_unavailable(
            "search service disconnected", pause_seconds=900, tier=4,
        )

        payload = state.read_search_service_state()

        assert payload["known"] is True
        assert payload["available"] is False
        assert payload["reason"] == "search service disconnected"
        assert payload["tier"] == 4
        assert 800 <= payload["retry_in_seconds"] <= 900

    def test_an_expired_window_is_not_an_outage(self, state, monkeypatch):
        """A stale file must never become a permanent red banner."""
        state.mark_search_unavailable("down", pause_seconds=900, tier=1)
        # Move the clock past the recorded window.
        real_time = time.time
        monkeypatch.setattr(time, "time", lambda: real_time() + 1000)

        payload = state.read_search_service_state()

        assert payload["available"] is True
        assert payload["retry_in_seconds"] == 0

    def test_a_success_closes_the_breaker(self, state):
        state.mark_search_unavailable("down", pause_seconds=900, tier=2)
        state.mark_search_available()

        payload = state.read_search_service_state()

        assert payload["available"] is True
        assert payload["reason"] == ""

    def test_no_file_means_no_known_problem(self, state):
        payload = state.read_search_service_state()

        assert payload["known"] is False
        assert payload["available"] is True


class TestTheStatusEndpointCarriesIt:
    def test_queue_processor_status_includes_the_search_service(
        self, state, monkeypatch
    ):
        service = importlib.import_module("services.queue.queue_diagnostics_service")
        state.mark_search_unavailable("search service disconnected", pause_seconds=600, tier=3)

        payload, status = service.queue_processor_status()

        assert status == 200
        assert payload["search_service"]["available"] is False
        assert payload["search_service"]["reason"] == "search service disconnected"


class TestTheDashboardRendersIt:
    def test_the_dashboard_has_a_host_and_a_renderer(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        template = (root / "test_site" / "templates" / "Pages" / "dashboard.html").read_text(
            encoding="utf-8"
        )
        script = (root / "test_site" / "static" / "js" / "pages" / "dashboard.js").read_text(
            encoding="utf-8"
        )

        assert 'id="searchServiceAlert"' in template
        assert "renderSearchServiceAlert" in script
        assert "search_service" in script
        # The host must be polled with the rest of the dashboard.
        assert "renderSearchServiceAlert(serviceHost, data)" in script
