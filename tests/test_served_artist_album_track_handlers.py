"""Dead inline handlers on the SERVED pages (test_site), artist/album/track.

WHY THE TREE MATTERS

``helpers.test_site_mode.config_enables_test_site()`` returns True
unconditionally ("test_site has been merged as the main UI"), the rebuilt tree is
Jinja loader #1, and ``_SHADOWED_TEMPLATES`` is EMPTY — so any template present
in both trees is served from ``test_site``, and the live copy is only a fallback
for a file test_site lacks. Auditing ``templates/pages/*.html`` therefore audits
files nobody sees.

``tests/test_rebuilt_pages_have_no_dead_buttons.py`` covers the rebuilt DOWNLOADS
pages. This file covers the rebuilt ARTIST / ALBUM / TRACK pages, which had no
such guard.

WHAT IT FOUND (the entries in KNOWN_DEAD below)

``components/modals/_artist_modals.html`` renders ``#editTrackModal`` with

    onclick="saveEditedTrackFromArtistPage()"

That name exists only in the LIVE tree's ``static/js/artist_detail.js``, which
the artist pages never load; they load ``pages/artist.js``, whose equivalent is
``saveArtistEditedTrack()`` (and the sibling modal in the same file already calls
that one correctly). The modal is REACHABLE — ``services/genres.js``
(``editTrackArtist``) shows ``#editTrackModal`` on the artist page — so Save threw
ReferenceError and the edit was lost.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TS_TEMPLATES = REPO_ROOT / "test_site/templates"
LIVE_TEMPLATES = REPO_ROOT / "templates"
REBUILT_STATIC = REPO_ROOT / "test_site/static"
LIVE_STATIC = REPO_ROOT / "static"

PAGES = (
    "Pages/album_detail.html",
    "Pages/artist_detail.html",
    "Pages/artist_detail_v2.html",
    "Pages/track_detail.html",
    "Pages/artist_list.html",
    "Pages/artist_corrections.html",
    "Pages/artist_genres.html",
)

#: (page, function) pairs still dead on the served pages, with the reason.
#:
#: The album entries are the SHARED-PARTIAL gap that
#: ``test_rebuilt_pages_have_no_dead_buttons.py`` already records for the rebuilt
#: downloads pages (see its "same _organize_group.html sharing problem" notes).
#: The controllers live in ``pages/download-queue.js``, which the album page does
#: not load; the partials come from ``_organize_group.html`` / ``_manual_match.html``
#: / ``_slskd_results.html``, which it does. Fixing it properly means giving the
#: album page its own organizer controller, not duplicating the whole queue page.
KNOWN_DEAD: dict[tuple[str, str], str] = {
    ("Pages/album_detail.html", "searchMatchTargets"): (
        "SHARED PARTIAL. _manual_match.html is included here; its controller "
        "lives in pages/download-queue.js, which this page does not load."
    ),
    ("Pages/album_detail.html", "searchMBForOrganize"): (
        "SHARED PARTIAL. Same _organize_group.html situation — recorded in the "
        "rebuilt-tree test too."
    ),
    ("Pages/album_detail.html", "searchDiscogsForOrganize"): (
        "SHARED PARTIAL. Same _organize_group.html situation."
    ),
    ("Pages/album_detail.html", "clearMBSelection"): (
        "SHARED PARTIAL. Same _organize_group.html situation."
    ),
    ("Pages/album_detail.html", "confirmOrganizeGroup"): (
        "SHARED PARTIAL. Same _organize_group.html situation."
    ),
    ("Pages/album_detail.html", "searchAgainSlskd"): (
        "SHARED PARTIAL. _slskd_results.html's re-search button; defined nowhere "
        "at all, so it is dead on every page that renders the partial."
    ),
}

_NOT_A_FUNCTION = frozenset({
    "if", "for", "while", "switch", "catch", "typeof", "function", "return",
    "alert", "confirm", "prompt", "console", "window", "document", "event",
    "parseInt", "parseFloat", "String", "Number", "JSON", "Object", "Array",
    "Math", "Date", "setTimeout", "setInterval", "clearTimeout", "clearInterval",
    "fetch", "encodeURIComponent", "decodeURIComponent", "bootstrap", "Player",
    "event.preventDefault", "stopPropagation", "preventDefault",
})

_HANDLER_RE = re.compile(r'on(?:click|change|submit|input|keydown|keyup|load)="([^"]*)"')
_CALL_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
_DEFS = (
    re.compile(r"function\s+(\w+)\s*\("),
    re.compile(r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:function|\()"),
    re.compile(r"(\w+)\s*:\s*(?:async\s*)?function"),
    re.compile(r"(?:window|global)\.(\w+)\s*="),
    re.compile(r"(\w+)\s*=\s*(?:async\s*)?\(?[\w,\s]*\)?\s*=>"),
)


def _strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)//.*$", "", src)


def _blank_template_comments(text: str) -> str:
    """Blank Jinja comments, preserving line numbers.

    A "BUG FIXED: this called onclick=…" note IS NOT a live handler.
    """
    return re.sub(r"\{#.*?#\}", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)


def _template_chain(page: pathlib.Path) -> list[pathlib.Path]:
    """The page plus every partial it includes/extends, transitively."""
    seen: list[pathlib.Path] = []
    stack = [page]
    while stack:
        current = stack.pop()
        if current in seen or not current.is_file():
            continue
        seen.append(current)
        body = current.read_text(encoding="utf-8", errors="replace")
        for name in re.findall(
            r"\{%-?\s*(?:include|extends|from)\s+['\"]([^'\"]+)['\"]", body
        ):
            for base in (TS_TEMPLATES, LIVE_TEMPLATES):
                candidate = base / name
                if candidate.is_file():
                    stack.append(candidate)
                    break
    return seen


def _script_bodies(templates: list[pathlib.Path]) -> list[str]:
    """JS the page loads: versioned_static refs (rebuilt first) + inline blocks."""
    out: list[str] = []
    for template in templates:
        body = template.read_text(encoding="utf-8", errors="replace")
        for name in re.findall(r"versioned_static\(\s*['\"]([^'\"]+)['\"]", body):
            for root in (REBUILT_STATIC, LIVE_STATIC):
                candidate = root / name
                if candidate.is_file():
                    out.append(candidate.read_text(encoding="utf-8", errors="replace"))
                    break
        for inline in re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>", body, re.S):
            out.append(inline)
    return out


def _defined(templates: list[pathlib.Path]) -> set[str]:
    names: set[str] = set()
    for body in _script_bodies(templates):
        clean = _strip_js_comments(body)
        for pattern in _DEFS:
            names |= set(pattern.findall(clean))
    return names


def _handlers(templates: list[pathlib.Path]) -> list[tuple[str, str, int]]:
    found: list[tuple[str, str, int]] = []
    for template in templates:
        raw = template.read_text(encoding="utf-8", errors="replace")
        blanked = _blank_template_comments(raw)
        for match in _HANDLER_RE.finditer(blanked):
            body = match.group(1)
            for call in _CALL_RE.finditer(body):
                name = call.group(1)
                if name in _NOT_A_FUNCTION:
                    continue
                if body[max(0, call.start() - 1):call.start()] == ".":
                    continue
                found.append((name, template.name, blanked.count("\n", 0, match.start()) + 1))
    return found


def _pages() -> list[pathlib.Path]:
    return [TS_TEMPLATES / rel for rel in PAGES if (TS_TEMPLATES / rel).is_file()]


@pytest.mark.parametrize("rel", PAGES)
def test_no_dead_inline_handler(rel: str) -> None:
    page = TS_TEMPLATES / rel
    if not page.is_file():
        pytest.skip(f"{rel} is not in the rebuilt tree")

    templates = _template_chain(page)
    defined = _defined(templates)

    dead = []
    for name, template, line in _handlers(templates):
        if name in defined:
            continue
        if (rel, name) in KNOWN_DEAD:
            continue
        dead.append(f"{name}()  ({template}:{line})")

    assert not dead, (
        f"{rel} calls handlers nothing on the page defines:\n  "
        + "\n  ".join(sorted(set(dead)))
        + "\n\nThis page is the one actually served. Fix the button, or record "
        "the pair in KNOWN_DEAD with the reason."
    )


class TestTheArtistTrackEditModal:
    """Pins the concrete fix so the dead Save button is not restored."""

    def test_the_modal_calls_a_function_that_exists(self) -> None:
        modal = TS_TEMPLATES / "components/modals/_artist_modals.html"
        src = modal.read_text(encoding="utf-8")
        # Strip Jinja comments first: the fix's own explanation mentions the old
        # handler name, and a naive search matches the prose.
        live = _blank_template_comments(src)
        assert 'onclick="saveEditedTrackFromArtistPage()"' not in live, (
            "the Save button calls a name defined only in the LIVE tree's "
            "artist_detail.js, which these pages do not load"
        )
        assert 'onclick="saveArtistEditedTrack()"' in live

    def test_that_function_is_provided_by_the_loaded_controller(self) -> None:
        artist_js = (REBUILT_STATIC / "js/pages/artist.js").read_text(encoding="utf-8")
        assert re.search(r"function\s+saveArtistEditedTrack\s*\(", artist_js), (
            "pages/artist.js no longer defines saveArtistEditedTrack — the "
            "modal's Save button would be dead again"
        )

    def test_the_modal_is_reachable_from_the_artist_page(self) -> None:
        """It is not inert markup: genres.js shows it on the artist page."""
        genres_js = (REBUILT_STATIC / "js/services/genres.js").read_text(encoding="utf-8")
        assert "editTrackModal" in genres_js

    def test_the_artist_page_loads_the_controller(self) -> None:
        page = (TS_TEMPLATES / "Pages/artist_detail.html").read_text(encoding="utf-8")
        assert "js/pages/artist.js" in page

    def test_the_artist_page_does_not_load_the_live_controller(self) -> None:
        """The bug's cause: the name lived in a file this page never loads."""
        page = (TS_TEMPLATES / "Pages/artist_detail.html").read_text(encoding="utf-8")
        assert "js/artist_detail.js" not in page
