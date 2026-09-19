"""Startup self-checks in ``entrypoint.sh`` must not rot into false alarms.

``entrypoint.sh`` boots every container, so its self-checks are the first thing
an operator reads when something looks wrong. One of them —
``verify_scan_unwrap_fix()`` — reported this on every single boot:

    Scan unwrap fix check: source_marker_hits=0 loaded_module=MISSING
    ⚠ Scan unwrap fix NOT VERIFIED — source_marker_hits=0, loaded_module=MISSING
    ⚠ If this is the current image, check for stale __pycache__/old image.

It was a **false alarm, and it could never pass again**. The check grepped for
the literal ``"Album future completed"``, a log message the original
Future-unwrap fix happened to emit. A later refactor of the track executor
(``5193741e`` "Cleaned up some issues causing timeouts during scanning") removed
that message while keeping the actual fix, so both arms of the check went
permanently negative.

That is worse than having no check at all: the ONLY thing this check exists for
is telling an operator whether a stale ``.pyc`` / old image is running, and a
warning that always fires cannot make that distinction. The real invariant —
the collector unwraps each completed future — still held the whole time.

Two guards, because the failure is a class, not an instance:

1. ``test_entrypoint_grep_tokens_still_exist`` — EVERY literal the script greps
   out of a file must still be present in that file. This catches the generic
   "self-check pins a string a refactor deletes" rot, whatever it is pinned to.
2. ``test_scan_unwrap_check_would_pass_on_this_tree`` — runs the *as-shipped
   predicate* (greppable marker present AND the collector unwraps its futures)
   and requires it to be satisfied. This one cannot rot: it asserts the
   behaviour, not the wording.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

ENTRYPOINT = REPO_ROOT / "entrypoint.sh"
SCAN_RUNNER_REL = "services/popularity/scan_stage_runner.py"
SCAN_RUNNER = REPO_ROOT / SCAN_RUNNER_REL

#: The collector that owns the per-future unwrap.
COLLECTOR = "_execute_track_jobs_safely"

#: The retired marker. Kept here so the guard can assert it does NOT come back:
#: re-pinning the check to another log string would reintroduce the same rot.
RETIRED_MARKER = "Album future completed"

#: ``src_state=$(grep -c "<token>" "$file" ...)`` and ``local file="/app/..."``.
_GREP_TARGET_RE = re.compile(r'grep -c "([^"]+)"\s+"\$file"')
_APP_FILE_RE = re.compile(r'local file="(/app/[^"]+)"')


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _entrypoint_code() -> str:
    """``entrypoint.sh`` with ``#`` comments removed — live code only.

    The script legitimately QUOTES the retired marker in the comment that
    explains why the check was rewritten ("...it looked for the literal
    'Album future completed'..."). Scanning that prose would flag the
    documentation and force the history to be deleted, which is exactly the
    false-positive trap this suite warns about elsewhere. Only live code counts.

    ``#`` inside a quoted string would be misread as a comment start; none of
    this script's checks put one there, and being predictable matters more here
    than being general.
    """
    return "\n".join(line.split("#", 1)[0] for line in _read(ENTRYPOINT).splitlines())


def _function_source(path: Path, name: str) -> str:
    """Return the source of a top-level function, without importing the module."""
    source = _read(path)
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{path.name} has no top-level function named {name!r}")


def test_entrypoint_grep_tokens_still_exist() -> None:
    """Every literal the entrypoint greps for must exist in the file it names.

    A startup self-check is usually written as "grep for the marker that the fix
    added". That is fine until a refactor reworks the code around the marker and
    the literal disappears — then the check reports failure forever, and the
    "stale image" signal it was built to give is drowned out by its own noise.

    NOTE: this matches the ``grep -c "<token>" "$file"`` idiom with ``$file``
    bound by ``local file="/app/..."``. If the script grows a second such pair
    with a different marker, extend this rather than loosening the match.
    """
    entry = _entrypoint_code()
    tokens = _GREP_TARGET_RE.findall(entry)
    app_files = _APP_FILE_RE.findall(entry)

    assert tokens, (
        "entrypoint.sh no longer contains a `grep -c \"<token>\" \"$file\"` "
        "self-check. If the idiom changed, update this guard so the 'self-check "
        "pins a string that a refactor deletes' class stays covered."
    )
    assert app_files, (
        "entrypoint.sh greps a $file but never binds it to an /app/... path, so "
        "this guard cannot tell which file it is checking."
    )

    for raw in app_files:
        rel = raw.removeprefix("/app/").lstrip("/")
        target = REPO_ROOT / rel
        assert target.is_file(), (
            f"entrypoint.sh self-checks {raw}, which does not exist in the repo "
            f"(expected {rel}). A check pointed at a missing path can only warn."
        )
        body = _read(target)
        for token in tokens:
            assert token in body, (
                f"entrypoint.sh greps for {token!r} in {rel}, but that string is "
                "gone from the file. The self-check can never pass again — it "
                "will warn on every boot, which destroys its only purpose "
                "(distinguishing a stale image from a healthy one). Re-point it "
                "at the behaviour that actually changed, or drop it."
            )


def test_scan_unwrap_check_probes_the_collector_function() -> None:
    """The unwrap check must inspect the collector, not the whole module."""
    entry = _entrypoint_code()
    assert COLLECTOR in entry, (
        f"entrypoint.sh no longer imports {COLLECTOR}. The album-Future unwrap "
        "lives in that function, so a probe that does not look at it can pass "
        "or fail for reasons unrelated to the fix."
    )
    assert 'future.result()' in entry, (
        "entrypoint.sh no longer requires `future.result()` — the call that IS "
        "the unwrap — so the check would accept code that lets a raw Future "
        "object escape into the 'Album failed: <Future ...>' report."
    )


def test_scan_unwrap_check_does_not_use_a_retired_log_string() -> None:
    """Pin the BEHAVIOUR, never a log string: the literal must stay retired.

    Checked against the script's LIVE CODE only. The comment explaining why the
    check was rewritten has to be able to name the string it retired.
    """
    assert RETIRED_MARKER not in _entrypoint_code(), (
        f"entrypoint.sh checks for the retired log string {RETIRED_MARKER!r} "
        "again. That message was removed by a refactor while the fix stayed, so "
        "pinning any log literal to it reintroduces the permanent false alarm."
    )


def test_scan_unwrap_check_would_pass_on_this_tree() -> None:
    """Run the shipped predicate: both arms must report PRESENT.

    This mirrors, in Python, exactly what ``verify_scan_unwrap_fix()`` does —
    grep the source for the collector marker, then inspect the collector's own
    source for the per-future unwrap. If this fails, the container would boot
    with a false "Scan unwrap fix NOT VERIFIED" warning; if the collector ever
    stops unwrapping, this fails for the right reason instead.

    The scan-runner text is read RAW (not comment-stripped) so this stays a
    faithful mirror of the shell ``grep``, which does not strip comments either.
    """
    source = _read(SCAN_RUNNER)

    # Arm 1 — the greppable marker used by the shell check.
    assert "as_completed" in source, (
        f"{SCAN_RUNNER_REL} no longer contains 'as_completed'. entrypoint.sh "
        "greps that token, so the check would report collector_source=0 and "
        "warn on every boot."
    )

    # Arm 2 — what a fresh import of the collector would see.
    fn_source = _function_source(SCAN_RUNNER, COLLECTOR)
    assert "as_completed" in fn_source, (
        f"{COLLECTOR} no longer iterates completed futures."
    )
    assert "future.result()" in fn_source, (
        f"{COLLECTOR} no longer calls .result() on each completed future. That "
        "call is the 'Album failed: <Future ...>' fix: without it a worker "
        "failure is reported as the Future object instead of the exception."
    )
    assert "except" in fn_source, (
        f"{COLLECTOR} unwraps futures without guarding the call, so one failed "
        "track would abort collection for the whole album."
    )
