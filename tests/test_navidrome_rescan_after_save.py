"""Tests: post-save Navidrome rescan (the "import puts the old data back" fix).

Reported (2026-10-03):

    "When updating the metadata using the album page on test_site, it says the
    metadata is updated on the tracks, but the next navidrome import puts the
    old data back in.  It's meant to update the files, which navidrome would
    then sync."

The save DOES write the file tags (verified by probe).  What was missing is
the "which navidrome would then sync" half:

* The album save triggered NO Navidrome rescan, so Navidrome kept serving its
  PRE-save rows.
* The import's pre-import sync could report success without any scan having
  run (see ``test_navidrome_remote_sync_consolidation.py``), and the pipeline
  logged "scan finished" unconditionally.
* The import then upserted Navidrome's stale rows over the saved values.

Fixed by: (a) ONE coalesced background rescan request after a save that
changed something (``navidrome_rescan_service``), (b) a verified
drain-and-wait scan trigger, and (c) the import pipeline honouring and
loudly reporting the sync result.

Covered here:
1. ``request_rescan`` — coalescing (bursts collapse into ≤2 runs), config
   gating, state reset, crash resilience, never blocks.
2. The album-save route requests a rescan when it changed something, and
   does NOT when the save was a no-op (a rescan per no-op would reintroduce
   the 2026-08-30 scan storm).
3. ``sync_remote_navidrome_before_import`` — returns and logs the truth for
   ok / failed / raising / unconfigured.

NOTE: ``tests/conftest.py`` stubs ``request_rescan`` for every test so no
route test spawns a real scan thread.  The service tests below therefore
capture the REAL function at import time (before the stub applies) and call
it through that reference — the stub only replaces the module attribute.
"""

from __future__ import annotations

import threading

import pytest

# Captured at collection time — BEFORE the conftest autouse fixture replaces
# the module attribute with its no-op stub.
from services.scanning.navidrome_rescan_service import (
    get_rescan_state,
    request_rescan as real_request_rescan,
)


# ===========================================================================
# 1. The coalescing rescan service
# ===========================================================================

class _FakeRescanClient:
    """Navidrome client stub whose trigger can be gated on an event."""

    def __init__(self, gate: "threading.Event | None" = None, ok: bool = True):
        self._gate = gate
        self._ok = ok
        self.calls = 0

    def trigger_and_wait_for_scan(self) -> bool:
        self.calls += 1
        if self._gate is not None:
            self._gate.wait(timeout=5)
        return self._ok


@pytest.fixture
def rescan(monkeypatch):
    """The real service module with a fake client and NO real threads.

    ``threading.Thread`` is replaced in the service's namespace so the
    worker is CAPTURED instead of started; tests then run it inline via
    ``run_worker()`` — fully deterministic coalescing assertions.
    """
    from services.scanning import navidrome_rescan_service as svc

    spawned: list[tuple] = []

    class _CapturedThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}

        def start(self):
            spawned.append((self.target, self.args, self.kwargs))

    class _StubThreading:
        Thread = _CapturedThread

    monkeypatch.setattr(svc, "threading", _StubThreading)
    # Never really sleep: the rate limit would otherwise cost the suite five
    # minutes.  Recorded instead so rate-limit tests can assert the wait.
    # ``raising=False`` so a service that does not sleep at all is a
    # BEHAVIOURAL test failure (nothing recorded), not a fixture error.
    slept: list[float] = []
    monkeypatch.setattr(svc, "sleep", slept.append, raising=False)
    # Fresh coalescer state per test (the real state dict is module-global).
    monkeypatch.setattr(
        svc, "_state", {"running": False, "pending": False, "reason": None}
    )
    # Navidrome configured by default; tests override to exercise gating.
    monkeypatch.setattr(
        "services.scanning.navidrome_scan_service.get_navidrome_config",
        lambda: {"base_url": "http://navidrome:4533", "user": "u", "pass": "p"},
    )

    class _Harness:
        module = svc
        # staticmethod: a bare function on a class would bind `self` as the
        # first argument when called via an instance.
        request = staticmethod(real_request_rescan)
        client: "_FakeRescanClient | None" = None

        def configure(self, ok=True):
            self.client = _FakeRescanClient(ok=ok)
            monkeypatch.setattr(
                "services.scanning.navidrome_scan_service.get_nav_client",
                lambda: self.client,
            )

        def run_worker(self):
            """Execute the captured worker inline (deterministic)."""
            assert spawned, "no worker thread was spawned"
            target, args, kwargs = spawned.pop(0)
            target(*args, **kwargs)

    # NOTE: `spawned = spawned` inside the class body would be a NameError —
    # a class block's own store target shadows the enclosing function scope
    # for reads.  Bind it after the class instead.
    _Harness.spawned = spawned
    _Harness.slept = slept
    return _Harness()


