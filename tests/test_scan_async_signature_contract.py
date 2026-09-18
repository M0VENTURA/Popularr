"""Regression: artist/letter-initiated popularity scans died before starting.

Reported as::

    Traceback (most recent call last):
      File "/usr/local/lib/python3.11/threading.py", line 1045, in _bootstrap_inner
        self.run()
      File "/usr/local/lib/python3.11/threading.py", line 982, in run
        self._target(*self._args, **self._kwargs)
    TypeError: run_popularity_from_artist() got an unexpected keyword argument
    'caller_scan_type'

## What was wrong

``routes/scan_routes/popularity.py::api_scan_from_artist`` launches the worker
like this::

    run_async(
        run_popularity_from_artist,
        artist=artist,
        force_rescan=force_rescan,
        progress_file=progress_file,
        caller_scan_type="popularity",
        daemon=False,
    )

``run_popularity_from_artist`` did not accept ``caller_scan_type``. The
keyword was added to the route in July (``e1b133a8``) and never added to the
function, so this scan path has been broken since it was written.

## Why nobody noticed for so long

This is the insidious part, and the reason a guard is worth having:

``run_async`` starts a **daemon thread** and returns immediately. The
``TypeError`` was raised inside ``Thread.run()``, so it surfaced only as a
traceback on the daemon thread's stderr. Nothing re-raised it to the request,
wrote it to the scan log, or recorded it in scan state. The route still replied

    {"success": true, "message": "Popularity scan started from artist: ..."}

and the dashboard showed a scan that would never progress — indistinguishable
from a slow scan or an empty library.

Two things now prevent a repeat:

1. ``run_popularity_from_artist`` accepts ``caller_scan_type``.
2. ``run_async`` validates the target's signature BEFORE starting the thread,
   so any future mismatch raises synchronously at the call site.
"""

from __future__ import annotations

import inspect
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. The function must accept every keyword its caller passes.
# ---------------------------------------------------------------------------


def test_run_popularity_from_artist_accepts_caller_scan_type() -> None:
    """The exact keyword the route passes must be accepted."""
    from services.popularity.pipeline import run_popularity_from_artist

    params = inspect.signature(run_popularity_from_artist).parameters
    assert "caller_scan_type" in params, (
        "run_popularity_from_artist no longer accepts caller_scan_type, but "
        "routes/scan_routes/popularity.py::api_scan_from_artist passes it. The "
        "worker thread will die on its first line with TypeError and the route "
        "will still report 'scan started'."
    )
    assert params["caller_scan_type"].default is None, (
        "caller_scan_type should default to None so omitting it keeps the "
        "historical behaviour"
    )


def test_run_popularity_from_artist_defaults_to_popularity() -> None:
    """Omitting the keyword must fall back to the 'popularity' scan type."""
    import ast

    src = (REPO_ROOT / "services" / "popularity" / "pipeline.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(src)
    fn = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
            and n.name == "run_popularity_from_artist"
        ),
        None,
    )
    assert fn is not None, "run_popularity_from_artist not found"
    body = ast.get_source_segment(src, fn) or ""
    assert 'caller_scan_type or "popularity"' in body, (
        "the None default should resolve to 'popularity', matching what the "
        "internal run_popularity_scan() call sends today"
    )


def test_every_run_popularity_from_artist_call_site_matches_its_signature() -> None:
    """Parse the route and check the real call against the real signature.

    Static, so it catches a future rename/removal of the parameter WITHOUT
    needing to start a scan.

    NOTE: the routes never CALL this function directly — they pass it as the
    target of ``run_async(target, **kwargs)``. So the audit has to inspect
    ``run_async`` calls whose first argument names it, not calls to it.
    """
    import ast

    from services.popularity.pipeline import run_popularity_from_artist

    sig = inspect.signature(run_popularity_from_artist)
    accepted = set(sig.parameters)
    has_var_kw = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )

    call_sites: list[tuple[str, int, list[str]]] = []
    for rel in (
        "routes/scan_routes/popularity.py",
        "routes/scan_routes/api.py",
        "routes/scan_routes/control.py",
    ):
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        src = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not (isinstance(f, ast.Name) and f.id == "run_async"):
                continue
            if not node.args or not isinstance(node.args[0], ast.Name):
                continue
            if node.args[0].id != "run_popularity_from_artist":
                continue
            kwargs = [k.arg for k in node.keywords if k.arg]
            kwargs = [k for k in kwargs if k != "daemon"]
            call_sites.append((rel, node.lineno, kwargs))

    assert call_sites, (
        "no run_async(run_popularity_from_artist, ...) call sites found — update "
        "this test if the route was renamed rather than deleting the guard"
    )

    for rel, lineno, kwargs in call_sites:
        if has_var_kw:
            continue
        unexpected = sorted(set(kwargs) - accepted)
        assert not unexpected, (
            f"{rel}:{lineno} passes {unexpected} to run_popularity_from_artist, "
            f"which its signature does not accept ({sorted(accepted)}). The "
            "background thread would die with TypeError before running any code."
        )


