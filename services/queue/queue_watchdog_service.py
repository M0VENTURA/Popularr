"""Automatic restart for a STOPPED download queue processor.

WHY THIS EXISTS
---------------
``/api/queue-processor/status`` can already say "stopped", and the Downloads /
Config pages already draw that verdict plus a **Restart Processor** button. But
both of those surfaces are pages the operator has to be looking at. A queue
that has silently stopped keeps accepting new items and processing none of
them, and nothing draws attention to it — the dashboard, which is the surface
the operator actually leaves open, knew nothing about the queue at all.

This module is the missing half:

  * ``auto_restart_queue_processor()`` — one decision + one action, callable
    directly (used by the scheduler job and by the tests), and
  * ``queue_processor_watchdog_tick()`` — the module-level entry point
    APScheduler references by name.

WHY THE PROCESSOR NEEDS A WATCHDOG RATHER THAN A SUPERVISOR
-----------------------------------------------------------
The download queue is driven by ONE of two interchangeable processes and
neither PID is visible to the other (see ``queue_heartbeat.py``):

  * ``services.queue.queue_worker`` — the standalone worker started by
    ``entrypoint.sh`` / ``popularr-queue-worker.service``, and
  * the APScheduler ``download_queue_processor`` job inside the web process.

The WebUI owns neither, so it cannot ``kill`` and re-exec one. What it CAN do —
and what the Restart button already does — is make the next cycle happen now:
re-arm the in-process job, or run a single cycle itself when there is no job to
re-arm (``queue_diagnostics_service._kick_queue_cycle``). That restores
processing and re-stamps the heartbeat, which is what "restart" honestly means
here. A supervisor daemon would be a lie about a process this app does not own.

CONFIG
------
``queue.auto_restart`` in ``config.yaml`` (Config page ▸ Queue Retry Windows):

    enabled           — default ``true``
    cooldown_seconds  — default ``300``, floored at 30

The COOLDOWN is what keeps a genuinely broken environment (database down, so
every cycle raises) from turning the watchdog into a restart storm: at most one
automatic restart per window, and the manual button is never blocked by it.

The CHECK CADENCE is deliberately not a separate knob — it is the queue
worker's own ``interval_seconds``, so the watchdog polls on the same beat as
the driver it is watching. Making it a third setting would be a control whose
only correct value is "the same as the other one".

WHAT COUNTS AS "FAILED"
-----------------------
Only ``status == "stopped"``. That is the same signal the card draws, so the
alert the operator sees and the condition the watchdog acts on can never
disagree. ``running`` and ``restarting`` are both left alone — a cycle in
flight is not a failure, and re-kicking it would double up work.

The decision is deliberately re-READ from the status endpoint each tick rather
than cached: a heartbeat that arrives between two ticks must cancel the
restart, not race it.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import structlog

from helpers.config_helpers import get_state_directory
from helpers.logging_config import log_queue

logger = structlog.get_logger(__name__)

#: APScheduler job id. Named here (not in the scheduler) so the registration and
#: every reader agree on one string — a second literal is how a job ends up
#: registered under one id and looked up under another.
WATCHDOG_JOB_ID = "queue_processor_watchdog"

#: Filename of the watchdog's own state, under the state directory.
WATCHDOG_FILE_NAME = "queue_processor_watchdog.json"

#: Auto-restart is ON by default: a stopped queue is never a desired state, and
#: the worst case of a false positive is one redundant cycle.
DEFAULT_ENABLED = True

#: Minimum gap between AUTOMATIC restarts.
DEFAULT_COOLDOWN_SECONDS = 300.0

#: Never allow a cooldown short enough to become a storm, whatever the config
#: says. The status verdict itself is stable for at least
#: ``queue_heartbeat._MIN_STALE_AFTER_SECONDS`` (60s), so a sub-30s cooldown
#: could only ever produce repeats of the same verdict.
_MIN_COOLDOWN_SECONDS = 30.0

#: Never poll faster than this, even with a very short worker interval — the
#: watchdog shares the process with the web server.
_MIN_CHECK_INTERVAL_SECONDS = 60.0


# =============================================================================
# CONFIG
# =============================================================================

def _as_bool(value: Any, default: bool) -> bool:
    """Config booleans arrive as bools from YAML and as strings from forms."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return default


def _as_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def get_auto_restart_config() -> dict[str, Any]:
    """The ``queue.auto_restart`` section, with defaults applied and clamped.

    Returns ``enabled``, ``cooldown_seconds`` and ``check_interval_seconds``.
    Every value has a usable default, so a config.yaml that predates this
    feature behaves exactly like one that sets the defaults explicitly.
    """
    section: dict[str, Any] = {}
    try:
        from helpers.config_helpers import get_config

        queue_cfg = (get_config() or {}).get("queue") or {}
        raw = queue_cfg.get("auto_restart")
        if isinstance(raw, dict):
            section = raw
    except Exception as exc:
        logger.debug("Queue auto-restart config unreadable — using defaults", error=str(exc))

    cooldown = _as_float(section.get("cooldown_seconds"), DEFAULT_COOLDOWN_SECONDS)
    return {
        "enabled": _as_bool(section.get("enabled"), DEFAULT_ENABLED),
        "cooldown_seconds": max(_MIN_COOLDOWN_SECONDS, cooldown),
        "check_interval_seconds": check_interval_seconds(),
    }


