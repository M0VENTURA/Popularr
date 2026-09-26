"""Guard: every {% from "..." import name %} must actually RESOLVE.

THE BUG THIS EXISTS FOR
-----------------------
`test_site/templates/Pages/downloads/monitor.html` did:

    {% from "_album_category_section.html" import render_album_row %}

with the name at the TEMPLATE ROOT. The file lives at
`components/_album_category_section.html`, and Jinja's FileSystemLoader searches
`templates/` only — so the import raised TemplateNotFound the moment that page
rendered. Verified by rendering a minimal template containing just the import:
it fails in BOTH the rebuilt and the live tree.

WHY IT WENT UNNOTICED
---------------------
The import is evaluated at RENDER time, not compile time, so a plain
`env.get_template(...)` succeeds and only the real page render fails. And the
page in question is in `_SHADOWED_TEMPLATES`, so the LIVE copy is served instead
and the break was masked.

WHY A GUARD RATHER THAN A ONE-LINE FIX
--------------------------------------
The spelling is easy to get wrong (it resolved in old_system, where the file sat
at the template root), and the failure mode is a 500 on page render rather than
an error at startup. This pins, for EVERY template in BOTH trees:

  1. each imported macro name is ACTUALLY DEFINED in the target file, and
  2. the target file RESOLVES through the app's real loader.

Comments are stripped first: this session repeatedly produced false findings by
matching text inside `{# #}` comments, and several macro files document their
own import examples.
"""

from __future__ import annotations

import re

import pytest

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_TEMPLATES = REPO_ROOT / "test_site" / "templates"
LIVE_TEMPLATES = REPO_ROOT / "templates"

_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)

#: ``{% from "x.html" import a, b as c %}`` — the ``-`` trims are optional.
_IMPORT_RE = re.compile(
    r"""\{%-?\s*from\s+["'](?P<target>[^"']+)["']\s+import\s+(?P<names>[^%]+?)\s*-?%\}"""
)
_MACRO_RE = re.compile(r"\{%-?\s*macro\s+([A-Za-z_$][\w$]*)\s*\(")


def _strip(text: str) -> str:
    return _HTML_COMMENT.sub("", _JINJA_COMMENT.sub("", text))


def _templates() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for label, base in (("test_site", TEST_TEMPLATES), ("live", LIVE_TEMPLATES)):
        if base.is_dir():
            for path in sorted(base.rglob("*.html")):
                out.append((label, path))
    return out


def _search_paths() -> list[Path]:
    """The loader search order the app builds: rebuilt tree first, live second."""
    return [TEST_TEMPLATES, LIVE_TEMPLATES]


def _resolve(target: str) -> Path | None:
    for base in _search_paths():
        candidate = base / target.replace("/", "/")
        if candidate.is_file():
            return candidate
    return None


def _imports(text: str) -> list[tuple[str, list[str]]]:
    """``[(target, [macro names])]`` from comment-stripped template source."""
    found: list[tuple[str, list[str]]] = []
    for m in _IMPORT_RE.finditer(_strip(text)):
        names: list[str] = []
        for raw in m.group("names").split(","):
            token = raw.strip()
            if not token:
                continue
            # `X as Y` imports X.
            names.append(token.split()[0])
        found.append((m.group("target"), names))
    return found


class TestEveryMacroImportResolves:

    def test_the_scan_finds_templates(self):
        """Guard the guard: a mis-scoped walk would make every check vacuous."""
        templates = _templates()
        assert len(templates) > 40, (
            f"only {len(templates)} templates found — the walk is broken"
        )
        names = {p.name for _, p in templates}
        assert "_album_category_section.html" in names
        assert "_release_section.html" in names

    def test_every_imported_template_exists(self):
        """THE REPORTED BUG: a bare-root name cannot resolve."""
        broken: list[str] = []
        for label, path in _templates():
            for target, _names in _imports(path.read_text(encoding="utf-8")):
                if _resolve(target) is None:
                    broken.append(
                        f"{label}: {path.relative_to(REPO_ROOT)} imports "
                        f"{target!r}, which does not resolve through the "
                        f"templates/ loader"
                    )
        assert not broken, (
            "a template import cannot resolve. Jinja's FileSystemLoader searches "
            "templates/ only, so the name must include its subdirectory "
            "(components/...). The import is evaluated at RENDER time, so this "
            "surfaces as a 500 on the page rather than at startup.\n  "
            + "\n  ".join(broken)
        )

    def test_every_imported_macro_is_defined(self):
        """An import of a name the target does not define raises ImportError."""
        broken: list[str] = []
        for label, path in _templates():
            for target, names in _imports(path.read_text(encoding="utf-8")):
                resolved = _resolve(target)
                if resolved is None:
                    continue  # reported by the test above
                defined = set(_MACRO_RE.findall(_strip(resolved.read_text(encoding="utf-8"))))
                for name in names:
                    if name not in defined:
                        broken.append(
                            f"{label}: {path.relative_to(REPO_ROOT)} imports "
                            f"{name!r} from {target!r}, but that file defines "
                            f"{sorted(defined) or 'no macros'}"
                        )
        assert not broken, (
            "a template imports a macro the target does not define — this raises "
            "ImportError when the page renders.\n  " + "\n  ".join(broken)
        )


class TestTheGuardDetectsTheRealBug:
    """Mutation check: the guard must fail on the exact defect."""

    def test_the_bare_root_spelling_is_rejected(self):
        """The precise regression: root-relative import of a components/ file."""
        assert _resolve("_album_category_section.html") is None, (
            "'_album_category_section.html' unexpectedly resolves at the template "
            "root — if the file was moved there, update this guard"
        )
        assert _resolve("components/_album_category_section.html") is not None, (
            "the macro file must exist under components/ for this guard to mean "
            "anything"
        )

    def test_imports_are_extracted_from_source(self):
        """The extractor must see real imports (and only real ones)."""
        sample = (
            '{% from "components/_album_category_section.html" import render_album_row %}\n'
            '{# {% from "_album_category_section.html" import render_album_row %} #}\n'
        )
        found = _imports(sample)
        assert found == [("components/_album_category_section.html", ["render_album_row"])], (
            f"the extractor mis-parsed a commented-out import: {found}"
        )

    def test_a_missing_macro_is_detected(self, tmp_path: Path):
        """A name that exists in no target must be reported, not ignored."""
        target = tmp_path / "thing.html"
        target.write_text("{% macro real_macro() %}x{% endmacro %}", encoding="utf-8")
        macros = set(_MACRO_RE.findall(_strip(target.read_text(encoding="utf-8"))))
        assert "real_macro" in macros
        assert "not_a_macro" not in macros
