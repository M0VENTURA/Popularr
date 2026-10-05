"""Centralized logging configuration for Popularr.
Config-driven, thread-safe logging configuration.
"""

from __future__ import annotations

import os
import shutil
import time
import logging
import logging.config
import logging.handlers
from typing import Any

import structlog

from helpers.config_helpers import get_config

# Force all standard library logging formatters to use local time instead of UTC
logging.Formatter.converter = time.localtime

_VALID_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

# Direct handle to the unified_scan.log handler (set in _setup_standard_logging)
# so a runtime set_log_level() toggle can raise/lower its level.
_UNIFIED_FILE_HANDLER = None

# Size-based log retention. Rotates when a file reaches 5MB and keeps up to 
# 3 backup files (.1, .2, .3), meaning max 20MB per log type.
_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_LOG_BACKUP_COUNT = 3


def _resolve_log_level() -> str:
    """Resolve the configured root log level (default ``INFO``)."""
    try:
        cfg = get_config()
        level = (cfg.get("logging", {}) or {}).get("level") or cfg.get("log_level")
    except Exception:
        level = None
        
    if not level:
        level = os.environ.get("LOG_LEVEL") or os.environ.get("SPTNR_LOG_LEVEL") or "INFO"
        
    level = str(level).strip().upper()
    if level not in _VALID_LEVELS:
        level = "INFO"
    return level


def set_log_level(level: str) -> str:
    """Update the root logger level at runtime (no restart required)."""
    level = str(level or "").strip().upper()
    if level not in _VALID_LEVELS:
        level = "INFO"
    logging.getLogger().setLevel(level)

    global _UNIFIED_FILE_HANDLER
    if _UNIFIED_FILE_HANDLER is not None:
        _UNIFIED_FILE_HANDLER.setLevel(level)

    return level


def resolve_log_dir() -> str:
    """Resolve the directory path for log files safely."""
    try:
        cfg = get_config()
        log_dir = cfg.get("paths", {}).get("log_dir") or cfg.get("log_dir")
        if log_dir:
            return log_dir
    except Exception:
        pass

    log_path = os.environ.get("LOG_PATH", "/config")
    
    if not log_path.endswith("/") and "." in os.path.basename(log_path):
        log_path = os.path.dirname(log_path)

    try:
        os.makedirs(log_path, exist_ok=True)
        return log_path
    except (PermissionError, OSError):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        fallback = os.path.join(script_dir, "logs")
        os.makedirs(fallback, exist_ok=True)
        return fallback


class UnifiedLogFilter(logging.Filter):
    """Filters out noisy API requests and sub-INFO noise from unified logs."""

    def _debug_enabled(self) -> bool:
        try:
            return _resolve_log_level() == "DEBUG"
        except Exception:
            return False

    def filter(self, record: logging.LogRecord) -> bool:
        if self._debug_enabled():
            if record.name.startswith("uvicorn.access") or record.name.startswith("werkzeug"):
                return False
            if record.name.startswith("apscheduler"):
                return False
            return True

        if record.levelno < logging.INFO:
            return False

        if record.name.startswith("uvicorn.access") or record.name.startswith("werkzeug"):
            return False

        if record.name.startswith("apscheduler"):
            return False

        return True


def log_unified(message: str, **kwargs: Any) -> None:
    """Write a progress message to the unified scan log."""
    if kwargs:
        rendered = message + " " + " ".join(f"{k}={v!r}" for k, v in kwargs.items())
    else:
        rendered = message
    logging.getLogger("popularr.unified").info(rendered)


def debug_enabled() -> bool:
    """True when the configured log level is DEBUG.

    The single gate for "detail" output. ``logging.level: debug`` in config.yaml
    (or ``LOG_LEVEL=DEBUG``) turns it on; anything else keeps the scan log to
    the readable section report only.
    """
    try:
        return _resolve_log_level() == "DEBUG"
    except Exception:
        return False


