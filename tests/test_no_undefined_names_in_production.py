"""Guard: production code must have no UNDEFINED NAMES.

WHY THIS EXISTS
---------------
A crash shipped that nothing in the suite could catch:

    Artist scan failed: cannot access local variable '_singles_pass' where it
    is not associated with a value

``run_scan`` read ``_singles_pass`` ~30 lines BEFORE binding it. Because the
name is assigned somewhere in the function, Python treats it as a local, so
the early read raised ``UnboundLocalError`` — and because ``force=True``
short-circuits past the read, only NORMAL scans crashed. Every artist in a
58-artist scan reported "failed" while the scan marched on, so the collection
was silently never scanned.

``tests/test_scan_singles_pass_is_bound_before_use.py`` pins that one fix.
This file closes the CLASS: pyflakes reports ``undefined name`` for this exact
pattern (verified — it flags line 1189 of the pre-fix file and reports the
fixed file clean), so running it over the production tree turns "we hope
nobody does this again" into a test failure.

WHY PYFLAKES AND NOT OUR OWN AST WALKER
---------------------------------------
A hand-rolled read-before-bind checker was written first and produced 11
findings, ALL false positives — comprehension targets (``for name, info in
...``) and ``except Exception as exc`` read-ordering artefacts. pyflakes
understands scoping natively and produced 2 REAL bugs that the hand-rolled
version missed entirely:

  * ``services/metadata/artist_metadata_service.py`` used
    ``escape_lucene_special_chars`` but imported it only inside a DIFFERENT
    function, so the MusicBrainz half of the artist-ID lookup raised
    NameError on every call. It was swallowed by ``except Exception`` at
    DEBUG level, so the lookup silently returned an empty artist MBID.
  * ``routes/logs.py`` had an unreachable ``return jsonify(result)`` after a
    return, referencing a name that never existed.

So the authoritative tool is used rather than a bespoke approximation.

SCOPE
-----
``services/``, ``db/``, ``helpers/``, ``routes/``, ``api_clients/`` — the code
that runs. ``old_system/`` is a frozen reference snapshot and is skipped.

Only ``undefined name`` is enforced. Style findings (unused imports,
shadowed names) are NOT failed on: they are real but not breaking, and gating
on them would make this test noise that gets disabled.
"""

from __future__ import annotations

import re

import pyflakes.api
import pyflakes.reporter
import pytest

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Production packages. ``tests/`` is deliberately excluded — a test helper
#: referenced before definition in a fixture is a test-authoring issue, not a
#: runtime crash in the shipped application.
PRODUCTION_PACKAGES: tuple[str, ...] = (
    "services",
    "db",
    "helpers",
    "routes",
    "api_clients",
)

#: Frozen snapshot; never imported by the application.
SKIP_DIRS = frozenset({"old_system", ".venv", "venv", "node_modules", ".git", "__pycache__"})

#: The finding this guard is about.
_ENFORCED = "undefined name"

#: pyflakes cannot analyse a module that does ``import *``, so it emits a
#: NOTICE (``unable to detect undefined names``) rather than a finding. A
#: notice is not a defect, so it must not fail the build. It IS worth knowing
#: about: a star-import is a blind spot where an undefined name could hide, so
#: the notice is asserted separately in
#: :meth:`TestKnownBlindSpots.test_star_import_blind_spots_are_known`.
_NOTICE = "unable to detect undefined names"


class _CollectingReporter(pyflakes.reporter.Reporter):
    """Capture pyflakes output instead of writing it to stderr/stdout."""

    def __init__(self) -> None:
        super().__init__(self, self)
        self.problems: list[str] = []

    def write(self, message: str) -> None:  # noqa: D102 - Reporter interface
        text = (message or "").strip()
        if text:
            self.problems.append(text)

    def flush(self) -> None:  # noqa: D102 - Reporter interface
        pass


def _production_files() -> list[Path]:
    files: list[Path] = []
    for package in PRODUCTION_PACKAGES:
        root = REPO_ROOT / package
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT)
            if SKIP_DIRS & set(rel.parts):
                continue
            files.append(path)
    return files


def _run_pyflakes(paths: list[Path]) -> list[str]:
    reporter = _CollectingReporter()
    for path in paths:
        try:
            pyflakes.api.checkPath(str(path), reporter)
        except Exception as exc:  # pragma: no cover - pyflakes internal error
            reporter.problems.append(f"{path}: pyflakes could not analyse ({exc})")
    return reporter.problems


def _undefined_name_findings(problems: list[str]) -> list[str]:
    """Actual undefined-name findings, excluding the star-import notice."""
    return [
        p for p in problems
        if _ENFORCED in p and _NOTICE not in p
    ]


def test_pyflakes_is_available():
    """Guard the guard: without pyflakes the assertions are vacuous."""
    assert pyflakes.api is not None, (
        "pyflakes must be importable; it is a test dependency for this guard"
    )


