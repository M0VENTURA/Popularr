"""Flask/Quart application lifecycle hooks.

Registers ``before_request`` and ``after_request`` handlers on the app.
Provides:
- Request timing (duration logged as ``X-Request-Duration`` header).
- Basic security headers on every response.
- Authentication gate: every route requires a logged-in session EXCEPT a
  small public allow-list.
- First-run interceptor: redirects to setup if Navidrome is unconfigured.

Called once during app factory setup.
"""

from __future__ import annotations

import time
import traceback
from typing import Any

import structlog
from quart import Response, g, jsonify, redirect, request, session, url_for
from werkzeug.exceptions import HTTPException

# FIX: Moved to top level to avoid import overhead on every single HTTP request
from helpers.config_helpers import needs_setup

logger = structlog.get_logger(__name__)

_ALWAYS_PUBLIC_ENDPOINTS = frozenset({
    "static",
    "ui.login",
    "ui.logout",
})

_SETUP_PUBLIC_ENDPOINTS = frozenset({
    "ui.setup",
    "ui.api_test_navidrome_connection",
    "ui.api_setup_save",
    "ui.api_setup_save_partial",
    "misc_api.api_essentia_download_status",
    "misc_api.api_essentia_download_models",
    "scans.api_navidrome_import",
})


def _is_api_request() -> bool:
    """True when the caller expects JSON rather than an HTML page.

    Path-based detection (``/api/``) was the original test, but a handful of
    fetch-only endpoints live OUTSIDE that prefix — the scan control routes
    (``/scan/stop-popularity`` and friends) are called by the dashboard with
    ``fetch`` and a JSON body. For those, an expired session used to answer
    ``302 -> /login``, so the client followed the redirect, received the login
    PAGE, and reported "Server returned HTML instead of JSON (HTTP 200)" —
    pointing the user at the wrong problem entirely.

    A request that explicitly asks for JSON now gets JSON errors, so a
    session timeout surfaces as a 401 the client can act on. Requests that ask
    for HTML (page navigations) and traditional form posts (no JSON signal)
    keep the redirect-to-login behaviour.
    """
    if request.path.startswith("/api/"):
        return True

    accept = (request.headers.get("Accept") or "").strip().lower()
    if "application/json" in accept:
        return True

    content_type = (request.headers.get("Content-Type") or "").lower()
    if "application/json" in content_type:
        return True

    return (request.headers.get("X-Requested-With") or "").lower() == "xmlhttprequest"


def register_app_hooks(app: Any) -> None:
    @app.errorhandler(Exception)
    async def handle_unhandled_exception(exc: Exception) -> Any:  # FIX: Made async
        """Return JSON for escaped exceptions instead of Quart's default HTML 500 page."""
        if isinstance(exc, HTTPException):
            return exc
            
        try:
            request_method = request.method
            request_path = request.path
            qs = request.query_string.decode('utf-8', 'replace')
            if qs:
                request_path += f"?{qs[:200]}"
        except Exception:
            request_method = "UNKNOWN"
            request_path = "(no request context)"

        logger.error(
            "Unhandled exception",
            method=request_method,
            path=request_path,
            exc_type=type(exc).__name__,
            error=str(exc),
            traceback=traceback.format_exc()
        )
        
        return jsonify({"success": False, "error": "An internal server error occurred. Please try again."}), 500

    @app.before_request
    async def before_request() -> Any:  # FIX: Made async
        # FIX: Use monotonic time for accurate duration tracking
        g.start_time = time.monotonic()

        endpoint = request.endpoint or ""

        # ── Always-public endpoints (static, login, logout) ──────────────
        if endpoint in _ALWAYS_PUBLIC_ENDPOINTS or endpoint.endswith(".static"):
            return

        # ── First run: setup wizard + its APIs are public ────────────────
        if needs_setup():
            if endpoint in _SETUP_PUBLIC_ENDPOINTS:
                return
            if _is_api_request():
                return jsonify({"success": False, "error": "Setup required"}), 401
            return redirect(url_for("ui.setup"))

        # ── Configured app: require a session for everything else ────────
        if session.get("username"):
            return

        if _is_api_request():
            return jsonify({"success": False, "error": "Authentication required"}), 401
        return redirect(url_for("ui.login"))

    @app.after_request
    async def after_request(response: Response) -> Response:  # FIX: Made async
        # Request timing
        if hasattr(g, "start_time"):
            # FIX: Use monotonic time 
            duration_ms = int((time.monotonic() - g.start_time) * 1000)
            response.headers["X-Request-Duration-Ms"] = str(duration_ms)
            
            if duration_ms > 1000:
                logger.debug(
                    "Slow request",
                    method=request.method,
                    path=request.path,
                    duration_ms=duration_ms,
                )

        # Security headers
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")

        return response
