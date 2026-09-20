"""Background service initialisation bridge.

Provides ``initialize_app_services()`` for ``app.py`` to start background
worker services. Currently starts the APScheduler for periodic tasks.

To add background services, import and start them here so ``app.py`` stays
clean of business logic.
"""

import os
import logging

logger = logging.getLogger(__name__)


def initialize_app_services(app=None, is_leader=None):
    """Start background services after the Flask app is created.

    Called once during app factory setup (``app.py``). Starts APScheduler
    for periodic tasks (library sync, popularity scan, queue processor) and
    mirrors the Download Retry Scheduler's ``auto_start`` config into its
    runtime state so the config page shows Running after boot.

    Guarded by leader status to prevent duplicate scheduling across multi-worker deployments.

    ⚠️ THE GUARD IS A REAL ELECTION, NOT AN ENV FLAG.  ``ENABLE_BACKGROUND_WORKERS``
    defaults to "true" and is the SAME value in every worker of a container, so a
    multi-worker deployment (``entrypoint.sh`` runs hypercorn with 4 workers)
    used to start FOUR schedulers — four copies of every periodic job, each
    making its own external API calls with its own in-process rate limiter.
    See ``helpers/leader_lock.py``.
    """
    if is_leader is None:
        try:
            from helpers.leader_lock import acquire_background_worker_leadership

            is_leader = acquire_background_worker_leadership()
        except Exception as exc:  # never let the election break startup
            logger.warning("[TASK_MANAGER] Leader election failed, assuming leader: %s", exc)
            is_leader = True

    if not is_leader:
        logger.debug("[TASK_MANAGER] Skipping background services initialization on non-leader worker.")
        return

    # Boot hygiene: a scan killed by a crash/reboot leaves its DB
    # ``scan_states`` row flagged running (the completion path never ran).
    # The schema bootstrap already resets these before workers spawn, but
    # direct ``python app.py`` dev runs skip bootstrap — reset here too so
    # the scheduler/dashboard never see a phantom active scan.
    try:
        from services.scanning.scan_state import reset_stale_scan_states
        stale = reset_stale_scan_states()
        if stale:
            logger.warning(
                "[TASK_MANAGER] Reset %d stale scan-state row(s) left 'running' by a previous crash/reboot",
                stale,
            )
    except Exception as exc:
        logger.debug("[TASK_MANAGER] Stale scan-state reset skipped: %s", exc)

    try:
        from services.scheduler.scheduler_service import start_scheduler
        start_scheduler(app=app)
    except Exception as exc:
        logger.warning("Failed to start scheduler: %s", exc)

    # Retry scheduler: keep the UI state in sync with the config.  The actual
    # retry work runs inside the queue worker's maintenance hooks (gated by
    # the same config), so this only mirrors the flag the config page polls.
    try:
        from services.downloads.download_scheduler_service import start_scheduler as _start_retry_scheduler
        from services.downloads.download_retry_service import _retry_scheduler_enabled
        if _retry_scheduler_enabled():
            _start_retry_scheduler()
            logger.info("[RETRY_SCHEDULER] Auto-start on boot: ENABLED")
    except Exception as exc:
        logger.warning("Failed to sync retry scheduler state: %s", exc)

    logger.debug("[TASK_MANAGER] Background services initialised (app=%s)", bool(app))
