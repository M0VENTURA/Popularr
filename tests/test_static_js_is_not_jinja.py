"""Guard: no file served as JavaScript may contain Jinja template syntax.

The bug this guards — reported as "The filter between In Library and Missing on
the artist page still doesn't work. I can't hide the missing releases that are
populated":

``static/js/artist_detail.js`` is not JavaScript. It is a 5346-line Jinja PAGE
TEMPLATE whose first line is ``{% extends "base.html" %}``, yet
``templates/pages/artist_detail_v2.html`` loads it with ``<script src>``. The
browser parses the Jinja as JS, throws

    SyntaxError: Unexpected token '%'

on line 1, and **discards the entire file**. Every function it defined silently
ceased to exist — including ``setArtistFilter()``, which the filter bar's inline
``onclick`` attributes call. Every click therefore threw

    ReferenceError: setArtistFilter is not defined

and nothing filtered.

That class of failure is uniquely nasty: it produces no server error, no
console warning from the app, and no failing Python test — the feature is simply
dead. This test makes it fail loudly instead.

A SyntaxError in one ``<script src>`` does not prevent LATER scripts from
running, which is why the filter was restored as its own module.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories served to the browser as static assets. old_system/ is a frozen
# reference snapshot and is excluded.
STATIC_ROOTS = (REPO_ROOT / "static", REPO_ROOT / "test_site" / "static")

_JINJA_TOKEN = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)


def _strip_comments(src: str) -> str:
    """Remove JS block and line comments.

    Documentation legitimately quotes Jinja (this project's own changelogs do),
    so only uncommented code may be scanned.
    """
    text = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)


def _js_files() -> list[Path]:
    files: list[Path] = []
    for root in STATIC_ROOTS:
        if root.is_dir():
            files.extend(sorted(root.rglob("*.js")))
    return files


def test_js_files_are_discovered() -> None:
    """The scan must find files, or the guard is vacuous."""
    files = _js_files()
    assert files, "no .js files discovered - STATIC_ROOTS is wrong"
    names = {f.name for f in files}
    assert "artist-album-filter.js" in names


@pytest.mark.parametrize(
    "js_file", _js_files(), ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_static_js_contains_no_jinja(js_file: Path) -> None:
    """A .js file served to the browser must be valid JavaScript, not a template."""
    src = js_file.read_text(encoding="utf-8")
    code = _strip_comments(src)

    tokens = _JINJA_TOKEN.findall(code)
    assert not tokens, (
        f"{js_file.relative_to(REPO_ROOT)} contains Jinja template syntax in "
        f"code ({len(tokens)} token(s), e.g. {tokens[0][:60]!r}). A browser will "
        "throw SyntaxError on the first such token and discard the ENTIRE file, "
        "so every function it defines — and every inline onclick that calls one "
        "— silently stops working. Move the template to templates/ and keep the "
        ".js file as plain JavaScript."
    )

    first_line = next((l for l in src.splitlines() if l.strip()), "")
    assert not first_line.lstrip().startswith("{%"), (
        f"{js_file.relative_to(REPO_ROOT)} starts with a Jinja tag "
        f"({first_line.strip()[:60]!r}), so it cannot be served as JavaScript."
    )


def test_artist_filter_module_is_loaded_and_publishes_globals() -> None:
    """The artist album filter must be a real module AND still be loaded.

    Pins the specific regression: the page-wide filter bar called
    ``setArtistFilter(...)`` from inline onclick attributes, so the function had
    to exist on ``window``.

    That page-wide bar is GONE — the artist page now filters PER RELEASE
    SECTION (All / Library / Missing radios inside each
    components/_release_section.html card, wired by
    static/js/artist-releases.js). The module is still kept and loaded as the
    shared helper for any remaining page-wide control, so its globals must
    still resolve — but asserting that the artist page calls
    ``setArtistFilter`` from an onclick would now assert markup that was
    deliberately removed.
    """
    module = REPO_ROOT / "static" / "js" / "artist-album-filter.js"
    assert module.is_file(), "static/js/artist-album-filter.js is missing"

    body = module.read_text(encoding="utf-8")
    assert "function setArtistFilter(" in body
    assert "window.setArtistFilter" in body, (
        "setArtistFilter must be published on window — inline onclick handlers "
        "resolve by global name"
    )

    # The module understood only the OLD `.category-section .album-row` markup
    # and had to be retargeted when the release rows moved to .release-item.
    # A selector that can never match is valid CSS/JS, so this needs pinning.
    assert "release-item" in body, (
        "artist-album-filter.js does not reference the new .release-item rows. "
        "Its old selector (.category-section .album-row) matches nothing after "
        "the release-section migration, which makes the whole module inert "
        "while looking entirely correct."
    )

    template = REPO_ROOT / "templates" / "pages" / "artist_detail_v2.html"
    html = template.read_text(encoding="utf-8")
    assert "artist-album-filter.js" in html, (
        "artist_detail_v2.html does not load the filter module"
    )

    assert "releases-sections" in html, (
        "artist_detail_v2.html lost the #releases-sections marker, so "
        "js/artist-releases.js (which owns the per-section All/Library/Missing "
        "filter and the tracklists) never initialises"
    )
