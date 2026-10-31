"""
APScheduler integration for Popularr.

Replaces ad-hoc ``threading.Thread`` for periodic background tasks with
a persistent, configurable scheduler that survives gunicorn worker reloads.

Managed jobs:
    - ``library_sync`` — Periodic library sync with Navidrome (default: every 6 hours)
    - ``popularity_scan`` — Periodic popularity recalculation (default: daily)
    - ``playlist_maintenance`` — GLOBAL playlist reconciliation (default: daily)
    - ``download_queue_processor`` — Process queued downloads (default: every 30s)

Architecture:
    The scheduler runs in a dedicated thread within each gunicorn worker.
    For true cross-worker persistence, the scheduler state can be persisted
    via SQLAlchemy job store (configured below).

Usage:
    from services.scheduler.scheduler_service import get_scheduler
    scheduler = get_scheduler()
    scheduler.start()
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.jobstores.base import ConflictingIdError
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from sqlalchemy.exc import IntegrityError

from db.engine import get_engine
from helpers.logging_config import log_queue

logger = structlog.get_logger(__name__)


class ResilientSQLAlchemyJobStore(SQLAlchemyJobStore):
    """SQLAlchemy job store that survives transient PostgreSQL failures.

    APScheduler's stock ``SQLAlchemyJobStore`` keeps a persistent session on a
    pooled connection.  When PostgreSQL restarts or drops idle connections,
    that session's connection goes stale and every subsequent ``update_job`` /
    ``get_due_jobs`` raises ``OperationalError`` with no recovery — the
    scheduler effectively stops.

    This subclass retries transient DB errors a few times and disposes the
    engine's pool so the next attempt checks out a fresh connection.
    """

    _RETRIES = 3
    _RETRY_DELAY = 1.0

    def _execute(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        from tenacity import (
            Retrying,
            retry_if_exception,
            stop_after_attempt,
            wait_exponential,
        )

        def _call() -> Any:
            return fn(*args, **kwargs)

        def _before_retry(retry_state: Any) -> None:
            logger.warning(
                "Transient DB error in jobstore",
                attempt=retry_state.attempt_number,
                error=str(retry_state.outcome.exception()) if retry_state.outcome else None,
            )
            try:
                self.engine.dispose()
            except Exception:
                pass

        return Retrying(
            stop=stop_after_attempt(self._RETRIES),
            wait=wait_exponential(
                multiplier=1.0, exp_base=2, min=self._RETRY_DELAY, max=5.0
            ),
            retry=retry_if_exception(self._is_transient),
            reraise=True,
            before_sleep=_before_retry,
        )(_call)

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        """Return True for connection-level DB errors worth retrying."""
        if exc is None:
            return False
        try:
            from sqlalchemy.exc import OperationalError as SAOperationalError
            from sqlalchemy.exc import InterfaceError as SAInterfaceError
            if isinstance(exc, (SAOperationalError, SAInterfaceError)):
                return True
        except Exception:
            pass
        
        orig = getattr(exc, "orig", None)
        if orig is not None:
            cls_name = type(orig).__name__.lower()
            if any(k in cls_name for k in ("operationalerror", "interfaceerror", "connectionerror")):
                return True
        return False

    def add_job(self, job: Any) -> Any:
        """Add the job, tolerating the multi-worker duplicate-key race."""
        try:
            return self._execute(super().add_job, job)
        except (ConflictingIdError, IntegrityError) as exc:
            logger.info(
                "Job already exists in jobstore (concurrent worker race); applying as update",
                job_id=job.id,
                error=str(exc),
            )
            return self._execute(super().update_job, job)

    def update_job(self, job: Any) -> Any:
        return self._execute(super().update_job, job)

    def remove_job(self, job_id: Any) -> Any:
        return self._execute(super().remove_job, job_id)

    def remove_all_jobs(self) -> Any:
        return self._execute(super().remove_all_jobs)

    def lookup_job(self, job_id: Any) -> Any:
        return self._execute(super().lookup_job, job_id)

    def get_due_jobs(self, now: Any) -> Any:
        return self._execute(super().get_due_jobs, now)

    def get_next_run_time(self) -> Any:
        return self._execute(super().get_next_run_time)

    def get_all_jobs(self) -> Any:
        return self._execute(super().get_all_jobs)

    def get_jobs(self, jobstore_alias: Any = None) -> Any:
        return self._execute(super().get_jobs, jobstore_alias)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_SCHEDULER: BackgroundScheduler | None = None


def peek_scheduler() -> BackgroundScheduler | None:
    """The existing scheduler, or ``None`` — WITHOUT creating one.

    ``get_scheduler()`` builds the instance on demand, which also builds its
    SQLAlchemy job store and therefore opens a database connection. That makes it
    the wrong call for a read-only status check: asking "is the scheduler paused"
    would create a scheduler (and a connection) as a side effect, and would report
    a freshly-built, never-started instance as the current state.

    Used by the safe-shutdown status, which must observe and never cause.
    """
    return _SCHEDULER


def get_scheduler() -> BackgroundScheduler:
    """Return the singleton scheduler instance, creating it if necessary."""
    global _SCHEDULER
    if _SCHEDULER is not None:
        return _SCHEDULER
    from helpers.config_helpers import get_config
    cfg = get_config() or {}
    scheduler_cfg = cfg.get("scheduler", {})

    jobstores: dict[str, Any] = {}
    try:
        engine = get_engine()
        jobstores["default"] = ResilientSQLAlchemyJobStore(engine=engine)
    except Exception as exc:
        logger.warning("SQLAlchemy job store unavailable, using in-memory", error=str(exc))

    _SCHEDULER = BackgroundScheduler(
        jobstores=jobstores or None,
        timezone=scheduler_cfg.get("timezone", "UTC"),
        job_defaults={
            "coalesce": True,       
            "max_instances": 1,     
            "misfire_grace_time": 300,  
        },
    )

    _register_default_jobs(_SCHEDULER, scheduler_cfg, root_cfg=cfg)
    return _SCHEDULER


# ---------------------------------------------------------------------------
# Single-execution guards
# ---------------------------------------------------------------------------

def _scheduler_env_enabled() -> bool:
    """Honour the ``ENABLE_SCHEDULER`` env gate (split-worker deployments)."""
    return str(os.environ.get("ENABLE_SCHEDULER", "1")).strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


_JOB_LOCK_KEY_MAP = {
    "library_sync": "popularr_scheduler_library_sync",
    "popularity_scan": "popularr_scheduler_popularity_scan",
    "playlist_maintenance": "popularr_scheduler_playlist_maintenance",
    "navidrome_import": "popularr_scheduler_navidrome_import",
    "download_queue_processor": "popularr_scheduler_download_queue",
    "queue_processor_watchdog": "popularr_scheduler_queue_watchdog",
    "missing_releases_scan": "popularr_scheduler_missing_releases",
    "upcoming_wikipedia_scrape": "popularr_scheduler_upcoming_wikipedia",
    "upcoming_musicbrainz_scan": "popularr_scheduler_upcoming_musicbrainz",
}


@contextmanager
def _job_tick_lock(job_id: str):
    """Cross-process mutual exclusion for ONE scheduled job tick."""
    from services.queue.queue_lock import queue_cycle_lock

    key = _JOB_LOCK_KEY_MAP.get(job_id, f"popularr_scheduler_{job_id}")
    with queue_cycle_lock(key=key, max_attempts=1, attempt_interval=0.1) as acquired:
        yield acquired


# ---------------------------------------------------------------------------
# Job definitions
# ---------------------------------------------------------------------------

def _same_trigger(existing: Any, trigger: Any) -> bool:
    """Return True when *existing* job already uses the same trigger config."""
    if existing is None or existing.trigger is None or trigger is None:
        return False
    try:
        a = existing.trigger
        b = trigger
        if type(a) is not type(b):
            return False
        if isinstance(a, IntervalTrigger):
            return getattr(a, "interval", None) == getattr(b, "interval", None)
        if isinstance(a, CronTrigger):
            return getattr(a, "fields", None) == getattr(b, "fields", None)
        return str(a) == str(b)
    except Exception:
        return False


# ✅ EXTRACTED TO MODULE LEVEL FOR PICKLING/SERIALIZATION
#: Single-flight guard for the in-process queue tick. APScheduler's
#: ``max_instances`` does NOT cover the cycle: the tick returns immediately
#: after spawning its thread, so a slow cycle (minutes) used to get one new
#: thread per interval. They all waited on the cross-process cycle lock and
#: returned — wasted work and extra heartbeat writes.
_QUEUE_CYCLE_THREAD: threading.Thread | None = None
_QUEUE_CYCLE_THREAD_LOCK = threading.Lock()


def _download_queue_processor_tick() -> None:
    """Module-level function so APScheduler can serialize it by reference."""
    global _QUEUE_CYCLE_THREAD
    try:
        import threading as _th
        from services.queue.queue_heartbeat import (
            queue_processor_alive,
            record_queue_processor_cycle,
        )
        from services.queue.queue_orchestrator import process_cycle

        def _run_cycle() -> None:
            try:
                # Keep the heartbeat fresh WHILE the cycle runs: a batch of
                # searches can take minutes, and a stamp written only
                # afterwards made the processor look stopped for the whole
                # batch (dashboard "Restart Processor" on a live queue).
                with queue_processor_alive("scheduler"):
                    process_cycle()
                # Stamped only AFTER a successful cycle — this tick and the
                # standalone queue_worker are the two drivers whose liveness
                # /api/queue-processor/status reports (queue_heartbeat.py).
                record_queue_processor_cycle("scheduler")
            except Exception as _exc:
                logger.warning("download_queue_processor cycle failed", error=str(_exc))
                # services/scheduler is NOT in the logging routing table, so
                # the line above only reaches unified. The queue's own log
                # needs it too — "the queue stopped" is a queue.log question.
                log_queue("[QUEUE] cycle failed", error=str(_exc))

        with _QUEUE_CYCLE_THREAD_LOCK:
            if _QUEUE_CYCLE_THREAD is not None and _QUEUE_CYCLE_THREAD.is_alive():
                logger.debug("Previous queue cycle still running — skipping tick")
                return
            _QUEUE_CYCLE_THREAD = _th.Thread(
                target=_run_cycle,
                name="queue-cycle",
                daemon=True,
            )
            _QUEUE_CYCLE_THREAD.start()
    except Exception as _exc:
        logger.warning("download_queue_processor spawn failed", error=str(_exc))
        log_queue("[QUEUE] cycle spawn failed", error=str(_exc))


# ---------------------------------------------------------------------------
# Queue processor status / restart support
# ---------------------------------------------------------------------------
def queue_processor_schedule_snapshot() -> dict[str, Any]:
    """Describe the in-process ``download_queue_processor`` job. Side-effect free.

    Reads ``_SCHEDULER`` directly instead of calling ``get_scheduler()``:
    building that singleton (and opening its SQLAlchemy jobstore) is a startup
    side effect that a status endpoint polled every 10 seconds must never
    trigger. ``_SCHEDULER`` is ``None`` when the scheduler was never created or
    was stopped by ``stop_scheduler()``.

    This is the SECOND piece of evidence behind ``/api/queue-processor/status``
    — the heartbeat written by a finished cycle is the first. A freshly booted
    app has no heartbeat yet but does have an armed job, and must not be
    reported as stopped.

    Returns a dict with:
        ``available``             — the singleton exists in this process
        ``running``               — the scheduler thread is actually started
        ``job_registered``        — the queue job is scheduled
        ``next_run_in_seconds``   — seconds until the next tick, or ``None``
        ``reason``                — human explanation, always present
    """
    scheduler = _SCHEDULER
    if scheduler is None:
        return {
            "available": False,
            "running": False,
            "job_registered": False,
            "next_run_in_seconds": None,
            "reason": "scheduler not initialised in this process",
        }

    try:
        running = bool(scheduler.running)
    except Exception:
        running = False

    job: Any = None
    job_error: str | None = None
    if running:
        try:
            job = scheduler.get_job("download_queue_processor")
        except Exception as exc:
            job_error = str(exc)

    next_run_in: float | None = None
    if job is not None:
        try:
            next_run = job.next_run_time
            if next_run is not None:
                next_run_in = max(
                    0.0,
                    (next_run - datetime.now(next_run.tzinfo)).total_seconds(),
                )
        except Exception:
            next_run_in = None

    if not running:
        reason = "scheduler is not running in this process"
    elif job_error:
        reason = f"queue processor job lookup failed: {job_error}"
    elif job is None:
        reason = "queue processor job is not registered (disabled?)"
    else:
        reason = "queue processor job is scheduled"

    return {
        "available": True,
        "running": running,
        "job_registered": job is not None,
        "next_run_in_seconds": next_run_in,
        "reason": reason,
    }


def kick_download_queue_processor() -> dict[str, Any]:
    """Ask the in-process queue job to fire immediately (the Restart button).

    "Restart the processor" has no process to restart here: the processor is
    either this scheduler job or the external ``queue_worker`` started by
    ``entrypoint.sh``, neither of which the WebUI owns. The honest equivalent
    is *make the next cycle happen now* — this re-arms the job, and the caller
    runs a cycle itself when there is no job to re-arm.

    Returns a dict describing what actually happened. ``attempted`` is False
    when this process has nothing to re-arm, so the caller knows to fall back.
    """
    scheduler = _SCHEDULER
    if scheduler is None:
        return {"attempted": False, "reason": "scheduler not initialised in this process"}

    try:
        if not scheduler.running:
            return {"attempted": False, "reason": "scheduler is not running in this process"}
    except Exception:
        return {"attempted": False, "reason": "scheduler state unavailable"}

    try:
        job = scheduler.get_job("download_queue_processor")
    except Exception as exc:
        return {"attempted": False, "reason": f"job lookup failed: {exc}"}
    if job is None:
        return {"attempted": False, "reason": "queue processor job is not registered"}

    try:
        # next_run_time is a first-class APScheduler field: setting it to "now"
        # fires the job on the scheduler's next wakeup instead of waiting out
        # the interval, and the IntervalTrigger resumes its normal cadence after.
        job.modify(next_run_time=datetime.now(scheduler.timezone))
    except Exception as exc:
        return {"attempted": False, "reason": f"job reschedule failed: {exc}"}

    return {"attempted": True, "reason": "queue processor job scheduled to run immediately"}


def _register_default_jobs(
    scheduler: BackgroundScheduler,
    cfg: dict[str, Any],
    *,
    root_cfg: dict[str, Any] | None = None,
) -> None:
    """Register built-in periodic jobs unless they exist or are disabled.

    ``cfg`` is the ``scheduler:`` sub-mapping (so ``jobs`` is
    ``scheduler.jobs``, as documented).  ``root_cfg`` is the FULL config, used
    only to fall back to the top-level ``watcher:`` section when ``scheduler:``
    does not carry its own copy.

    ⚠️ This function used to receive DIFFERENT dicts from its two callers:
    ``get_scheduler()`` passed ``config["scheduler"]`` while
    ``reschedule_jobs_from_config()`` passed the ROOT config.  Each caller
    therefore saw half the settings — boot honoured ``scheduler.jobs``
    intervals but ignored every ``watcher`` toggle, and a Config save honoured
    the toggles but read ``config["jobs"]``, which does not exist, silently
    resetting every custom interval to its default.  Both callers now pass the
    same shape.
    """
    jobs = cfg.get("jobs", {}) or {}
    root_cfg = root_cfg if isinstance(root_cfg, dict) else {}
    watcher = cfg.get("watcher") or root_cfg.get("watcher") or {}

    def _enabled(job_id: str, watcher_key: str | None, default: bool = True) -> bool:
        """Decide whether a job runs.

        ``watcher_key`` is the ``watcher:`` field to fall back to when the job's
        own ``enabled`` key is absent.  Pass ``None`` for a job that must NOT
        inherit a Watcher toggle — see the ``navidrome_import`` registration for
        why that distinction exists.
        """
        try:
            job_cfg = jobs.get(job_id, {}) or {}
            if "enabled" in job_cfg:
                return bool(job_cfg.get("enabled"))
            if watcher_key is None:
                return default
            return bool(watcher.get(watcher_key, default))
        except Exception:
            return default

    def _interval(job_id: str, field: str, default: float) -> float:
        """Read a job's interval, preserving an explicit 0.

        Was ``float(... or default)``, which maps ``0`` back onto the default —
        so "set it to 0 to disable" silently did nothing. 0 is now returned as
        0 and the caller treats it as *disabled*, matching how 0 already means
        "never" for the rescan windows and the missing-release tracklist limit.
        """
        try:
            raw = jobs.get(job_id, {}).get(field, default)
            value = float(raw)
        except (TypeError, ValueError):
            return float(default)
        return value if value > 0 else 0.0

    def _func_ref(callable_obj: Any) -> str | None:
        if callable_obj is None:
            return None
        module = getattr(callable_obj, "__module__", None)
        qualname = getattr(callable_obj, "__qualname__", None) or getattr(callable_obj, "__name__", None)
        if not module or not qualname:
            return None
        return f"{module}:{qualname}"

    def _existing_job(job_id: str) -> Any:
        try:
            existing = scheduler.get_job(job_id)
            if existing is not None:
                return existing
        except Exception:
            existing = None
        for alias, store in getattr(scheduler, "_jobstores", {}).items():
            try:
                existing = store.lookup_job(job_id)
            except Exception:
                continue
            if existing is not None:
                return existing
        return None

    def _remove_persisted_job(job_id: str) -> None:
        for alias, store in getattr(scheduler, "_jobstores", {}).items():
            try:
                store.remove_job(job_id)
            except Exception:
                continue

    def _put(job_id: str, name: str, trigger: Any, replace: bool = True, **kwargs: Any) -> None:
        try:
            existing = _existing_job(job_id)
            new_func = kwargs.get("func")
            _func_changed = (
                new_func is not None
                and getattr(existing, "func_ref", None) is not None
                and getattr(existing, "func_ref", None) != _func_ref(new_func)
            )
            if existing is not None and (not _same_trigger(existing, trigger) or _func_changed):
                try:
                    scheduler.remove_job(job_id)
                except Exception:
                    pass
                _remove_persisted_job(job_id)
                existing = None
                
            if existing is None:
                scheduler.add_job(
                    kwargs.pop("func"),
                    trigger=trigger,
                    id=job_id,
                    name=name,
                    replace_existing=replace,
                    **kwargs,
                )
                logger.info("APScheduler job registered", job_id=job_id)
        except Exception as exc:
            logger.warning("APScheduler failed to register job", job_id=job_id, error=str(exc))

    def _remove_job(job_id: str) -> None:
        try:
            if _existing_job(job_id) is not None:
                try:
                    scheduler.remove_job(job_id)
                except Exception:
                    pass
                _remove_persisted_job(job_id)
                logger.info("APScheduler job removed (disabled in config)", job_id=job_id)
        except Exception as exc:
            logger.warning("APScheduler failed to remove job", job_id=job_id, error=str(exc))

    # ── Library sync ──
    library_interval = _interval("library_sync", "interval_minutes", 360)
    if library_interval <= 0 or not _enabled("library_sync", "auto_import_enabled", True):
        _remove_job("library_sync")
    else:
        try:
            _put(
                "library_sync", "Library sync with Navidrome",
                IntervalTrigger(minutes=library_interval),
                func=_run_library_sync_job,
            )
        except Exception as exc:
            logger.warning("APScheduler failed to register library_sync", error=str(exc))

    # ── Popularity scan ──
    popularity_interval = _interval("popularity_scan", "interval_minutes", 1440)
    if popularity_interval <= 0 or not _enabled("popularity_scan", "auto_popularity_scan", True):
        _remove_job("popularity_scan")
    else:
        try:
            _put(
                "popularity_scan", "Popularity recalculation",
                IntervalTrigger(minutes=popularity_interval),
                func=_run_popularity_scan_job,
            )
        except Exception as exc:
            logger.warning("APScheduler failed to register popularity_scan", error=str(exc))

    # ── Playlist maintenance (GLOBAL reconciliation) ───────────────────────
    #
    # The authoritative library-wide playlist pass. It rebuilds EVERY managed
    # playlist (genre, decade, New Music, curated, favourites), sweeps genuine
    # orphaned GENRE playlists and reconciles the playlist index. Incremental
    # artist/album finalisation does not run it: it refreshes only the genre
    # playlists that artist's changes touch, so the daily pass is what
    # guarantees eventual consistency.
    #
    # `interval_minutes: 0` disables it; `enabled: false` too.
    playlist_maintenance_interval = _interval("playlist_maintenance", "interval_minutes", 1440)
    if playlist_maintenance_interval <= 0 or not _enabled("playlist_maintenance", None, True):
        _remove_job("playlist_maintenance")
    else:
        try:
            _put(
                "playlist_maintenance", "Global playlist reconciliation",
                IntervalTrigger(minutes=playlist_maintenance_interval),
                func=_run_playlist_maintenance_job,
                max_instances=1,
                coalesce=True,
            )
        except Exception as exc:
            logger.warning("APScheduler failed to register playlist_maintenance", error=str(exc))

    # ── Navidrome import (keep the database current) ──
    #
    # The scheduled Import (the same pipeline the dashboard's Import button
    # runs) so `tracks` follows the library without anyone pressing it. Default
    # every 4 hours: often enough that a newly added album appears the same day,
    # rarely enough that the remote Navidrome rescan it triggers is not a
    # constant background hum.
    #
    # ⚠️ `interval_minutes: 0` disables it, and `enabled: false` too. It is
    # independent of `library_sync` (the 6-hourly delta sync) on purpose: they
    # answer different questions, and this one can be turned off without losing
    # the incremental sync.
    #
    # ⚠️ AND ITS ENABLE FLAG IS ITS OWN — NOT `watcher.auto_import_enabled`.
    #
    # This job used to read `_enabled("navidrome_import", "auto_import_enabled",
    # True)`, so it fell back to the Watcher card's **Auto Import** toggle.
    # Unticking "Auto Import" therefore DELETED the four-hourly import job: the
    # exact opposite of what that setting advertises, and it silently killed the
    # one schedule whose entire purpose is "keep the database current on its
    # own". The scheduler then reported itself healthy — the job was simply
    # absent from `scheduler.get_jobs()`, which nothing surfaced.
    #
    # Only an explicit `scheduler.jobs.navidrome_import.enabled: false` (or
    # `interval_minutes: 0`) turns it off now. `library_sync` / `popularity_scan`
    # keep the Watcher fallback — those are the scans that toggle actually names.
    navidrome_import_interval = _interval("navidrome_import", "interval_minutes", 240)
    if navidrome_import_interval <= 0 or not _enabled("navidrome_import", None, True):
        _remove_job("navidrome_import")
    else:
        try:
            _put(
                "navidrome_import", "Navidrome import (keep database current)",
                IntervalTrigger(minutes=navidrome_import_interval),
                func=_run_navidrome_import_job,
                max_instances=1,
                coalesce=True,
            )
        except Exception as exc:
            logger.warning("APScheduler failed to register navidrome_import", error=str(exc))

    # ── Download queue processor ──
    if _enabled("download_queue_processor", "downloads_watcher_enabled", True):
        interval_seconds = _interval(
            "download_queue_processor",
            "interval_seconds",
            float(watcher.get("scan_interval", 60) or 60),
        )
        try:
            # ✅ Reference the module-level function
            _put(
                "download_queue_processor", "Process download queue",
                IntervalTrigger(seconds=interval_seconds),
                func=_download_queue_processor_tick,
                max_instances=1,
                coalesce=True,
                # Inherit job_defaults' 300s rather than overriding it down to
                # 30 — which is what this job used to do, and 30 is BELOW the
                # default. The reported "Run time of job \"Process download
                # queue\" was missed by 0:00:38" is exactly that: a tick that
                # ran 38s long passed the 30s grace by 8s, so APScheduler
                # DISCARDED the next run and downloads idled a whole extra
                # interval for no reason. A late tick is safe — `coalesce`
                # collapses any backlog into a single run and `max_instances`
                # forbids overlap, so lateness can never make ticks pile up.
            )
        except Exception as exc:
            logger.warning("APScheduler failed to register download_queue_processor", error=str(exc))
    else:
        _remove_job("download_queue_processor")

    # ── Download queue processor WATCHDOG (auto-restart) ──
    #
    # A separate job rather than a hook inside the processor tick, because the
    # tick only runs when the processor is healthy: the case this exists for is
    # the one where nothing is driving the queue at all. See
    # ``services/queue/queue_watchdog_service.py``.
    #
    # Gated on the queue processor being enabled as well: a deployment that has
    # deliberately turned the download queue off must not have a job quietly
    # kicking cycles into it every minute.
    try:
        from services.queue.queue_watchdog_service import (
            WATCHDOG_JOB_ID,
            get_auto_restart_config,
        )

        watchdog_cfg = get_auto_restart_config()
        queue_enabled = _enabled(
            "download_queue_processor", "downloads_watcher_enabled", True
        )

        if watchdog_cfg.get("enabled") and queue_enabled:
            _put(
                WATCHDOG_JOB_ID,
                "Auto-restart a stopped download queue processor",
                IntervalTrigger(
                    seconds=float(watchdog_cfg.get("check_interval_seconds") or 60.0)
                ),
                func=_run_queue_processor_watchdog_job,
                max_instances=1,
                coalesce=True,
            )
        else:
            _remove_job(WATCHDOG_JOB_ID)
    except Exception as exc:
        logger.warning("APScheduler failed to register queue_processor_watchdog", error=str(exc))

    # ── Missing releases scan ──
    try:
        from helpers.config_helpers import get_config as _get_config
        _feats = (_get_config() or {}).get("features", {}) or {}
        enabled = bool(_feats.get("daily_musicbrainz_release_scan_enabled", True))
        if enabled:
            interval_minutes = _interval("missing_releases_scan", "interval_minutes", 1440)
            _put(
                "missing_releases_scan", "MusicBrainz missing releases refresh",
                IntervalTrigger(minutes=interval_minutes),
                func=_run_missing_releases_job,
            )
        else:
            _remove_job("missing_releases_scan")
    except Exception as exc:
        logger.warning("APScheduler failed to register missing_releases_scan", error=str(exc))

    # ── Upcoming releases ──
    try:
        from helpers.config_helpers import get_feature
        scan_enabled = get_feature("upcoming_releases_scan_enabled", None)
        if scan_enabled is None:
            scan_enabled = get_feature("daily_musicbrainz_release_scan_enabled", True)
        scan_enabled = bool(scan_enabled)
    except Exception:
        scan_enabled = True

    if scan_enabled:
        try:
            _put(
                "upcoming_wikipedia_scrape", "Upcoming releases: Wikipedia scrape + stale purge",
                CronTrigger(hour=3, minute=0),
                func=_run_wikipedia_scrape_task,
            )
        except Exception as exc:
            logger.warning("APScheduler failed to register upcoming_wikipedia_scrape", error=str(exc))

        try:
            _put(
                "upcoming_musicbrainz_scan", "Upcoming releases: MusicBrainz collection refresh",
                CronTrigger(day_of_week="sun", hour=4, minute=0),
                func=_run_upcoming_musicbrainz_job,
            )
        except Exception as exc:
            logger.warning("APScheduler failed to register upcoming_musicbrainz_scan", error=str(exc))
    else:
        _remove_job("upcoming_wikipedia_scrape")
        _remove_job("upcoming_musicbrainz_scan")


def reschedule_jobs_from_config() -> dict[str, Any]:
    """Re-read config and re-apply job gates/intervals to the running scheduler."""
    stats: dict[str, Any] = {"jobs": 0, "error": None}
    try:
        from helpers.config_helpers import get_config
        cfg = get_config() or {}
        if not _scheduler_env_enabled():
            logger.info("APScheduler config re-apply skipped — ENABLE_SCHEDULER is false")
            return stats
            
        scheduler = get_scheduler()
        if not scheduler.running:
            scheduler.start()
            logger.info("APScheduler started (reschedule_jobs_from_config)")

        # Same shape as ``get_scheduler()``: jobs live under ``scheduler.jobs``.
        # Passing the ROOT config here read ``config["jobs"]`` — a key that
        # never exists — so every save reset scheduler intervals to defaults.
        _register_default_jobs(scheduler, cfg.get("scheduler") or {}, root_cfg=cfg)
        stats["jobs"] = len(list(scheduler.get_jobs() or []))
        logger.info("APScheduler config re-applied", registered_jobs=stats["jobs"])
        return stats
    except Exception as exc:
        logger.error("APScheduler reschedule_jobs_from_config failed", error=str(exc), exc_info=True)
        return {**stats, "error": str(exc)}


def _run_wikipedia_scrape_task() -> None:
    """Scrape Wikipedia upcoming-releases sources, then purge stale rows."""
    with _job_tick_lock("upcoming_wikipedia_scrape") as _acquired:
        if not _acquired:
            logger.debug("Skipping upcoming_wikipedia_scrape tick — another worker holds the lock")
            return
        from services.upcoming_releases.wikipedia_scraper_service import (
            scrape as scrape_wikipedia,
            purge_stale_upcoming_releases,
        )
        try:
            scrape_wikipedia()
        finally:
            purge_stale_upcoming_releases()


def _run_library_sync_job() -> None:
    """Scheduled library-sync tick, guarded so only ONE worker executes it."""
    with _job_tick_lock("library_sync") as _acquired:
        if not _acquired:
            logger.debug("Skipping library_sync tick — another worker holds the lock")
            return
        from services.library.library_sync_service import request_library_sync
        request_library_sync()


def _run_navidrome_import_job() -> None:
    """Scheduled Navidrome import tick — keeps ``tracks`` current.

    Runs the SAME pipeline as the dashboard's Import button, so the scheduled and
    manual paths cannot drift. Three guards, each answering a different question:

    1. ``_job_tick_lock`` — is another WORKER already ticking? (4 hypercorn
       workers all run this scheduler job; the leader gate covers startup, this
       covers the steady state.)
    2. ``is_import_running()`` — is an import already running ANYWHERE? The
       progress file is cross-process and cross-host (the standalone queue
       worker shares the state volume), so this catches a manual import, a boot
       import, or this job's own previous tick still winding down.
    3. ``is_runtime_running("navidrome")`` — the cheap in-process check, which
       also covers the window before the progress file is first written.

    ⚠️ IT DOES NOT WAIT FOR — OR BLOCK — OTHER SCANS. A popularity/album scan
    running at the same time is fine: the import reads Navidrome and writes
    ``tracks``; the other scans read the library and write scores. What the
    import DOES do is suppress the other scans' incidental Navidrome polling
    while it runs (``services/navidrome/import_gate``), so the two are not
    competing for the server.
    """
    with _job_tick_lock("navidrome_import") as _acquired:
        if not _acquired:
            logger.debug("Skipping navidrome_import tick — another worker holds the lock")
            return

        try:
            from services.navidrome.import_gate import is_import_running
            from services.scanning.runtime_state import is_runtime_running

            if is_runtime_running("navidrome") or is_import_running():
                logger.info(
                    "Skipping scheduled Navidrome import — an import is already running"
                )
                return
        except Exception as exc:
            logger.debug("Navidrome import pre-check failed", error=str(exc))

        try:
            from services.scanning.pipelines.navidrome_pipeline import (
                run_navidrome_import_scan,
            )
            from services.scanning.scan_state import get_scan_progress_path

            # mode="all" is the INCREMENTAL import (``diff_mode=not force``), so
            # a tick with nothing changed costs the marker check and returns —
            # it is not a forced full rescan every four hours.
            run_navidrome_import_scan(
                mode="all",
                progress_file=get_scan_progress_path("navidrome_scan"),
            )
        except Exception as exc:
            logger.warning("Scheduled Navidrome import failed", error=str(exc))


def _run_queue_processor_watchdog_job() -> None:
    """Scheduled auto-restart tick for a STOPPED download queue processor.

    Guarded like every other job here so two web workers can never both decide
    the queue is down and both kick it. ``ENABLE_SCHEDULER`` already keeps this
    to the leader in a multi-worker deployment; the lock is the belt to that
    pair of braces, and it is the same mechanism the other ticks use.
    """
    with _job_tick_lock("queue_processor_watchdog") as _acquired:
        if not _acquired:
            logger.debug(
                "Skipping queue_processor_watchdog tick — another worker holds the lock"
            )
            return
        from services.queue.queue_watchdog_service import (
            queue_processor_watchdog_tick,
        )

        queue_processor_watchdog_tick()


def _run_missing_releases_job() -> None:
    """Scheduled MusicBrainz missing-releases refresh, guarded across workers."""
    with _job_tick_lock("missing_releases_scan") as _acquired:
        if not _acquired:
            logger.debug("Skipping missing_releases_scan tick — another worker holds the lock")
            return
        from services.metadata.artist_scan_service import start_missing_release_scan
        start_missing_release_scan()


def _run_upcoming_musicbrainz_job() -> None:
    """Scheduled MusicBrainz upcoming-releases refresh, guarded across workers."""
    with _job_tick_lock("upcoming_musicbrainz_scan") as _acquired:
        if not _acquired:
            logger.debug("Skipping upcoming_musicbrainz_scan tick — another worker holds the lock")
            return
        from services.upcoming_releases.musicbrainz_fetcher_service import fetch_musicbrainz_upcoming_releases
        fetch_musicbrainz_upcoming_releases()


def _run_playlist_maintenance_job() -> None:
    """Daily GLOBAL playlist reconciliation, guarded across workers."""
    with _job_tick_lock("playlist_maintenance") as _acquired:
        if not _acquired:
            logger.debug(
                "Skipping playlist_maintenance tick — another worker holds the lock"
            )
            return
        try:
            from services.popularity.stages.finalise_stage import (
                run_global_playlist_rebuild,
            )

            run_global_playlist_rebuild(reason="scheduled daily maintenance")
        except Exception as exc:
            logger.warning("Scheduled playlist maintenance failed", error=str(exc))


def _run_popularity_scan_job() -> None:
    """Scheduled popularity scan, guarded against duplicate worker ticks."""
    with _job_tick_lock("popularity_scan") as _acquired:
        if not _acquired:
            logger.debug("Skipping popularity_scan tick — another worker holds the lock")
            return
        _run_scheduled_popularity_scan()


def _run_scheduled_popularity_scan() -> None:
    """Run the scheduled popularity recalculation, guarded against overlap."""
    from services.scanning.pipelines.popularity_pipeline import (
        is_popularity_scan_active,
        run_popularity_mode,
    )

    if is_popularity_scan_active():
        logger.info("Skipping scheduled popularity scan — a popularity scan is already active")
        return

    from services.scanning.runtime_state import (
        clear_runtime,
        is_runtime_running,
        scan_lock,
        set_runtime,
    )

    with scan_lock:
        if is_runtime_running("popularity"):
            logger.info("Skipping scheduled popularity scan — a popularity scan is already running")
            return
        set_runtime("popularity", {"thread": threading.current_thread(), "type": "scheduled-popularity"})

    try:
        run_popularity_mode(mode="popularity")
    finally:
        clear_runtime("popularity")


# ---------------------------------------------------------------------------
# Convenience: start/stop from Flask
# ---------------------------------------------------------------------------

def start_scheduler(app: Any = None) -> BackgroundScheduler | None:
    """Start the APScheduler background scheduler."""
    try:
        if not _scheduler_env_enabled():
            logger.info("APScheduler skipped — ENABLE_SCHEDULER is false (split deployment)")
            return None
        scheduler = get_scheduler()
        if not scheduler.running:
            scheduler.start()
            logger.info("APScheduler started")
        return scheduler
    except Exception as exc:
        logger.error("APScheduler failed to start", error=str(exc))
        return None


def stop_scheduler() -> None:
    """Gracefully shut down the scheduler."""
    global _SCHEDULER
    if _SCHEDULER and _SCHEDULER.running:
        _SCHEDULER.shutdown(wait=False)
        _SCHEDULER = None
        logger.info("APScheduler shut down")
