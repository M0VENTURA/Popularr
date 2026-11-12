"""A DEFERRED setRating is not a rejection.

REPORTED
--------
During a popularity scan, against a perfectly healthy Navidrome:

    WARNING api_clients.navidrome  Navidrome rejected setRating
        track_id='6L89CyXCzw8HpiQY0UcuNm' rating=1 code=None message=None
        username='moventura'
    WARNING services.popularity.stages.finalise_stage  Aborting Navidrome rating
        sync after consecutive failures — Navidrome unreachable?
        artist='Elvenking' failures=3
    WARNING services.popularity.stages.finalise_stage  Navidrome rating syncs
        failed — check credentials

``code=None message=None`` is the tell: no Subsonic error came back at all.

ROOT CAUSE
----------
``NavidromeClient._get_subsonic_response`` returns an EMPTY dict when the
IMPORT GATE defers a request (``_defer_for_import`` → an import is running).
``set_rating_status`` read ``data.get("status") == "ok"``, did not find it, then
looked for an error code that was not there either, and fell through to the
generic ``"failed"`` branch — logging the misleading "rejected setRating" line.

Three such deferrals tripped the consecutive-failure breaker, which aborted the
artist and reported "Navidrome unreachable? / check credentials" about a server
that had never been contacted and was entirely healthy.

Deferral is EXPECTED and RETRYABLE: it must be its own outcome, excluded from
the failure count, and must reset the breaker.
"""

from __future__ import annotations

import pytest

from api_clients.navidrome import NavidromeClient
from services.navidrome import rating_sync_service as rss


class _StubClient:
    """A NavidromeClient whose endpoint returns a canned response."""

    username = "moventura"
    is_import = False

    def __init__(self, data):
        self._data = data
        self.calls = 0

    def _get_subsonic_response(self, endpoint, **kwargs):
        self.calls += 1
        return self._data

    set_rating_status = NavidromeClient.set_rating_status


# ---------------------------------------------------------------------------
# 1. The client classifies a deferred call as "deferred"
# ---------------------------------------------------------------------------

class TestTheClientClassifiesDeferral:
    def test_an_empty_response_is_deferred_not_failed(self):
        """The import gate returns {} — no status, no error."""
        assert _StubClient({}).set_rating_status("abc", 3) == "deferred"

    def test_an_ok_response_is_still_ok(self):
        assert _StubClient({"status": "ok"}).set_rating_status("abc", 3) == "ok"

    def test_a_real_rejection_is_still_failed(self):
        """A genuine error DOES carry a status/code — it must not be excused."""
        client = _StubClient({"status": "failed", "error": {"code": 0, "message": "nope"}})
        assert client.set_rating_status("abc", 3) == "failed"

    def test_a_stale_id_is_still_not_found(self):
        """Code 70 keeps its own outcome, unchanged by this fix."""
        client = _StubClient({"status": "failed", "error": {"code": 70}})
        assert client.set_rating_status("abc", 3) == "not_found"

    def test_credentials_still_report_auth(self):
        client = _StubClient({"status": "failed", "error": {"code": 40}})
        assert client.set_rating_status("abc", 3) == "auth"

    def test_a_deferred_call_emits_no_rejection_warning(self, caplog):
        """The reported warning line must not be produced by a deferral."""
        import logging

        with caplog.at_level(logging.WARNING, logger="api_clients.navidrome"):
            _StubClient({}).set_rating_status("abc", 3)

        assert not [r for r in caplog.records if "rejected setRating" in r.getMessage()], (
            "a deferred call logged 'rejected setRating' — that is the "
            "misleading warning from the report"
        )


# ---------------------------------------------------------------------------
# 2. The aggregator propagates it
# ---------------------------------------------------------------------------

class _DeferringClient:
    def set_rating_status(self, track_id, stars):
        return "deferred"


class _FailingClient:
    def set_rating_status(self, track_id, stars):
        return "failed"


class TestTheAggregatorPropagatesDeferral:
    def test_all_deferred_aggregates_to_deferred(self):
        assert rss.rating_status_for_track([_DeferringClient()], "abc", 3) == "deferred"

    def test_deferred_beside_a_real_failure_is_a_failure(self):
        """Only ALL-deferred is excused; one genuine failure still matters."""
        clients = [_DeferringClient(), _FailingClient()]
        assert rss.rating_status_for_track(clients, "abc", 3) == "failed"

    def test_one_ok_wins_over_deferral(self):
        class _Ok:
            def set_rating_status(self, track_id, stars):
                return "ok"

        assert rss.rating_status_for_track([_Ok(), _DeferringClient()], "abc", 3) == "ok"

    def test_deferral_alone_never_reports_failed(self):
        clients = [_DeferringClient() for _ in range(3)]
        assert rss.rating_status_for_track(clients, "abc", 3) == "deferred"


# ---------------------------------------------------------------------------
# 3. The finalise stage excludes it from the failure count
# ---------------------------------------------------------------------------

class TestTheFinaliseStageExcludesDeferral:
    @pytest.fixture()
    def source(self) -> str:
        from pathlib import Path

        return (
            Path(__file__).resolve().parents[1]
            / "services/popularity/stages/finalise_stage.py"
        ).read_text(encoding="utf-8", errors="replace")

    def test_a_deferred_branch_exists(self, source):
        assert 'elif _rating_status == "deferred":' in source

    def test_deferral_resets_the_consecutive_failure_breaker(self, source):
        block = source.split('elif _rating_status == "deferred":', 1)[1][:900]
        assert "_consecutive_failures = 0" in block, (
            "a deferral must not advance the breaker — three of them aborted "
            "the artist with 'Navidrome unreachable?'"
        )

    def test_the_failed_count_subtracts_deferred(self, source):
        assert "_failed = _attempted - _synced - _stale - _deferred" in source, (
            "deferrals are still counted as failures, so the summary reports a "
            "healthy server as failing"
        )

    def test_the_summary_reports_deferrals_separately(self, source):
        assert "deferred (import running)" in source
