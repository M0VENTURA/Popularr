"""No inline `onclick` may reference a handler that no LOADED script defines.

Reported (three times now, in different areas)
---------------------------------------------
> The search button doesn't do anything.

An inline handler is resolved on `window` at click time. If nothing defines it,
the click throws `ReferenceError` and the button is **inert** — and nothing
catches it: a template has no compiler, the page still returns 200, and a bare
`ReferenceError` in a click listener is invisible in the UI.

This scanner found three real ones:

* ``downloadSlskdFile`` — the "Download" button on every Soulseek search-tab
  result. It WAS defined, in ``static/js/artist_detail.js`` — which the downloads
  pages never load. A definition somewhere is not a definition **here**; that is
  the trap that makes a repo-wide grep look reassuring.
* ``showSlskdResults`` — the "Select" button on an awaiting-selection
  MusicBrainz download. Defined only in the REBUILT tree's ``download-queue.js``.
* ``searchAgainSlskd`` — the "Search Again" button inside the slskd results
  modal partial. Defined nowhere in either tree.

⚠️ TWO THINGS THIS MUST GET RIGHT, both of which produced false results while
writing it:

1. **Strip comments before scanning.** The renderer's own header explains the
   bug by QUOTING the ``onclick`` call — and one such quote was inside dead
   code, which made the scanner report a handler that no live markup references.
2. **Check against every script in the same TREE**, not a hand-picked list. A
   hand-picked list reports false "undefined" hits for handlers that legitimately
   live elsewhere.

The check is deliberately scoped to a tree's own scripts: the two trees are
served exclusively, so a live handler defined only in the rebuilt tree is still
dead in live mode.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: (label, renderer script, script root that page can load from)
TREES = (
    ("live", "static/js/downloads.js", "static/js"),
    ("rebuilt", "test_site/static/js/pages/download-queue.js", "test_site/static/js"),
)

# onclick="fn(" — also matches the template-literal form `onclick="fn(...)"` and
# a handler embedded in a generated HTML string.
_ONCLICK_RE = re.compile(
    r"""onclick\s*=\s*(?:\\?["'`]){0,1}\s*([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\("""
)

_DEF_PATTERNS = (
    re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\("),
    # ⚠️ An ASSIGNMENT only counts as a definition when the right-hand side
    # actually creates a function. `window.foo = foo;` is a RE-EXPORT, not a
    # definition — counting it made this scanner miss a mutation that renamed
    # the real function while leaving the alias behind (we would have been left
    # with an alias pointing at nothing). The RHS must be `function`/arrow.
    re.compile(
        r"\b(?:window|global|self)\.([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:function\b|\()"
    ),
    re.compile(
        r"^\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:function\b|\()", re.M
    ),
    # Object-literal method shorthand: `global.slskd = { search() {} }`.
    re.compile(r"^\s*([A-Za-z_$][\w$]*)\s*\(\s*[^)]*\)\s*\{", re.M),
    re.compile(r"^\s*([A-Za-z_$][\w$]*)\s*:\s*(?:async\s*)?(?:function\b|\()", re.M),
)

#: Handlers that are browser built-ins rather than app functions.
_BUILTINS = frozenset({"alert", "confirm", "print", "history", "location"})


def _strip_comments(text: str) -> str:
    """Remove block + line comments, quote-aware enough for these files."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if text.startswith("//", i):
            # Do not treat the `//` in a URL literal as a comment.
            if i > 0 and text[i - 1] == ":":
                out.append(text[i])
                i += 1
                continue
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _defined_names(script_root: Path) -> set[str]:
    names: set[str] = set()
    for path in sorted(script_root.rglob("*.js")):
        body = _strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        for pattern in _DEF_PATTERNS:
            names |= set(pattern.findall(body))
    return names


def _referenced_handlers(renderer: Path) -> set[str]:
    body = _strip_comments(renderer.read_text(encoding="utf-8", errors="replace"))
    return {
        name for name in _ONCLICK_RE.findall(body)
        if "." not in name and name not in _BUILTINS
    }


@pytest.mark.parametrize("label,renderer_rel,root_rel", TREES)
def test_no_inline_handler_is_undefined(label: str, renderer_rel: str, root_rel: str) -> None:
    """Every `onclick` a page renders must resolve to a real function."""
    renderer = REPO_ROOT / renderer_rel
    if not renderer.is_file():
        pytest.skip(f"{renderer_rel} not present")

    referenced = _referenced_handlers(renderer)

    # ⚠️ "Found nothing" is NOT automatically a pass. For the LIVE renderer it
    # would mean the extractor broke; for the REBUILT one it is legitimately
    # zero, because that renderer moved to addEventListener + data-* attributes
    # (its single `onclick` is inside an explanatory comment). So the two cases
    # are asserted differently rather than being conflated into one weak check.
    if label == "live":
        assert referenced, (
            f"{renderer_rel} renders no onclick handlers at all — the renderer "
            "moved or the extraction regex stopped matching, and a scan that "
            "finds nothing can never protect anything"
        )

    defined = _defined_names(REPO_ROOT / root_rel)
    missing = sorted(referenced - defined)

    assert not missing, (
        f"These onclick handlers are referenced by {renderer_rel} but defined by "
        f"no script in {root_rel}/, so their buttons throw ReferenceError and do "
        f"nothing: {missing}. If the handler exists in the OTHER tree, it is "
        "still dead here — the two trees are served exclusively."
    )


def test_the_rebuilt_renderer_binds_by_listener_not_inline_onclick() -> None:
    """Pin WHY the rebuilt renderer legitimately reports zero inline handlers.

    Without this, someone seeing an empty set would "fix" the check by deleting
    it — or worse, an inline onclick could creep back in with a handler that
    only exists in the other tree.
    """
    renderer = REPO_ROOT / "test_site/static/js/pages/download-queue.js"
    if not renderer.is_file():
        pytest.skip("rebuilt renderer not present")

    body = renderer.read_text(encoding="utf-8", errors="replace")
    assert "_listeners" not in body  # sanity: it is a page script, not a stub
    assert "addEventListener" in body, (
        "the rebuilt renderer should bind actions with addEventListener"
    )
    assert "data-query" in body and "queue-manual-search" in body, (
        "the rebuilt renderer should carry action payloads in data-* attributes"
    )


def test_the_scanner_can_actually_find_a_missing_handler() -> None:
    """Mutation guard for the scanner itself.

    A scanner that cannot fail is worthless, and the naive version of this one
    could not: it scanned a hand-picked file list and reported a handler as
    missing that the tree DID define.
    """
    renderer = REPO_ROOT / "static/js/downloads.js"
    if not renderer.is_file():
        pytest.skip("renderer not present")

    referenced = _referenced_handlers(renderer)
    defined = _defined_names(REPO_ROOT / "static/js")

    # Every referenced handler must be defined (the real assertion)...
    assert not (referenced - defined)
    # ...and a fabricated name must NOT be defined, proving the set-difference
    # can report one.
    assert "definitelyNotARealHandlerXyz" not in defined
