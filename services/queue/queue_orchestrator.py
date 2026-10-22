"""Queue orchestration service.

Background-processing orchestration layer for the download queue.

Responsibilities:
    - Find queue items ready for processing.
    - Claim items safely (avoids double-processing).
    - Delegate to downloads/queue services.
    - Run lightweight periodic maintenance hooks.
    - Return consistent service-style responses.

Fairness (why this changed)
---------------------------
The ready list used to include 'queued' rows still waiting on their retry
time, ordered oldest first, and the batch was cut to the free in-flight slots
BEFORE any claim. Every slot went to old rows the claim then refused (409,
counted as "failed"), so newer items never started and the old ones were
retried the moment their wait ended. Now:

* the repository returns only claimable rows, fewest attempts first;
* a refused claim is a SKIP that uses no slot, and the batch keeps going
  until the free slots are filled with items that actually START;
* an item is marked failed here only when the processor left it in the claim
  status, so attempts are not counted twice.
"""

from __future__ import annotations

import importlib
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional

import structlog

from db.repositories import queue as queue_repository
from helpers.normalization_service import normalise_result
from helpers.response_helpers import _fail, _ok
from services.queue.queue_config import _SLSKD_INTER_ITEM_DELAY_SECONDS

logger = structlog.get_logger(__name__)

MAX_IN_FLIGHT = 15


# =============================================================================
# TYPES
# =============================================================================

ProcessorFunc = Callable[[dict[str, Any]], Any]
MaintenanceFunc = Callable[[], Any]


@dataclass(frozen=True)
class ProcessorCandidate:
    module: str
    function: str


@dataclass(frozen=True)
class MaintenanceCandidate:
    module: str
    function: str


# =============================================================================
# CONFIG
# =============================================================================

CLAIM_STATUS = "searching"

DO_NOT_PROCESS_STATUSES = {
    "completed",
    "imported",
    "deleted",
    "failed",
    "unmatched",
    "in_collection",
    "matched",
}

PROCESSOR_CANDIDATES: tuple[ProcessorCandidate, ...] = (
    ProcessorCandidate(
        "services.downloads.download_processing_service",
        "process_queue_item",
    ),
    ProcessorCandidate(
        "services.downloads.download_processing_service",
        "process_download_queue_item",
    ),
    ProcessorCandidate(
        "services.queue.queue_processing_worker_service",
        "process_queue_item",
    ),
)

MAINTENANCE_CANDIDATES: tuple[MaintenanceCandidate, ...] = (
    MaintenanceCandidate(
        "services.downloads.download_retry_service",
        "retry_due_items",
    ),
    MaintenanceCandidate(
        "services.downloads.download_scan_service",
        "check_completed_downloads",
    ),
    MaintenanceCandidate(
        "services.downloads.cleanup_engine_service",
        "cleanup_stale_downloads",
    ),
    MaintenanceCandidate(
        "services.queue.queue_cleanup_service",
        "cleanup_stuck_items",
    ),
    MaintenanceCandidate(
        "services.downloads.slskd_reaper_service",
        "reap_stalled_transfers",
    ),
)

#: Substrings that identify a DATABASE outage (the log shows DNS failures for
#: ``popularr-postgres``, "shutting down", "recovery mode" and "not yet
#: accepting connections"). Used to skip the rest of a cycle instead of
#: running every hook into the same error.
_DB_OUTAGE_MARKERS: tuple[str, ...] = (
    "operationalerror",
    "could not translate host name",
    "connection refused",
    "the database system is",
    "server closed the connection",
    "timeout expired",
    "could not connect",
    "connection to server",
)


def is_db_outage_error(error: Any) -> bool:
    """True when ``error`` (exception or message) looks like a lost database."""
    text_value = str(error or "").lower()
    if not text_value:
        return False
    return any(marker in text_value for marker in _DB_OUTAGE_MARKERS)


def database_available() -> bool:
    """Cheap probe: can the database answer a trivial query right now?"""
    try:
        from sqlalchemy import text

        from db.engine import db_session

        with db_session() as session:
            session.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.debug("Database probe failed", error=str(exc))
        return False


# =============================================================================
# DYNAMIC SERVICE RESOLUTION
# =============================================================================