def test_the_scan_finds_production_files():
    """Guard the guard: a mis-scoped walk would make the check pass trivially."""
    files = _production_files()
    assert len(files) > 100, (
        f"only {len(files)} production python files found — the walk is broken, "
        "so the undefined-name check proves nothing"
    )
    names = {f.name for f in files}
    # These two are load-bearing for the bug this file was written for.
    assert "scan_stage_runner.py" in names
    assert "artist_metadata_service.py" in names


def test_no_undefined_names_in_production_code():
    """THE REQUIREMENT: no shipped module may reference an undefined name.

    This is what would have failed on the ``_singles_pass`` crash before it
    reached a scan, and it is what found the two other latent NameErrors.
    """
    files = _production_files()
    problems = _run_pyflakes(files)

    offenders = _undefined_name_findings(problems)
    assert not offenders, (
        "production code references undefined names. A name that is assigned "
        "somewhere in a function is treated as LOCAL by Python, so reading it "
        "earlier raises UnboundLocalError and aborts the caller — that is the "
        "'_singles_pass' scan crash. A name that is never bound anywhere raises "
        "NameError, which a broad `except Exception` can hide at DEBUG level. "
        f"Offending site(s):\n  " + "\n  ".join(offenders)
    )


class TestKnownBlindSpots:
    """``import *`` modules cannot be analysed — record them explicitly.

    A star-import module is exempt from the check, so a NEW one appearing is a
    loss of coverage worth noticing rather than silence. Pinning the known set
    means adding another is a deliberate decision.
    """

    #: Proxy modules that re-export a unified implementation wholesale.
    ALLOWED_STAR_IMPORT_MODULES: frozenset[str] = frozenset({
        "services/downloads/download_processing_service.py",
    })

    def test_star_import_blind_spots_are_known(self):
        files = _production_files()
        problems = _run_pyflakes(files)
        blind: set[str] = set()
        for p in problems:
            if _NOTICE not in p:
                continue
            # "<path>:<line>:<col>: 'from x import *' used; unable to ..."
            # Format: "<path>:<line>:<col>: 'from x import *' used; unable ..."
            # ⚠️ Match the trailing ":<line>:<col>: " rather than splitting on
            # the first colon — a Windows path starts with a drive letter
            # ("C:\..."), so a naive split yields just "C".
            m = re.match(r"^(?P<path>.+?):\d+:\d+:\s", p)
            raw = m.group("path") if m else p
            try:
                blind.add(str(Path(raw).relative_to(REPO_ROOT)).replace("\\", "/"))
            except ValueError:
                blind.add(Path(raw).name)

        unexpected = blind - self.ALLOWED_STAR_IMPORT_MODULES
        assert not unexpected, (
            "new star-import module(s) added — pyflakes cannot detect undefined "
            "names there, so the undefined-name guard does not cover them. "
            "Replace the star-import with explicit names, or add the module "
            f"here if the blind spot is deliberate: {sorted(unexpected)}"
        )


class TestTheGuardDetectsTheRealBug:
    """Mutation check: the guard must fail on the actual defect."""

    def test_a_read_before_bind_is_reported(self, tmp_path: Path):
        """Reproduce the ``_singles_pass`` shape and require a finding."""
        victim = tmp_path / "victim.py"
        victim.write_text(
            "def run_scan(force=False):\n"
            "    label = 'Forced' if force else ('Singles' if _singles_pass else 'Normal')\n"
            "    _singles_pass = True\n"
            "    return label\n",
            encoding="utf-8",
        )
        problems = _run_pyflakes([victim])
        assert any(_ENFORCED in p and "_singles_pass" in p for p in problems), (
            "pyflakes did not report the read-before-bind shape, so this guard "
            f"cannot detect the reported crash. Findings: {problems}"
        )

    def test_a_never_bound_name_is_reported(self, tmp_path: Path):
        """The ``escape_lucene_special_chars`` shape: never imported at all."""
        victim = tmp_path / "victim2.py"
        victim.write_text(
            "def lookup(artist):\n"
            "    return f'artist:\"{escape_lucene_special_chars(artist)}\"'\n",
            encoding="utf-8",
        )
        problems = _run_pyflakes([victim])
        assert any(
            _ENFORCED in p and "escape_lucene_special_chars" in p for p in problems
        ), (
            "pyflakes did not report a never-bound name, so the second class of "
            f"bug would slip through. Findings: {problems}"
        )

    def test_the_current_tree_is_clean(self):
        """Sanity: the fixes for the two found bugs are actually in place."""
        files = _production_files()
        problems = _undefined_name_findings(_run_pyflakes(files))
        assert not problems, (
            "regressions appeared in the undefined-name check during this run: "
            + "\n  ".join(problems)
        )
