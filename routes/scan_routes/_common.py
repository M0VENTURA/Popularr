"""Shared helpers for scan route modules.

Keep Flask route files thin: parse request data, call services, and return a
response. Reusable helpers for booleans, redirects, and background thread
launching live here.
"""

from __future__ import annotations

import functools
import inspect
import threading
from typing import Any, Callable

import structlog
from quart import redirect, url_for

logger = structlog.get_logger(__name__)


TRUE_VALUES = {"1", "true", "yes", "on"}


def form_bool(value: Any) -> bool:
    """Return True for common HTML checkbox/query string truthy values."""
    return str(value or "").strip().lower() in TRUE_VALUES


def assert_callable_accepts_kwargs(target: Callable, kwargs: dict[str, Any]) -> None:
    """Raise TypeError up-front if ``target`` cannot accept these keywords.

    WHY THIS EXISTS: ``run_async`` starts a thread and returns immediately, so a
    signature mismatch does not surface at the call site — it kills the worker
    before its first line, with the traceback written to the daemon thread's
    stderr and nothing in the scan log or DB. The route still answers
    "scan started" and the dashboard shows a scan that will never progress.

    That is exactly what happened to artist/letter scans: the route passed
    ``caller_scan_type`` while ``run_popularity_from_artist`` did not accept it,
    so every such scan died instantly and silently.

    Checking the signature BEFORE the thread starts turns that into a loud,
    immediate error at the call site, while the scan is still being requested.
    """
    try:
        sig = inspect.signature(target)
    except (TypeError, ValueError):
        # Some builtins/C-callables expose no signature; do not block them.
        return

    accepts_var_kw = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    if accepts_var_kw:
        return

    accepted = {
        name
        for name, p in sig.parameters.items()
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    }
    unexpected = sorted(set(kwargs) - accepted)
    if unexpected:
        raise TypeError(
            f"{getattr(target, '__name__', target)}() got an unexpected keyword "
            f"argument {unexpected[0]!r}. The background thread would have died "
            f"before running any code. Accepted: {sorted(accepted)}"
        )


def run_async(target: Callable, *args, daemon: bool = True, **kwargs) -> threading.Thread:
    """Run a callable in a background thread and return the thread object.

    Routes use this to stay non-blocking. Long-running scan code belongs in
    ``services.scanning`` modules, not in the route functions.

    Raises TypeError synchronously if ``target`` cannot accept ``kwargs``, and
    logs any exception the thread raises — a silent daemon-thread crash is
    indistinguishable from a scan that simply never progresses.
    """
    assert_callable_accepts_kwargs(target, kwargs)

    @functools.wraps(target)
    def _guarded(*a: Any, **kw: Any) -> None:
        try:
            target(*a, **kw)
        except BaseException:
            # Log before re-raising so the failure reaches the app log, not
            # just the daemon thread's stderr where nothing collects it.
            logger.error(
                "Background scan worker crashed",
                target=getattr(target, "__name__", repr(target)),
                exc_info=True,
            )
            raise

    thread = threading.Thread(target=_guarded, args=args, kwargs=kwargs, daemon=daemon)
    thread.start()
    return thread


def is_process_alive(process_ref: Any) -> bool:
    """Handle the project’s mixed process/thread/dict scan references."""
    if process_ref is None:
        return False

    if isinstance(process_ref, dict):
        process_ref = process_ref.get("thread")

    if process_ref is None:
        return False

    if hasattr(process_ref, "is_alive"):
        return bool(process_ref.is_alive())

    if hasattr(process_ref, "poll"):
        return process_ref.poll() is None

    return False


def redirect_for_artist(artist: str):
    """Redirect to an artist page, falling back to the dashboard."""
    if artist:
        return redirect(url_for("ui.artist_detail", name=artist))
    return redirect(url_for("ui.dashboard"))


def redirect_for_album(artist: str, album: str):
    """Redirect to an album page, falling back to the artist/dashboard page."""
    if artist and album:
        return redirect(url_for("ui.album_detail", album_path=f"{artist}/{album}"))
    return redirect_for_artist(artist)