def _load_callable(module_name: str, function_name: str) -> Callable[..., Any] | None:
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        logger.debug("Optional module unavailable", module=module_name, error=str(exc))
        return None

    func = getattr(module, function_name, None)
    if not callable(func):
        logger.debug("Optional function unavailable", module=module_name, function=function_name)
        return None

    return func


def _resolve_processor() -> ProcessorFunc | None:
    for candidate in PROCESSOR_CANDIDATES:
        func = _load_callable(candidate.module, candidate.function)
        if callable(func):
            logger.debug(
                "Using resolved queue processor",
                module=candidate.module,
                function=candidate.function,
            )
            return func
    return None


def _iter_maintenance_hooks() -> Iterable[MaintenanceFunc]:
    for candidate in MAINTENANCE_CANDIDATES:
        func = _load_callable(candidate.module, candidate.function)
        if callable(func):
            yield func


# =============================================================================
# ITEM NORMALISATION
# =============================================================================

def _as_dict(item: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)

    if hasattr(item, "keys"):
        try:
            return {key: item[key] for key in item.keys()}
        except Exception:
            pass

    try:
        return dict(item)
    except Exception:
        return {}


def _item_id(item: Mapping[str, Any]) -> Optional[int]:
    raw_id = item.get("id")
    if raw_id is None:
        raw_id = item.get("queue_id")

    if raw_id is None:
        return None

    if isinstance(raw_id, int):
        return raw_id

    if isinstance(raw_id, str):
        raw_id = raw_id.strip()
        if not raw_id:
            return None

    try:
        return int(raw_id)
    except (TypeError, ValueError):
        return None


def _item_status(item: Mapping[str, Any]) -> str:
    return str(item.get("status") or "").strip().lower()


def _should_process(item: Mapping[str, Any]) -> bool:
    item_id = _item_id(item)
    if item_id is None:
        return False

    status = _item_status(item)
    if status in DO_NOT_PROCESS_STATUSES:
        return False

    return True


# =============================================================================
# REPOSITORY ADAPTERS
# =============================================================================

def _get_ready_items(limit: int) -> list[dict[str, Any]]:
    items = queue_repository.get_ready_for_processing(limit) or []
    return [_as_dict(item) for item in items]


def _claim_item(item: Mapping[str, Any]) -> dict[str, Any] | None:
    queue_id = _item_id(item)
    if not queue_id:
        return None

    claim_func = getattr(queue_repository, "claim_queue_item", None)
    if callable(claim_func):
        claimed = claim_func(queue_id, status=CLAIM_STATUS)
        return _as_dict(claimed) if claimed else None

    claim_func = getattr(queue_repository, "claim_for_processing", None)
    if callable(claim_func):
        claimed = claim_func(queue_id, status=CLAIM_STATUS)
        return _as_dict(claimed) if claimed else None

    update_func = getattr(queue_repository, "update_queue_item", None)
    if callable(update_func):
        updated = update_func(queue_id, status=CLAIM_STATUS)
        return _as_dict(updated) if updated else None

    logger.error("No repository claim/update function available", queue_id=queue_id)
    return None


def _still_claimed(queue_id: int) -> bool:
    """Is the row still in the claim status (the processor set no outcome)?

    Fails CLOSED (False): when the status cannot be read we do not know the
    processor left it alone, and marking it failed then could count an
    attempt twice or push a live transfer back to 'queued'. A row genuinely
    left in 'searching' is reset by ``cleanup_stuck_items`` anyway.
    """
    try:
        return bool(queue_repository.item_has_status(int(queue_id), CLAIM_STATUS))
    except Exception:
        return False


def _mark_failed(
    item: Mapping[str, Any],
    reason: str,
    *,
    clear_file_path: bool = False,
) -> dict[str, Any] | None:
    queue_id = _item_id(item)
    if not queue_id:
        return None

    try:
        failed = queue_repository.mark_failed(
            int(queue_id),
            str(reason or "Queue item processor failed"),
        )
        if failed and clear_file_path:
            queue_repository.update_queue_item(int(queue_id), file_path=None)
        return _as_dict(failed) if failed else None
    except Exception:
        logger.exception("Failed to mark queue item as failed", queue_id=queue_id)
        return None


