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
from quart import flash, jsonify, redirect, request, url_for

from helpers.logging_config import log_unified
from services.scanning.runtime_state import scan_lock

logger = structlog.get_logger(__name__)


TRUE_VALUES = {"1", "true", "yes", "on"}


def wants_json() -> bool:
    """Return True when the caller expects JSON rather than an HTML page.

    WHY THIS EXISTS: the ``/scan/stop-*`` routes were built as HTML FORM
    endpoints — they ``flash()`` a message and ``redirect()`` to the dashboard
    so the user sees a banner. The dashboard's JavaScript calls the same URLs
    with ``fetch`` + JSON, so the redirect was followed to the dashboard, which
    answered ``200 text/html``, and the client's ``JSON.parse`` failed with
    "Server returned HTML instead of JSON (HTTP 200)".

    The HTTP status in that message being 200 is the tell: the request
    SUCCEEDED (the stop flag was set) but the response was a web page.

    Content negotiation keeps BOTH callers working instead of breaking the
    form path to satisfy the fetch path.

    An explicit ``Accept`` wins over ``Content-Type``, because a browser using
    ``fetch`` defaults ``Accept`` to ``*/*`` while a form post never sends
    ``Accept: application/json``.
    """
    accept = (request.headers.get("Accept") or "").strip().lower()
    if "application/json" in accept:
        return True
    if "text/html" in accept:
        return False

    content_type = (request.headers.get("Content-Type") or "").lower()
    if "application/json" in content_type:
        return True

    return (request.headers.get("X-Requested-With") or "").lower() == "xmlhttprequest"


async def stop_response(message: str, category: str = "info") -> Any:
    """Reply to a stop request in the format the caller can actually read.

    JSON clients get ``{"success": true, "message": ...}``; an HTML form client
    still gets the flash banner + dashboard redirect it has always had.
    """
    if wants_json():
        return jsonify({"success": True, "message": message})
    await flash(message, category)
    return redirect(url_for("ui.dashboard"))


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


