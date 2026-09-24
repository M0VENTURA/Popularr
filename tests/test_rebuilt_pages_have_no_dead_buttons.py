"""Every inline button handler on a rebuilt page must resolve.

A button "does nothing when clicked" when its inline handler names a function
the page never loads. TypeScript would catch a misspelt import; a Jinja template
plus `onclick="fn()"` has nothing watching it at all, so the failure is silent —
the browser throws ``ReferenceError: fn is not defined`` into a console nobody
has open, and the click is simply inert.

This module makes that a test failure instead.

── WHAT THIS ACTUALLY FOUND (2026-09-24) ───────────────────────────────────
An audit of the rebuilt tree produced 38 unreachable handlers across 7 pages,
in three distinct shapes — worth remembering, because the fix differs:

  (a) DEFINED AND SERVED, JUST NOT LOADED BY THIS PAGE.
      ``performSlskdSearch`` lives in ``js/pages/soulseek-search.js``, but
      ``artist_detail.html`` / ``artist_detail_v2.html`` / ``downloads/monitor``
      never load that file, while all three render a Soulseek Search button
      calling it. The live tree works because ``static/js/artist_detail.js``
      still defines it — so this was a refactor regression, invisible until
      someone clicked.

  (b) DEFINED IN A SCRIPT THE PAGE DOES NOT INCLUDE, ON A SHARED PARTIAL.
      ``_playlists.html`` is included by the queue page, the search page AND
      the live tree; its buttons expect ``createPlaylist`` /
      ``importPlaylistFromCSV`` from scripts only the search page loads.

  (c) NO DEFINITION ANYWHERE — an obsolete button.
      ``openSlskdSearchAlbum``, ``openAlbumArtModal``, ``alignTracklist``,
        ``openMbReleaseModal``, ``downloadMissingTracks``, ``renameAlbumFiles``.
        Two of these call functions that exist ONLY in ``old_system/`` (frozen
        reference, never to be migrated).

        (``autoLinkAllMbids`` was in this group and is now IMPLEMENTED — see the
        note at its old register position.)
``versioned_static()`` under test-site mode serves the rebuilt tree, and the
static view FALLS BACK to the live tree for anything the rebuilt tree lacks
(``js/downloads.js`` still resolves to ``static/js/downloads.js``). So a script
tag is resolved against ``test_site/static/`` first and ``static/`` second —
matching runtime exactly. Getting this wrong in either direction produces both
false positives and false negatives; an earlier version of this audit checked
only the rebuilt tree and reported pages as broken when they were fine.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_SITE = REPO_ROOT / "test_site"
PAGES_DIR = TEST_SITE / "templates" / "Pages"
REBUILT_STATIC = TEST_SITE / "static"
LIVE_STATIC = REPO_ROOT / "static"

# Keywords that can legally start a handler but are not functions we define.
_NOT_A_FUNCTION = frozenset({
    "this", "event", "return", "if", "void", "javascript", "alert", "confirm",
    "console", "window", "document", "encodeURIComponent", "parseInt",
    "parseFloat", "String", "Number", "JSON", "Object", "Array", "new",
    "bootstrap", "setTimeout", "clearTimeout", "location",
})

COMMENT_RE = re.compile(r"/\*[\s\S]*?\*/|//[^\n]*")
_JINJA_COMMENT_RE = re.compile(r"\{#[\s\S]*?#\}")
_HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
_SCRIPT_SRC_RE = re.compile(
    r"""<script[^>]+src\s*=\s*["']\{\{\s*versioned_static\(['"]([^'"]+)['"]\)\s*\}\}["']"""
)
_INLINE_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", re.I)
_INCLUDE_RE = re.compile(r"{%-?\s*(?:include|extends)\s+['\"]([^'\"]+)['\"]")
_HANDLER_RE = re.compile(r"\bon(?:click|change|submit|input|keyup|keydown)\s*=\s*\"([^\"]*)\"")
_CALL_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*\(")

# A definition is a `function name(`, a `name = function` / `name = () =>`,
# a `global.name =` / `window.name =`, or an object-literal `name: function`.
_DEF_PATTERNS = (
    re.compile(r"function\s+([A-Za-z_$][\w$]*)\s*\("),
    re.compile(r"(?:^|[\s;{}])([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?function"),
    re.compile(r"(?:^|[\s;{}])(?:global|window)\.([A-Za-z_$][\w$]*)\s*="),
    re.compile(r"(?:^|[\s;{}])([A-Za-z_$][\w$]*)\s*=\s*\([^)]*\)\s*=>"),
    re.compile(r"(?:^|[\s;{}])([A-Za-z_$][\w$]*)\s*:\s*(?:async\s*)?(?:function|\()"),
    re.compile(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*="),
    # `global.a = global.b = fn` and `global = { a, b }` shorthands
    re.compile(r"(?:global|window)\.([A-Za-z_$][\w$]*)\b"),
)


def _strip_js_comments(source: str) -> str:
    """Blank out comments while preserving newlines and string literals.

    ⚠️ Required, not cosmetic. An assertion over raw source matches the COMMENT
    that documents the very trap it is guarding (this codebase has hit that four
    times). Comment-aware stripping also stops an apostrophe inside a comment
    from desynchronising the scanner, which an earlier version of this audit
    tripped over.
    """
    out: list[str] = []
    i = 0
    length = len(source)
    state: str | None = None  # None | "'" | '"' | "`" | "line" | "block"
    while i < length:
        char = source[i]
        nxt = source[i + 1] if i + 1 < length else ""
        if state == "line":
            if char == "\n":
                state = None
                out.append(char)
            i += 1
            continue
        if state == "block":
            if char == "*" and nxt == "/":
                state = None
                i += 2
            else:
                if char == "\n":
                    out.append("\n")
                i += 1
            continue
        if state in ("'", '"', "`"):
            out.append(char)
            if char == "\\":
                out.append(nxt)
                i += 2
                continue
            if char == state:
                state = None
            i += 1
            continue
        if char == "/" and nxt == "/":
            state = "line"
            i += 2
            continue
        if char == "/" and nxt == "*":
            state = "block"
            i += 2
            continue
        if char in ("'", '"', "`"):
            state = char
            out.append(char)
            i += 1
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _blank_template_comments(source: str) -> str:
    """Blank Jinja/HTML comments, preserving newlines so line numbers hold."""
    def _blank(match: re.Match[str]) -> str:
        return "".join(ch if ch == "\n" else " " for ch in match.group(0))

    return _HTML_COMMENT_RE.sub(_blank, _JINJA_COMMENT_RE.sub(_blank, source))


def _resolve_template(name: str) -> Path | None:
    for candidate in (
        TEST_SITE / "templates" / name,
        TEST_SITE / "templates" / f"{name}.html",
        TEST_SITE / "templates" / "components" / name,
        TEST_SITE / "templates" / "components" / f"{name}.html",
    ):
        if candidate.is_file():
            return candidate
    return None


def _collect_templates(start: Path) -> list[Path]:
    """The page plus every template it includes or extends, transitively."""
    seen: set[Path] = set()
    stack = [start]
    ordered: list[Path] = []
    while stack:
        current = stack.pop()
        if current in seen or not current.is_file():
            continue
        seen.add(current)
        ordered.append(current)
        body = current.read_text(encoding="utf-8", errors="replace")
        for name in _INCLUDE_RE.findall(body):
            resolved = _resolve_template(name)
            if resolved is not None:
                stack.append(resolved)
    return ordered


def _resolve_script(filename: str) -> Path | None:
    """Mirror the runtime static lookup: rebuilt tree first, live tree second."""
    for root in (REBUILT_STATIC, LIVE_STATIC):
        candidate = root / filename
        if candidate.is_file():
            return candidate
    return None


def _defined_names(sources: list[str]) -> set[str]:
    names: set[str] = set()
    for source in sources:
        clean = _strip_js_comments(source)
        for pattern in _DEF_PATTERNS:
            names.update(pattern.findall(clean))
    return names


def _inline_handlers(templates: list[Path]) -> list[tuple[str, str, int]]:
    """(function name, template path, line number) for every inline handler."""
    found: list[tuple[str, str, int]] = []
    for template in templates:
        raw = template.read_text(encoding="utf-8", errors="replace")
        blanked = _blank_template_comments(raw)
        for match in _HANDLER_RE.finditer(blanked):
            handler = match.group(1)
            call = _CALL_RE.search(handler)
            if not call:
                continue
            name = call.group(1)
            if name in _NOT_A_FUNCTION:
                continue
            # Skip method calls (`foo.bar()`).
            if handler[max(0, call.start() - 1):call.start()] == ".":
                continue
            line = blanked.count("\n", 0, match.start()) + 1
            rel = template.relative_to(REPO_ROOT).as_posix()
            found.append((name, rel, line))
    return found


def _page_files() -> list[Path]:
    if not PAGES_DIR.is_dir():  # pragma: no cover - defensive
        return []
    return sorted(PAGES_DIR.rglob("*.html"))


# ---------------------------------------------------------------------------
# Known-inert buttons — the register
# ---------------------------------------------------------------------------
# Every entry is a button that is ALREADY dead today, with the evidence that it
# is dead. Two rules keep this honest:
#
#   1. The test below asserts each entry is still dead, so the moment someone
#      implements the function (or deletes the button) the stale entry FAILS and
#      has to be removed. This cannot rot into a blanket suppression.
#   2. It is not a general-purpose escape hatch — a NEW dead button still fails
#      the build, which is the entire point of the guard.
#
# These are NOT fixed here because each needs a product decision, not a
# mechanical edit. They are the residue after fixing the ones that had a real
# implementation sitting unused (see the module docstring).
_OBSOLETE_BUTTONS: dict[tuple[str, str], str] = {
    # ── name exists nowhere in either tree ──────────────────────────────────
    # ⚠️ FIVE ALBUM HANDLERS USED TO BE REGISTERED HERE and have been REMOVED,
    # because all five are now implemented in BOTH trees:
    #     openAlbumArtModal     — search/paste/upload dialog; the three backend
    #                             endpoints (search-art, set-art, upload-art)
    #                             already existed and were unreachable from the
    #                             album page. Also called by the pencil over the
    #                             album art, so that was dead too.
    #     downloadMissingTracks — clicks the existing `.mb-queue-missing` rows,
    #                             whose payloads live in closures and so cannot
    #                             be read back out of the DOM.
    #     renameAlbumFiles      — POST /api/album/{artist}/{album}/rename-files
    #                             already existed and was unreachable.
    #     alignTracklist        — renumbers via the same per-track
    #                             /api/v1/tracks/<id>/apply-mb-field endpoint the
    #                             inline Apply button uses. NOTE: track numbers
    #                             on this page are DISPLAY-ONLY cells, so there
    #                             is nothing to write in the DOM.
    #     autoLinkAllMbids      — POST /api/musicbrainz/link-album-mbids.
    # test_the_register_has_no_stale_entries is what forces each deletion, and
    # tests/test_busy_popup.py::test_the_five_handlers_are_not_in_the_obsolete_register
    # keeps this register and that suite from disagreeing about what is dead.
    # _organize_group.html is shared by album_detail.html and the queue page.
    # Its controls live in pages/download-queue.js, so the ALBUM page cannot
    # reach them (and the queue page can, because it loads that script).
    ("album_detail.html", "searchMBForOrganize"): (
        "_organize_group.html is shared with downloads/queue.html, where these "
        "come from pages/download-queue.js. The album page loads album.js, "
        "which does not define them."
    ),
    ("album_detail.html", "searchDiscogsForOrganize"): (
        "Same _organize_group.html sharing problem as searchMBForOrganize."
    ),
    ("album_detail.html", "clearMBSelection"): (
        "Same _organize_group.html sharing problem."
    ),
    ("album_detail.html", "confirmOrganizeGroup"): (
        "Same _organize_group.html sharing problem."
    ),
    # ── defined only in the LIVE tree, which these pages do not load ────────
    ("artist_detail.html", "saveEditedTrackFromArtistPage"): (
        "Defined only in static/js/artist_detail.js (live). Needs hoisting "
        "into the rebuilt pages/ modules."
    ),
    ("artist_detail_v2.html", "saveEditedTrackFromArtistPage"): (
        "Same function, same cause as artist_detail.html above."
    ),
    ("downloads/monitor.html", "saveEditedTrackFromArtistPage"): (
        "Same function, same cause as artist_detail.html above."
    ),
    ("downloads/monitor.html", "toggleMissing"): (
        "Defined only in static/js/artist_detail.js (live). Six call sites on "
        "this page."
    ),
    ("downloads/monitor.html", "addEditArtistTrackGenre"): (
        "Defined only in static/js/artist_detail.js (live). pages/album.js has "
        "`addEditTrackGenre`, and the two signatures disagree (the live one "
        "reads the track id from the DOM), so this needs a decision rather "
        "than an alias."
    ),
    ("downloads/monitor.html", "fetchArtistCountry"): (
        "Defined in pages/artist-detail-extras.js, which this page does not "
        "load. A missing-script fix."
    ),
    ("downloads/monitor.html", "editArtistCountry"): (
        "Same cause as fetchArtistCountry above."
    ),
    # ── shared partials whose buttons need their controllers ────────────────
    ("downloads/queue.html", "saveEditedTrack"): (
        "_track_edit.html is shared with album_detail.html, where these live "
        "in pages/album.js. This page needs its own track-edit controller."
    ),
    ("downloads/queue.html", "addEditTrackGenre"): (
        "Same _track_edit.html sharing problem."
    ),
    ("downloads/queue.html", "saveComprehensiveEditedTrack"): (
        "Same _track_edit.html sharing problem."
    ),
    ("downloads/queue.html", "importPlaylistFromCSV"): (
        "_playlists.html is shared with downloads/search.html, which DOES load "
        "features/csv-import.js. Loading it here would pull in that whole page."
    ),
    ("downloads/queue.html", "createPlaylist"): (
        "publishes global.createPlaylist from pages/playlist.js, which does "
        "not declare its top-level state safely enough to load on this page."
    ),
}


def unreachable_handlers() -> dict[str, list[tuple[str, str, int]]]:
    """page -> [(function, template, line), ...] that the page cannot resolve."""
    report: dict[str, list[tuple[str, str, int]]] = {}
    for page in _page_files():
        templates = _collect_templates(page)
        sources: list[str] = []
        for template in templates:
            body = template.read_text(encoding="utf-8", errors="replace")
            for filename in _SCRIPT_SRC_RE.findall(body):
                resolved = _resolve_script(filename)
                if resolved is not None:
                    sources.append(resolved.read_text(encoding="utf-8", errors="replace"))
            for inline in _INLINE_SCRIPT_RE.findall(body):
                sources.append(inline)

        defined = _defined_names(sources)
        broken = [
            (name, path, line)
            for name, path, line in _inline_handlers(templates)
            if name not in defined
        ]
        if broken:
            report[page.relative_to(PAGES_DIR).as_posix()] = broken
    return report


def _known_dead(page: str, name: str) -> bool:
    return (page, name) in _OBSOLETE_BUTTONS


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNoUnreachableButtonHandlers:
    def test_the_harness_can_find_the_pages(self):
        """A discovery bug would make every assertion below vacuously pass."""
        pages = _page_files()
        assert len(pages) >= 20, f"only found {len(pages)} pages under {PAGES_DIR}"

    def test_the_harness_resolves_scripts_through_the_fallback(self):
        """``js/downloads.js`` exists only in the live tree.

        If the resolver ignored the live fallback it would drop every definition
        that file contributes and report working pages as broken.
        """
        assert _resolve_script("js/services/slskd.js") is not None
        assert _resolve_script("js/downloads.js") is not None, (
            "the live-tree fallback is not being applied"
        )

    def test_the_harness_ignores_commented_out_handlers(self):
        """A commented-out onclick must not be reported."""
        assert _strip_js_comments("// function ghost() {}\n") .strip() == ""
        assert "ghost" not in _strip_js_comments("// function ghost() {}")

    def test_every_served_page_resolves_every_inline_handler(self):
        """The regression guard: no NEW button may be inert."""
        report = unreachable_handlers()
        unexpected = {
            page: [
                item for item in items
                if not _known_dead(page, item[0])
            ]
            for page, items in report.items()
        }
        unexpected = {p: i for p, i in unexpected.items() if i}
        if not unexpected:
            return

        lines = [
            "Inline handlers that the page cannot resolve — the button will do "
            "nothing when clicked:",
        ]
        for page, items in sorted(unexpected.items()):
            lines.append(f"\n  {page}")
            for name, path, line in sorted(set(items)):
                lines.append(f"      {name}()   {path}:{line}")
        lines.append(
            "\nFix by (a) loading the script that defines it, (b) moving the "
            "definition somewhere the page already loads, or (c) deleting the "
            "button if the function no longer exists anywhere."
        )
        lines.append(
            "\nIf this is a long-standing dead button that needs a product "
            "decision, add it to _OBSOLETE_BUTTONS with the evidence — do NOT "
            "weaken this test."
        )
        pytest.fail("\n".join(lines))

    def test_the_register_has_no_stale_entries(self):
        """A register entry that is no longer dead must be deleted.

        Without this the register rots into a blanket suppression: someone
        implements the function, the guard keeps skipping it, and a real
        regression later goes unnoticed. Requiring the at-this-time-of-writing
        shape fixes that.
        """
        report = unreachable_handlers()
        still_dead = {
            (page, item[0])
            for page, items in report.items()
            for item in items
        }
        stale = sorted(set(_OBSOLETE_BUTTONS) - still_dead)
        assert not stale, (
            "these _OBSOLETE_BUTTONS entries are no longer dead — the function "
            "now resolves (or the button was removed), so delete the entry:\n"
            + "\n".join(f"    {page}: {name}" for page, name in stale)
        )

    def test_the_register_only_covers_real_dead_buttons(self):
        """A typo'd page path would silently suppress a live button elsewhere."""
        pages = {p.relative_to(PAGES_DIR).as_posix() for p in _page_files()}
        unknown_pages = sorted({page for page, _ in _OBSOLETE_BUTTONS} - pages)
        assert not unknown_pages, (
            "_OBSOLETE_BUTTONS references pages that do not exist: "
            f"{unknown_pages}"
        )
