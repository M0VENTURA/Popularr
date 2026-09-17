"""Flask/Quart blueprint registration.

Central import-and-register for all API blueprints used by the app.
Called once during app factory setup (``helpers.app_bootstrap.register_all_blueprints``).

Architecture rules:
- Every new route module with a ``Blueprint`` must be added here.
- Blueprints can set ``url_prefix`` at registration time.
- No business logic is stored in this file.
"""

from __future__ import annotations

from typing import Any

import structlog

from routes.downloads import downloads_bp
from routes.playlists import playlists_bp
from routes.favourites import favourites_bp
from routes.album_routes import album_bp
from routes.artist_routes import artist_bp
from routes.analytics import analytics_bp
from routes.logs import logs_bp
from routes.metadata import metadata_bp
from routes.track_routes import track_bp
from routes.download_search_routes import slskd_bp, slsk_bp
from routes.musicbrainz_routes import mb_bp
from routes.social_routes import listenbrainz_bp, lastfm_bp, weekly_bp
from routes.upcoming_releases_routes import upcoming_bp
from routes.misc_routes import misc_api_bp
from routes.api_v1 import api_v1_bp
from routes.navidrome import navidrome_bp
from routes.scan_routes import scans_bp
from routes.scan_routes.library_routes import library_bp
from routes.ui_routes import ui_bp
from routes.preview_routes import preview_bp
from routes.queue import (
    queue_processing_bp,
    queue_matching_bp,
    queue_cleanup_bp,
    queue_diagnostics_bp,
)

logger = structlog.get_logger(__name__)


def register_all_blueprints(app: Any) -> None:
    """Register all application blueprints with the Quart/Flask app instance."""
    blueprints = [
        (downloads_bp, None),
        (playlists_bp, None),
        (favourites_bp, None),
        (album_bp, "/api/album"),
        (artist_bp, None),
        (analytics_bp, None),
        (logs_bp, None),
        (metadata_bp, None),
        (track_bp, None),
        (slskd_bp, None),
        (slsk_bp, None),
        (mb_bp, None),
        (listenbrainz_bp, None),
        (lastfm_bp, None),
        (weekly_bp, None),
        (upcoming_bp, None),
        (misc_api_bp, None),
        (api_v1_bp, None),
        (ui_bp, None),
        # /new — preview of the rebuilt templates under New/. Registered last of
        # the UI blueprints and behind its own prefix, so it cannot shadow a
        # live route. Delete this line together with routes/preview_routes.py at
        # cutover (see New/MIGRATION.md).
        (preview_bp, None),
        (navidrome_bp, None),
        (scans_bp, None),
        (library_bp, None),
        (queue_processing_bp, None),
        (queue_matching_bp, None),
        (queue_cleanup_bp, None),
        (queue_diagnostics_bp, None),
    ]

    registered_count = 0
    for bp, prefix in blueprints:
        try:
            if prefix:
                app.register_blueprint(bp, url_prefix=prefix)
            else:
                app.register_blueprint(bp)
            registered_count += 1
        except Exception as exc:
            logger.error(
                "Failed to register blueprint",
                blueprint=getattr(bp, "name", str(bp)),
                prefix=prefix,
                error=str(exc),
            )
            raise

    logger.info("All application blueprints successfully registered", total=registered_count)
