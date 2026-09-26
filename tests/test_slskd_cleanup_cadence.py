"""Guard: stale-search cleanup must run on a CADENCE, not on every search.

Reported: "Can you stop the clearing of searching on soulseek so it happens
with each 10 searches, not every search?"

``clear_stale_searches`` does a ``GET /searches`` (budget up to 6 s) and a
DELETE per terminal-state search it finds.  It was called UNCONDITIONALLY from
BOTH search entry points:

  * ``routes/download_search_routes.py`` — the manual UI search
  * ``services/downloads/slskd_service.py::search_and_filter`` — automated

That means every single search paid a full slskd round-trip first, which
serialises against the search itself (slskd serialises API operations) and
makes a search feel slow even when there is nothing stale to remove.

These tests pin the cadence: the cleanup runs on the 1st search and then once
every N searches (default 10), and NO cleanup happens on the searches in
between.
"""

from __future__ import annotations

import pytest

from services.downloads.slskd_service import SlskdService


@pytest.fixture(autouse=True)
def _reset_cadence():
    """Reset the SHARED counter around every test.

    The cadence counter is a CLASS attribute (deliberately — both search entry
    points build a fresh ``SlskdService`` per operation, so instance state
    would reset every call and the cleanup would run every search).  That means
    it leaks between tests, so it must be reset explicitly.
    """
    SlskdService.reset_cleanup_cadence()
    yield
    SlskdService.reset_cleanup_cadence()


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


class _RecordingHttp:
    """Records every request so the cleanup cadence can be counted."""

    base_url = "http://localhost:5030/api/v0"

    def __init__(self, stale=None):
        self.enabled = True
        self.stale = list(stale or [])
        self.list_searches_calls = 0
        self.deleted: list[str] = []

    def get_json(self, endpoint, *args, **kwargs):
        if endpoint.split("?")[0] == "searches":
            self.list_searches_calls += 1
            return [dict(s) for s in self.stale]
        return []

    def delete(self, endpoint, *args, **kwargs):
        self.deleted.append(endpoint.split("/")[-1].split("?")[0])
        return _FakeResp(204)

    def put(self, endpoint, *args, **kwargs):
        return _FakeResp(204)

    def post_json(self, *args, **kwargs):
        return _FakeResp(200, {"id": "search-1"})


def _terminal(search_id: str) -> dict:
    return {"id": search_id, "state": "Completed, TimedOut"}


# ---------------------------------------------------------------------------
# The cadence gate itself
# ---------------------------------------------------------------------------