def log_scan_detail(message: str, **kwargs: Any) -> None:
    """Write a scan DETAIL line — emitted only when debug logging is enabled.

    Used for the high-volume per-call instrumentation (MusicBrainz request
    tracing, enrichment section timings, per-track stage transitions). At the
    default ``info`` level these are dropped entirely so the unified scan log
    keeps the section report the operator actually reads; at ``debug`` they are
    emitted inline so a stalled or mis-scoring scan can still be traced.
    """
    if not debug_enabled():
        return
    log_unified(message, **kwargs)


def log_queue(message: str, **kwargs: Any) -> None:
    """Write a download-queue event to ``queue.log``."""
    if kwargs:
        message = message + " " + " ".join(f"{k}={v!r}" for k, v in kwargs.items())
    logging.getLogger("popularr.queue").info(message)


def log_search(message: str, **kwargs: Any) -> None:
    """Write a Soulseek search event to ``search.log``."""
    if kwargs:
        message = message + " " + " ".join(f"{k}={v!r}" for k, v in kwargs.items())
    logging.getLogger("popularr.search").info(message)


class SafePrefixFormatter(logging.Formatter):
    """Appends a service prefix safely without mutating the shared LogRecord."""
    
    def __init__(self, prefix: str, fmt: str | None = None, datefmt: str | None = None):
        super().__init__(fmt, datefmt)
        self.prefix = prefix

    def format(self, record: logging.LogRecord) -> str:
        original_msg = record.msg
        if isinstance(record.msg, str) and not record.msg.startswith(self.prefix):
            record.msg = f"{self.prefix}{record.msg}"
            
        result = super().format(record)
        record.msg = original_msg  
        return result


# ---------------------------------------------------------------------------
# Multi-process safe rotation
# ---------------------------------------------------------------------------

#: How long a rotation lock may sit before we assume its holder died mid-rotate.
#: Long enough that a normal copy of a 5 MB file never looks abandoned, short
#: enough that a crash cannot leave the log unbounded forever.
_ROTATE_LOCK_STALE_SECONDS = 15.0


def _rotate_lock_path(base_filename: str) -> str:
    """Path of the inter-process lock guarding a rotation.

    Deliberately **dot-prefixed and not an extension of the log name**: the log
    readers glob ``<base> + ".*"`` to find rotated backups
    (``services.log_service._read_last_lines_with_rotation``), so a lock named
    ``unified_scan.log.rotate.lock`` would be picked up and tailed as if it were
    a backup. ``.<name>.rotate.lock`` never matches that glob, and it does not
    end in ``.log`` so the /logs page cannot request it either.
    """
    directory, name = os.path.split(base_filename)
    return os.path.join(directory, f".{name}.rotate.lock")


class SharedRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A rotating handler that tolerates SEVERAL processes writing one file.

    Popularr is not a single writer. ``app.py`` calls ``setup_logging("WebUI")``
    and Hypercorn runs it in every worker (``--workers 4`` in ``entrypoint.sh``),
    while ``services.queue.queue_worker`` calls
    ``setup_logging("QueueWorker")`` in its own process. All of them attach the
    root logger to the same four files — ``unified_scan.log``, ``info.log``,
    ``debug.log`` and ``error.log``.

    The stock :class:`logging.handlers.RotatingFileHandler` is written for one
    process and misbehaves badly with several:

    * ``doRollover()`` **renames** the live file to ``<base>.1``. Every other
      process still holds an open descriptor on that now-renamed inode, so from
      that moment its lines are written into ``<base>.1`` and are invisible to
      anything that opens the live file by name — the Scanner tab of the System
      Logs modal simply stops showing that process's output. When the orphaned
      process eventually rotates in turn it renames the *new* live file over
      ``<base>.1``, destroying the other process's history. This is exactly the
      reported symptom: the log "shows sometimes, but breaks when multiple scans
      run at the same time".
    * ``shouldRollover()`` trusts ``stream.tell()``, which is this process's own
      byte count. ``entrypoint.sh`` copytruncates some logs, after which that
      counter is stale and high while the file on disk is empty — so the handler
      renames a near-empty file over a perfectly good backup.

    Two changes fix both without a new dependency:

    * rotation is decided from **the size on disk**, the only value every
      process agrees on;
    * rotation **truncates in place** (copytruncate, the same mechanism
      ``entrypoint.sh`` already uses) instead of renaming, so no descriptor is
      ever orphaned. That is safe because every writer here opens the file in
      append mode (``FileHandler`` uses ``"a"``, i.e. ``O_APPEND``), which forces
      each write to the current end of file no matter where the descriptor's
      offset happens to be.

    A best-effort ``O_CREAT|O_EXCL`` lock keeps two processes from copytruncating
    over each other; if the lock is held we simply skip this rotation and let the
    next record retry against the size on disk.
    """

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        """Rotate on the file's on-disk size, not this process's byte counter.

        Named after the stdlib hook ``RotatingFileHandler.emit`` actually calls;
        overriding anything else leaves the stock ``stream.tell()`` logic in
        charge.
        """
        if self.maxBytes <= 0:
            return False
        try:
            return os.path.getsize(self.baseFilename) >= self.maxBytes
        except OSError:
            return False

    def doRollover(self) -> None:
        """Copy the live file to ``.1`` and truncate it — never rename it away."""
        lock_path = _rotate_lock_path(self.baseFilename)
        lock_fd: int | None = None
        try:
            lock_fd = self._acquire_rotation_lock(lock_path)
            if lock_fd is None:
                # Another writer is rotating right now (or just did). The size
                # check that got us here runs again on the next record, so
                # skipping is safe and keeps the other process's truncation.
                return
            self._copy_and_truncate()
        finally:
            if lock_fd is not None:
                try:
                    os.close(lock_fd)
                except OSError:
                    pass
                try:
                    os.remove(lock_path)
                except OSError:
                    pass
            # Reopen even when we skipped: if an external rotator truncated
            # the file, our own stream offset must be re-derived from scratch.
            try:
                self.stream = self._open()
            except OSError:
                pass

    @staticmethod
    def _acquire_rotation_lock(lock_path: str) -> int | None:
        """Take the rotation lock, or ``None`` if another holder has it.

        A lock left behind by a process that died mid-rotation is stolen once it
        is older than ``_ROTATE_LOCK_STALE_SECONDS`` so a crash can never leave
        the log unbounded.
        """
        for _ in range(2):
            try:
                return os.open(
                    lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(lock_path)
                except OSError:
                    return None  # vanished between open and stat; next record retries
                if age <= _ROTATE_LOCK_STALE_SECONDS:
                    return None  # genuinely in use
                try:
                    os.remove(lock_path)
                except OSError:
                    return None
            except OSError:
                return None  # unwritable dir — fall back to never rotating
        return None

    def _copy_and_truncate(self) -> None:
        base = self.baseFilename
        if self.backupCount > 0:
            for i in range(self.backupCount - 1, 0, -1):
                src = f"{base}.{i}"
                dst = f"{base}.{i + 1}"
                if os.path.exists(src):
                    try:
                        os.replace(src, dst)
                    except OSError:
                        pass
            if os.path.exists(base):
                try:
                    shutil.copyfile(base, f"{base}.1")
                except OSError:
                    pass
        # Truncate in place. Unlike a rename this leaves every other process's
        # descriptor pointing at a live file, and O_APPEND sends their next
        # write to the new end instead of past it (no NUL-padded sparse file).
        # As with any copytruncate there is a narrow window between the copy and
        # the truncate where a concurrent write is lost — bounded to a handful
        # of lines, and the same trade-off entrypoint.sh already makes. Losing a
        # few lines beats losing every line that process writes from here on.
        try:
            fd = os.open(base, os.O_RDWR | os.O_CREAT)
        except OSError:
            return
        try:
            os.ftruncate(fd, 0)
        except OSError:
            pass
        finally:
            os.close(fd)


def setup_logging(service_name: str = "popularr") -> None:
    """Configures centralized logging system."""
    log_dir = resolve_log_dir()
    use_structlog = os.environ.get("STRUCTLOG", "").strip() in ("1", "true", "yes")

    _configure_structlog_bridge()
    _setup_standard_logging(service_name, log_dir, use_structlog=use_structlog)


def _plain_renderer(_logger: Any, _method: str, event_dict: dict[str, Any]) -> str:
    """Render a structlog event dict as a plain log line."""
    ts = event_dict.get("timestamp", "")
    level = str(event_dict.get("level", "info")).upper()
    name = event_dict.get("logger", "")
    event = event_dict.pop("event", "")
    parts = [f"{ts} [{level}]" if ts else f"[{level}]"]
    if name:
        parts.append(f"[{name}]")
    if event:
        parts.append(str(event))
    for key, value in event_dict.items():
        if key in ("logger", "level", "timestamp"):
            continue
        parts.append(f"{key}={value!r}")
    return " ".join(parts)


def _configure_structlog_bridge() -> None:
    """Wire structlog so its loggers flow into stdlib handlers."""
    shared_processors = [
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _build_logging_config(service_name: str, log_dir: str, use_structlog: bool = False) -> dict[str, Any]:
    """Build the dictConfig payload for the app's file logging.

    Kept separate from :func:`_setup_standard_logging` so a test can assert which
    handler class every file handler uses — the multi-process rotation contract
    is invisible otherwise, and reverting to the stock
    ``logging.handlers.RotatingFileHandler`` silently re-introduces the bug where
    one process's output disappears from ``unified_scan.log`` after another
    process rotates.
    """
    fmt = "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s"
    date_fmt = "%Y-%m-%d %H:%M:%S"
    root_level = _resolve_log_level()

    if use_structlog:
        is_console = bool(os.environ.get("STRUCTLOG_CONSOLE"))
        processor = structlog.dev.ConsoleRenderer(colors=False) if is_console else structlog.processors.JSONRenderer()
        
        unified_formatter = {
            "()": structlog.stdlib.ProcessorFormatter,
            "processor": processor,
            "foreign_pre_chain": [
                structlog.stdlib.add_log_level,
                structlog.stdlib.add_logger_name,
                structlog.processors.TimeStamper(fmt="iso", utc=False),
            ],
        }
        verbose_formatter = unified_formatter
    else:
        _pf_foreign_chain = [
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
        ]
        unified_formatter = {
            "()": structlog.stdlib.ProcessorFormatter,
            "processor": _plain_renderer,
            "foreign_pre_chain": _pf_foreign_chain,
        }
        verbose_formatter = {
            "()": structlog.stdlib.ProcessorFormatter,
            "processor": _plain_renderer,
            "foreign_pre_chain": _pf_foreign_chain,
        }

    config: dict[str, Any] = {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "unified_filter": {
                "()": UnifiedLogFilter,
            }
        },
        "formatters": {
            "unified": unified_formatter,
            "verbose": verbose_formatter,
            "prefixed": {
                "()": SafePrefixFormatter,
                "prefix": f"{service_name}_",
                "format": fmt,
                "datefmt": date_fmt,
            },
        },
        # Every file handler MUST be the shared class: these files are written
        # by several processes at once (one per Hypercorn worker plus the queue
        # worker), and the stock RotatingFileHandler renames the file out from
        # under the others. See SharedRotatingFileHandler.
        "handlers": {
            "unified_file": {
                "class": "helpers.logging_config.SharedRotatingFileHandler",
                "filename": os.path.join(log_dir, "unified_scan.log"),
                "maxBytes": _LOG_MAX_BYTES,
                "backupCount": _LOG_BACKUP_COUNT,
                "encoding": "utf-8",
                "formatter": "unified",
                "filters": ["unified_filter"],
                "level": root_level,
            },
            "info_file": {
                "class": "helpers.logging_config.SharedRotatingFileHandler",
                "filename": os.path.join(log_dir, "info.log"),
                "maxBytes": _LOG_MAX_BYTES,
                "backupCount": _LOG_BACKUP_COUNT,
                "encoding": "utf-8",
                "formatter": "verbose",
                "level": "INFO",
            },
            "debug_file": {
                "class": "helpers.logging_config.SharedRotatingFileHandler",
                "filename": os.path.join(log_dir, "debug.log"),
                "maxBytes": _LOG_MAX_BYTES,
                "backupCount": _LOG_BACKUP_COUNT,
                "encoding": "utf-8",
                "formatter": "verbose",
                "level": "DEBUG",
            },
            "error_file": {
                "class": "helpers.logging_config.SharedRotatingFileHandler",
                "filename": os.path.join(log_dir, "error.log"),
                "maxBytes": _LOG_MAX_BYTES,
                "backupCount": _LOG_BACKUP_COUNT,
                "encoding": "utf-8",
                "formatter": "verbose",
                "level": "ERROR",
            },
            "queue_file": {
                "class": "helpers.logging_config.SharedRotatingFileHandler",
                "filename": os.path.join(log_dir, "queue.log"),
                "maxBytes": _LOG_MAX_BYTES,
                "backupCount": _LOG_BACKUP_COUNT,
                "encoding": "utf-8",
                "formatter": "unified",
                "level": "INFO",
            },
            "search_file": {
                "class": "helpers.logging_config.SharedRotatingFileHandler",
                "filename": os.path.join(log_dir, "search.log"),
                "maxBytes": _LOG_MAX_BYTES,
                "backupCount": _LOG_BACKUP_COUNT,
                "encoding": "utf-8",
                "formatter": "unified",
                "level": "INFO",
            },
        },
        "loggers": {
            "": {
                "handlers": ["unified_file", "info_file", "debug_file", "error_file"],
                "level": root_level,
            },
            "popularr.unified": {
                "handlers": ["unified_file"],
                "level": "INFO",
                "propagate": False,
            },
            "popularr.queue": {
                "handlers": ["queue_file"],
                "level": "INFO",
                "propagate": False,
            },
            "popularr.search": {
                "handlers": ["search_file"],
                "level": "INFO",
                "propagate": False,
            },
            "services.musicbrainz": {
                "level": "DEBUG",
                "propagate": True,
            },
            "helpers.musicbrainz": {
                "level": "DEBUG",
                "propagate": True,
            },
            "api_clients.musicbrainz_http": {
                "level": "DEBUG",
                "propagate": True,
            },
            "services.queue": {
                "handlers": ["queue_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "services.downloads": {
                "handlers": ["queue_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "db.repositories.queue": {
                "handlers": ["queue_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "db.repositories.queue_admin": {
                "handlers": ["queue_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "urllib3": {"level": "ERROR"},
            "httpx": {"level": "ERROR"},
            "apscheduler.schedulers.background": {"level": "WARNING"},
            "apscheduler.executors.default": {"level": "WARNING"},
        },
    }

    return config


def _setup_standard_logging(service_name: str, log_dir: str, use_structlog: bool = False) -> None:
    """Configure standard dictConfig-based logging using size-based rotation."""
    config = _build_logging_config(service_name, log_dir, use_structlog=use_structlog)
    logging.config.dictConfig(config)

    global _UNIFIED_FILE_HANDLER
    _UNIFIED_FILE_HANDLER = None
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        if isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", "").endswith(
            os.path.join(log_dir, "unified_scan.log")
        ):
            _UNIFIED_FILE_HANDLER = handler
            break
