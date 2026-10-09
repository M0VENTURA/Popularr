"""Cross-process liveness heartbeat for the download queue processor.

WHY THIS EXISTS
---------------
The download queue is driven by ONE of two interchangeable processes:

  * ``services.queue.queue_worker`` — the standalone worker started by
    ``entrypoint.sh`` / ``popularr-queue-worker.service``, and
  * the APScheduler ``download_queue_processor`` job, which lives inside the
    web process.

Neither process can see the other's PID, so ``/api/queue-processor/status``
cannot answer "is the processor running?" by inspecting the process table —
and it must not pretend otherwise. The old implementation returned a hardcoded
``running=False, message="Queue processor status not implemented"``, which is
why the Download Queue Processor card sat on "Checking processor status…"
for ever.

Instead, every finished ``process_cycle()`` stamps a small JSON file here. A
reader treats a stamp younger than the staleness window as *running* and
anything older — or missing entirely — as *stopped*. This is the same
runtime-state pattern the app already uses for scan progress and the API
rate-limiter counters.

FILE LAYOUT
-----------
``<state>/queue_processor_health.json`` by default, where ``<state>`` is
``SCAN_STATE_DIR`` (``/state``). ``QUEUE_PROCESSOR_HEALTH_FILE`` overrides the
whole path — it used to be read only so a restart could *delete* it, which was
a no-op because nothing ever wrote it; it now names the heartbeat itself.

WRITERS / CONCURRENCY
---------------------
Three writers, two processes: the standalone worker, the scheduler tick, and
the Restart button's one-off kick. Every write is atomic (``os.replace`` of a
per-PID temp file), so a concurrent reader can never observe a half-written
file. Writes are best-effort: an unwritable state directory must never take a
queue cycle down, so failures are logged at debug level and swallowed.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: Filename used when ``QUEUE_PROCESSOR_HEALTH_FILE`` is not set.
HEALTH_FILE_NAME = "queue_processor_health.json"

#: Never declare the processor dead faster than this, even with a very short
#: configured interval — one slow cycle must not flip the card to "Stopped".
_MIN_STALE_AFTER_SECONDS = 60.0

#: How many configured cycles may be missed before the processor counts as
#: stopped. Three keeps a restart, a hung cycle and a late cycle all green,
#: while a processor that has genuinely stopped trips the card.
_STALE_AFTER_CYCLES = 3


def health_file_path() -> str:
    """Absolute path of the queue processor heartbeat file."""
    override = str(os.environ.get("QUEUE_PROCESSOR_HEALTH_FILE") or "").strip()
    if override:
        return override
    from helpers.config_helpers import get_state_directory

    return os.path.join(get_state_directory(), HEALTH_FILE_NAME)


def stale_after_seconds() -> float:
    """Age at which a heartbeat stops counting as evidence of life.

    Derived from the configured worker interval (``queue.worker.interval_seconds``)
    rather than hard-coded, so shortening the cycle shortens the detection
    window too. Never below :data:`_MIN_STALE_AFTER_SECONDS`.
    """
    try:
        from helpers.config_helpers import get_queue_worker_config

        interval = float(get_queue_worker_config().get("interval_seconds", 60) or 60)
    except Exception:
        interval = 60.0
    return max(_MIN_STALE_AFTER_SECONDS, interval * _STALE_AFTER_CYCLES)


def record_queue_processor_cycle(driver: str, *, outcome: str = "completed") -> None:
    """Stamp the heartbeat: *driver* just finished a queue cycle.

    Called by each driver AFTER ``process_cycle()`` returns — deliberately not
    from inside ``process_cycle`` itself, so a cycle that RAISED (a genuinely
    dead processor) leaves no stamp and the status endpoint reports the truth.

    ``outcome`` distinguishes a cycle that did the work from one that was
    skipped because another driver held the cycle lock; both prove the caller's
    loop is alive.
    """
    path = health_file_path()
    now = time.time()
    payload: dict[str, Any] = {
        "driver": str(driver or "unknown"),
        "outcome": outcome,
        "pid": os.getpid(),
        "ts": now,
        "recorded_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
    }
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = f"{path}.{os.getpid()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.debug(
            "Queue processor heartbeat not written",
            path=path,
            driver=payload["driver"],
            error=str(exc),
        )


def read_queue_processor_health() -> dict[str, Any] | None:
    """Read the heartbeat, adding ``age_seconds`` (``None`` when unreadable).

    Returns ``None`` when there is no stamp at all — which is itself the
    answer "no driver has completed a cycle in this deployment".
    """
    path = health_file_path()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.debug("Queue processor heartbeat unreadable", path=path, error=str(exc))
        return None

    if not isinstance(data, dict):
        return None

    ts: float | None
    try:
        raw_ts = data.get("ts")
        ts = float(raw_ts) if raw_ts is not None else None
    except (TypeError, ValueError):
        ts = None
    if ts is None:
        recorded = data.get("recorded_at")
        if recorded:
            try:
                ts = datetime.fromisoformat(str(recorded)).timestamp()
            except ValueError:
                ts = None

    age: float | None = None
    if ts is not None:
        # Clamped at zero so a clock that jumped backwards reports "just now"
        # instead of a negative age the UI would render as "-3s ago".
        age = max(0.0, time.time() - ts)

    data["age_seconds"] = age
    data["path"] = path
    return data