class TestCleanupCadence:
    def test_first_search_runs_the_cleanup(self):
        http = _RecordingHttp(stale=[_terminal("old-1")])
        svc = SlskdService(http, search_cleanup_interval=10)
        svc.maybe_clear_stale_searches(budget_seconds=2)
        assert http.list_searches_calls == 1, "the 1st search must clean up"
        assert "old-1" in http.deleted

    def test_nothing_is_cleared_on_the_searches_between(self):
        """Searches 2..10 must NOT touch slskd for cleanup."""
        http = _RecordingHttp(stale=[_terminal("old-1")])
        svc = SlskdService(http, search_cleanup_interval=10)

        svc.maybe_clear_stale_searches(budget_seconds=2)  # 1st -> runs
        assert http.list_searches_calls == 1

        for _ in range(9):                                # searches 2..10
            svc.maybe_clear_stale_searches(budget_seconds=2)

        assert http.list_searches_calls == 1, (
            "the cleanup ran again inside the 10-search window — every search "
            "is still paying a slskd round-trip"
        )

    def test_the_eleventh_search_runs_the_cleanup_again(self):
        http = _RecordingHttp(stale=[_terminal("old-1")])
        svc = SlskdService(http, search_cleanup_interval=10)

        for _ in range(11):
            svc.maybe_clear_stale_searches(budget_seconds=2)

        assert http.list_searches_calls == 2, (
            "the 11th search (1 + interval) must clean up again"
        )

    def test_interval_of_one_cleans_every_search(self):
        """Setting the interval to 1 restores the old per-search behaviour."""
        http = _RecordingHttp(stale=[_terminal("old-1")])
        svc = SlskdService(http, search_cleanup_interval=1)
        for _ in range(3):
            svc.maybe_clear_stale_searches(budget_seconds=2)
        assert http.list_searches_calls == 3

    def test_a_zero_or_negative_interval_is_treated_as_disabled(self):
        """0 must mean "never clean automatically", not "clean every time"."""
        http = _RecordingHttp(stale=[_terminal("old-1")])
        svc = SlskdService(http, search_cleanup_interval=0)
        for _ in range(5):
            svc.maybe_clear_stale_searches(budget_seconds=2)
        assert http.list_searches_calls == 0, (
            "an interval of 0 (disabled) must not clean up on every search"
        )

    def test_counter_counts_searches_not_cleanups(self):
        """The window must advance once per search, whatever the outcome."""
        http = _RecordingHttp(stale=[_terminal("old-1")])
        svc = SlskdService(http, search_cleanup_interval=3)
        # 6 searches with an interval of 3 -> cleanups on #1 and #4.
        for _ in range(6):
            svc.maybe_clear_stale_searches(budget_seconds=2)
        assert http.list_searches_calls == 2

    def test_a_failing_cleanup_does_not_break_the_search(self):
        class _Boom(_RecordingHttp):
            def get_json(self, endpoint, *args, **kwargs):
                if endpoint.split("?")[0] == "searches":
                    raise RuntimeError("slskd down")
                return []

        http = _Boom()
        svc = SlskdService(http, search_cleanup_interval=10)
        # Must not raise: a cleanup failure cannot fail the user's search.
        svc.maybe_clear_stale_searches(budget_seconds=2)

    def test_a_cleanup_that_raises_outright_is_swallowed(self):
        """The gate's own except must swallow, not just the callee's.

        ``clear_stale_searches`` swallows its internal errors, so this uses a
        subclass whose ``clear_stale_searches`` raises directly — that is the
        only way to prove the gate's belt-and-braces guard is load-bearing.
        """
        class _Raising(SlskdService):
            def clear_stale_searches(self, budget_seconds: float = 8) -> None:
                raise RuntimeError("cleanup exploded")

        http = _RecordingHttp()
        svc = _Raising(http, search_cleanup_interval=10)
        # Must not raise.
        svc.maybe_clear_stale_searches(budget_seconds=2)

    def test_cadence_survives_a_fresh_service_per_search(self):
        """The counter MUST be process-wide, not per-instance.

        Both real entry points build a NEW ``SlskdService`` per operation — the
        manual route one per HTTP request, ``download_pipeline_service`` one per
        queued download.  If the counter were instance state it would reset on
        every call and the cleanup would run on every search, i.e. the exact
        bug being fixed.  Building a fresh service per iteration is the only
        way to catch that.
        """
        http = _RecordingHttp(stale=[_terminal("old-1")])
        for _ in range(10):
            # A brand-new instance every time, exactly like production.
            SlskdService(http, search_cleanup_interval=10).maybe_clear_stale_searches(
                budget_seconds=2
            )
        assert http.list_searches_calls == 1, (
            "the cadence must be shared across instances — a per-instance "
            "counter runs the cleanup on every search"
        )

    def test_the_configured_interval_is_read_fresh_each_time(self, monkeypatch):
        """A Config-page save must take effect without a restart.

        The interval is re-read from config on every call on purpose.  Caching
        it in a class attribute would survive ``clear_config_cache()``, so an
        operator changing the value in the UI would see no effect until the
        process restarted.
        """
        import services.downloads.slskd_service as mod

        values = {"n": 3}
        monkeypatch.setattr(mod, "_configured_cleanup_interval", lambda: values["n"])

        http = _RecordingHttp()
        svc = SlskdService(http)  # no override -> reads config
        for _ in range(3):
            svc.maybe_clear_stale_searches(budget_seconds=2)
        assert http.list_searches_calls == 1, "interval 3 cleans on #1 only"

        # Operator saves a new value; the very next search must honour it.
        values["n"] = 1
        svc.maybe_clear_stale_searches(budget_seconds=2)
        assert http.list_searches_calls == 2, (
            "the interval is being cached — a Config-page save is ignored "
            "until restart"
        )


# ---------------------------------------------------------------------------
# The entry points must use the gated call, not the unconditional one
# ---------------------------------------------------------------------------

class TestEntryPointsUseTheGate:
    def test_search_and_filter_uses_the_gated_cleanup(self, monkeypatch):
        import inspect
        import re

        from services.downloads import slskd_service as mod

        src = inspect.getsource(mod.SlskdService.search_and_filter)
        assert "maybe_clear_stale_searches" in src, (
            "search_and_filter must go through the cadence gate, not call "
            "clear_stale_searches on every search"
        )
        # See the note in test_the_manual_route_uses_the_gated_cleanup: a bare
        # substring check is defeated by "maybe_clear_stale_searches".
        ungated = re.findall(r"(?<!maybe_)clear_stale_searches\s*\(", src)
        assert not ungated, (
            "search_and_filter still calls the UNGATED cleanup — every "
            "automated search pays a slskd round-trip first"
        )

    def test_the_manual_route_uses_the_gated_cleanup(self):
        import inspect
        import re

        from routes import download_search_routes as mod

        src = inspect.getsource(mod)
        assert "maybe_clear_stale_searches" in src, (
            "the manual /api/slskd/search route must go through the cadence gate"
        )
        # ⚠️ Match a BARE ``clear_stale_searches`` call.  A plain
        # ``"clear_stale_searches" not in src`` is satisfied by the substring
        # inside "maybe_clear_stale_searches", so it would never fail — the
        # same comment/substring trap this session has hit repeatedly.
        ungated = re.findall(
            r"(?<!maybe_)clear_stale_searches\s*\(", src
        )
        assert not ungated, (
            f"the manual search route still calls the UNGATED cleanup "
            f"({len(ungated)} site(s)) — every manual search pays a slskd "
            "round-trip before it starts"
        )
