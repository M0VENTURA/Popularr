"""Safe shutdown: quiesce the app so PostgreSQL can be stopped cleanly.

The reported need
-----------------
> Would it be possible to have a safe shutdown option in the config that will
> stop the database for a clean update of the postgres server?

Why ``docker compose stop db`` is not enough on its own, and therefore what
these tests have to pin:

1. ``docker stop`` sends SIGTERM, and PostgreSQL treats SIGTERM as a **smart
   shutdown** — it stops accepting work and then WAITS FOR EVERY CLIENT TO
   DISCONNECT. The app's pool holds idle connections open, and an idle pooled
   connection is still a client, so the postmaster waits until Docker's stop
   timeout expires and the container is SIGKILLed. Closing the pool is what
   makes the stop clean, so it is a first-class part of the feature.
2. Stopping the database underneath a running scan or queue cycle means the app
   writes into a server that is going away. Quiescing first is the other half.

The flag AUTO-EXPIRES, and that is load-bearing rather than a nicety: a persisted
"quiesced" flag with no expiry would leave the application permanently dormant
after a forgotten shutdown — it would look like a hang with no visible cause.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.maintenance import safe_shutdown_service as ss


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch):
    """Point the state directory at a temp dir, so no test touches /state."""
    monkeypatch.setenv("SCAN_STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def no_scheduler(monkeypatch):
    """No scheduler exists, so pausing is a no-op rather than a DB connection."""
    import services.scheduler.scheduler_service as sched

    monkeypatch.setattr(sched, "_SCHEDULER", None)
    return sched


@pytest.fixture
def quiet_work(monkeypatch):
    """No scans running and no queue items in flight."""
    monkeypatch.setattr(ss, "_active_scan_types", lambda: [])
    monkeypatch.setattr(ss, "_inflight_queue_items", lambda: 0)


# ---------------------------------------------------------------------------
# The flag
# ---------------------------------------------------------------------------

def test_nothing_is_requested_by_default(state_dir) -> None:
    assert ss.is_safe_shutdown_requested() is False
    assert ss.safe_shutdown_status()["requested"] is False


def test_request_then_cancel_round_trips(state_dir, no_scheduler, quiet_work) -> None:
    assert ss.request_safe_shutdown(reason="test")["success"] is True
    assert ss.is_safe_shutdown_requested() is True

    result = ss.cancel_safe_shutdown()
    assert result["success"] is True
    assert ss.is_safe_shutdown_requested() is False


def test_the_flag_lives_in_the_state_dir_not_the_database(state_dir, no_scheduler) -> None:
    """It must be readable while the database is DOWN — that is the whole point."""
    ss.request_safe_shutdown(reason="test")

    flag = Path(state_dir) / ss.STATE_FILE_NAME
    assert flag.is_file(), "the flag must be a state-dir file, not a DB row"

    payload = json.loads(flag.read_text(encoding="utf-8"))
    assert payload["requested"] is True
    assert payload["reason"] == "test"
    assert payload["requested_at"]


def test_cancelling_when_nothing_was_requested_is_harmless(state_dir, no_scheduler) -> None:
    assert ss.cancel_safe_shutdown()["success"] is True
    assert ss.is_safe_shutdown_requested() is False


def test_an_unreadable_flag_fails_OPEN(state_dir, monkeypatch, no_scheduler) -> None:
    """A corrupt flag must NOT be read as "quiesced".

    Reading it as quiesced would silently stop all download processing because of
    a bad file, and the queue would look broken with nothing in the log to explain
    it. A missed quiesce is recoverable; a queue that stops for no visible reason
    is not.
    """
    (Path(state_dir) / ss.STATE_FILE_NAME).write_text("{not json", encoding="utf-8")

    assert ss.is_safe_shutdown_requested() is False


# ---------------------------------------------------------------------------
# Auto-expiry — the anti-dormancy guard
# ---------------------------------------------------------------------------

def test_an_expired_request_clears_itself(state_dir, no_scheduler, monkeypatch) -> None:
    """A forgotten shutdown must not leave the app dormant forever."""
    stale = {
        "requested": True,
        "requested_at": (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(),
        "reason": "forgotten",
        "auto_cancel_after_minutes": 60,
    }
    (Path(state_dir) / ss.STATE_FILE_NAME).write_text(json.dumps(stale), encoding="utf-8")

    assert ss.is_safe_shutdown_requested() is False, "an expired request must not hold"
    assert not (Path(state_dir) / ss.STATE_FILE_NAME).exists(), (
        "the expired flag must be cleared on read, not left to linger"
    )


def test_a_fresh_request_is_not_expired(state_dir, no_scheduler, quiet_work) -> None:
    ss.request_safe_shutdown(reason="fresh", auto_cancel_after_minutes=120)
    assert ss.is_safe_shutdown_requested() is True


def test_zero_minutes_disables_the_expiry(state_dir, no_scheduler, quiet_work) -> None:
    """``0`` means "no expiry", and that has to be honoured rather than read as 0 minutes."""
    stale = {
        "requested": True,
        "requested_at": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
        "reason": "explicitly unbounded",
        "auto_cancel_after_minutes": 0,
    }
    (Path(state_dir) / ss.STATE_FILE_NAME).write_text(json.dumps(stale), encoding="utf-8")

    assert ss.is_safe_shutdown_requested() is True


def test_status_reports_the_time_until_expiry(state_dir, no_scheduler, quiet_work) -> None:
    ss.request_safe_shutdown(reason="test", auto_cancel_after_minutes=120)

    status = ss.safe_shutdown_status()
    assert status["expires_at"] is not None
    assert 0 < status["seconds_until_expiry"] <= 120 * 60


# ---------------------------------------------------------------------------
# safe_to_stop is DERIVED, never asserted
# ---------------------------------------------------------------------------

def test_not_safe_when_nothing_was_requested(state_dir, no_scheduler, quiet_work) -> None:
    """Safe means "quiesced AND idle" — an idle app that is still scheduled is not safe."""
    assert ss.safe_shutdown_status()["safe_to_stop"] is False


def test_safe_once_quiesced_and_idle(state_dir, no_scheduler, quiet_work) -> None:
    ss.request_safe_shutdown(reason="test")
    assert ss.safe_shutdown_status()["safe_to_stop"] is True


def test_not_safe_while_a_scan_is_running(state_dir, no_scheduler, monkeypatch) -> None:
    monkeypatch.setattr(ss, "_active_scan_types", lambda: ["library"])
    monkeypatch.setattr(ss, "_inflight_queue_items", lambda: 0)

    ss.request_safe_shutdown(reason="test")

    status = ss.safe_shutdown_status()
    assert status["safe_to_stop"] is False
    assert status["active_scans"] == ["library"]


def test_not_safe_while_queue_items_are_in_flight(state_dir, no_scheduler, monkeypatch) -> None:
    monkeypatch.setattr(ss, "_active_scan_types", lambda: [])
    monkeypatch.setattr(ss, "_inflight_queue_items", lambda: 3)

    ss.request_safe_shutdown(reason="test")

    status = ss.safe_shutdown_status()
    assert status["safe_to_stop"] is False
    assert status["queue_items"] == 3


# ---------------------------------------------------------------------------
# Draining
# ---------------------------------------------------------------------------

def test_drain_returns_immediately_when_idle(state_dir, no_scheduler, quiet_work) -> None:
    result = ss.wait_for_quiescence(timeout_seconds=5)
    assert result["drained"] is True
    assert result["timed_out"] is False
    assert result["polls"] == 1


def test_drain_times_out_and_REPORTS_what_is_left(state_dir, no_scheduler, monkeypatch) -> None:
    """A timeout must report, not force.

    A queue item can legitimately be mid-move of a large file; abandoning that is
    worse than telling the operator it is still running.
    """
    monkeypatch.setattr(ss, "_active_scan_types", lambda: ["library"])
    monkeypatch.setattr(ss, "_inflight_queue_items", lambda: 2)

    result = ss.wait_for_quiescence(timeout_seconds=2)

    assert result["drained"] is False
    assert result["timed_out"] is True
    assert result["active_scans"] == ["library"]
    assert result["queue_items"] == 2


# ---------------------------------------------------------------------------
# Releasing the pool — the part that makes the DB stop clean
# ---------------------------------------------------------------------------

def test_release_disposes_the_engine_but_keeps_it(state_dir, monkeypatch) -> None:
    """⚠️ The engine must be KEPT, not discarded.

    Keeping it is what makes a quiesce survivable without a restart: SQLAlchemy
    opens a fresh connection on the next checkout, so the app recovers by itself
    once the database is back. Re-creating the engine would drop the memoised
    session factory and any session bound to the old one.
    """
    import db.engine as engine_module

    disposed: list[str] = []

    class _FakeEngine:
        def dispose(self):
            disposed.append("sync")

    monkeypatch.setattr(engine_module, "_ENGINE", _FakeEngine())
    monkeypatch.setattr(engine_module, "_ASYNC_ENGINE", None)

    report = ss.release_database_connections()

    assert disposed == ["sync"]
    assert report["disposed"] is True
    assert report["errors"] == []
    assert engine_module._ENGINE is not None, "the engine must be kept for reconnection"


def test_release_reports_a_dispose_failure(state_dir, monkeypatch) -> None:
    import db.engine as engine_module

    class _BrokenEngine:
        def dispose(self):
            raise RuntimeError("pool refused to close")

    monkeypatch.setattr(engine_module, "_ENGINE", _BrokenEngine())
    monkeypatch.setattr(engine_module, "_ASYNC_ENGINE", None)

    report = ss.release_database_connections()

    assert report["errors"], "a failed dispose must be reported, not swallowed"
    assert "pool refused to close" in report["errors"][0]


# ---------------------------------------------------------------------------
# The composed sequence
# ---------------------------------------------------------------------------

def test_prepare_runs_quiesce_then_drain_then_release(state_dir, no_scheduler, quiet_work, monkeypatch) -> None:
    """The ORDER is the feature: the scheduler's job store holds a connection.

    Disposing the pool before the scheduler stopped firing would have the
    scheduler reconnect against a database about to be taken down.
    """
    order: list[str] = []

    monkeypatch.setattr(ss, "request_safe_shutdown", lambda **kw: (order.append("quiesce") or {"success": True}))
    monkeypatch.setattr(ss, "wait_for_quiescence", lambda *a, **kw: (order.append("drain") or {"drained": True}))
    monkeypatch.setattr(ss, "release_database_connections", lambda: (order.append("release") or {"disposed": True}))
    monkeypatch.setattr(ss, "safe_shutdown_status", lambda: {"safe_to_stop": True})

    result = ss.prepare_for_database_shutdown(reason="test")

    assert order == ["quiesce", "drain", "release"]
    assert result["success"] is True


def test_prepare_stops_if_the_flag_cannot_be_written(state_dir, monkeypatch, no_scheduler) -> None:
    """A flag that did not persist must not release the connections.

    Releasing the pool without a persisted flag would make the app unable to
    answer while nothing records that a shutdown was intended — the operator would
    see a dead app with no explanation.
    """
    released: list[bool] = []
    monkeypatch.setattr(ss, "_write_state", lambda state: False)
    monkeypatch.setattr(ss, "release_database_connections", lambda: (released.append(True) or {}))

    result = ss.prepare_for_database_shutdown(reason="test")

    assert result["success"] is False
    assert released == [], "connections must NOT be released when the flag failed to write"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_config_defaults_are_usable_and_bounded() -> None:
    from helpers.config_helpers import get_safe_shutdown_config

    cfg = get_safe_shutdown_config()
    assert cfg["enabled"] is True
    assert cfg["drain_timeout_seconds"] == 60
    assert cfg["release_connections"] is True
    assert cfg["auto_cancel_after_minutes"] > 0, (
        "a default of 0 would mean no expiry, and a forgotten shutdown would "
        "leave the app dormant forever"
    )


def test_config_tolerates_junk(monkeypatch) -> None:
    monkeypatch.setattr(
        "helpers.config_helpers.get_config",
        lambda: {"maintenance": {"safe_shutdown": {
            "drain_timeout_seconds": "soon", "auto_cancel_after_minutes": None,
        }}},
    )
    from helpers.config_helpers import get_safe_shutdown_config

    cfg = get_safe_shutdown_config()
    assert cfg["drain_timeout_seconds"] == 60
    assert cfg["auto_cancel_after_minutes"] == 120


def test_config_clamps_negatives(monkeypatch) -> None:
    monkeypatch.setattr(
        "helpers.config_helpers.get_config",
        lambda: {"maintenance": {"safe_shutdown": {
            "drain_timeout_seconds": -5, "auto_cancel_after_minutes": -1,
        }}},
    )
    from helpers.config_helpers import get_safe_shutdown_config

    cfg = get_safe_shutdown_config()
    assert cfg["drain_timeout_seconds"] == 0
    assert cfg["auto_cancel_after_minutes"] == 0


def test_sanitizer_keeps_the_nested_block_a_dict() -> None:
    """Without the nested entry the Config page save coerces the block to ``{}``."""
    from routes.ui_routes import _sanitize_config_sections

    cleaned = _sanitize_config_sections(
        {"maintenance": {"safe_shutdown": {"enabled": True, "drain_timeout_seconds": 30}}}
    )
    assert cleaned["maintenance"]["safe_shutdown"] == {
        "enabled": True, "drain_timeout_seconds": 30,
    }


def test_sanitizer_coerces_a_scalar_block() -> None:
    from routes.ui_routes import _sanitize_config_sections

    cleaned = _sanitize_config_sections({"maintenance": {"safe_shutdown": "yes"}})
    assert cleaned["maintenance"]["safe_shutdown"] == {}


# ---------------------------------------------------------------------------
# The queue worker's view of the flag
# ---------------------------------------------------------------------------

def test_worker_idles_when_the_flag_is_set(state_dir, no_scheduler, quiet_work) -> None:
    """The worker must STOP starting new queue work while quiesced.

    Without this the operator quiesces, sees "SAFE TO STOP", and the worker starts
    a fresh download against a database that is being taken down.
    """
    from services.queue import queue_worker

    assert queue_worker._safe_shutdown_requested() is False

    ss.request_safe_shutdown(reason="test")
    assert queue_worker._safe_shutdown_requested() is True


def test_worker_FAILS_OPEN_when_the_flag_cannot_be_read(monkeypatch) -> None:
    """⚠️ An unreadable flag must mean "keep working".

    Failing closed would silently stop all download processing because of a
    permissions problem or a bad state directory — the queue would look broken
    with nothing in the log to explain it. A missed quiesce is recoverable.
    """
    from services.queue import queue_worker

    def _boom():
        raise RuntimeError("state directory is gone")

    monkeypatch.setattr(ss, "is_safe_shutdown_requested", _boom)

    assert queue_worker._safe_shutdown_requested() is False


def test_worker_reports_quiesced_once_the_flag_is_set(state_dir, no_scheduler, quiet_work) -> None:
    from services.queue import queue_worker

    ss.request_safe_shutdown(reason="test")
    assert queue_worker._safe_shutdown_requested() is True

    ss.cancel_safe_shutdown()
    assert queue_worker._safe_shutdown_requested() is False, (
        "cancelling must let the worker resume without a restart"
    )