def check_interval_seconds() -> float:
    """How often to look, derived from the queue worker's own interval.

    See the module docstring: this is deliberately not a separate setting.
    """
    try:
        from helpers.config_helpers import get_queue_worker_config

        interval = float(get_queue_worker_config().get("interval_seconds", 60) or 60)
    except Exception:
        interval = 60.0
    return max(_MIN_CHECK_INTERVAL_SECONDS, interval)


# =============================================================================
# STATE (last automatic restart)
# =============================================================================

def watchdog_file_path() -> str:
    """Absolute path of the watchdog's state file."""
    override = str(os.environ.get("QUEUE_PROCESSOR_WATCHDOG_FILE") or "").strip()
    if override:
        return override
    return os.path.join(get_state_directory(), WATCHDOG_FILE_NAME)


def read_watchdog_state() -> dict[str, Any]:
    """Last automatic restart, or ``{}``.

    Tolerant by design: an unreadable or corrupt file must never stop the
    watchdog from running, because "we lost track of the last restart" is a
    far smaller problem than "the queue never gets restarted again".
    """
    path = watchdog_file_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.debug("Queue watchdog state unreadable", error=str(exc))
        return {}


def _write_watchdog_state(state: dict[str, Any]) -> None:
    """Persist the state atomically (per-PID temp file + ``os.replace``)."""
    path = watchdog_file_path()
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = f"{path}.{os.getpid()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.debug("Queue watchdog state not written", error=str(exc))


def auto_restart_snapshot() -> dict[str, Any]:
    """What the status endpoint (and so the UI) needs to report about this.

    Kept separate from ``read_watchdog_state`` so the payload shape is owned in
    one place and never leaks raw file keys to the browser.
    """
    cfg = get_auto_restart_config()
    state = read_watchdog_state()
    return {
        "enabled": bool(cfg["enabled"]),
        "cooldown_seconds": cfg["cooldown_seconds"],
        "check_interval_seconds": cfg["check_interval_seconds"],
        "last_restart_at": state.get("last_restart_at"),
        "last_restart_iso": state.get("last_restart_iso"),
        "last_reason": state.get("last_reason"),
        "restart_count": int(state.get("restart_count") or 0),
    }


# =============================================================================
# THE DECISION
# =============================================================================

def auto_restart_queue_processor() -> dict[str, Any]:
    """Restart the queue processor IF it is stopped and the cooldown allows it.

    Always returns a dict describing what was decided — this is called by a
    scheduler tick whose only other output is a log line, so the return value
    is what the tests assert on.
    """
    cfg = get_auto_restart_config()
    if not cfg["enabled"]:
        return {
            "attempted": False,
            "restarted": False,
            "reason": "auto-restart is disabled in the configuration",
        }

    # Imported lazily: ``queue_diagnostics_service`` reads this module for the
    # status payload, so a module-level import here would be a cycle.
    from services.queue.queue_diagnostics_service import (
        queue_processor_restart,
        queue_processor_status,
    )

    status_payload, _status_code = queue_processor_status()
    state = str(status_payload.get("status") or "").strip().lower()

    if state != "stopped":
        return {
            "attempted": False,
            "restarted": False,
            "status": state or "unknown",
            "reason": f"queue processor is {state or 'in an unknown state'} — nothing to restart",
        }

    cooldown = float(cfg["cooldown_seconds"])
    now = time.time()
    previous = read_watchdog_state()
    last_restart = _as_float(previous.get("last_restart_at"), 0.0)
    if last_restart and (now - last_restart) < cooldown:
        remaining = cooldown - (now - last_restart)
        return {
            "attempted": False,
            "restarted": False,
            "status": state,
            "seconds_until_eligible": round(remaining, 1),
            "reason": (
                f"waiting out the {int(cooldown)}s auto-restart cooldown "
                f"({int(remaining)}s left)"
            ),
        }

    restart_payload, _restart_code = queue_processor_restart()

    restart_count = int(previous.get("restart_count") or 0) + 1
    reason = str(status_payload.get("message") or "queue processor was not running")
    _write_watchdog_state(
        {
            "last_restart_at": now,
            "last_restart_iso": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "last_reason": reason,
            "restart_count": restart_count,
        }
    )

    # WARNING, not info: this fires only when something was genuinely wrong.
    logger.warning(
        "Auto-restarted the download queue processor",
        restart_count=restart_count,
        reason=reason,
    )
    try:
        log_queue(
            "[QUEUE] auto-restart",
            reason=reason,
            restart_count=restart_count,
        )
    except Exception as exc:
        logger.debug("Queue auto-restart log line not written", error=str(exc))

    return {
        "attempted": True,
        "restarted": True,
        "status": state,
        "restart_count": restart_count,
        "reason": reason,
        "message": restart_payload.get("message"),
    }


def queue_processor_watchdog_tick() -> None:
    """APScheduler entry point — module level so it can be serialised by name.

    Swallows everything: a watchdog that raises inside a scheduler thread would
    be logged as a job failure and, worse, would stop *itself* from running the
    next cycle. A watchdog has no business taking the scheduler down.
    """
    try:
        result = auto_restart_queue_processor()
        if result.get("restarted"):
            logger.info("Queue watchdog restart complete", **{
                "restart_count": result.get("restart_count"),
            })
    except Exception as exc:
        logger.warning("Queue processor watchdog tick failed", error=str(exc))
