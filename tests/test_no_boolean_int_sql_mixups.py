"""Guard: no SQL mixes a BOOLEAN column with an INTEGER literal.

THE BUG THIS EXISTS FOR
-----------------------
A startup repair failed on PostgreSQL with:

    ERROR: COALESCE types boolean and integer cannot be matched

    UPDATE tracks
    SET is_cover = 1, ...
    WHERE is_cover_reason = 'cover attribution removed from title'
      AND COALESCE(is_cover, 0) = 0              -- is_cover is BIGINT  -> fine
      AND COALESCE(cover_manual_override, 0) = 0 -- BOOLEAN             -> FAILS

`cover_manual_override` is BOOLEAN, and COALESCE requires every argument to
share one type, so `COALESCE(<boolean>, 0)` is rejected outright. The same
mistake existed in two more places:

  * `services/enrichment/cover_detector_impl.py` — clearing an unconfirmed
    cover verdict, so the deep detector's own cleanup silently did nothing.
  * `routes/misc_routes.py` — the sandbox metrics query, which would 500.

WHY CI COULD NOT CATCH IT
-------------------------
The suite runs against SQLite (see `tests/conftest.py`), and SQLite is loosely
typed: it accepts `COALESCE(boolean, 0)` and compares boolean to integer without
complaint. There is no Postgres-backed test to catch a dialect-specific type
error, so these only ever surfaced in production — the reported error is a
Postgres server log, not a test failure.

This guard closes that gap WITHOUT needing a database: it reads the column types
from the schema registry and scans the SQL in the source.

WHAT IS ENFORCED
----------------
  * COALESCE(<boolean column>, <numeric literal>)
  * <boolean column> = 0 / = 1 (and !=, <>, IN (0,1))

The correct boolean spellings (`= TRUE`, `= FALSE`, `IS NOT TRUE`,
`COALESCE(<boolean>, FALSE)`) are NOT flagged, so the fix cannot be reverted
into a different failure.

WHAT IS DELIBERATELY *NOT* FLAGGED
----------------------------------
Python KEYWORD arguments that happen to share a column's name, e.g.

    update_queue_item(queue_id, copied_individually=1, ...)

That looks like a mix-up but is not one: `db/repositories/queue.py` coerces the
value with `bool(...)` before binding it, so the driver sends a real boolean.
Only SQL TEXT is scanned — a filter that also removes the false positives from
comprehension targets and dict literals. Flagging it would mean editing six
correct call sites, which is how a guard earns its way into being switched off.
"""

from __future__ import annotations

import re
import sys

import pytest

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: This module's own name, for monkeypatching module-level helpers. Using the
#: literal string meant a typo silently patched nothing (caught by the mutation
#: tests below failing rather than passing).
_THIS_MODULE = __name__

PRODUCTION_PACKAGES = ("services", "db", "helpers", "routes", "api_clients", "migrations")
SKIP_DIRS = frozenset({"old_system", ".venv", "venv", "node_modules", ".git", "__pycache__"})

_COLUMN_RE = re.compile(r'"(\w+)"\s*:\s*"([A-Z][A-Z0-9 ()]*)"')
_MAPPED_RE = re.compile(r"(\w+)\s*:\s*Mapped\[[^\]]*\]\s*=\s*mapped_column\(([^)]*)\)")

#: Python comments are stripped before matching. A FIX's own explanatory comment
#: quotes the broken pattern (to record why it was wrong), and matching that text
#: reports false positives — a trap this session hit several times. The '#' must
#: not be inside a string literal, so a line is only treated as commented when it
#: contains no quote characters.
_PY_COMMENT = re.compile(r"(?<![\"'])#(?!\{).*$", re.M)

_COALESCE = re.compile(r"COALESCE\s*\(\s*([\w.]+)\s*,\s*([^)]*?)\s*\)", re.I)
_COMPARE = re.compile(r"\b(\w+)\s*(?:=|!=|<>)\s*([01])\b")
_IN_LIST = re.compile(r"\b(\w+)\s+IN\s*\(\s*([01](?:\s*,\s*[01])*)\s*\)", re.I)


def _strip_comments(text: str) -> str:
    """Blank comments while preserving line numbering exactly."""
    out: list[str] = []
    for line in text.splitlines():
        if "#" in line and '"' not in line and "'" not in line:
            line = line[: line.index("#")]
        out.append(line)
    return "\n".join(out)