def guard_and_launch_popularity_scan(
    runner: Callable,
    *args: Any,
    **kwargs: Any,
) -> bool:
    """Start a popularity-family scan under the duplicate-run guard.

    Returns True when the scan was started, False when one is already running
    (mirrors the dashboard's ``/api/popularity/run`` duplicate protection).

    THE SINGLE GATE
    ---------------
    Every popularity-scan trigger shares ONE guard so no path can start a
    second scan while one is already running — the dashboard API, the
    scheduler, the targeted artist/album routes and now the album-page "full"
    scan (which used to launch ``run_album_pipeline`` with NO guard at all:
    a second trigger overlapped the first, both marked the SAME shared
    ``popularity_scan`` progress row running, the runner's skip-guards saw the
    other generation's rows as fresh and self-froze, and the reported
    "Popularity Scan - no tracks processed: 1 album(s) exceeded their
    per-album time budget" was the result of two generations of the SAME
    album grinding on the shared 1 req/s MusicBrainz budget).

    Guard order:
      * in-process runtime registry first (cheap);
      * cross-process shared scan-state rows second (a scan running in
        ANOTHER hypercorn worker is invisible to the per-process registry),
      * with the stale-row SELF-HEAL — if the DB says a scan is running but no
        LIVE worker in THIS process owns it, the row is a phantom (a worker
        crashed before its ``finally``, or a previous process died mid-scan).
        Clear it so targeted scans are never permanently blocked.
    """
    from services.scanning.runtime_state import (
        clear_runtime,
        is_runtime_running,
        set_runtime,
    )

    # ``scan_type`` is OUR bookkeeping (runtime-registry type label + failure
    # record). It must NOT be force-forwarded to the runner: the targeted
    # popularity runners accept it (it is their mode selector), but the
    # artist/album "full" pipelines (``run_artist_pipeline`` /
    # ``run_album_pipeline``) do NOT — forwarding it blindly made
    # ``run_album_pipeline`` die with "unexpected keyword argument 'scan_type'"
    # inside the daemon thread (reported 2026-10-12: "Targeted worker failed:
    # run_album_pipeline() got an unexpected keyword argument 'scan_type'").
    # Resolve it once here and drop it from what the runner receives; anything
    # ELSE the runner does not accept is a genuine caller bug and is dropped
    # loudly (a mismatched signature must not kill the worker silently).
    _scan_type = str(kwargs.get("scan_type") or "popularity")
    _runner_kwargs = dict(kwargs)
    try:
        _sig = inspect.signature(runner)
        if not any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in _sig.parameters.values()
        ):
            _accepted = {
                name
                for name, p in _sig.parameters.items()
                if p.kind
                in (
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                )
            }
            # ``scan_type`` is expected to be absent from the full pipelines
            # (see above); dropping it is the DESIGNED behaviour, so it must
            # not be reported as a caller bug. Any other unexpected keyword is
            # still surfaced loudly.
            _dropped = sorted(set(_runner_kwargs) - _accepted - {"scan_type"})
            _runner_kwargs = {
                k: v for k, v in _runner_kwargs.items() if k in _accepted
            }
            if _dropped:
                logger.warning(
                    "Dropping kwargs the scan runner does not accept",
                    runner=getattr(runner, "__name__", repr(runner)),
                    dropped=_dropped,
                )
    except (TypeError, ValueError):
        pass

    with scan_lock:
        # In-process guard first (cheap).  Then the cross-process guard.
        if is_runtime_running("popularity"):
            return False

        # Cross-process: if the DB says a scan is running but no LIVE worker in
        # THIS process owns it, the row is stale (a worker crashed before its
        # finally, or a previous process was killed mid-scan) — self-heal it so
        # targeted scans are never permanently blocked by a phantom "already
        # running" (mirrors the dashboard API path).
        from services.scanning.pipelines.popularity_pipeline import is_popularity_scan_active
        from services.scanning.runtime_state import get_runtime
        from services.scanning.scan_state import (
            clear_progress_file,
            get_scan_progress_path,
            read_progress_file,
        )

        if is_popularity_scan_active():
            _live_owner = False
            for _scan_type in ("popularity_scan", "full_scan"):
                _state = read_progress_file(get_scan_progress_path(_scan_type))
                if _state.get("is_running"):
                    _rt = get_runtime(_scan_type.replace("_scan", ""))
                    if _rt is not None:
                        _owner_thread = _rt.get("thread") if isinstance(_rt, dict) else _rt
                        if is_process_alive(_owner_thread):
                            _live_owner = True
                            break
            if _live_owner:
                return False
            # No live owner — the DB row is stale.  Reset it so THIS scan
            # starts cleanly instead of being permanently blocked.
            try:
                for _scan_type in ("popularity_scan", "full_scan"):
                    _state = read_progress_file(get_scan_progress_path(_scan_type))
                    if _state.get("is_running"):
                        clear_progress_file(get_scan_progress_path(_scan_type))
                log_unified("Cleared stale scan-state rows (phantom 'already running') before starting scan")
            except Exception:
                pass

        def _worker() -> None:
            try:
                runner(*args, **_runner_kwargs)
            except Exception as exc:
                # A daemon-thread exception would otherwise be swallowed by
                # ``run_async`` (no handler) — the route returns "started",
                # the worker dies silently, and the Unified Scan log shows
                # nothing at all for the requested artist.  Surface it so the
                # failure is diagnosable (mirrors the dashboard API worker).
                import traceback

                log_unified(f"[POPULARITY] Targeted worker failed: {exc}")
                logger.error(
                    "[POPULARITY] Targeted worker failed",
                    error=str(exc),
                    traceback=traceback.format_exc(),
                )
                try:
                    from services.scanning.scan_history_service import record_scan
                    record_scan(
                        _scan_type,
                        "failed",
                        message=f"targeted scan failed: {exc}",
                        artist=str(args[0] if args else kwargs.get("artist") or ""),
                        album="",
                    )
                except Exception:
                    pass
            finally:
                clear_runtime("popularity")

        thread = run_async(_worker)
        set_runtime("popularity", {"thread": thread, "type": _scan_type})
    return True


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
