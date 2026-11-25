"""Our own search cleanup must not race slskd's search finalisation.

slskd logged, repeatedly:

    Failed to finalize search for '...': The database operation was expected to
    affect 1 row(s), but actually affected 0 row(s)
    at slskd.Search.SearchService.Update(Search search)
    at slskd.Search.SearchService.<>c__DisplayClass25_1.<<StartAsync>b__3>d

That is optimistic concurrency on slskd's search row: we deleted it while
slskd's own ``StartAsync`` continuation was still writing the final state. The
fix is to stop a search, wait for it to become terminal, observe it for a grace
window, and only then delete it.
"""

from __future__ import annotations

import importlib
import time

import pytest


class _FakeHttp:
    """Minimal slskd HTTP surface used by cancel/cleanup."""

    def __init__(self, state: str = "InProgress") -> None:
        self.state = state
        self.deleted: list[str] = []
        self.stopped: list[str] = []
        self.searches: list[dict] = []

    def stop_search(self, search_id: str) -> bool:
        self.stopped.append(search_id)
        if self.state in ("InProgress", "Queued"):
            self.state = "Completed"
        return True

    def delete_search(self, search_id: str) -> bool:
        self.deleted.append(search_id)
        return True

    def get_json(self, path, timeout=None, default=None):
        if str(path).strip("/") == "searches":
            return self.searches
        return {"state": self.state}


@pytest.fixture()
def svc():
    module = importlib.import_module("services.downloads.slskd_service")
    module.SlskdService.reset_owned_searches()
    return module


def _service(svc, http):
    return svc.SlskdService(http_client=http)


class TestCancelSearchWaitsForTerminal:
    def test_an_active_search_is_never_deleted(self, svc):
        http = _FakeHttp(state="InProgress")
        # The stop flips the fake to Completed, so make it stay active instead.
        http.stop_search = lambda sid: http.stopped.append(sid) or True
        service = _service(svc, http)
        svc.SlskdService.mark_search_owned("s1")

        deleted = service.cancel_search("s1", timeout=0.2)

        assert deleted is False
        assert http.deleted == [], (
            "deleting an active search races slskd's own finalize write"
        )
        assert "s1" in svc.SlskdService._owned_search_ids_snapshot()

    def test_a_terminal_search_is_not_deleted_inside_the_grace_window(self, svc):
        http = _FakeHttp(state="Completed")
        service = _service(svc, http)

        assert service.cancel_search("s2", timeout=0.2) is False
        assert http.deleted == []

    def test_a_terminal_search_is_deleted_after_the_grace_window(self, svc):
        http = _FakeHttp(state="Completed")
        service = _service(svc, http)
        svc.SlskdService._TERMINAL_FIRST_SEEN["s3"] = (
            time.monotonic() - svc.SlskdService.SEARCH_FINALIZE_GRACE_SECONDS - 1
        )
        svc.SlskdService.mark_search_owned("s3")

        assert service.cancel_search("s3", timeout=0.2) is True
        assert http.deleted == ["s3"]
        assert "s3" not in svc.SlskdService._owned_search_ids_snapshot()


class TestCleanupStopsBeforeDeleting:
    def test_a_stuck_active_search_is_stopped_not_deleted(self, svc):
        http = _FakeHttp(state="InProgress")
        http.stop_search = lambda sid: http.stopped.append(sid) or True
        http.searches = [{
            "id": "stuck-1",
            "state": "InProgress",
            "startedAt": "2020-01-01T00:00:00Z",
        }]
        service = _service(svc, http)
        svc.SlskdService.mark_search_owned("stuck-1")

        service.clear_stale_searches(budget_seconds=1)

        assert http.stopped == ["stuck-1"]
        assert http.deleted == [], (
            "the row must survive until slskd has finalized it"
        )