# ---------------------------------------------------------------------------
# 2. run_async must fail loudly, not silently, on a signature mismatch.
# ---------------------------------------------------------------------------


def test_run_async_rejects_unexpected_kwargs_synchronously() -> None:
    """A mismatch must raise at the call site, not inside the thread."""
    from routes.scan_routes._common import run_async

    def target(artist: str, verbose: bool = False) -> None:  # pragma: no cover
        raise AssertionError("must never be called")

    with pytest.raises(TypeError) as excinfo:
        run_async(target, artist="A", caller_scan_type="popularity")

    message = str(excinfo.value)
    assert "caller_scan_type" in message, (
        "the error must name the offending keyword so the cause is obvious"
    )


def test_run_async_accepts_a_matching_signature() -> None:
    """The happy path must still run the target and return a started thread."""
    from routes.scan_routes._common import run_async

    ran = threading.Event()

    def target(artist: str, caller_scan_type: str | None = None) -> None:
        ran.set()

    thread = run_async(target, artist="A", caller_scan_type="popularity")
    assert ran.wait(5), "the worker never ran"
    thread.join(5)
    assert not thread.is_alive()


def test_run_async_allows_var_keyword_targets() -> None:
    """A ``**kwargs`` target accepts anything and must not be blocked."""
    from routes.scan_routes._common import run_async

    seen: dict = {}
    done = threading.Event()

    def target(**kwargs) -> None:
        seen.update(kwargs)
        done.set()

    run_async(target, anything=1, and_more=2)
    assert done.wait(5)
    assert seen == {"anything": 1, "and_more": 2}


def test_run_async_logs_a_crashing_worker() -> None:
    """A worker exception must reach the logger, not only the thread's stderr.

    This is the part that made the original bug invisible: with no handler, the
    traceback went nowhere the app collects.
    """
    from unittest.mock import patch

    from routes.scan_routes import _common

    def boom() -> None:
        raise RuntimeError("worker exploded")

    with patch.object(_common.logger, "error") as mock_error:
        thread = _common.run_async(boom)
        thread.join(5)

    assert mock_error.called, (
        "a crashing scan worker must be logged; otherwise a dead scan looks "
        "identical to a scan that is simply still running"
    )
    logged = " ".join(str(c) for c in mock_error.call_args_list)
    assert "crashed" in logged.lower() or "Background" in logged


# ---------------------------------------------------------------------------
# 3. The bug class, swept across every scan-route run_async call site.
# ---------------------------------------------------------------------------


def test_no_scan_route_passes_unknown_kwargs_to_its_thread_target() -> None:
    """Audit EVERY run_async(target, ...) in routes/scan_routes/.

    Resolves locally-defined targets by name; imported targets are skipped
    because they need the module imported (which the targeted tests above cover
    for the one function that actually regressed). This keeps the sweep cheap
    while still catching the local-worker case.
    """
    import ast

    scan_routes = REPO_ROOT / "routes" / "scan_routes"
    if not scan_routes.is_dir():
        pytest.skip("routes/scan_routes not present")

    problems: list[str] = []
    checked = 0

    for path in sorted(scan_routes.glob("*.py")):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)

        local_defs: dict[str, ast.FunctionDef] = {
            n.name: n  # type: ignore[misc]
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not (isinstance(f, ast.Name) and f.id == "run_async"):
                continue
            if not node.args or not isinstance(node.args[0], ast.Name):
                continue

            tgt_name = node.args[0].id
            fn = local_defs.get(tgt_name)
            if fn is None:
                continue  # imported target — covered by the targeted tests

            checked += 1
            a = fn.args
            accepted = (
                {p.arg for p in a.posonlyargs}
                | {p.arg for p in a.args}
                | {p.arg for p in a.kwonlyargs}
            )
            if a.kwarg is not None:
                continue

            passed = {k.arg for k in node.keywords if k.arg}
            passed.discard("daemon")  # consumed by run_async itself
            unexpected = sorted(passed - accepted)
            if unexpected:
                problems.append(
                    f"{path.name}:{node.lineno} -> {tgt_name} unexpected "
                    f"{unexpected}"
                )

    assert checked, "no local run_async worker targets found — guard is vacuous"
    assert not problems, (
        "run_async call(s) pass keywords their worker does not accept, so the "
        "thread dies before running any code:\n  " + "\n  ".join(problems)
    )