# =============================================================================
# PROCESSING
# =============================================================================

def process_queue_item(
    item: Mapping[str, Any],
    processor: ProcessorFunc | None = None,
) -> tuple[dict[str, Any], int]:
    """Process one queue item."""
    raw_item = _as_dict(item)
    queue_id = _item_id(raw_item)

    if not queue_id:
        return _fail("Queue item has no id", 400, item=raw_item)

    if not _should_process(raw_item):
        return _ok(
            skipped=True,
            queue_id=queue_id,
            reason="Item is not eligible for processing",
            status=_item_status(raw_item),
        )

    resolved_processor = processor or _resolve_processor()
    if resolved_processor is None:
        return _fail(
            "No migrated queue item processor is available",
            501,
            queue_id=queue_id,
            hint=(
                "Add a process_queue_item(item) function to "
                "services.downloads.download_processing_service or update "
                "PROCESSOR_CANDIDATES in services.queue.queue_orchestrator."
            ),
        )

    claimed = _claim_item(raw_item)
    if not claimed:
        # Another driver took it, or its retry time has not arrived yet. Not a
        # failure: reporting it as one (409) made a batch that started nothing
        # look like real work, and it used up a slot.
        return _ok(skipped=True, queue_id=queue_id, reason="not_claimable")

    try:
        result = resolved_processor(claimed)
        payload, status = normalise_result(result)
        payload.setdefault("queue_id", queue_id)

        # The pipeline normally records its own outcome (downloading,
        # backed_off, mark_failed…). Only a row it left in the CLAIM status is
        # marked failed here — so an attempt is never counted twice, and a live
        # transfer is never pushed back to 'queued'. The old check also ignored
        # every failure below HTTP 500, leaving those rows in 'searching' until
        # the stuck cleanup reset them.
        failed = (status >= 400 or payload.get("success") is False) and not payload.get("skipped")
        if failed and _still_claimed(queue_id):
            _mark_failed(claimed, payload.get("error") or "Queue item processor failed")

        return payload, status

    except Exception as exc:
        logger.exception("Queue item processing failed", queue_id=queue_id, error=str(exc))
        if _still_claimed(queue_id):
            _mark_failed(claimed, str(exc))
        try:
            from services.queue.queue_diagnostics_service import log_queue_event
            log_queue_event(
                "failed",
                f"{str(claimed.get('artist') or '')} - {str(claimed.get('title') or '')} → processing failed: {exc}",
                queue_id=queue_id,
            )
        except Exception:
            pass
        return _fail(str(exc), 500, queue_id=queue_id)


def process_next_batch(
    limit: int = 50,
    *,
    inter_item_delay_seconds: float | None = None,
    max_in_flight: int | None = None,
    use_cycle_lock: bool = True,
) -> tuple[dict[str, Any], int]:
    """Process a batch of ready queue items.

    ``limit`` is how many ready rows are FETCHED; at most the free in-flight
    slots are STARTED. Fetching more than the slots lets a refused claim be
    skipped without wasting the slot.
    """
    started_at = time.time()

    try:
        limit = max(1, int(limit or 50))
    except (TypeError, ValueError):
        limit = 50

    if inter_item_delay_seconds is None:
        inter_item_delay_seconds = _SLSKD_INTER_ITEM_DELAY_SECONDS

    if max_in_flight is None:
        max_in_flight = _resolve_max_in_flight()

    try:
        processor = _resolve_processor()
        if processor is None:
            return _fail(
                "No migrated queue item processor is available",
                501,
                hint=(
                    "Create services.downloads.download_processing_service."
                    "process_queue_item(item), or adjust PROCESSOR_CANDIDATES."
                ),
            )

        def _run_batch() -> tuple[dict[str, Any], int]:
            available_slots = _available_in_flight_slots(max_in_flight)
            if available_slots <= 0:
                # Every slot is busy: do not even fetch. Waiting is correct.
                return _ok(
                    total=0,
                    processed=0,
                    succeeded=0,
                    skipped=0,
                    failed=0,
                    throttled=True,
                    more_ready=False,
                    elapsed_seconds=round(time.time() - started_at, 3),
                    results=[],
                )

            items = _get_ready_items(limit)
            if not items:
                return _ok(
                    total=0,
                    processed=0,
                    succeeded=0,
                    skipped=0,
                    failed=0,
                    throttled=False,
                    more_ready=False,
                    elapsed_seconds=round(time.time() - started_at, 3),
                    results=[],
                )

            return _process_batch(
                items=items,
                processor=processor,
                inter_item_delay_seconds=inter_item_delay_seconds,
                started_at=started_at,
                max_to_start=available_slots,
                fetch_limit=limit,
            )

        if use_cycle_lock:
            from services.queue.queue_lock import queue_cycle_lock

            with queue_cycle_lock() as acquired:
                if not acquired:
                    # DEBUG: with the standalone worker AND the scheduler
                    # thread both driving cycles, this fires for every other
                    # driver on every tick. It is expected contention.
                    logger.debug("Another queue cycle holds the lock — skipping batch")
                    return _ok(
                        total=0,
                        processed=0,
                        succeeded=0,
                        skipped=0,
                        failed=0,
                        throttled=True,
                        more_ready=False,
                        reason="Another queue cycle is running",
                        elapsed_seconds=round(time.time() - started_at, 3),
                        results=[],
                    )
                return _run_batch()

        return _run_batch()

    except Exception as exc:
        logger.exception("process_next_batch failed", error=str(exc))
        return _fail(str(exc), 500, db_outage=is_db_outage_error(exc))