class TestRequestRescan:

    def test_unconfigured_navidrome_is_rejected_without_a_thread(self, rescan, monkeypatch):
        monkeypatch.setattr(
            "services.scanning.navidrome_scan_service.get_navidrome_config",
            lambda: {},
        )
        assert rescan.request("test") is False
        assert rescan.spawned == [], "no scan thread may start when Navidrome is unconfigured"

    def test_partially_configured_navidrome_is_rejected(self, rescan, monkeypatch):
        monkeypatch.setattr(
            "services.scanning.navidrome_scan_service.get_navidrome_config",
            lambda: {"base_url": "", "user": "", "pass": ""},
        )
        assert rescan.request("test") is False
        assert rescan.spawned == []

    def test_first_request_spawns_exactly_one_worker(self, rescan):
        rescan.configure()
        assert rescan.request("album metadata save") is True
        assert len(rescan.spawned) == 1
        state = get_rescan_state()
        assert state["running"] is True
        assert state["reason"] == "album metadata save"

    def test_bursts_coalesce_into_one_running_plus_one_followup(self, rescan):
        """N saves during a running scan collapse into ONE queued follow-up —
        the property that prevents the 2026-08-30 scan storm from returning."""
        rescan.configure()
        for i in range(10):
            assert rescan.request(f"save-{i}") is True
        assert len(rescan.spawned) == 1, (
            f"expected 1 spawned worker for a burst, got {len(rescan.spawned)}"
        )
        state = get_rescan_state()
        assert state["running"] is True
        assert state["pending"] is True, "coalesced request must be remembered"

    def test_burst_costs_at_most_two_scans(self, rescan):
        """A burst of 3 saves = 1 initial run + 1 follow-up, then idle."""
        rescan.configure()
        rescan.request("save-1")
        rescan.request("save-2")
        rescan.request("save-3")

        rescan.run_worker()   # first run; sees pending → re-runs inline
        assert rescan.client.calls == 2, (
            "a burst of 3 saves must cost at most 2 scans (1 + 1 follow-up)"
        )
        assert rescan.spawned == [], "no further worker may be spawned"
        state = get_rescan_state()
        assert state["running"] is False
        assert state["pending"] is False

    def test_idle_state_after_a_lone_request(self, rescan):
        rescan.configure()
        rescan.request("save")
        rescan.run_worker()
        assert rescan.client.calls == 1
        assert get_rescan_state() == {
            "running": False, "pending": False, "reason": None,
        }

    def test_failed_scan_is_still_reported_and_state_resets(self, rescan, monkeypatch):
        rescan.configure(ok=False)
        logs: list[str] = []
        monkeypatch.setattr(rescan.module, "log_unified", lambda m, **k: logs.append(m))

        rescan.request("save")
        rescan.run_worker()

        assert any("did NOT complete" in m for m in logs), (
            f"a failed background rescan must say so; got {logs}"
        )
        assert get_rescan_state()["running"] is False

    def test_completed_scan_logs_completion(self, rescan, monkeypatch):
        rescan.configure(ok=True)
        logs: list[str] = []
        monkeypatch.setattr(rescan.module, "log_unified", lambda m, **k: logs.append(m))

        rescan.request("save")
        rescan.run_worker()

        assert any("rescan complete" in m for m in logs)
        assert get_rescan_state()["running"] is False

    def test_worker_crash_resets_state(self, rescan, monkeypatch):
        """A crash inside the worker must not strand the coalescer in
        'running' forever (which would silently stop all future rescans)."""
        rescan.configure()

        def _boom(reason):
            raise RuntimeError("worker exploded")

        monkeypatch.setattr(rescan.module, "_run_once", _boom)
        rescan.request("save")
        with pytest.raises(RuntimeError):
            rescan.run_worker()
        assert get_rescan_state()["running"] is False
        assert get_rescan_state()["pending"] is False

    def test_request_is_accepted_even_if_the_client_is_broken(self, rescan, monkeypatch):
        """The client is resolved INSIDE the worker — request_rescan itself
        must not raise when Navidrome breaks between request and run."""
        rescan.configure()

        def _boom():
            raise RuntimeError("Navidrome is not configured")

        monkeypatch.setattr(
            "services.scanning.navidrome_scan_service.get_nav_client", _boom
        )
        assert rescan.request("save") is True
        rescan.run_worker()   # must swallow the error, not raise
        assert get_rescan_state()["running"] is False


