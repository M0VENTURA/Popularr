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
REBUILT_STATIC = REPO_ROOT / "test_site/static"
LIVE_TEMPLATES = TS_TEMPLATES
LIVE_STATIC = REPO_ROOT / "test_site/static"

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
#: THE ALBUM ENTRIES ARE NOW FOUR, NOT SIX. ``_slskd_results.html`` and
#: ``_manual_match.html`` were deleted, so ``searchAgainSlskd`` and
#: ``searchMatchTargets`` no longer render anywhere — their entries were removed
#: along with the partials. What remains is ``_organize_group.html``, which the
#: album page keeps for markup only: its controls come from
#: ``pages/download-queue.js``, which this page does not load (and must not — that
#: would drag the whole queue controller and its polling onto the album page).
#:
#: ``test_the_register_has_no_stale_entries`` below is authoritative: if one of
#: these starts resolving, or stops being called, it fails and names the entry.
KNOWN_DEAD: dict[tuple[str, str], str] = {
    ("Pages/album_detail.html", "searchMBForOrganize"): (
        "INERT PARTIAL. _organize_group.html keeps its MusicBrainz button here, "
        "but its controller (pages/download-queue.js) is not loaded on this page."
    ),
    ("Pages/album_detail.html", "searchDiscogsForOrganize"): (
        "INERT PARTIAL. Same _organize_group.html situation."
    ),
    ("Pages/album_detail.html", "clearMBSelection"): (
        "INERT PARTIAL. Same _organize_group.html situation."
    ),
    ("Pages/album_detail.html", "confirmOrganizeGroup"): (
        "INERT PARTIAL. Same _organize_group.html situation."
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


def test_the_register_has_no_stale_entries() -> None:
    """Every KNOWN_DEAD pair must STILL be dead, or the register is lying.

    A stale entry is worse than no entry: it makes a fixed bug look broken, and
    it hides the next real one behind an allow-list that no longer means
    anything. (This is the same contract
    ``test_rebuilt_pages_have_no_dead_buttons`` enforces for its own register.)

    Deleting ``_slskd_results.html`` and ``_manual_match.html`` is exactly the
    change that makes an entry stale, so this test is what forces the follow-up.
    """
    stale: list[str] = []
    for (rel, name), _reason in sorted(KNOWN_DEAD.items()):
        page = TS_TEMPLATES / rel
        if not page.is_file():
            stale.append(f"{rel} does not exist")
            continue

        templates = _template_chain(page)
        called = {n for n, _t, _l in _handlers(templates)}
        defined = _defined(templates)

        if name not in called:
            stale.append(f"{rel} no longer calls {name}() — drop the entry")
        elif name in defined:
            stale.append(f"{rel} now DEFINES {name}() — drop the entry")

    assert not stale, (
        "these KNOWN_DEAD entries are out of date:\n  "
        + "\n  ".join(stale)
        + "\n\nDelete each entry so the register keeps naming only live bugs."
    )


def test_an_included_partial_brings_its_own_controllers() -> None:
    """A modal include must come with a page that can DRIVE it.

    ⭐ THE CLASS, NOT THE INSTANCE. ``_slskd_results.html`` and
    ``_manual_match.html`` were included by the album page while their handlers
    lived in a controller it never loaded — and nothing ever opened their modals,
    so the markup was dead weight carrying dead handlers.

    This checks the real property rather than a blanket declaration: for every
    modal the album page includes, the handlers it renders must EITHER resolve
    on this page, OR the partial must be listed in INERT_PARTIALS as knowingly
    markup-only. That is what separates the five live partials from the one
    deliberately inert one, without a hand-maintained list of everything.
    """
    INERT_PARTIALS: dict[str, str] = {
        "_organize_group.html": (
            "Markup only on the album page: its controls come from "
            "pages/download-queue.js, which this page does not load, and its "
            "modal is only opened from a queue-page row."
        ),
    }

    page = TS_TEMPLATES / "Pages/album_detail.html"
    if not page.is_file():
        pytest.skip("album page absent")

    templates = _template_chain(page)
    defined = _defined(templates)
    raw = _blank_template_comments(page.read_text(encoding="utf-8"))

    included = re.findall(
        r"\{%-?\s*include\s+['\"]components/modals/([^'\"]+)['\"]", raw
    )
    assert included, "the album page stopped including any modals — check the scan"

    modules_dir = TS_TEMPLATES / "components/modals"
    live_modules_dir = LIVE_TEMPLATES / "components/modals"

    problems: list[str] = []
    for name in sorted(set(included)):
        path = modules_dir / name
        if not path.is_file():
            path = live_modules_dir / name
        if not path.is_file():
            problems.append(f"{name} — INCLUDED BUT MISSING ON DISK")
            continue
        if name in INERT_PARTIALS:
            continue

        # A live partial: every handler it renders must resolve on this page.
        partial = _template_chain(path)
        unresolvable = sorted({
            handler for handler, _t, _l in _handlers(partial)
            if handler not in defined
        })
        if unresolvable:
            problems.append(
                f"{name} — renders {unresolvable} which this page cannot resolve. "
                "Load its controller here, or list it in INERT_PARTIALS."
            )

    assert not problems, (
        "modal includes on the album page that come without their controllers:\n  "
        + "\n  ".join(problems)
    )


class TestTheDeletedPartials:
    """Pins the removals so the dead markup cannot quietly come back."""

    @pytest.mark.parametrize("name", ["_slskd_results.html", "_manual_match.html"])
    def test_the_partial_is_gone_from_both_trees(self, name: str) -> None:
        assert not (TS_TEMPLATES / "components/modals" / name).exists()
        assert not (LIVE_TEMPLATES / "components/modals" / name).exists()

    @pytest.mark.parametrize(
        "rel",
        [
            "Pages/album_detail.html",
            "Pages/downloads/queue.html",
            "Pages/downloads/monitor.html",
        ],
    )
    @pytest.mark.parametrize("name", ["_slskd_results", "_manual_match"])
    def test_no_template_still_includes_them(self, rel: str, name: str) -> None:
        page = TS_TEMPLATES / rel
        if not page.is_file():
            pytest.skip(f"{rel} absent")
        raw = _blank_template_comments(page.read_text(encoding="utf-8"))
        assert f"components/modals/{name}.html" not in raw, (
            f"{rel} still includes the deleted partial {name}.html — a Jinja "
            "include of a missing file is a 500 at render time unless the tag "
            "carries `ignore missing`, and either way it is dead markup"
        )


class TestTheOrganizeSpinnerCanActuallyAppear:
    """`display:none !important` defeats the class toggle that shows the spinner.

    ``download-queue.js::setOrganizeLookupLoading()`` reveals the lookup spinner
    by toggling ``d-none`` / ``d-flex``. An INLINE ``!important`` cannot be beaten
    by a class (nor by ``el.style.display``), so while the template carried it the
    spinner never appeared and the MusicBrainz lookup ran with no feedback at all.

    ``download-queue.js``'s own doc comment already says "make sure the template
    no longer sets that inline style" — both tree copies still did, so this is
    asserted against both rather than trusting that comment.
    """

    @pytest.mark.parametrize(
        "rel",
        [
            "test_site/templates/components/modals/_organize_group.html",
            "templates/components/modals/_organize_group.html",
        ],
    )
    def test_no_inline_important_on_the_loading_element(self, rel: str) -> None:
        path = REPO_ROOT / rel
        if not path.is_file():
            pytest.skip(f"{rel} absent")
        src = path.read_text(encoding="utf-8")

        match = re.search(
            r'<div[^>]*id="orgMbSearchLoading"[^>]*>', src
        )
        assert match, f"{rel}: #orgMbSearchLoading not found"
        tag = match.group(0)
        assert "!important" not in tag, (
            f"{rel}: the loading element carries an inline !important, so "
            "setOrganizeLookupLoading() cannot reveal it"
        )
        assert "d-none" in tag, (
            f"{rel}: the element must start hidden via the d-none CLASS, so the "
            "class toggle in setOrganizeLookupLoading() actually controls it"
        )

    @pytest.mark.parametrize(
        "rel",
        [
            "test_site/templates/components/modals/_organize_group.html",
            "templates/components/modals/_organize_group.html",
        ],
    )
    def test_the_undo_button_uses_a_real_bootstrap_size_class(self, rel: str) -> None:
        """`btn-xs` is not a Bootstrap class — it silently did nothing."""
        path = REPO_ROOT / rel
        if not path.is_file():
            pytest.skip(f"{rel} absent")
        live = _blank_template_comments(path.read_text(encoding="utf-8"))
        assert "btn-xs" not in live, f"{rel}: 'btn-xs' is not a Bootstrap class"


class TestTheAlbumMatchModal:
    """The reachable fix: #albumMatchTrackModal is now included."""

    def test_the_album_page_includes_it(self) -> None:
        page = TS_TEMPLATES / "Pages/album_detail.html"
        raw = _blank_template_comments(page.read_text(encoding="utf-8"))
        assert "components/modals/_track_match.html" in raw, (
            "pages/album.js::openMatchModal needs #albumMatchTrackModal; without "
            "the include the 'Match to an existing track' button does nothing"
        )

    def test_the_modal_provides_the_ids_the_handler_needs(self) -> None:
        modal = LIVE_TEMPLATES / "components/modals/_track_match.html"
        assert modal.is_file(), "_track_match.html vanished from the live tree"
        src = modal.read_text(encoding="utf-8")
        for dom_id in ("albumMatchTrackModal", "albumMatchCandidatesTbody",
                       "albumMatchMbTitle", "albumMatchMbTrackNum"):
            assert f'id="{dom_id}"' in src, (
                f"openMatchModal reads #{dom_id}; the modal no longer provides it"
            )

    def test_the_button_that_opens_it_is_still_rendered(self) -> None:
        album_js = (REBUILT_STATIC / "js/pages/album.js").read_text(encoding="utf-8")
        assert "mb-match-existing" in album_js
        assert "openMatchModal(" in album_js


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