def _resolve_max_in_flight() -> int:
    try:
        env_value = int(os.getenv("QUEUE_MAX_IN_FLIGHT") or 0)
        if env_value > 0:
            return env_value
    except (TypeError, ValueError):
        pass
    try:
        from helpers.config_helpers import get_queue_worker_config
        return int(get_queue_worker_config().get("max_in_flight") or MAX_IN_FLIGHT)
    except Exception:
        return MAX_IN_FLIGHT


def _available_in_flight_slots(max_in_flight: int) -> int:
    try:
        counts = queue_repository.get_queue_status_counts()
        in_flight = int(counts.get("searching", 0) or 0) + int(
            counts.get("downloading", 0) or 0
        )
        return max(0, int(max_in_flight) - in_flight)
    except Exception as exc:
        logger.debug("In-flight slot check failed", error=str(exc))
        return max_in_flight


def _process_batch(
    *,
    items: list[dict[str, Any]],
    processor: ProcessorFunc,
    inter_item_delay_seconds: float | None,
    started_at: float,
    max_to_start: int | None = None,
    fetch_limit: int | None = None,
) -> tuple[dict[str, Any], int]:
    processed = 0
    succeeded = 0
    skipped = 0
    failed = 0
    started = 0
    reached_end = True
    results: list[dict[str, Any]] = []

    for index, item in enumerate(items):
        if max_to_start is not None and started >= max_to_start:
            reached_end = False
            break

        queue_id = _item_id(item)

        try:
            payload, status = process_queue_item(item, processor=processor)

            if payload.get("skipped"):
                # A skip used no slot and did no network work: no delay, and it
                # does not count toward ``max_to_start``.
                skipped += 1
                results.append({
                    "queue_id": queue_id,
                    "status_code": status,
                    "success": True,
                    "skipped": True,
                    "reason": payload.get("reason"),
                    "error": None,
                })
                continue

            processed += 1
            started += 1

            if status < 400 and payload.get("success", True):
                succeeded += 1
            else:
                failed += 1

            results.append({
                "queue_id": queue_id,
                "status_code": status,
                "success": payload.get("success", status < 400),
                "skipped": False,
                "error": payload.get("error"),
            })

            # A lost database ends the batch: every remaining item would fail
            # the same way, and marking them failed would also fail.
            if is_db_outage_error(payload.get("error")):
                logger.warning("Database unavailable — ending queue batch early", queue_id=queue_id)
                reached_end = False
                break

        except Exception as exc:
            logger.exception("Unhandled batch item failure", queue_id=queue_id, error=str(exc))
            failed += 1
            started += 1

            if queue_id and _still_claimed(queue_id):
                _mark_failed(item, str(exc))

            results.append({
                "queue_id": queue_id,
                "status_code": 500,
                "success": False,
                "error": str(exc),
            })
            if is_db_outage_error(exc):
                reached_end = False
                break

        if (
            inter_item_delay_seconds
            and inter_item_delay_seconds > 0
            and index < len(items) - 1
        ):
            time.sleep(float(inter_item_delay_seconds))

    elapsed_seconds = round(time.time() - started_at, 3)

    # More rows may be waiting when the fetch was full and every fetched row
    # was consumed (skipped or started) without filling the slots. The worker
    # uses this to run the next cycle straight away instead of sleeping.
    more_ready = bool(
        reached_end
        and fetch_limit is not None
        and len(items) >= fetch_limit
        and (max_to_start is None or started < max_to_start)
    )

    return _ok(
        total=len(items),
        processed=processed,
        succeeded=succeeded,
        skipped=skipped,
        failed=failed,
        started=started,
        more_ready=more_ready,
        elapsed_seconds=elapsed_seconds,
        results=results,
    )