class TestRateLimitBetweenRuns:
    """A TRICKLE of saves must not keep Navidrome scanning back-to-back.

    The ``pending`` flag collapses a *burst*, but it did nothing for a
    trickle — the reported log showed 12 full-library rescans in 35 minutes
    with gaps as short as 44 seconds, Navidrome's ``getScanStatus`` started
    timing out, and Popularr's UI stalled behind those slow responses.
    """

    @staticmethod
    def _interval(module) -> float:
        """The service's minimum interval between runs.

        Asserts rather than returning ``None`` so a service that lost the
        rate limit entirely fails HERE, with a message, instead of with an
        AttributeError further down the test.
        """
        value = getattr(module, "MIN_SCAN_INTERVAL_SECONDS", None)
        assert isinstance(value, (int, float)) and value > 0, (
            "navidrome_rescan_service must expose a positive "
            "MIN_SCAN_INTERVAL_SECONDS — without it a trickle of saves keeps "
            "Navidrome scanning back-to-back"
        )
        return float(value)

    def test_a_lone_save_is_never_delayed(self, rescan):
        """Only FOLLOW-UPS are rate limited: a single edit refreshes at once."""
        rescan.configure()
        rescan.request("save")
        rescan.run_worker()

        assert rescan.slept == [], "a lone run must not be deferred"
        assert rescan.client.calls == 1

    def test_a_followup_run_waits_out_the_minimum_interval(self, rescan, monkeypatch):
        """The queued save is still scanned — just not immediately."""
        rescan.configure()
        logs: list[str] = []
        monkeypatch.setattr(rescan.module, "log_unified", lambda m, **k: logs.append(m))

        rescan.request("save-1")
        rescan.request("save-2")   # lands while the first run is scanning
        rescan.run_worker()

        assert rescan.client.calls == 2, (
            "rate limiting must DELAY the queued save, never drop it"
        )
        assert len(rescan.slept) == 1, "the follow-up must be deferred exactly once"
        assert rescan.slept[0] == pytest.approx(self._interval(rescan.module), abs=1.0), (
            f"expected a wait of MIN_SCAN_INTERVAL_SECONDS before the follow-up, "
            f"got {rescan.slept[0]}"
        )
        assert any("rate limit" in m for m in logs), (
            "the deferral must be visible in the scan log, or an operator sees "
            "Navidrome go stale with no explanation"
        )

    def test_a_slow_run_pays_its_own_cooldown(self, rescan, monkeypatch):
        """The interval is measured from the run's START, so a run that already
        outlasted it needs no extra wait before the follow-up."""
        rescan.configure()
        interval = self._interval(rescan.module)
        clock = {"t": 0.0}
        monkeypatch.setattr(rescan.module, "monotonic", lambda: clock["t"])

        original_run = rescan.module._run_once

        def _slow_run(reason):
            clock["t"] += interval + 60
            return original_run(reason)

        monkeypatch.setattr(rescan.module, "_run_once", _slow_run)

        rescan.request("save-1")
        rescan.request("save-2")
        rescan.run_worker()

        assert rescan.client.calls == 2
        assert rescan.slept == [], (
            "a run that already lasted longer than the interval must not be "
            "padded with another full wait"
        )

    def test_a_short_run_waits_out_only_the_remainder(self, rescan, monkeypatch):
        """Wait = interval − elapsed, not the whole interval again."""
        rescan.configure()
        interval = self._interval(rescan.module)
        clock = {"t": 0.0}
        monkeypatch.setattr(rescan.module, "monotonic", lambda: clock["t"])

        original_run = rescan.module._run_once

        def _run(reason):
            clock["t"] += 60.0
            return original_run(reason)

        monkeypatch.setattr(rescan.module, "_run_once", _run)

        rescan.request("save-1")
        rescan.request("save-2")
        rescan.run_worker()

        assert rescan.slept, "the follow-up must be deferred"
        assert rescan.slept[0] == pytest.approx(interval - 60.0, abs=1.0), (
            f"expected the remainder only ({interval - 60.0}s), got {rescan.slept[0]}"
        )

    def test_state_resets_and_the_coalescer_is_reusable(self, rescan):
        rescan.configure()
        rescan.request("save-1")
        rescan.request("save-2")
        rescan.run_worker()

        assert get_rescan_state() == {
            "running": False, "pending": False, "reason": None,
        }, "a deferred run must still leave the coalescer idle"
        # …and the next save must be able to start a fresh worker.
        assert rescan.request("save-3") is True
        assert len(rescan.spawned) == 1


