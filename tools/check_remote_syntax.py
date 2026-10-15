#!/usr/bin/env python3
"""Verify that a git ref's Python files all parse — BEFORE it breaks the app.

Why this exists
---------------
On 2026-10-15 `origin/develop` became unimportable. Three source files carried
a mangled duplicate of code that was already present, spliced into the middle of
an unrelated expression:

    api_clients/discogs_http.py:230            expected 'except' or 'finally' block
    routes/album_routes.py:967                 invalid syntax
    services/popularity/stages/finalise_stage.py:3942   unexpected indent

Nothing caught it. `tests/test_no_python_syntax_errors.py` parses the WORKING
TREE, and the damage only ever existed on the remote — it arrived through the
workspace auto-commit, which writes straight to `origin/develop` and therefore
runs no local hooks and no local tests.

The failure mode is always the same shape: a file whose remote copy is STALE
relative to the branch gets its old content spliced back in at commit time. It
shows up as a syntax error far more often than as a silent logic change, which
makes a parse check a very high-value, very cheap guard.

Usage
-----
    python tools/check_remote_syntax.py                    # origin/develop
    python tools/check_remote_syntax.py <ref>              # any ref
    python tools/check_remote_syntax.py --staged           # what you are about to commit
    python tools/check_remote_syntax.py --worktree         # files on disk

Exit code is 0 when everything parses, 1 otherwise, so it can gate a hook or be
read by an agent. Each failure names the file, the line and the message, and --
because the inserted text is always a DUPLICATE of something already in the
file -- it also reports how many times the reported line's first few tokens
occur in the file. A count above 1 on a syntax error is the signature of this
exact failure mode.

Output is deliberately ASCII-only: this repo has raised ``UnicodeEncodeError``
from structlog printing emoji to a cp1252 console, and a diagnostic tool that
crashes while reporting a problem is worse than no tool. The module docstring is
the one exception -- it is never printed.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Vendored / archived trees: `old_system/` is a frozen reference snapshot that
#: is deliberately never updated, so a parse failure there is not actionable.
SKIP_PREFIXES = ("old_system/", "old_system\\", ".venv/", "node_modules/")


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout


def _python_files(ref: str) -> list[str]:
    out = _git("ls-tree", "-r", "--name-only", ref)
    return [
        name
        for name in out.split("\n")
        if name.endswith(".py") and not name.startswith(SKIP_PREFIXES)
    ]


def _signature_hits(source: str, line_number: int) -> int:
    """How many times the failing line's opening tokens appear in the file.

    A syntax error whose line is a near-duplicate of another line in the same
    file is the fingerprint of the stale-copy splice this script guards against.
    Returns 0 when the line cannot be read (the count is a hint, not a verdict).
    """
    lines = source.splitlines()
    if not (1 <= line_number <= len(lines)):
        return 0
    tokens = lines[line_number - 1].strip()[:40]
    if not tokens:
        return 0
    return sum(1 for line in lines if tokens and tokens in line)


def check_ref(ref: str) -> list[tuple[str, int, str, int]]:
    """Parse every Python file at ``ref``. Returns (path, line, message, dupes)."""
    failures: list[tuple[str, int, str, int]] = []
    names = _python_files(ref)

    for name in names:
        source = _git("show", f"{ref}:{name}")
        try:
            ast.parse(source)
        except SyntaxError as exc:
            failures.append(
                (name, exc.lineno or 0, exc.msg, _signature_hits(source, exc.lineno or 0))
            )
        except ValueError as exc:
            # e.g. source containing null bytes
            failures.append((name, 0, str(exc), 0))

    print(f"[check_remote_syntax] {ref}: {len(names)} python file(s) checked")
    return failures


def check_worktree() -> list[tuple[str, int, str, int]]:
    failures: list[tuple[str, int, str, int]] = []
    count = 0
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith(SKIP_PREFIXES) or ".venv" in rel or "node_modules" in rel:
            continue
        count += 1
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            failures.append((rel, 0, str(exc), 0))
            continue
        try:
            ast.parse(source)
        except SyntaxError as exc:
            failures.append(
                (rel, exc.lineno or 0, exc.msg, _signature_hits(source, exc.lineno or 0))
            )

    print(f"[check_remote_syntax] worktree: {count} python file(s) checked")
    return failures


def check_staged() -> list[tuple[str, int, str, int]]:
    """Only the files staged for commit — what a pre-commit hook wants."""
    out = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    names = [
        name
        for name in out.split("\n")
        if name.endswith(".py") and not name.startswith(SKIP_PREFIXES)
    ]
    failures: list[tuple[str, int, str, int]] = []

    for name in names:
        # Read the STAGED blob, not the worktree copy: they differ when a hook
        # or a formatter has touched the file since `git add`.
        blob = _git("show", f":{name}")
        try:
            ast.parse(blob)
        except SyntaxError as exc:
            failures.append(
                (name, exc.lineno or 0, exc.msg, _signature_hits(blob, exc.lineno or 0))
            )

    print(f"[check_remote_syntax] staged: {len(names)} python file(s) checked")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "ref",
        nargs="?",
        default="origin/develop",
        help="git ref to check (default: origin/develop)",
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="check the staged blobs instead of a ref (for a pre-commit hook)",
    )
    parser.add_argument(
        "--worktree",
        action="store_true",
        help="check the files on disk instead of a ref",
    )
    args = parser.parse_args(argv)

    if args.staged:
        failures = check_staged()
    elif args.worktree:
        failures = check_worktree()
    else:
        failures = check_ref(args.ref)

    if not failures:
        print("[check_remote_syntax] OK - everything parses")
        return 0

    print(f"[check_remote_syntax] {len(failures)} FAILURE(S):\n", file=sys.stderr)
    for name, line, message, dupes in failures:
        print(f"  {name}:{line}: {message}", file=sys.stderr)
        if dupes > 1:
            print(
                f"      -> that line's opening tokens appear {dupes}x in this file "
                "- the signature of a stale copy spliced back in, not a typo",
                file=sys.stderr,
            )
    print(
        "\nIf this is a ref, do NOT build on it: restore the affected files to "
        "their last-good revision and commit the repair first.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