# =============================================================================
# MAINTENANCE
# =============================================================================

def run_maintenance() -> tuple[dict[str, Any], int]:
    """Run optional queue/download maintenance hooks.

    Stops at the first DATABASE-outage failure: the remaining hooks would hit
    the same error (the log shows every hook failing in turn during each
    Postgres restart), which only adds noise and slows recovery.
    """
    results: list[dict[str, Any]] = []
    db_outage = False

    for hook in _iter_maintenance_hooks():
        hook_name = f"{hook.__module__}.{hook.__name__}"
        try:
            result = hook()
            payload, status = normalise_result(result)
            error = payload.get("error")
            results.append({
                "hook": hook_name,
                "status_code": status,
                "success": payload.get("success", status < 400) and not error,
                "error": error,
            })
            if is_db_outage_error(error):
                db_outage = True
                break
        except Exception as exc:
            if is_db_outage_error(exc):
                logger.warning("Maintenance hook hit a database outage", hook=hook_name)
                db_outage = True
            else:
                logger.exception("Maintenance hook failed", hook=hook_name, error=str(exc))
            results.append({
                "hook": hook_name,
                "status_code": 500,
                "success": False,
                "error": str(exc),
            })
            if db_outage:
                break

    failures = [row for row in results if not row.get("success")]

    return _ok(
        hooks=len(results),
        failures=len(failures),
        db_outage=db_outage,
        results=results,
    )


def process_cycle(
    *,
    batch_size: int = 50,
    run_maintenance_hooks: bool = True,
) -> tuple[dict[str, Any], int]:
    """Full worker cycle (optional maintenance + queue batch processing).

    ⚠️ The maintenance pass is INSIDE the cycle lock, so the standalone worker
    and the APScheduler thread never both run it (two
    ``check_completed_downloads`` passes could reconcile the same transfer).

    A database outage found by maintenance ends the cycle before the batch:
    the batch would only fail every item against the same missing database.
    """
    from services.queue.queue_lock import queue_cycle_lock

    started_at = time.time()
    with queue_cycle_lock() as acquired:
        if not acquired:
            logger.debug("Another queue cycle holds the lock — skipping cycle")
            return _ok(
                total=0,
                processed=0,
                succeeded=0,
                skipped=0,
                failed=0,
                throttled=True,
                more_ready=False,
                reason="Another queue cycle is running",
                elapsed_seconds=round(time.time() - started_at, 3),
            )

        maintenance_payload: dict[str, Any] | None = None
        if run_maintenance_hooks:
            maintenance_payload, _ = run_maintenance()
            if (maintenance_payload or {}).get("db_outage"):
                return _fail(
                    "Database unavailable",
                    503,
                    db_outage=True,
                    more_ready=False,
                    maintenance=maintenance_payload,
                )

        # The lock is already held by this function — taking it again inside
        # ``process_next_batch`` would wait the full attempt budget and then
        # report itself as throttled.
        batch_payload, batch_status = process_next_batch(
            batch_size, use_cycle_lock=False
        )

    batch_payload["maintenance"] = maintenance_payload

    return batch_payload, batch_status
