"""Guard: a NON-forced scan must not die on ``_singles_pass``.

REPORTED (scan log, every artist of a 58-artist dashboard scan)
---------------------------------------------------------------
    [POPULARITY] Scan failed
    Artist scan failed: cannot access local variable '_singles_pass' where it
    is not associated with a value
    [SCAN] section completed section='bounded_call' elapsed_s=8.679
    label="artist pipeline 'Warrel Dane'"

Every artist reported "failed" yet the full scan advanced to the next one, so
the collection was never actually scanned.

ROOT CAUSE
----------
Inside ``run_scan`` the scan banner computed

    _scan_mode_label = "Forced Scan" if force else (
        "Singles Pass" if _singles_pass else "Normal Scan"
    )

~100 lines BEFORE ``_singles_pass`` was assigned:

    _singles_pass = bool(options.get("singles_only")
                         or options.get("singles_with_missing_popularity"))

Python decides at compile time that the name is local to ``run_scan`` because
it is assigned somewhere in the function, so reading it before that assignment
raises ``UnboundLocalError`` — a subclass of ``NameError``, which is what the
log shows.

WHY FORCED SCANS LOOKED FINE
----------------------------
``"Forced Scan" if force else (...)`` short-circuits: with ``force=True`` the
``else`` branch never evaluates, so ``_singles_pass`` is never read before its
assignment and the scan succeeds.  Only ``force=False`` (the default for the
dashboard scan and the artist page) crashed — which is exactly the asymmetry
that made this hard to spot.

The fix binds ``_singles_pass`` from ``options`` immediately after ``options``
is built, so it precedes every use.  These tests pin both the ordering (a real
scan to the banner) and the source-level invariant.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE = REPO_ROOT / "services" / "popularity" / "scan_stage_runner.py"

VAR = "_singles_pass"


# ---------------------------------------------------------------------------
# A real (non-forced) scan must get past the banner
# ---------------------------------------------------------------------------


class _StopAfterBanner(BaseException):
    """Sentinel: the banner was reached, so the bug would have fired.

    Subclasses ``BaseException``, NOT ``Exception``, on purpose. The banner is
    wrapped in ``except Exception`` handlers (the scan-report call and the genre
    prune both swallow ``Exception``), so an ``Exception`` sentinel would be
    silently eaten and the test would pass without proving anything.
    """


class _NullProgressTracker:
    """``progress_tracker`` update/start/finish are pure reporting."""

    def __call__(self, *args, **kwargs):
        return None


def _drive_to_banner(monkeypatch, recorded: list[str]):
    """Patch the scan down to its banner and record the reported mode label.

    The oracle is the mode label handed to the scan report. ``run_scan``
    computes it on the line that (when the bug is present) raises before the
    report is ever called — so a missing record IS the failure.
    """
    from services.popularity import scan_stage_runner as mod
    from helpers import scan_report

    def _record_and_stop(*args, **kwargs):
        recorded.append(kwargs.get("mode"))
        raise _StopAfterBanner()

    monkeypatch.setattr(mod, "load_candidates", lambda options=None: [{"artist": "A", "album": "B"}])
    monkeypatch.setattr(mod, "get_feature", lambda name, default=None: False)
    monkeypatch.setattr(mod, "update", _NullProgressTracker())
    monkeypatch.setattr(mod, "start", _NullProgressTracker())
    monkeypatch.setattr(mod, "finish", _NullProgressTracker())
    monkeypatch.setattr(mod, "log_unified", lambda *a, **k: None)
    monkeypatch.setattr(scan_report, "scan_started", _record_and_stop)
    return mod


def test_a_non_forced_scan_passes_the_banner(monkeypatch):
    """THE REGRESSION: ``force=False`` must reach the scan report.

    With the bug present the mode label raises ``UnboundLocalError`` on the
    line before ``scan_started`` is called, so nothing is recorded. Reaching
    the report with the right label proves the banner evaluated.
    """
    recorded: list[str] = []
    mod = _drive_to_banner(monkeypatch, recorded)

    with pytest.raises(_StopAfterBanner):
        mod.run_scan(artist_filter="Warrel Dane", force=False)

    assert recorded == ["Normal Scan"], (
        "a non-forced scan never reached the scan-report banner (recorded "
        f"{recorded!r}). This is the reported crash: the banner reads "
        f"{VAR} before it is bound, and ``force=False`` does not "
        "short-circuit past it."
    )


def test_both_scan_modes_pass_the_banner(monkeypatch):
    """Forced AND non-forced must both work.

    ``force=True`` masked the bug via short-circuiting, so testing only one of
    the two modes would have missed it either way. Both are pinned.
    """
    for force, expected in ((False, "Normal Scan"), (True, "Forced Scan")):
        recorded: list[str] = []
        mod = _drive_to_banner(monkeypatch, recorded)
        with pytest.raises(_StopAfterBanner):
            mod.run_scan(artist_filter="Warrel Dane", force=force)
        assert recorded == [expected], (
            f"force={force} reported {recorded!r}, expected {expected!r}"
        )


# ---------------------------------------------------------------------------
# Source-level invariant: every read must follow a binding
# ---------------------------------------------------------------------------


def _control_flow_order(func) -> list[tuple[int, str]]:
    """Ordered ``(lineno, 'read'|'assign')`` events for the function body.

    Deliberately shallow: the bug is a straight-line ordering problem in
    ``run_scan``'s own body, so statements nested in inner functions (which
    have their own scope and run later) and in ``if``/``for`` blocks are
    treated as ordered events rather than a full flow analysis.  This is
    enough to state "the first binding precedes the first read".
    """
    tree = ast.parse(inspect.getsource(func))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))

    events: list[tuple[int, str]] = []
    for node in fn.body:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id == VAR:
                kind = "assign" if isinstance(sub.ctx, ast.Store) else "read"
                events.append((sub.lineno, kind))
            elif isinstance(sub, ast.arg) and sub.arg == VAR:
                events.append((sub.lineno, "assign"))
    return sorted(events)


class TestBindingPrecedesUse:
    def test_the_first_event_for_the_name_is_a_binding(self):
        from services.popularity import scan_stage_runner as mod

        events = _control_flow_order(mod.run_scan)
        assert events, (
            f"{VAR} is no longer used in run_scan — if it was removed, delete "
            f"this guard; if it was renamed, update VAR in this test."
        )
        first_lineno, first_kind = events[0]
        assert first_kind == "assign", (
            f"run_scan reads {VAR} at line {first_lineno} before binding it. "
            "Because the name is assigned somewhere in the function, Python "
            "treats it as local and this raises UnboundLocalError on every "
            "scan with force=False — the exact reported crash. Bind it from "
            "``options`` before its first use."
        )

    def test_the_name_is_bound_exactly_once(self):
        """A second binding would reintroduce the ordering hazard."""
        from services.popularity import scan_stage_runner as mod

        assigns = [ln for ln, kind in _control_flow_order(mod.run_scan) if kind == "assign"]
        assert len(assigns) == 1, (
            f"{VAR} is assigned {len(assigns)} times in run_scan (lines "
            f"{assigns}). One definition means the read/bind order can be "
            "verified statically; two means the later one can drift below a "
            "use again."
        )

    def test_the_binding_is_derived_from_options(self):
        """It must still mean what the branches below it assume."""
        src = inspect.getsource(
            __import__(
                "services.popularity.scan_stage_runner", fromlist=["run_scan"]
            ).run_scan
        )
        code = "\n".join(
            ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
        )
        assert (
            'options.get("singles_only")' in code
            and 'options.get("singles_with_missing_popularity")' in code
        ), (
            f"{VAR} must be derived from the singles_only / "
            "singles_with_missing_popularity options"
        )


# ---------------------------------------------------------------------------
# The class of bug, not just this instance
# ---------------------------------------------------------------------------


def _same_scope_events(fn: ast.FunctionDef) -> list[tuple[int, str, str]]:
    """``(lineno, name, 'read'|'assign')`` for ``fn``'s OWN scope only.

    Nested ``def``/``class``/``lambda`` bodies are skipped: they are a
    different scope, execute later, and a name they bind is not a
    ``run_scan`` local. Including them produced ~25 false positives (every
    helper defined inside ``run_scan`` looked like an unbound local), which is
    why this walker excludes them rather than using a bare ``ast.walk``.

    ``for``/``with``/``except`` targets and comprehension targets are bindings,
    because they are assignments in the same scope.
    """
    events: list[tuple[int, str, str]] = []

    def is_scope_boundary(node: ast.AST) -> bool:
        return isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        )

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if is_scope_boundary(child):
                continue
            if isinstance(child, ast.Name):
                kind = "assign" if isinstance(child.ctx, ast.Store) else "read"
                events.append((child.lineno, child.id, kind))
            visit(child)

    visit(fn)
    return sorted(events)


class TestNoOtherUseBeforeBinding:
    def test_every_function_local_is_bound_before_it_is_read(self):
        """Catch the same mistake for every local of ``run_scan``.

        The reported bug was one variable; the risk is the pattern. For every
        name that ``run_scan`` assigns in its OWN scope — i.e. every name
        Python treats as a function local, which is precisely the set that can
        raise ``UnboundLocalError`` — the first event must be the binding, not
        a read.
        """
        from services.popularity import scan_stage_runner as mod

        fn = ast.parse(inspect.getsource(mod.run_scan)).body[0]
        assert isinstance(fn, ast.FunctionDef)

        events = _same_scope_events(fn)
        # ``posonlyargs``/``args``/``kwonlyargs`` are lists; ``vararg``/
        # ``kwarg`` are a single ``arg`` or ``None``.
        params = {
            a.arg
            for a in (
                fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs
                + [a for a in (fn.args.vararg, fn.args.kwarg) if a is not None]
            )
        }

        locals_bound = {name for _, name, kind in events if kind == "assign"} - params

        offenders: list[tuple[str, int, int]] = []
        for name in sorted(locals_bound):
            first_assign = min(ln for ln, n, k in events if n == name and k == "assign")
            reads = [ln for ln, n, k in events if n == name and k == "read"]
            if reads and min(reads) < first_assign:
                offenders.append((name, min(reads), first_assign))

        assert not offenders, (
            "run_scan reads these locals before binding them — latent "
            "UnboundLocalError on any run that does not short-circuit past "
            f"them (name, first_read_line, first_bind_line): {offenders}"
        )

    def test_the_guard_can_actually_detect_the_bug(self, monkeypatch):
        """Mutation check: the guard must fail when the ordering is broken.

        Without this, a guard that silently walks the wrong tree would pass
        forever. The real source is re-parsed with the ``_singles_pass``
        binding moved back below its use, and the checker must flag it.
        """
        from services.popularity import scan_stage_runner as mod

        src = inspect.getsource(mod.run_scan)
        binding = (
            "    _singles_pass = bool(\n"
            '        options.get("singles_only") or options.get("singles_with_missing_popularity")\n'
            "    )\n"
        )
        assert binding in src, (
            "the _singles_pass binding is no longer where this guard expects "
            "it — update the mutation literal"
        )
        broken = src.replace(binding, "", 1)
        # Re-insert it just before the "scan threads" block, i.e. AFTER the
        # banner that reads it — the original defect.
        marker = "    _scan_threads = 4"
        assert marker in broken
        broken = broken.replace(marker, binding + marker, 1)

        fn = ast.parse(broken).body[0]
        assert isinstance(fn, ast.FunctionDef)

        events = _same_scope_events(fn)
        reads = [ln for ln, n, k in events if n == VAR and k == "read"]
        assigns = [ln for ln, n, k in events if n == VAR and k == "assign"]
        assert reads and assigns, "mutation did not reproduce the bug"
        assert min(reads) < min(assigns), (
            "the source mutation should read _singles_pass before binding it; "
            "the guard's ordering check is not looking at the right code"
        )
