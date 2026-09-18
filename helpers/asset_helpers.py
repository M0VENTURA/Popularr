"""Versioned static asset helper — automatic cache-busting via file mtime.

Usage in templates::

    {{ versioned_static('css/popularr.css') }}
    {{ versioned_static('js/unified_search.js') }}

The query string tracks each file's last modification time, so deployed
instances and mobile clients always pull the newest asset after a deploy —
no more manual ``?v=N`` bumps (the old ``?v=3``-style links are converted
in base.html and the page templates).
"""

from __future__ import annotations

import os
from typing import Any

# filename -> mtime version.  Static files change rarely; the cache avoids a
# stat per template render while still picking up edits (mtime changes).
_mtime_cache: dict[str, int] = {}


def register_asset_helpers(app: Any) -> None:
    """Register the ``versioned_static`` template global on a Quart app."""

    @app.context_processor
    def _inject_asset_version() -> dict[str, Any]:
        def versioned_static(filename: str) -> str:
            from quart import url_for

            version: int | None = _mtime_cache.get(filename)
            # Search every active static root, not just app.static_folder: in
            # test-site cutover mode the rebuilt assets live in test_site/static
            # and only the fallback assets (dist/vendor/*) are in static/. Using
            # app.static_folder alone would miss every rebuilt file's mtime and
            # silently drop the cache-buster.
            for root in _static_roots(app):
                try:
                    mtime = int(os.path.getmtime(os.path.join(root, filename)))
                except (OSError, TypeError, ValueError):
                    continue
                if version != mtime:
                    _mtime_cache[filename] = mtime
                    version = mtime
                break

            url = url_for("static", filename=filename)
            return f"{url}?v={version}" if version else url

        return {"versioned_static": versioned_static}


def _static_roots(app: Any) -> list[str]:
    """Static directories to stat, highest priority first.

    The rebuilt tree is consulted first when test-site mode is on, with the live
    ``static/`` folder always last so fallback assets still resolve.
    """
    roots: list[str] = []
    try:
        from helpers.test_site_mode import static_roots
        roots = static_roots()
    except Exception:
        roots = []
    live = str(app.static_folder or "")
    if live and live not in roots:
        roots.append(live)
    return [r for r in roots if r]
