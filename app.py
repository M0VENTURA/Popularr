#!/usr/bin/env python3
"""
Application entrypoint for Popularr Web UI.

This file is intentionally minimal and orchestration-focused.

Responsibilities:
- Configure logging
- Create Quart app instance
- Register extensions (flash helpers, filters, blueprints, hooks)
- Ensure required files and config exist
- Initialize database schema
- Start background services (via Leader Election)
- Launch the web server
"""

import os
import threading
import time
import logging

from quart import Quart
from sqlalchemy import text

from helpers.secret_key import resolve_secret_key
from helpers.logging_config import setup_logging
from db.bootstrap import init_database_and_schema
from helpers.flash_manager import register_flash_helpers
from helpers.app_bootstrap import register_all_blueprints
from helpers.app_hooks import register_app_hooks
from helpers.template_filters import register_filters
from helpers.file_manager import ensure_default_log_files
from helpers.task_manager import initialize_app_services
from helpers.asset_helpers import register_asset_helpers

# SQLAlchemy engine — initialises the connection pool on startup.
# Gracefully degrades if sqlalchemy is not installed (e.g. older Docker image).
try:
    from db.engine import get_engine, run_migrations_on_startup
    _sqlalchemy_available = True
except Exception:
    _sqlalchemy_available = False

    def get_engine():
        return None

    def run_migrations_on_startup():
        return False

setup_logging("WebUI")

try:
    from helpers.logging_config import resolve_log_dir, _resolve_log_level
    logging.getLogger(__name__).info(
        "Logging ready: dir=%s level=%s",
        resolve_log_dir(),
        _resolve_log_level(),
    )
except Exception:
    pass

if _sqlalchemy_available:
    try:
        get_engine()
    except Exception as exc:
        logging.getLogger(__name__).warning("SQLAlchemy engine init failed: %s", exc)

app = Quart(__name__)
app.secret_key = resolve_secret_key()
app.config["PERMANENT_SESSION_LIFETIME"] = 86400

CONFIG_PATH = os.environ.get("CONFIG_PATH") or "/config/config.yaml"
LOG_PATH = os.environ.get("LOG_PATH", "/config/app.log")

ensure_default_log_files(CONFIG_PATH, LOG_PATH)

register_flash_helpers(app)
register_filters(app)
register_all_blueprints(app)
register_app_hooks(app)
register_asset_helpers(app)

# -------------------------------------------------------------------------
# TEST-SITE CUTOVER
# -------------------------------------------------------------------------
# When config.yaml sets features.use_test_site: true, the rebuilt UI tree under
# test_site/ is served at the ROOT of the app. Every URL is unchanged, so all
# links, forms and url_for calls work with no shims — the rebuilt site behaves
# exactly like the live one. Applied AFTER blueprint registration so the static
# view it rebinds already exists, and before the server starts.
#
# Failure here is non-fatal by design: a bad config value or a missing tree
# leaves the live UI serving rather than taking the app down.
from helpers.test_site_mode import maybe_apply_cutover

_TEST_SITE_ACTIVE = maybe_apply_cutover(app)

# -------------------------------------------------------------------------
# LEADER ELECTION & BACKGROUND SERVICES
# -------------------------------------------------------------------------

def _keep_lock_alive(conn) -> None:
    """Hold the dedicated connection open forever to maintain the advisory lock."""
    while True:
        time.sleep(60)
        try:
            # Ping the database to prevent TCP/idle timeouts from dropping the connection
            conn.execute(text("SELECT 1"))
        except Exception:
            pass

def _elect_leader_and_start_services(app_instance: Quart) -> None:
    """Ensure background tasks are only started on a single Hypercorn worker."""
    if not _sqlalchemy_available:
        return
        
    engine = get_engine()
    if not engine:
        return
        
    try:
        # Check out a dedicated connection and set AUTOCOMMIT so we don't
        # trigger "idle-in-transaction" timeouts on Postgres.
        conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        
        # Use a unique 64-bit integer for the Scheduler/Background worker lock
        _SCHEDULER_LOCK_KEY = 0x504F50534348  # "POPSCH"
        
        # pg_try_advisory_lock is non-blocking
        result = conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"), 
            {"k": _SCHEDULER_LOCK_KEY}
        ).scalar()
        
        if result:
            logging.getLogger(__name__).info("Acquired leader lock. Starting background services.")
            
            # Start the background tasks only on this specific worker
            initialize_app_services(app_instance)
            
            # Keep the connection alive in a daemon thread so the lock never drops
            threading.Thread(
                target=_keep_lock_alive, 
                args=(conn,), 
                daemon=True, 
                name="leader-keepalive"
            ).start()
        else:
            logging.getLogger(__name__).info("Another worker is the leader. Running in web-only mode.")
            # We didn't get the lock, so we don't need this dedicated connection
            conn.close()
            
    except Exception as exc:
        logging.getLogger(__name__).warning("Leader election failed: %s", exc)

# Execute the leader election synchronously during app boot
_elect_leader_and_start_services(app)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
