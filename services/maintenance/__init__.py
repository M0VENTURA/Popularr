"""Maintenance operations that quiesce the application.

- ``safe_shutdown_service``: stop doing work and release database connections so
  PostgreSQL can be stopped cleanly for an update.
"""

from __future__ import annotations

__all__ = ["safe_shutdown_service"]
