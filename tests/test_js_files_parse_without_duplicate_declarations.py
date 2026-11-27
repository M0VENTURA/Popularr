"""No JS file may declare the same name twice in one scope.

⚠️ WHY THIS EXISTS — AND WHY THE EXISTING TESTS COULD NOT CATCH IT.

Reported: *"a lot of the sections in the album page aren't working when
pressed"*, with the browser console showing

    album.js:2390 Uncaught SyntaxError: Identifier 'trackArtist' has already been declared
    Uncaught ReferenceError: openAlbumLookupModal is not defined

A duplicate ``const`` is a **parse-time** SyntaxError: the browser discards the
WHOLE file, so nothing in it is defined — every section handler, every module
global, and every inline ``onclick`` fails. The symptom looks like dozens of
unrelated buttons being broken.

The Python-side guards all missed it because they inspect source TEXT. A
``btn.textContent`` assertion, a string-presence check, and a Jinja-token scan
all pass happily on a file that cannot be parsed. Only a real parse catches this
class of defect, so this test parses.

esprima 4 understands ES2017; the two ES2020 operators (``?.`` and ``??``) are
rewritten for PARSING only. This test is about declarations, so the rewrite
cannot hide the thing it looks for.
"""

from __future__ import annotations

import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
JS_ROOTS = (
    REPO_ROOT / "test_site" / "static" / "js",
)

esprima = pytest.importorskip("esprima", reason="esprima is needed to parse JS")

FUNCS = frozenset({
    "FunctionDeclaration", "FunctionExpression", "ArrowFunctionExpression",
})

#: Nodes that open a NEW lexical block. A ``const`` inside one is a different
#: binding from the same name in a sibling block, which is legal and common —
#: two ``if`` branches each declaring ``const heading``.
BLOCK_SCOPES = frozenset({
    "BlockStatement", "ForStatement", "ForOfStatement", "ForInStatement",
    "SwitchStatement", "CatchClause",
})


def _js_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for root in JS_ROOTS:
        if root.is_dir():
            files.extend(sorted(root.rglob("*.js")))
    return files


def _parseable(source: str) -> str:
    """Lower the two ES2020 operators so esprima 4 (ES2017) can parse.

    Order matters: optional CALLS (``?.()``) must be rewritten before the bare
    optional member access (``?.``), or ``a?.b?.()`` becomes the invalid
    ``a.b.()``. Nullish coalescing is a plain logical-or substitution here —
    this file only inspects declarations, never evaluates them.
    """
    return (
        source.replace("?.(", "(")
        .replace("?.[", "[")
        .replace("?.", ".")
        .replace("??", "||")
    )


def _duplicates(path: pathlib.Path) -> list[str]:
    """Same-scope redeclarations in one file, as human-readable strings."""
    source = path.read_text(encoding="utf-8")
    tree = esprima.parseScript(_parseable(source), {"tolerant": False, "range": True})

    problems: list[str] = []

    def walk(node, scope, is_function_body=False):
        if not isinstance(node, dict):
            return
        node_type = node.get("type")

        if node_type in FUNCS:
            scope = {}
            is_function_body = False
        elif node_type in BLOCK_SCOPES and not is_function_body:
            scope = {}

        if node_type == "VariableDeclaration":
            for declarator in node.get("declarations") or []:
                ident = declarator.get("id", {})
                if ident.get("type") != "Identifier":
                    continue
                name = ident["name"]
                offset = ident.get("range", [0])[0]
                if name in scope:
                    line = source.count("\n", 0, offset) + 1
                    problems.append(
                        f"{path.name}: '{name}' redeclared near line {line}"
                    )
                else:
                    scope[name] = offset

        body_is_this_function = node_type in FUNCS
        for key, value in node.items():
            if isinstance(value, dict):
                walk(value, scope, body_is_this_function and key == "body")
            elif isinstance(value, list):
                for item in value:
                    walk(item, scope, False)

    walk(tree.toDict(), {})
    return problems


@pytest.mark.parametrize("path", _js_files(), ids=lambda p: p.name)
def test_no_duplicate_declarations(path: pathlib.Path) -> None:
    """A redeclaration here kills the file at parse time in the browser."""
    problems = _duplicates(path)
    assert not problems, (
        "duplicate declaration(s) — the browser discards the WHOLE file on a "
        "SyntaxError, so every handler and inline onclick in it stops working:\n  "
        + "\n  ".join(problems)
    )


def test_the_checker_catches_a_redeclaration(tmp_path: pathlib.Path) -> None:
    """Guard the guard: a checker that cannot fail proves nothing."""
    probe = tmp_path / "probe.js"
    probe.write_text(
        "function f() {\n  const a = 1;\n  const a = 2;\n}\n", encoding="utf-8"
    )
    assert _duplicates(probe), "the checker must catch a same-scope duplicate"


def test_sibling_blocks_may_reuse_a_name(tmp_path: pathlib.Path) -> None:
    """Legal JS must not be reported, or the test is noise people switch off."""
    probe = tmp_path / "ok.js"
    probe.write_text(
        "function f(x) {\n"
        "  if (x) { const heading = 1; use(heading); }\n"
        "  if (!x) { const heading = 2; use(heading); }\n"
        "}\n",
        encoding="utf-8",
    )
    assert _duplicates(probe) == []


def test_the_album_page_parses() -> None:
    """The specific file from the report must parse cleanly end to end."""
    album = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "album.js"
    source = album.read_text(encoding="utf-8")
    esprima.parseScript(_parseable(source), {"tolerant": False, "range": True})