# ===========================================================================
# 2. The album-save route requests a rescan only when it changed something
# ===========================================================================

class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeRow:
    def __init__(self, mapping):
        self._mapping = dict(mapping)


class _Track(dict):
    @property
    def _mapping(self):
        return dict(self)


class _FakeSession:
    def __init__(self, tracks):
        self._tracks = tracks

    def execute(self, statement, params=None, *a, **k):
        sql = str(statement)
        if "FROM tracks" in sql:
            return _FakeResult([_FakeRow(t) for t in self._tracks])
        return _FakeResult([])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


ALBUM_URL = "/album/Madball/Not%20Your%20Kingdom"


@pytest.fixture
def album_save(monkeypatch):
    """Drive the real album-save POST with a rescan recorder.

    Mirrors the harness of ``test_album_review_save_persists`` (auth gate,
    DB, file-tag writer all faked) but records ``request_rescan`` calls.
    The recorder replaces the conftest autouse stub for this test (same
    ``monkeypatch`` instance — last setattr wins).

    The track's ``disc_number`` is empty on purpose: a single-disc strip of a
    stray "1" would count as a change and defeat the no-op test.
    """
    from routes import ui_routes as ui
    from services.scanning import navidrome_rescan_service as rescan_svc

    calls: list[str] = []
    tracks = [_Track({
        "id": "t1",
        "title": "Not Your Kingdom",
        "artist": "Madball",
        "album": "Not Your Kingdom",
        "album_artist": "Madball",
        "track_number": "1",
        "disc_number": "",
        "recording_mbid": "rec-1",
        "file_path": "/music/Madball/Not Your Kingdom/t1.flac",
        "year": "2024",
        "is_cover": 0,
    })]

    monkeypatch.setattr("helpers.app_hooks.needs_setup", lambda: False)
    monkeypatch.setattr(ui, "db_session", lambda *a, **k: _FakeSession(tracks))
    monkeypatch.setattr(ui, "get_config", lambda: {})
    monkeypatch.setattr(ui, "insert_or_update_track", lambda track_id, payload: None)
    monkeypatch.setattr(
        "db.repositories.metadata.update_track_genres",
        lambda track_id, genres_str: 1,
    )
    monkeypatch.setattr(ui, "resolve_music_file_path", lambda p: str(p) if p else None)
    monkeypatch.setattr(ui, "update_file_tags", lambda path, tags: True)
    monkeypatch.setattr(ui, "track_carries_live_state", lambda t: False)
    monkeypatch.setattr(ui, "get_album_tag_inconsistencies", lambda *a, **k: [])
    monkeypatch.setattr(ui, "get_recent_album_scans", lambda *a, **k: [])
    # The recorder — overrides the conftest autouse no-op stub.
    monkeypatch.setattr(
        rescan_svc, "request_rescan",
        lambda reason="": calls.append(reason) or True,
    )

    class _Harness:
        rescan_calls = calls
    return _Harness()


async def _post(client, form: dict):
    return await client.post(ALBUM_URL, form=form)


