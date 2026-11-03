"""Download/search pipeline hardening.

Three reported problems:

1. **A 12-hour backoff was retried ~80 minutes later.** ``next_retry_at`` is a
   naive ``TIMESTAMP``; an aware UTC value was stored as one process's wall
   clock and compared with ``CURRENT_TIMESTAMP`` using another session's
   ``TimeZone``. Writes are now UTC-naive and every due-time query binds a
   Python-computed naive UTC "now", so neither side depends on the session
   ``TimeZone``.

2. **slskd 409 "must be connected and logged in" counted as ``no_results``.**
   While slskd re-authenticates, the search was treated as an empty result and
   the queue item climbed the search-miss backoff ladder. A 409 of that shape
   is now retried briefly, then reported as "unavailable" so the caller takes
   the disconnected path (5-minute retry, no attempt counted).

3. **The same folder was imported twice, concurrently.** Two threads converted
   the same FLAC; the first moved/deleted it and the second raised ``[Errno 2]``.
   Folder imports are now serialised per folder with the queue's cross-process
   lock, and a duplicate caller returns ``skipped`` immediately.

Also pins the duplicate "Download scheduler started" fix: background services
start from a ``before_serving`` hook, not at import (the entrypoint's pre-flight
import used to start them once, then Hypercorn's leader started them again).
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

from db.repositories.queue import _as_utc_naive
from services.downloads import slskd_service as slskd
from services.downloads.download_folder_service import _folder_import_lock_key


# ===========================================================================
# 1. next_retry_at is stored and compared as naive UTC
# ===========================================================================
class TestRetryWindowIsUtc:
    def test_an_aware_utc_value_is_stored_without_an_offset(self):
        assert _as_utc_naive("2026-11-04T01:24:11.714184+00:00") == (
            "2026-11-04 01:24:11.714184"
        )

    def test_a_non_utc_aware_value_is_converted_to_utc(self):
        local = datetime(
            2026, 11, 4, 12, 24, 11, tzinfo=timezone(timedelta(hours=11)),
        )
        assert _as_utc_naive(local) == "2026-11-04 01:24:11"

    def test_a_naive_value_is_left_as_already_utc(self):
        assert _as_utc_naive("2026-11-04 01:24:11") == "2026-11-04 01:24:11"

    def test_none_stays_none(self):
        assert _as_utc_naive(None) is None
        assert _as_utc_naive("") == ""

    def test_every_write_goes_through_the_utc_normaliser(self):
        src = Path("db/repositories/queue.py").read_text(encoding="utf-8")
        # update_queue_item() and schedule_queue_retry() are the two explicit
        # writers of an aware ISO string.
        assert src.count("_as_utc_naive(") >= 3, (
            "an aware next_retry_at written without normalisation is stored as "
            "the session's wall clock and read back as local"
        )
        assert '"next_retry": _as_utc_naive(next_retry_at)' in src
        assert 'if key == "next_retry_at":' in src

    def test_the_due_comparison_uses_a_utc_now_bind(self):
        from db.repositories.queue import _utc_now_naive

        now = _utc_now_naive()
        assert now.tzinfo is None, "the column is a naive TIMESTAMP"
        assert abs((now - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()) < 5

        src = Path("db/repositories/queue.py").read_text(encoding="utf-8")
        assert "next_retry_at <= :now_utc" in src
        assert "next_retry_at <= CURRENT_TIMESTAMP" not in src, (
            "comparing a naive UTC column with a timestamptz uses the session "
            "timezone and reintroduces the early-retry bug"
        )


# ===========================================================================
# 2. slskd 409 "not logged in" is infrastructure, not no_results
# ===========================================================================
class _Resp:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


NOT_LOGGED_IN = (
    "The server connection must be connected and logged in to perform a "
    "search (currently: Connected)"
)


class _HttpStub:
    enabled = True

    def __init__(self):
        self.responses: list[_Resp] = []

    def post_json(self, endpoint, payload, timeout=None):
        return self.responses.pop(0) if self.responses else _Resp(200, "{}")


class TestSlskd409IsInfrastructure:
    def test_the_reported_body_is_recognised(self):
        assert slskd._is_not_logged_in_409(_Resp(409, NOT_LOGGED_IN)) is True

    def test_another_409_is_not_mistaken_for_it(self):
        assert slskd._is_not_logged_in_409(_Resp(409, "conflict")) is False

    def test_start_search_retries_then_marks_unavailable(self, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr(slskd.time, "sleep", lambda seconds: sleeps.append(seconds))

        http = _HttpStub()
        http.responses = [_Resp(409, NOT_LOGGED_IN) for _ in range(5)]
        svc = slskd.SlskdService(http_client=http)

        assert svc.start_search("Spice Girls - Holler", max_attempts=5) is None
        assert svc.search_unavailable_reason == "soulseek_not_logged_in", (
            "a 409 connection-state rejection must not look like an empty search"
        )
        assert len(sleeps) == slskd._SLSKD_LOGIN_RETRY_ATTEMPTS - 1, (
            "the retry window must be brief and bounded"
        )

    def test_search_and_filter_returns_none_for_an_unavailable_search(self):
        svc = slskd.SlskdService(http_client=_HttpStub())
        svc.maybe_clear_stale_searches = lambda budget_seconds=6: False
        svc.start_search = lambda query, timeout=None: None
        svc.search_unavailable_reason = "soulseek_not_logged_in"

        assert svc.search_and_filter("Spice Girls - Holler", wait_seconds=0) is None, (
            "None is the caller's 'disconnected — do not back off' outcome"
        )

    def test_a_genuine_empty_search_still_returns_a_list(self):
        svc = slskd.SlskdService(http_client=_HttpStub())
        svc.maybe_clear_stale_searches = lambda budget_seconds=6: False
        svc.start_search = lambda query, timeout=None: None
        svc.search_unavailable_reason = ""

        assert svc.search_and_filter("Spice Girls - Holler", wait_seconds=0) == []


# ===========================================================================
# 2b. Repeated stalls cooldown the PEER, not just the file
# ===========================================================================
class TestRepeatedStallsCooldownThePeer:
    def test_a_stall_three_hours_apart_still_counts(self):
        """The mirror peers were retried every ~2–3h, outside the old 2h window."""
        import time

        from services.downloads import download_pipeline_service as dps

        dps._blocked_peers.clear()
        dps._blocked_usernames.clear()
        dps._peer_stall_timestamps.clear()
        try:
            dps._peer_stall_timestamps["mirrorbot"] = [time.time() - 3 * 3600]
            dps._record_peer_stall("mirrorbot", "music/Album/01.flac")

            assert "mirrorbot" in dps._blocked_usernames, (
                "two stalls three hours apart must still ban the peer; the "
                "reported mirror bots were retried just outside the 2h window"
            )
            assert dps._is_peer_blocked("mirrorbot", "music/Album/02.flac"), (
                "the cooldown applies to the PEER, so its other files are "
                "skipped too"
            )
        finally:
            dps._blocked_peers.clear()
            dps._blocked_usernames.clear()
            dps._peer_stall_timestamps.clear()


# ===========================================================================
# 3. Folder imports are serialised
# ===========================================================================
class TestFolderImportLock:
    def test_the_lock_key_is_filesystem_safe_and_stable(self):
        key = _folder_import_lock_key("/downloads/Abyss (2024)")
        assert key == _folder_import_lock_key("/downloads/Abyss (2024)")
        assert "/" not in key and "\\" not in key and ":" not in key

    def test_a_duplicate_import_is_skipped(self, monkeypatch):
        from services.downloads import download_folder_service as dfs

        called: list[str] = []
        monkeypatch.setattr(
            dfs, "_match_folder_to_release_locked",
            lambda folder, mb: called.append(folder) or {"moved": 99},
        )
        monkeypatch.setattr(dfs, "_resolve_folder_abs", lambda p: ("/downloads/Abyss", "/downloads"))
        monkeypatch.setattr(dfs, "_try_cross_process_lock", lambda key: None)

        # Hold the folder lock exactly as a concurrent import would.
        with dfs._folder_import_lock("/downloads/Abyss") as held:
            assert held
            result = dfs.match_folder_to_release("/downloads/Abyss", "mbid")

        assert result["skipped"] is True
        assert result["moved"] == 0
        assert called == [], "the second pass started converting/moving files"

    def test_the_lock_holder_runs_the_import(self, monkeypatch):
        from services.downloads import download_folder_service as dfs

        monkeypatch.setattr(
            dfs, "_match_folder_to_release_locked",
            lambda folder, mb: {"success": True, "moved": 3},
        )
        monkeypatch.setattr(dfs, "_resolve_folder_abs", lambda p: ("/downloads/Abyss", "/downloads"))
        monkeypatch.setattr(dfs, "_try_cross_process_lock", lambda key: None)

        assert dfs.match_folder_to_release("/downloads/Abyss", "mbid")["moved"] == 3

    def test_the_lock_is_released_after_the_import(self, monkeypatch):
        from services.downloads import download_folder_service as dfs

        monkeypatch.setattr(
            dfs, "_match_folder_to_release_locked",
            lambda folder, mb: {"success": True, "moved": 1},
        )
        monkeypatch.setattr(dfs, "_resolve_folder_abs", lambda p: ("/downloads/Abyss", "/downloads"))
        monkeypatch.setattr(dfs, "_try_cross_process_lock", lambda key: None)

        assert dfs.match_folder_to_release("/downloads/Abyss", "mbid")["moved"] == 1
        with dfs._folder_import_lock("/downloads/Abyss") as held:
            assert held, "a finished import left the folder lock held"


# ===========================================================================
# 4. Background services do not start on import
# ===========================================================================
class TestNoSchedulerOnImport:
    def test_the_election_runs_from_before_serving(self):
        src = Path("app.py").read_text(encoding="utf-8")
        assert "@app.before_serving" in src
        hook = src.split("async def _start_background_services_on_serving", 1)
        assert len(hook) == 2, "the before_serving hook is missing"
        assert "_elect_leader_and_start_services(app)" in hook[1], (
            "the hook must perform the election"
        )

    def test_the_module_level_call_is_gone(self):
        tree = ast.parse(Path("app.py").read_text(encoding="utf-8"))
        top_level_calls = [
            node
            for node in tree.body
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "_elect_leader_and_start_services"
        ]
        assert top_level_calls == [], (
            "importing app started the scheduler, so the entrypoint's pre-flight "
            "import and Hypercorn's leader each logged 'Download scheduler started'"
        )

    def test_the_one_per_process_guard_exists(self):
        src = Path("app.py").read_text(encoding="utf-8")
        assert "_BACKGROUND_SERVICES_STARTED" in src
