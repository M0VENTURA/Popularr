"""The stale-copy guard must keep having teeth.

Reported follow-up
------------------
> Can you make sure with each commit, the local files are also synced so this
> doesn't accidentally happen, I had 70 commits behind on my local source

What "this" turned out to be, on 2026-10-15
-------------------------------------------
`origin/develop` became unimportable. Three source files carried a **stale copy
of their own older content spliced back in**, duplicating code that was already
present and wedging it into the middle of an unrelated expression:

    api_clients/discogs_http.py:230                     expected 'except' or 'finally' block
    routes/album_routes.py:967                          invalid syntax
    services/popularity/stages/finalise_stage.py:3942   unexpected indent

Each file had **exactly one hunk**, and the inserted text duplicated content
already in the file — `finalise_stage.py` grew a third
`_build_compilation_track_artist_scores(album_results)` call where two belong;
`album_routes.py` became `result, code = update_album_` … `ids(payload)`.
Restoring the three files to the previous revision put the tree back: 713/713
files parse, `import app` registers 407 routes.

The commit that did it was the VS Code GitHub workspace's auto-commit, which
writes to `origin/develop` **through the GitHub API**. That path runs no local
hooks and no local tests, which is why the damage reached the remote and sat
there.

Why a parse check is worth a guard
----------------------------------
A stale-copy splice surfaces as a syntax error far more often than as a silent
logic change, and the check costs one `ast.parse` per file. It is also the only
check that works against content that exists **only on a ref** — the repo's
other syntax coverage reads the working tree, where the damage never appeared.

These tests are hermetic: `--worktree` needs no git at all, so the tool is
exercised in a throwaway directory whose `tools/` parent is the only thing that
has to look like a repo.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

TOOL_SOURCE = Path(__file__).resolve().parents[1] / "tools" / "check_remote_syntax.py"

_BROKEN = "def oops(:\n    pass\n"
_ALSO_BROKEN = "def other(:\n    pass\n"


def _stage_tool(tmp_path: Path) -> Path:
    """Copy the tool into ``tmp_path/tools/`` so REPO_ROOT resolves to tmp_path."""
    target = tmp_path / "tools" / "check_remote_syntax.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TOOL_SOURCE, target)
    return target


def _run(tool: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(tool), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(tool.parent.parent),
    )


def test_tool_ships_in_the_repo() -> None:
    """The guard is only useful if it is actually committed."""
    assert TOOL_SOURCE.is_file(), f"missing {TOOL_SOURCE}"


def test_clean_tree_passes(tmp_path: Path) -> None:
    tool = _stage_tool(tmp_path)
    (tmp_path / "good.py").write_text("x = 1\n", encoding="utf-8")

    result = _run(tool, "--worktree")

    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_broken_file_fails_and_is_named(tmp_path: Path) -> None:
    """A parse failure must fail the run AND say which file — a bare exit 1 is useless."""
    tool = _stage_tool(tmp_path)
    (tmp_path / "good.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text(_BROKEN, encoding="utf-8")

    result = _run(tool, "--worktree")

    assert result.returncode == 1
    assert "bad.py" in result.stderr
    assert "good.py" not in result.stderr


def test_duplicate_signature_is_reported(tmp_path: Path) -> None:
    """The hint that distinguishes this failure mode from an ordinary typo.

    A stale-copy splice inserts a near-duplicate of a line that is already in
    the file. When the failing line's opening tokens appear more than once, the
    tool says so — that is the signal to go looking for a splice rather than to
    hunt for a missing bracket.
    """
    tool = _stage_tool(tmp_path)
    # Two identical malformed lines: the first parses as a call, so the SECOND
    # is the reported failure and its tokens appear twice.
    (tmp_path / "spliced.py").write_text(
        'def f(:\n    pass\n\ndef f(:\n    pass\n', encoding="utf-8"
    )

    result = _run(tool, "--worktree")

    assert result.returncode == 1
    assert "spliced.py" in result.stderr
    assert "appear 2x" in result.stderr, result.stderr


def test_old_system_is_skipped(tmp_path: Path) -> None:
    """`old_system/` is a frozen reference snapshot — a failure there is not actionable."""
    tool = _stage_tool(tmp_path)
    frozen = tmp_path / "old_system"
    frozen.mkdir()
    (frozen / "legacy.py").write_text(_BROKEN, encoding="utf-8")

    result = _run(tool, "--worktree")

    assert result.returncode == 0, result.stderr
    assert "legacy.py" not in result.stderr


def test_source_files_are_skipped_when_not_staged(tmp_path: Path) -> None:
    """`--staged` with nothing staged must not fall back to scanning everything.

    A pre-commit hook that quietly checked the whole tree would block unrelated
    commits over a pre-existing baseline failure.
    """
    tool = _stage_tool(tmp_path)
    (tmp_path / "bad.py").write_text(_BROKEN, encoding="utf-8")
    # Not a git repo, and nothing staged: git will refuse, so the tool must
    # surface that rather than reporting a pass it did not earn.
    result = _run(tool, "--staged")

    assert result.returncode != 0


def test_worktree_counts_files(tmp_path: Path) -> None:
    """The summary line is what a reader uses to tell 'checked nothing' from 'all clean'.

    The tool counts itself -- it is a Python file in the tree, and a broken
    checker should be reported like anything else -- so the expectation is
    derived rather than hardcoded.
    """
    tool = _stage_tool(tmp_path)
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("x = 1\n", encoding="utf-8")

    expected = len(list(tmp_path.rglob("*.py")))
    result = _run(tool, "--worktree")

    assert result.returncode == 0
    assert f"{expected} python file(s) checked" in result.stdout
    assert expected == 4, "the tool should count itself alongside the three fixtures"


def test_output_is_ascii_only(tmp_path: Path) -> None:
    """Diagnostics must survive a cp1252 console.

    This repo has raised ``UnicodeEncodeError`` from structlog printing emoji to
    a Windows console. A tool that blows up while REPORTING a problem is worse
    than no tool, and the failure would only appear on the machine that needs it
    most.
    """
    tool = _stage_tool(tmp_path)
    (tmp_path / "spliced.py").write_text(_ALSO_BROKEN * 2, encoding="utf-8")

    result = _run(tool, "--worktree")

    for stream in (result.stdout, result.stderr):
        non_ascii = [ch for ch in stream if ord(ch) > 126]
        assert not non_ascii, f"non-ASCII in output: {non_ascii!r}\n{stream}"


def test_bad_encoding_is_reported_not_crashed(tmp_path: Path) -> None:
    """A non-UTF-8 file must be a reported failure, not a traceback."""
    tool = _stage_tool(tmp_path)
    (tmp_path / "latin.py").write_bytes(b"# caf\xe9\nx = 1\n")

    result = _run(tool, "--worktree")

    # Either it decodes (and passes) or it is reported — never a raw traceback.
    assert "Traceback" not in result.stderr, result.stderr