def _boolean_columns() -> set[str]:
    """BOOLEAN column names, from db/schema.py and db/models.py.

    Two sources on purpose: the registry drives DDL/bootstrap and the models
    drive the ORM, and a column declared boolean in only one of them is itself a
    drift worth noticing. Unioning them means the guard errs toward catching a
    mix-up rather than toward missing one.
    """
    cols: set[str] = set()

    schema = REPO_ROOT / "db" / "schema.py"
    if schema.is_file():
        for m in _COLUMN_RE.finditer(schema.read_text(encoding="utf-8")):
            if m.group(2).startswith("BOOLEAN"):
                cols.add(m.group(1))

    models = REPO_ROOT / "db" / "models.py"
    if models.is_file():
        for m in _MAPPED_RE.finditer(models.read_text(encoding="utf-8")):
            if "Boolean" in m.group(2):
                cols.add(m.group(1))

    return cols


def _production_files() -> list[Path]:
    files: list[Path] = []
    for package in PRODUCTION_PACKAGES:
        root = REPO_ROOT / package
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if SKIP_DIRS & set(path.relative_to(REPO_ROOT).parts):
                continue
            files.append(path)
    return files


def _find_mixups(files: list[Path] | None = None) -> list[tuple[str, int, str, str]]:
    """``[(relpath, line, kind, snippet)]`` for every boolean/int mix-up.

    ``files`` defaults to the production tree. The mutation tests pass a single
    throwaway file rather than monkeypatching, so they exercise this SAME code
    path with no indirection to get wrong.
    """
    booleans = _boolean_columns()
    findings: list[tuple[str, int, str, str]] = []

    for path in (files if files is not None else _production_files()):
        try:
            text = _strip_comments(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        # Cheap pre-filter. ⚠️ It must list EVERY construct the scan below
        # looks for: an earlier version required "COALESCE" or " IN (" and so
        # silently skipped the `boolean_col = 0` form entirely — a coverage
        # hole caught by the mutation tests, not by reading the code.
        upper = text.upper()
        if not any(tok in upper for tok in ("COALESCE", " IN (", "WHERE", "SET ", "UPDATE ")):
            continue

        rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/") if path.is_relative_to(REPO_ROOT) else str(path)
        for lineno, line in enumerate(text.splitlines(), 1):
            # ⚠️ ONLY scan lines that are SQL. A Python keyword argument whose
            # name matches a column (`copied_individually=1`) is NOT a mix-up:
            # the repository coerces it with bool() before binding. Six correct
            # call sites look identical to the defect, so a name-only match
            # would produce six false positives and make this guard unusable.
            if not _sql_context(line):
                continue

            for m in _COALESCE.finditer(line):
                col = m.group(1).split(".")[-1]
                arg = m.group(2).strip()
                if col in booleans and arg in ("0", "1"):
                    findings.append(
                        (rel, lineno, "COALESCE(boolean, int)",
                         f"COALESCE({col}, {arg})")
                    )
            for m in _COMPARE.finditer(line):
                if m.group(1) in booleans:
                    findings.append(
                        (rel, lineno, "boolean = int", f"{m.group(1)} = {m.group(2)}")
                    )
            for m in _IN_LIST.finditer(line):
                if m.group(1) in booleans:
                    findings.append(
                        (rel, lineno, "boolean IN (int)", f"{m.group(1)} IN ({m.group(2)})")
                    )

    return findings


_SQL_KEYWORDS = (
    "SELECT", "UPDATE", "INSERT", "DELETE", "WHERE", "SET ",
    "COALESCE(", " AND ", " OR ", "FROM ",
)


def _sql_context(line: str) -> bool:
    """True when the line looks like SQL rather than Python call arguments.

    A Python keyword argument (`copied_individually=1`) contains no SQL keyword,
    so it is skipped. A real statement always carries at least one, because the
    constructs being checked (COALESCE, WHERE, SET, AND/OR) cannot appear in a
    bare assignment.
    """
    upper = line.upper()
    if "COALESCE(" in upper:
        return True
    return any(kw in upper for kw in _SQL_KEYWORDS)


class TestNoBooleanIntegerMixups:

    def test_the_guard_knows_the_boolean_columns(self):
        """Guard the guard: with no boolean columns the check is vacuous."""
        booleans = _boolean_columns()
        assert len(booleans) >= 5, (
            f"only {len(booleans)} boolean columns discovered — the schema read "
            "is broken, so the mix-up check would pass trivially"
        )
        # The three columns involved in the real defect.
        assert "cover_manual_override" in booleans
        assert "is_single" in booleans
        assert "mbid_manual_override" in booleans

    def test_no_boolean_column_is_combined_with_an_integer(self):
        """THE REQUIREMENT: PostgreSQL rejects these; SQLite hid them."""
        findings = _find_mixups()
        detail = "\n  ".join(
            f"{path}:{line}  [{kind}]  {snippet}" for path, line, kind, snippet in findings
        )
        assert not findings, (
            "SQL mixes a BOOLEAN column with an INTEGER literal. PostgreSQL "
            "rejects this ('COALESCE types boolean and integer cannot be "
            "matched', or 'operator does not exist: boolean = integer'), while "
            "SQLite — which the test suite uses — accepts it, so it only fails "
            "in production.\n\n  Use the boolean spelling instead: "
            "COALESCE(col, FALSE), col IS NOT TRUE, col = TRUE.\n\n  " + detail
        )


class TestTheGuardDetectsTheRealBug:
    """Mutation check: the guard must fail on the exact defect."""

    def test_the_reported_shape_is_detected(self, monkeypatch, tmp_path: Path):
        """Reproduce the reported WHERE clause and require a finding."""
        victim = tmp_path / "victim.py"
        victim.write_text(
            'SQL = ("UPDATE tracks SET is_cover = 1 "\n'
            '       "WHERE is_cover_reason = :r "\n'
            '       "  AND COALESCE(is_cover, 0) = 0 "\n'
            '       "  AND COALESCE(cover_manual_override, 0) = 0")\n',
            encoding="utf-8",
        )
        findings = _find_mixups([victim])
        assert any("cover_manual_override" in f[3] for f in findings), (
            "the guard did not report COALESCE(cover_manual_override, 0) — it "
            f"cannot detect the reported production error. Findings: {findings}"
        )

    def test_the_boolean_spelling_is_not_flagged(self, tmp_path: Path, monkeypatch):
        """The FIX must pass, or the guard blocks its own remedy."""
        victim = tmp_path / "fixed.py"
        victim.write_text(
            'SQL = ("UPDATE tracks SET is_cover = 1 "\n'
            '       "WHERE cover_manual_override IS NOT TRUE "\n'
            '       "  AND COALESCE(is_single, FALSE) = TRUE")\n',
            encoding="utf-8",
        )
        assert _find_mixups([victim]) == [], (
            "the correct boolean spelling was flagged as a mix-up, so the guard "
            "would fight its own fix"
        )

    def test_integer_columns_are_not_flagged(self, tmp_path: Path, monkeypatch):
        """A BIGINT flag may legitimately be COALESCEd with 0."""
        victim = tmp_path / "int_flag.py"
        victim.write_text(
            'SQL = "WHERE COALESCE(is_cover, 0) = 0 AND COALESCE(stars, 0) = 0"\n',
            encoding="utf-8",
        )
        assert _find_mixups([victim]) == [], (
            "an INTEGER column was wrongly flagged — is_cover and stars are "
            "BIGINT/INTEGER, where COALESCE(col, 0) is correct"
        )

    def test_a_commented_out_bad_pattern_is_not_flagged(self, tmp_path: Path, monkeypatch):
        """A fix's explanatory comment quotes the broken pattern.

        Matching comment text is the false-positive trap this session hit
        repeatedly, so it is pinned here explicitly.
        """
        victim = tmp_path / "commented.py"
        victim.write_text(
            "# COALESCE(cover_manual_override, 0) mixes boolean with integer\n"
            'SQL = "WHERE cover_manual_override IS NOT TRUE"\n',
            encoding="utf-8",
        )
        assert _find_mixups([victim]) == [], (
            "a commented-out example was reported as a live defect"
        )

    def test_a_boolean_compared_to_an_integer_is_detected(self, tmp_path: Path, monkeypatch):
        """The `= 0` form fails the same way and must be caught too."""
        victim = tmp_path / "cmp.py"
        victim.write_text(
            'SQL = "SELECT 1 FROM tracks WHERE is_single = 0"\n',
            encoding="utf-8",
        )
        assert _find_mixups([victim]), (
            "`boolean_column = 0` was not flagged, though PostgreSQL rejects it "
            "with 'operator does not exist: boolean = integer'"
        )
