"""Every Python file in the repo must at least PARSE.

Regression: a one-character edit in ``services/popularity/popularity_math.py``
left an unmatched ``)``::

    return min(1.0, max(0.0, float(effective_median) / float(m_peak))))

The module became unimportable, and because ``scan_stage_runner`` imports it the
failure surfaced far from its cause as::

    [POPULARITY] FATAL IMPORT ERROR: SyntaxError: unmatched ')'
    Artist scan failed: Could not import scanner module
    'services.popularity.scan_stage_runner'

i.e. the ENTIRE artist scan died, on a change that looked like it only touched
an unrelated return statement. Nothing in the suite caught it, because no test
imported the module and a syntax error in a dependency is invisible until
something traverses the import chain.

Why a repo-wide parse test rather than importing every module
------------------------------------------------------------
Importing everything would execute module-level side effects and drag in
optional/network dependencies, making the guard slow and flaky. Parsing with
``ast.parse`` needs no imports, no fixtures and no network, so it can cover the
WHOLE tree cheaply and deterministically -- which is the only way to catch a
break high in the import graph before a scan hits it in production.

``old_system/`` is skipped: it is a frozen reference snapshot that is never
imported by the application.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Frozen historical snapshot - deliberately never imported, so a parse error
# there cannot break the running application.
_SKIP_DIRS = frozenset({"old_system", ".venv", "venv", "node_modules", ".git"})


def _iter_python_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        if _SKIP_DIRS & set(path.relative_to(REPO_ROOT).parts):
            continue
        files.append(path)
    return files


def _python_files() -> list[pathlib.Path]:
    return _iter_python_files()


def test_the_scan_finds_python_files():
    """Guard the guard: if the walk mis-scopes, the parse test is vacuous."""
    files = _python_files()
    assert len(files) > 100, f"only found {len(files)} python files - walk is broken"

    names = {f.name for f in files}
    # These two are load-bearing for the bug this file guards.
    assert "popularity_math.py" in names
    assert "scan_stage_runner.py" in names


def test_no_python_file_has_a_syntax_error():
    """Every live .py file must parse.

    Collects ALL failures rather than stopping at the first, so one run shows
    the complete damage - a batch edit can corrupt several files at once.
    """
    broken: list[str] = []

    for path in _python_files():
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            broken.append(
                f"{path.relative_to(REPO_ROOT)}:{exc.lineno}: {exc.msg}\n"
                f"        >>> {(exc.text or '').rstrip()}"
            )
        except UnicodeDecodeError as exc:
            broken.append(f"{path.relative_to(REPO_ROOT)}: not valid UTF-8 ({exc})")

    assert not broken, (
        "Python file(s) failed to parse. A SyntaxError anywhere in the import "
        "graph surfaces far away as 'Could not import scanner module'.\n\n"
        + "\n".join(broken)
    )


@pytest.mark.parametrize(
    "module_path",
    [
        "services/popularity/popularity_math.py",
        "services/popularity/scan_stage_runner.py",
        "services/popularity/pipeline.py",
        "services/enrichment/musicbrainz_service.py",
    ],
)
def test_scanner_import_chain_parses(module_path: str):
    """Pin the specific chain whose break killed the artist scan.

    ``popularity_math`` -> ``scan_stage_runner`` is imported by
    ``services.popularity.pipeline._load_scanner_module()``; a parse failure
    anywhere in it aborts the whole scan before the first artist.
    """
    path = REPO_ROOT / module_path
    assert path.exists(), f"{module_path} is missing"
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
