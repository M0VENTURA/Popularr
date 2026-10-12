"""Regression tests for slskd transient-timeout resilience.

Soulseek/slskd serialises API operations, so a busy instance can answer
search/download requests slower than the HTTP timeout.  A single
ReadTimeout used to fail the entire queue item with ``no_results`` /
``download_failed``; these tests pin the retry behaviour.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from services.downloads.slskd_service import SlskdService, _is_transient_error


# ---------------------------------------------------------------------------
# _is_transient_error
# ---------------------------------------------------------------------------

class TestIsTransientError:
    def test_read_timeout_is_transient(self):
        assert _is_transient_error(TimeoutError("timed out")) is True

    def test_connection_error_is_transient(self):
        assert _is_transient_error(ConnectionError("refused")) is True

    def test_httpx_read_timeout_is_transient(self):
        try:
            import httpx
        except ImportError:
            pytest.skip("httpx not installed")
        assert _is_transient_error(httpx.ReadTimeout("timed out")) is True
        assert _is_transient_error(httpx.ConnectTimeout("timed out")) is True

    def test_non_transient_error_is_not_retried(self):
        assert _is_transient_error(ValueError("bad payload")) is False


# ---------------------------------------------------------------------------
# start_search retries transient errors
# ---------------------------------------------------------------------------

class _FakeHttp:
    base_url = "http://localhost:5030/api/v0"

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.calls = 0
        self.failures_before_success = 0
        self.posted_ids: list[str] = []

    def post_json(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.failures_before_success:
            raise TimeoutError("timed out (ReadTimeout)")
        # The new contract POSTs a client-generated id; the server honours it
        # and the response echoes it.  (A download POST body is a LIST, not a
        # dict — tolerate both shapes.)
        body = args[1] if len(args) > 1 else (kwargs.get("json") or {})
        posted_id = (body or {}).get("id") if isinstance(body, dict) else "search-123"
        self.posted_ids.append(str(posted_id))
        resp = _FakeResp(200, {"id": posted_id})
        return resp


class _FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = {}

    def json(self):
        return self._payload

    @property
    def text(self):
        return ""


class TestStartSearchRetry:
    def test_retries_transient_timeout_then_succeeds(self):
        http = _FakeHttp()
        http.failures_before_success = 2
        svc = SlskdService(http)
        search_id = svc.start_search("Spice Girls - Holler", max_attempts=5)
        assert search_id == http.posted_ids[-1], "the id is the client-generated one"
        assert http.calls == 3

    def test_gives_up_after_max_attempts(self):
        http = _FakeHttp()
        http.failures_before_success = 100
        svc = SlskdService(http)
        search_id = svc.start_search("Spice Girls - Holler", max_attempts=3)
        assert search_id is None
        assert http.calls == 3

    def test_returns_none_when_disabled(self):
        svc = SlskdService(_FakeHttp(enabled=False))
        assert svc.start_search("anything") is None


# ---------------------------------------------------------------------------
# download_file retries transient errors
# ---------------------------------------------------------------------------

class TestDownloadFileRetry:
    def test_retries_transient_timeout_then_succeeds(self):
        http = _FakeHttp()
        http.failures_before_success = 1
        svc = SlskdService(http)
        assert svc.download_file("hudsonk1992", "/Spice Girls/track.flac", size=5_000_000) is True
        assert http.calls == 2

    def test_returns_false_when_disabled(self):
        svc = SlskdService(_FakeHttp(enabled=False))
        assert svc.download_file("peer", "file.mp3", size=1) is False

    def test_refuses_a_zero_size_request(self):
        """slskd needs the real size to ask the peer; a 0-sized POST is dead."""
        http = _FakeHttp()
        svc = SlskdService(http)
        assert svc.download_file("peer", "file.mp3") is False
        assert http.calls == 0


# ---------------------------------------------------------------------------
# get_search_results treats a state timeout as still-in-progress
# ---------------------------------------------------------------------------

class _FakeStateHttp(_FakeHttp):
    def get_json(self, endpoint, **kwargs):
        raise TimeoutError("timed out (ReadTimeout)")


class TestGetSearchResultsResilience:
    def test_state_timeout_keeps_search_in_progress(self, caplog):
        import logging
        svc = SlskdService(_FakeStateHttp())
        with caplog.at_level(logging.WARNING, logger="services.downloads.slskd_service"):
            responses, state, is_complete = svc.get_search_results("search-1")
        assert responses == []
        assert is_complete is False
        assert "still in progress" not in caplog.text  # no misleading log


# ---------------------------------------------------------------------------
# start_search recovery: a POST that times out but actually started the search
# ---------------------------------------------------------------------------

class _RecoveringHttp(_FakeHttp):
    """POST always times out; the search may still exist under OUR id."""

    def __init__(self, search_state=None, enabled=True):
        super().__init__(enabled=enabled)
        self._search_state = search_state

    def post_json(self, *args, **kwargs):
        self.calls += 1
        body = args[1] if len(args) > 1 else (kwargs.get("json") or {})
        self.posted_ids.append(str((body or {}).get("id") or ""))
        raise TimeoutError("timed out (ReadTimeout)")

    def get_search_state(self, search_id, timeout=10):
        return dict(self._search_state) if self._search_state else {}

    def put(self, *args, **kwargs):
        return _FakeResp(204)

    def delete(self, *args, **kwargs):
        return _FakeResp(204)


def _owns(search_id: str):
    SlskdService.mark_search_owned(search_id)


def _active_search(search_id="search-abc", query="Spice Girls - Holler", state="InProgress", started=None):
    if started is None:
        from datetime import timedelta

        started = datetime.now(timezone.utc) - timedelta(seconds=10)
    return {"id": search_id, "state": state, "searchText": query, "startedAt": started.isoformat()}


class TestStartSearchRecovery:
    """Recovery is by OUR client-generated id, never by query text.

    The old ``find_active_search_by_query`` matched another worker's search on
    its query string, adopted it, and then deleted it in the cleanup — the
    other worker lost its results.  A POST that times out after the server
    accepted the request is recovered by checking whether OUR id exists.
    """

    def test_a_post_timeout_that_actually_started_the_search_forces_the_id(self):
        http = _RecoveringHttp(search_state={"id": "whatever", "state": "InProgress"})
        svc = SlskdService(http)
        search_id = svc.start_search("Spice Girls - Holler", max_attempts=2)
        # The returned id is the one WE posted (and now know exists), not a
        # query-text match.
        assert search_id is not None
        assert search_id in http.posted_ids

    def test_returns_none_when_our_search_does_not_exist(self):
        http = _RecoveringHttp(search_state=None)
        svc = SlskdService(http)
        assert svc.start_search("Spice Girls - Holler", max_attempts=2) is None

    def test_recovery_never_uses_query_text(self):
        import inspect

        from services.downloads import slskd_service as mod

        assert not hasattr(mod.SlskdService, "find_active_search_by_query"), (
            "query-text recovery was removed: it could adopt another worker's search"
        )


class TestClearStaleSearches:
    """Cleanup only touches OUR searches (plus truly orphaned ones)."""

    def _http_with(self, searches):
        cancelled = []

        class _CleaningHttp(_RecoveringHttp):
            def get_json(self, endpoint, *args, **kwargs):
                if str(endpoint).split("?")[0] == "searches":
                    return [dict(s) for s in self._searches]
                return {}

            def delete(self, endpoint, *args, **kwargs):
                seg = endpoint.split("/")[-1]
                cancelled.append("delete:" + seg.split("?")[0])
                return _FakeResp(204)

            def put(self, endpoint, *args, **kwargs):
                seg = endpoint.split("/")[-1]
                cancelled.append("stop:" + seg.split("?")[0])
                return _FakeResp(204)

        http = _CleaningHttp(search_state={})
        http._searches = searches
        http.cancelled = cancelled
        return http

    def test_owned_terminal_and_stuck_are_cleaned_but_fresh_is_not(self):
        from datetime import timedelta

        _owns("terminal")
        _owns("stuck")
        _owns("fresh")
        stuck_started = datetime.now(timezone.utc) - timedelta(minutes=10)   # > 180s
        fresh_started = datetime.now(timezone.utc) - timedelta(seconds=10)   # < 180s
        http = self._http_with([
            _active_search(search_id="terminal", state="Completed"),
            _active_search(search_id="stuck", state="InProgress", started=fresh_started) | {"startedAt": stuck_started.isoformat()},
            _active_search(search_id="fresh", state="InProgress", started=fresh_started),
        ])
        svc = SlskdService(http)
        svc.clear_stale_searches(budget_seconds=8)
        assert "delete:terminal" in http.cancelled
        assert "stop:stuck" in http.cancelled or "delete:stuck" in http.cancelled
        assert not any("fresh" in c for c in http.cancelled), "a fresh owned search must not be cancelled"

    def test_a_foreign_terminal_search_is_left_alone(self):
        """Searches the user started in the slskd web UI are not ours — a
        terminal one may still be on screen, so it is not deleted."""
        _owns("ours")
        http = self._http_with([
            _active_search(search_id="ours", state="Completed"),
            _active_search(search_id="manual", state="Completed"),
        ])
        SlskdService(http).clear_stale_searches(budget_seconds=8)
        assert any("ours" in c for c in http.cancelled)
        assert not any("manual" in c for c in http.cancelled)

    def test_a_truly_orphaned_active_search_is_cancelled(self):
        """A NOT-ours active search older than the generous orphan threshold."""
        from datetime import timedelta

        old_started = datetime.now(timezone.utc) - timedelta(minutes=45)
        http = self._http_with([
            _active_search(search_id="ghost", state="InProgress", started=old_started),
        ])
        SlskdService(http).clear_stale_searches(budget_seconds=8)
        assert any("ghost" in c for c in http.cancelled)


# ---------------------------------------------------------------------------
# query sanitisation parity with old_system
# ---------------------------------------------------------------------------

class TestQuerySanitisation:
    def test_ampersand_and_html_entities_become_spaces(self):
        from services.downloads.download_pipeline_service import _sanitize_slskd_query
        assert _sanitize_slskd_query("AC&amp;DC - Thunderstruck") == "AC DC - Thunderstruck"
        assert _sanitize_slskd_query("R\\u0026B - Song") == "R B - Song"
        assert _sanitize_slskd_query("AC/DC") == "AC/DC"

    def test_aggressive_punctuation_strip(self):
        from services.downloads.download_pipeline_service import _strip_all_query_punctuation_for_slskd
        assert _strip_all_query_punctuation_for_slskd("Where's the Love") == "Wheres the Love"
        assert _strip_all_query_punctuation_for_slskd("Hello! World") == "Hello World"
        assert _strip_all_query_punctuation_for_slskd("AC/DC") == "ACDC"


# ---------------------------------------------------------------------------
# candidate_duration is actually used by _score_soulseek_candidate
# ---------------------------------------------------------------------------

class TestSoulseekCandidateDurationScoring:
    def test_duration_bonus_raises_score(self):
        from services.queue.queue_scoring import _score_soulseek_candidate
        item = {"artist": "Spice Girls", "title": "Holler", "duration": "240"}
        base = _score_soulseek_candidate(
            "Spice Girls - Holler.flac", item, candidate_duration=None
        )
        exact = _score_soulseek_candidate(
            "Spice Girls - Holler.flac", item, candidate_duration=240
        )
        assert exact > base

    def test_large_duration_mismatch_penalises(self):
        from services.queue.queue_scoring import _score_soulseek_candidate
        item = {"artist": "Spice Girls", "title": "Holler", "duration": "240"}
        close = _score_soulseek_candidate(
            "Spice Girls - Holler.flac", item, candidate_duration=242
        )
        far = _score_soulseek_candidate(
            "Spice Girls - Holler.flac", item, candidate_duration=400
        )
        assert far < close


# ---------------------------------------------------------------------------
# year-mismatch guard in the pipeline scorer
# ---------------------------------------------------------------------------

class TestPipelineYearGuard:
    def test_year_mismatch_rejects(self):
        from services.downloads.download_pipeline_service import _year_mismatch_rejects
        assert _year_mismatch_rejects("Artist - Album [2012]/track.mp3", "2024") is True
        assert _year_mismatch_rejects("Artist - Album [2024]/track.mp3", "2024") is False
        assert _year_mismatch_rejects("Artist - Album (2012)/track.mp3", "2011") is False
        assert _year_mismatch_rejects("Artist - Album [2012]/track.mp3", None) is False

    def test_scorer_rejects_wrong_year_candidate(self):
        from services.downloads.download_pipeline_service import _score_result
        good = _score_result(
            {"filename": "Spice Girls - Holler (2000).mp3", "bitrate": 320},
            "Spice Girls", "Holler", expected_year="2000",
        )
        bad = _score_result(
            {"filename": "Spice Girls - Holler (2018).mp3", "bitrate": 320},
            "Spice Girls", "Holler", expected_year="2000",
        )
        assert good > 0
        assert bad == 0.0

