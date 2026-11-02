"""Artist-level scan pipeline.

This wraps the existing artist pipeline implementation so routes can call a
stable service function without importing old helpers directly.
"""

from __future__ import annotations

from typing import Any

import structlog

from helpers.logging_config import log_unified

logger = structlog.get_logger(__name__)


def run_artist_pipeline(
    artist_name: str,
    force: bool = False,
    **scan_options: Any,
) -> None:
    """Run the complete scan pipeline for one artist.

    Delegates to ``services.scanning.pipeline.run_artist_scan_pipeline``,
    which swallows most per-artist errors internally (a single artist must
    never abort a full library scan).  Any exception that DOES escape is a
    genuine bug — surface it with context instead of silently swallowing it.
    """
    try:
        from services.scanning.pipeline import run_artist_scan_pipeline
        run_artist_scan_pipeline(
            artist_name,
            force=force,
            scan_options=scan_options,
        )
    except Exception as exc:
        log_unified(f"❌ Artist scan failed for {artist_name}: {exc}")
        logger.error(
            "Artist pipeline crashed",
            artist=artist_name,
            error=str(exc),
            exc_info=True,
        )
        raise
