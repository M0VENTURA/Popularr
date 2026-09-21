"""Centralized logging configuration for Popularr.
Config-driven, thread-safe logging configuration.
"""

from __future__ import annotations

import os
import time
import logging
import logging.config
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


def _setup_standard_logging(service_name: str, log_dir: str, use_structlog: bool = False) -> None:
    """Configure standard dictConfig-based logging using size-based rotation."""
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
        "handlers": {
            "unified_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": os.path.join(log_dir, "unified_scan.log"),
                "maxBytes": _LOG_MAX_BYTES,
                "backupCount": _LOG_BACKUP_COUNT,
                "encoding": "utf-8",
                "formatter": "unified",
                "filters": ["unified_filter"],
                "level": root_level,
            },
            "info_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": os.path.join(log_dir, "info.log"),
                "maxBytes": _LOG_MAX_BYTES,
                "backupCount": _LOG_BACKUP_COUNT,
                "encoding": "utf-8",
                "formatter": "verbose",
                "level": "INFO",
            },
            "debug_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": os.path.join(log_dir, "debug.log"),
                "maxBytes": _LOG_MAX_BYTES,
                "backupCount": _LOG_BACKUP_COUNT,
                "encoding": "utf-8",
                "formatter": "verbose",
                "level": "DEBUG",
            },
            "error_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": os.path.join(log_dir, "error.log"),
                "maxBytes": _LOG_MAX_BYTES,
                "backupCount": _LOG_BACKUP_COUNT,
                "encoding": "utf-8",
                "formatter": "verbose",
                "level": "ERROR",
            },
            "queue_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": os.path.join(log_dir, "queue.log"),
                "maxBytes": _LOG_MAX_BYTES,
                "backupCount": _LOG_BACKUP_COUNT,
                "encoding": "utf-8",
                "formatter": "unified",
                "level": "INFO",
            },
            "search_file": {
                "class": "logging.handlers.RotatingFileHandler",
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