class TestAlbumSaveRequestsRescan:

    async def test_a_changed_save_requests_a_rescan(self, client, album_save):
        resp = await _post(client, {
            "album_title": "Not Your Kingdom (Remastered)",   # differs → updated_count > 0
            "album_artist": "Madball",
        })
        assert resp.status_code in (302, 200)
        assert album_save.rescan_calls == ["album metadata save"], (
            "a save that changed the album must ask Navidrome to rescan — "
            "otherwise the next import reads Navidrome's PRE-save rows and "
            "overwrites what was just saved"
        )

    async def test_a_no_op_save_does_not_request_a_rescan(self, client, album_save):
        """Posting identical values writes nothing → no scan to request."""
        resp = await _post(client, {
            "album_title": "Not Your Kingdom",   # unchanged
            "album_artist": "Madball",           # unchanged
        })
        assert resp.status_code in (302, 200)
        assert album_save.rescan_calls == [], (
            "a save that changed nothing must not trigger a Navidrome scan"
        )

    async def test_a_genre_only_save_does_not_request_a_rescan(self, client, album_save):
        """Genres are database-only now — there is no file to re-read.

        This test used to assert the opposite (*"genres reach the audio files,
        so Navidrome must rescan"*). The save no longer writes a genre tag —
        the popularity scan owns DB → file — and ``genres``/``manual_genres``
        are protected from the sync, so the value cannot be reverted either.
        Requesting a scan here would now cost a full Navidrome rescan for a
        change Navidrome was never told to look at.
        """
        resp = await _post(client, {
            "album_title": "Not Your Kingdom",
            "album_artist": "Madball",
            "album_genres": "Hardcore, Punk",
        })
        assert resp.status_code in (302, 200)
        assert album_save.rescan_calls == [], (
            "a genres-only save changes no file, so asking Navidrome to "
            "rescan costs a scan and buys nothing"
        )


# ===========================================================================
# 3. The import pipeline honours the sync result
# ===========================================================================

class _SyncClient:
    def __init__(self, outcome):
        self._outcome = outcome
        self.calls = 0

    def trigger_and_wait_for_scan(self) -> bool:
        self.calls += 1
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class TestSyncRemoteBeforeImport:

    @pytest.fixture
    def logs(self, monkeypatch):
        from services.scanning.pipelines import navidrome_pipeline as pipe
        messages: list[str] = []
        monkeypatch.setattr(pipe, "log_unified", lambda m, **k: messages.append(m))
        return messages

    def test_no_client_is_trivially_ok(self, logs):
        from services.scanning.pipelines.navidrome_pipeline import (
            sync_remote_navidrome_before_import,
        )
        assert sync_remote_navidrome_before_import(None) is True

    def test_verified_scan_reports_finished(self, logs):
        from services.scanning.pipelines.navidrome_pipeline import (
            sync_remote_navidrome_before_import,
        )
        client = _SyncClient(True)
        assert sync_remote_navidrome_before_import(client) is True
        assert client.calls == 1
        assert any("scan finished" in m for m in logs)

    def test_unverified_scan_reports_stale_warning(self, logs):
        from services.scanning.pipelines.navidrome_pipeline import (
            sync_remote_navidrome_before_import,
        )
        client = _SyncClient(False)
        assert sync_remote_navidrome_before_import(client) is False, (
            "an unverified remote scan must be reported as False so the "
            "import can warn that stale rows may overwrite saved edits"
        )
        assert any("STALE" in m for m in logs), (
            f"expected a stale-data warning, got: {logs}"
        )

    def test_raising_scan_reports_stale_warning(self, logs):
        from services.scanning.pipelines.navidrome_pipeline import (
            sync_remote_navidrome_before_import,
        )
        client = _SyncClient(RuntimeError("boom"))
        assert sync_remote_navidrome_before_import(client) is False
        assert any("STALE" in m or "scan error" in m for m in logs), (
            f"expected a stale-data warning, got: {logs}"
        )

    def test_success_is_not_claimed_when_sync_fails(self, logs):
        """The old pipeline logged 'Remote Navidrome scan finished'
        UNCONDITIONALLY — pin the truth-telling."""
        from services.scanning.pipelines import navidrome_pipeline as pipe

        client = _SyncClient(False)
        pipe.sync_remote_navidrome_before_import(client)
        assert not any("scan finished" in m for m in logs), (
            "claimed 'scan finished' although the sync was not verified"
        )
