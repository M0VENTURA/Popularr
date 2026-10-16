"""Mobile chrome: the app header, the system sheet and the status pill.

These pin the mobile shell redesign. The complaints it answers were concrete:

1. **~25% of the screen was chrome.** The navbar was ``--navbar-height: 132px``
   below ``lg`` because the promoted search row wrapped onto its own line, and
   the status bar spent a fixed 2.4rem on the word "Idle" whether or not
   anything was running.
2. **Two navigation systems on one screen.** A hamburger expanding
   ``#navbarNav`` sat next to a five-tab bottom bar, so "where can I go?" had
   two answers in two shapes.
3. **Breadcrumbs ate a second line** on eight pages to say what the page title
   already said.

The replacement: a single header row (``[<-] Page name .... [scan] [menu]``),
one system sheet holding everything the bottom bar does not, and a status bar
that is a 2px hairline until there is something to report.

Both trees are read here where the assertion is about *the live contract too*,
because ``templates/base.html`` is still served when
``features.use_test_site`` is off.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TS = REPO_ROOT / "test_site"

BASE_HTML = TS / "templates" / "base.html"
SYSTEM_SHEET = TS / "templates" / "components" / "_system_sheet.html"
POPULARR_CSS = TS / "static" / "css" / "popularr.css"
MAIN_JS = TS / "static" / "js" / "main.js"
DASHBOARD_JS = TS / "static" / "js" / "pages" / "dashboard.js"
FLYOUT_JS = TS / "static" / "js" / "ui" / "search-flyout.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def base() -> str:
    return _read(BASE_HTML)


@pytest.fixture(scope="module")
def css() -> str:
    return _read(POPULARR_CSS)


@pytest.fixture(scope="module")
def sheet() -> str:
    return _read(SYSTEM_SHEET)


@pytest.fixture(scope="module")
def main_js() -> str:
    return _read(MAIN_JS)


def _render_env(path: str):
    """Back-compat shim — the real environment lives in ``_test_site_render``."""
    from tests._test_site_render import build_env

    return build_env(path)


async def _render(app, path: str, child: str = "") -> str:
    """Render ``base.html`` (optionally via a child that extends it) as *path*.

    ``app`` is accepted so the calls read like the other suites', but the render
    itself is HERMETIC — see ``tests/_test_site_render.py`` for why swapping
    ``app.jinja_loader`` does not work in a full-suite sweep.
    """
    from tests._test_site_render import render_base, render_child

    if child:
        return render_child(child, path)
    return render_base(path)


def _header(html: str) -> str:
    """The app header row's markup (everything before the search wrapper)."""
    start = html.index('class="nav-brand-row')
    return html[start : html.index('id="navSearchForm"', start)]


def _tag_at(html: str, marker: str) -> str:
    """The whole tag containing *marker* (backwards to ``<``, forwards to ``>``).

    Slicing a fixed number of characters instead is how a "no such class"
    assertion passes on markup that merely has the class further away, and how
    a negative offset silently returns the END of the string.
    """
    idx = html.index(marker)
    start = html.rindex("<", 0, idx)
    return html[start : html.index(">", idx) + 1]


def _function_body(source: str, start: int) -> str:
    """*source* from *start* to the end of that function's body.

    A fixed character window silently stops covering the function once its
    docstring grows, and the failure reads as "the call is gone" rather than
    "the test looked too short a distance". Bounded by the closing brace.
    """
    return source[start : source.index("\n  }", start)]


def _wrapper_classes(html: str) -> set[str]:
    """The class list of the DIV wrapping the flyout's search input.

    Returned as a SET so an assertion about `d-none` cannot be satisfied by
    `d-lg-none` (or any other class that merely contains those characters) —
    a substring test on the raw attribute is exactly the trap that let a
    `d-lg-none` look like a `d-none` in an earlier review.
    """
    start = html.rindex("<div", 0, html.index('id="unifiedSearchIcon"'))
    tag = html[start : html.index(">", start) + 1]
    match = re.search(r'class="([^"]*)"', tag)
    return set((match.group(1) if match else "").split())


def _title_text(html: str) -> str:
    """The text INSIDE the mobile header's title element.

    Bounded by the closing tag, not by the next ``>``: the title is a
    ``<span>``, so "the next ``>``" is the end of its own opening tag and the
    result is always empty — a test that would pass on any markup at all.
    """
    idx = html.index('id="appHeaderTitle"')
    start = html.index(">", idx) + 1
    return html[start : html.index("</span>", start)].strip()


# ---------------------------------------------------------------------------
# 1. The header row replaces the brand row + hamburger
# ---------------------------------------------------------------------------


class TestTheHamburgerIsGone:
    def test_no_toggler_remains(self, base: str) -> None:
        assert "navbar-toggler" not in base, (
            "the hamburger sat next to a five-tab bottom bar — two competing "
            "navigation systems on one screen"
        )

    def test_the_collapse_is_still_rendered_for_desktop(self, base: str) -> None:
        """CONTROL: the desktop nav must not have been removed with the toggler.

        ``navbar-expand-lg`` expands this inline at lg+, which is where the
        desktop links live. Deleting it would have been the easy way to make
        the hamburger assertion above pass.
        """
        assert 'id="navbarNav"' in base
        assert 'class="collapse navbar-collapse nav-links-wrapper"' in base
        # And at least one real destination is still in it.
        assert "url_for('ui.artists')" in base

    def test_the_menu_button_opens_the_sheet(self, base: str) -> None:
        assert 'data-bs-target="#systemSheet"' in base, (
            "the destination every displaced item now lives in"
        )


class TestTheSearchFieldIsDesktopOnly:
    def test_the_wrapper_is_hidden_below_lg(self, base: str) -> None:
        idx = base.index('id="navSearchForm"')
        # The wrapper's own tag, not a window of following markup.
        tag = base[base.rindex("<div", 0, idx) : idx]
        assert "d-none d-lg-block" in tag, (
            "below lg the search row cost ~64px of navbar height and duplicated "
            "the bottom nav's Search tab"
        )

    def test_the_markup_is_kept_not_deleted(self, base: str) -> None:
        """CONTROL: hiding must not become removing.

        The Ctrl+K binding, the flyout's mirroring and its `aria-keyshortcuts`
        all still target this element, and a hardware keyboard is the desktop
        case where the field is most useful.
        """
        assert 'id="navSearchInput"' in base
        assert 'aria-keyshortcuts="Control+K Meta+K"' in base
        assert 'id="navSearchKbdHint"' in base

    def test_the_bottom_nav_still_offers_search(self, base: str) -> None:
        """The tab is now the ONLY way a phone reaches the flyout."""
        idx = base.index('class="bottom-nav"')
        nav = base[idx : base.index("</nav>", idx)]
        assert "openUnifiedSearch()" in nav


class TestTheFlyoutHasAUsableInputOnAMobile:
    """⭐ The reported bug: "the Search button brings up the modal, but the
    search entry isn't there."

    The flyout was designed AROUND the navbar field: it had no input of its own,
    and ``openUnifiedSearch()`` focused ``#navSearchInput`` back. Its own box was
    a deliberately hidden MIRROR (``d-none`` + ``aria-hidden`` + ``tabindex=-1``)
    that was never typed into.

    That contract only holds while the navbar field is on screen at every width.
    Making it ``d-none d-lg-block`` — so the bottom nav's Search tab could own the
    mobile entry point — left the flyout with NO usable box below lg: the modal
    opened and there was nowhere to type. These pin the corrected contract: the
    flyout's own box is visible exactly when the navbar's is not.
    """

    MODAL = TS / "templates" / "components" / "_unified_search_modal.html"

    @pytest.fixture(scope="class")
    def modal(self) -> str:
        return _read(self.MODAL)

    def test_the_box_is_not_unconditionally_hidden(self, modal: str) -> None:
        """CONTROL on the old contract, which is what caused the report."""
        assert "input-group mb-2 d-none" not in modal, (
            "the flyout's own input is hidden at every width again — a phone "
            "opens the modal with nowhere to type"
        )

    def test_the_box_is_visible_below_lg_and_hidden_above(self, modal: str) -> None:
        """`d-lg-none` mirrors base.html's `d-none d-lg-block` on the navbar box.

        Visible exactly when the navbar's is not, so a phone always has ONE
        field and a desktop never has two.
        """
        classes = _wrapper_classes(modal)
        assert "d-lg-none" in classes, (
            f"the wrapper's classes are {sorted(classes)}; below lg the navbar "
            "field is d-none, so this box must be the visible one"
        )
        assert "d-none" not in classes, (
            "the box is hidden at every width again"
        )

    def test_it_is_not_hidden_from_assistive_tech(self, modal: str) -> None:
        """`aria-hidden` on an element the user must interact with is a
        violation — it removes a real control from the accessibility tree."""
        start = modal.rindex("<div", 0, modal.index('id="unifiedSearchIcon"'))
        wrapper = modal[start : modal.index(">", start) + 1]
        assert "aria-hidden" not in wrapper, (
            "the input's wrapper is aria-hidden while visible"
        )

    def test_it_is_focusable(self, modal: str) -> None:
        """`tabindex="-1"` kept it out of the tab order, which was right while it
        was permanently hidden and wrong now that it is the only field."""
        idx = modal.index('id="unifiedSearchInput"')
        tag = modal[modal.rindex("<input", 0, idx) : modal.index(">", idx)]
        assert 'tabindex="-1"' not in tag, (
            "the only search field on a phone cannot be tabbed to"
        )
        assert "inputmode=\"search\"" in tag, "the mobile keyboard hint was lost"

    def test_open_focuses_a_box_that_is_actually_on_screen(self) -> None:
        """The focus call has to follow the same rule as the markup.

        Focusing a `display:none` element is a silent no-op, so a phone would
        open the modal with the caret nowhere and typing going to the document.
        """
        js = _read(FLYOUT_JS)
        start = js.index("function openUnifiedSearch(")
        body = js[start : start + 2400]
        assert "isVisible(navInput)" in body, (
            "openUnifiedSearch does not check whether the navbar box is on screen"
        )
        assert "input.focus()" in body, (
            "nothing focuses the flyout's own box when the navbar's is hidden"
        )

    def test_reopening_does_not_wipe_a_query_typed_on_a_phone(self) -> None:
        """⭐ The subtle half of the same bug.

        The prefill read only the banner boxes. Below lg those are `d-none` and
        permanently empty, so the assignment would overwrite whatever the user
        had typed the previous time they opened the flyout — the query would
        vanish, which reads as "my search disappeared".
        """
        js = _read(FLYOUT_JS)
        start = js.index("function openUnifiedSearch(")
        body = js[start : start + 1200]
        prefill = body[body.index("let value = prefill") : body.index("selectScope(")]
        assert "input.value" in prefill, (
            "the flyout's own box is not part of the prefill chain, so a reopen "
            "clears the query below lg where the banner boxes are empty"
        )

    def test_the_caret_lands_in_a_box_that_is_on_screen(self) -> None:
        """⭐ The BEHAVIOURAL statement of the reported bug.

        Which element gets the caret is a question about a real DOM at a real
        width, and no source-text assertion answers it: `input.focus()` is
        present in both the broken and the fixed version — what changes is
        whether the element it names is rendered.

        `openUnifiedSearch` is brace-matched out of the shipped source and driven
        against a DOM stub whose `offsetWidth` models the two responsive classes,
        so this cannot silently drift from the code it describes.
        """
        probe = REPO_ROOT / "tests" / "js" / "search-focus-probe.js"
        result = subprocess.run(
            ["node", str(probe), str(FLYOUT_JS)],
            capture_output=True, text=True, timeout=60,
        )
        assert result.stdout.strip(), f"probe produced nothing: {result.stderr[:400]}"
        data = json.loads(result.stdout)
        assert "error" not in data, data.get("error")

        # MOBILE — the report. The navbar box is `d-none`, so the caret must go
        # to the flyout's own box or the user has nowhere to type.
        assert data["mobile_focuses_flyout"] is True, (
            "tapping Search on a phone leaves the caret nowhere"
        )
        assert data["mobile_does_not_focus_navbar"] is True, (
            "the caret went to the hidden navbar field"
        )
        # DESKTOP — unchanged. The navbar field is the entry point there.
        assert data["desktop_focuses_navbar"] is True
        assert data["desktop_does_not_focus_flyout"] is True
        # And the prefill chain, both ways round.
        assert data["mobile_reopen_preserves_query"] is True
        assert data["desktop_navbar_wins_the_prefill"] is True
        assert data["explicit_prefill_wins"] is True

    def test_the_flyout_ignores_a_hidden_box_as_the_query_source(self) -> None:
        """Otherwise every phone search would submit an empty query.

        ``getQuerySource`` falls back to the first banner box when nothing holds
        focus, and that box is now off-screen below lg: it exists, it is empty,
        and it cannot be typed into.
        """
        js = _read(FLYOUT_JS)
        start = js.index("function getQuerySource()")
        body = js[start : js.index("\n    }", start)]
        assert re.search(r"isVisible\(boxes\[i\]\)", body), (
            "the fallback must skip boxes that are not rendered, or a phone "
            "submits a blank query"
        )
        # The helper itself must measure layout, not just DOM presence.
        helper = js[js.index("function isVisible(el)") :]
        helper = helper[: helper.index("\n  }")]
        assert "offsetWidth" in helper and "offsetHeight" in helper

    def test_open_focuses_a_box_that_exists_on_screen(self) -> None:
        js = _read(FLYOUT_JS)
        start = js.index("function openUnifiedSearch(")
        body = js[start : start + 2200]
        assert "isVisible(navInput)" in body, (
            "focus() on a display:none element is a silent no-op, so the caret "
            "would land nowhere and the user's typing would go to the document"
        )


# ---------------------------------------------------------------------------
# 2. The header's own controls
# ---------------------------------------------------------------------------


class TestTheHeaderControls:

    def test_scan_and_menu_are_present_and_mobile_only(self, base: str) -> None:
        header = _header(base)
        for control in ("appHeaderScan", "appHeaderMenu"):
            tag = _tag_at(header, f'id="{control}"')
            assert "d-lg-none" in tag, (
                f"{control} must not render on desktop, where the navbar "
                "already carries these destinations"
            )

    def test_the_back_button_is_mobile_only_too(self, base: str) -> None:
        """Asserted per element, on purpose.

        A header-wide ``"d-lg-none" in header`` passes while every control
        BUT the one under test carries the class — which is exactly how the
        scan button's own class was removable without a failure.
        """
        tag = _tag_at(_header(base), 'id="appHeaderBack"')
        assert "d-lg-none" in tag

    def test_the_brand_is_desktop_only(self, base: str) -> None:
        """The title replaces it on mobile; showing both would be redundant."""
        header = _header(base)
        brand = _tag_at(header, "navbar-brand")
        assert "d-none d-lg-flex" in brand, (
            "the brand must be hidden below lg, where the page title takes "
            "its place"
        )
        assert "url_for('ui.dashboard')" in brand, (
            "CONTROL: still the home link on desktop, not just hidden markup"
        )

    def test_the_back_button_is_hidden_on_a_root_destination(self, base: str) -> None:
        """Server-side, so there is no flash of a control that cannot go back.

        A back button that does nothing is worse than no button: the user taps
        it, nothing happens, and the control stops being trusted.
        """
        header = _header(base)
        assert 'id="appHeaderBack"' in header
        assert re.search(
            r'id="appHeaderBack"[\s\S]{0,400}?\{% if _nav_home %\}hidden\{% endif %\}',
            header,
        ), "the back button must be `hidden` on a root destination"

    def test_the_onclick_handlers_are_real_exports(self, base: str, main_js: str) -> None:
        header = _header(base)
        assert 'onclick="navigateBack()"' in header
        assert 'onclick="requestContextScan()"' in header
        # An inline onclick that names a missing function is a dead control.
        assert re.search(r"function navigateBack\(\)", main_js)
        assert re.search(r"function requestContextScan\(\)", main_js)
        assert "global.navigateBack = navigateBack;" in main_js
        assert "global.requestContextScan = requestContextScan;" in main_js

    def test_back_prefers_history_then_falls_back(self, main_js: str) -> None:
        """History first, so arriving from a search result returns there."""
        start = main_js.index("function navigateBack()")
        body = main_js[start : start + 900]
        assert "history.back()" in body
        assert "history.length > 1" in body
        assert "data-fallback-href" in body, (
            "a deep link has no history, so a structural parent is the only "
            "honest destination left"
        )
        assert "/dashboard" in body, "the last-resort destination"

    def test_the_scan_button_degrades_to_the_log(self, main_js: str) -> None:
        """With no page handler it must still do something real."""
        start = main_js.index("function requestContextScan()")
        body = main_js[start : start + 900]
        assert "popularr:scan-request" in body, (
            "pages opt in by listening, which is what keeps the shell from "
            "knowing what is on the page"
        )
        assert "cancelable: true" in body
        assert "openUnifiedLogModal()" in body, (
            "the log is the one scanning view that exists on every page"
        )


# ---------------------------------------------------------------------------
# 3. The mobile title comes from the page's own <title>
# ---------------------------------------------------------------------------


class TestTheMobileTitle:

    def test_base_derives_it_from_the_title_block(self, base: str) -> None:
        assert "{% block mobile_title %}" in base
        assert "self.title()|page_heading" in base, (
            "a page names itself once, in {% block title %}; asking 30 "
            "templates to declare the same string twice invites drift"
        )

    def test_the_block_is_inside_the_header(self, base: str) -> None:
        header = _header(base)
        assert "{% block mobile_title %}" in header, (
            "the block must be the header's title element, not defined "
            "somewhere a page cannot reach it"
        )

    @pytest.mark.asyncio
    async def test_a_child_pages_title_reaches_the_header(self, app) -> None:
        """End-to-end: the override flows through ``self.title()``.

        Read from rendered markup rather than the source, because the wiring
        (`self.title()` from inside a *different* block) is the part that can
        silently return the base template's own default instead of the child's.
        """
        html = await _render(
            app,
            "/artist/zzz",
            "{% extends 'base.html' %}"
            "{% block title %}When Your Heart Stops Beating · zzz · Popularr{% endblock %}",
        )
        title = _title_text(html)
        assert title.strip() == "When Your Heart Stops Beating", (
            f"the header title read {title.strip()!r}; the brand and the "
            "artist are context, not the page's name"
        )

    @pytest.mark.asyncio
    async def test_it_does_not_fall_back_to_the_base_default(self, app) -> None:
        """CONTROL: base's own title is 'Popularr Dashboard'.

        If ``self.title()`` resolved against the BASE template instead of the
        child, every page would render the same heading and the test above
        would still pass whenever the child happened to say 'Dashboard'.
        """
        html = await _render(
            app,
            "/playlists",
            "{% extends 'base.html' %}{% block title %}Playlists – Popularr{% endblock %}",
        )
        assert _title_text(html).strip() == "Playlists"

    @pytest.mark.asyncio
    async def test_a_page_can_override_the_wording(self, app) -> None:
        html = await _render(
            app,
            "/dashboard",
            "{% extends 'base.html' %}{% block title %}Dashboard · Popularr{% endblock %}"
            "{% block mobile_title %}Home{% endblock %}",
        )
        assert ">Home<" in html

    @pytest.mark.asyncio
    async def test_the_back_button_hidden_state_follows_the_path(self, app) -> None:
        for path, hidden in (("/dashboard", True), ("/", True), ("/artists", False),
                             ("/album/1", False)):
            html = await _render(app, path)
            tag = _tag_at(_header(html), 'id="appHeaderBack"')
            assert ("hidden" in tag) is hidden, (
                f"{path}: expected hidden={hidden} on the back button"
            )


# ---------------------------------------------------------------------------
# 4. The system sheet
# ---------------------------------------------------------------------------


class TestTheSystemSheet:

    def test_it_is_included_by_the_shell(self, base: str) -> None:
        assert SYSTEM_SHEET.is_file()
        assert "components/_system_sheet.html" in base, (
            "the sheet is shell furniture — it must not depend on a page "
            "including it"
        )

    def test_it_is_a_bottom_sheet(self, sheet: str) -> None:
        assert "offcanvas-bottom" in sheet, (
            "it must come from the same edge as the bar that opened it"
        )
        assert 'id="systemSheet"' in sheet

    def test_every_displaced_destination_is_reachable(self, sheet: str, base: str) -> None:
        """The hamburger held these; removing it must not strand them.

        ⚠️ REACHABLE, not "listed here". `Dashboard` and `All Artists` were
        deliberately removed from this sheet because the bottom bar already
        carries them as `Home` and `Artists` — so the property to assert is that
        a phone can still GET to each destination, from either surface. Pinning
        the sheet alone would freeze a duplicate the design brief asked to drop.
        """
        # The bar is in the shell, next to the sheet include.
        bottom_bar = base[base.index('class="bottom-nav"'): base.index('</nav>', base.index('class="bottom-nav"'))]
        reachable = sheet + bottom_bar

        for endpoint in (
            "ui.dashboard", "ui.artists", "ui.bookmarks", "ui.logs",
            "ui.correcting", "ui.help_page", "ui.config_editor", "ui.logout",
        ):
            assert f"url_for('{endpoint}')" in reachable, (
                f"{endpoint} is now unreachable from a phone"
            )
        for literal in ("/discover", "/downloads/discover/upcoming", "/missing"):
            assert f'href="{literal}"' in sheet, f"{literal} lost with the hamburger"

    def test_the_duplicates_the_brief_names_are_gone(self, sheet: str, base: str) -> None:
        """⭐ `Dashboard` / `All Artists` must NOT be in the sheet, and MUST be
        in the bottom bar — the other half of the deduplication.

        Asserted as a PAIR on purpose: removing them from the sheet without
        them being in the bar is the failure that would actually stranding a
        user, and asserting only one half would pass in that case.
        """
        bottom_bar = base[base.index('class="bottom-nav"'): base.index('</nav>', base.index('class="bottom-nav"'))]

        assert "url_for('ui.dashboard')" not in sheet, (
            "Dashboard is a duplicate — the bottom bar's Home tab already goes there"
        )
        assert "url_for('ui.artists')" not in sheet, (
            "All Artists is a duplicate — the bottom bar's Artists tab already goes there"
        )
        assert "url_for('ui.dashboard')" in bottom_bar, "Home must remain the way to Dashboard"
        assert "url_for('ui.artists')" in bottom_bar, "Artists must remain the way to All Artists"

    @pytest.mark.asyncio
    async def test_the_sheet_renders_with_the_menu_button_pointing_at_it(self, app) -> None:
        html = await _render(app, "/dashboard")
        assert 'id="systemSheet"' in html
        assert 'data-bs-target="#systemSheet"' in html

    def test_no_destination_exists_only_in_the_sheet(self, base: str, sheet: str) -> None:
        """Parity with the desktop navbar, in the direction that bites.

        Both lists name the same destinations in different shapes, so a new
        page added to one and not the other is the realistic failure. This
        asserts desktop -> (bottom bar + sheet); the reverse is deliberately
        *not* asserted, because the sheet groups a few secondary download
        destinations that the navbar does not list.

        ⚠️ The bottom bar is part of the target set. `Dashboard` and `All
        Artists` live there as Home/Artists and are no longer repeated in the
        sheet, so checking the sheet alone would report two false failures.
        """
        nav_start = base.index('class="navbar-nav')
        nav = base[nav_start : base.index("</ul>", base.index("bookmarksDropdown", nav_start))]
        desktop = set(re.findall(r"url_for\('(ui\.\w+)'\)", nav))
        assert desktop, "the navbar scan found nothing — the slice is wrong"

        bar_start = base.index('class="bottom-nav"')
        bottom_bar = base[bar_start : base.index("</nav>", bar_start)]
        reachable = bottom_bar + sheet

        missing = sorted(e for e in desktop if f"url_for('{e}')" not in reachable)
        assert not missing, (
            f"{missing} are in the desktop navbar but unreachable on a phone "
            "(neither the bottom bar nor the system sheet leads there)"
        )


# ---------------------------------------------------------------------------
# 5. The status bar: hairline -> pill
# ---------------------------------------------------------------------------


class TestTheStatusBarIsAnOverlayOnMobile:

    def test_the_token_stops_reserving_space(self, css: str) -> None:
        idx = css.index("@media (max-width: 991.98px)")
        block = css[idx : css.index("}", css.index("--status-bar-height", idx)) + 1]
        assert "--status-bar-height: 0px" in block, (
            "the bar is `fixed` on mobile, so leaving the token at 2.4rem would "
            "keep a gap for a 2px line"
        )

    def test_the_idle_state_is_a_hairline_and_not_tappable(self, css: str) -> None:
        # The LAST definition: the desktop rule comes first in the file, and
        # anchoring on an earlier `@media (max-width: 991.98px)` block (the
        # layout tokens) would pick that one up instead.
        idx = css.rindex("#scanStatusBar {")
        block = css[idx : css.index("&.is-active {", idx)]
        assert "position: fixed" in block
        assert "height: 2px" in block
        assert "pointer-events: none" in block, (
            "an invisible 2px strip must not swallow taps meant for the content "
            "behind it"
        )
        assert "display: none" in block, "idle text must not render"

    def test_the_active_state_restores_the_pill(self, css: str) -> None:
        idx = css.index("&.is-active {", css.rindex("#scanStatusBar {"))
        block = css[idx : idx + 1000]
        assert "pointer-events: auto" in block, (
            "the pill IS tappable (it opens the log) — the control has to undo "
            "the idle state's pointer-events: none"
        )
        assert "display: inline-flex" in block
        assert "#scanStatusLine" in block and "min-width: 0" in block, (
            "a long current_item must ellipsise rather than push the Log "
            "button off the edge"
        )

    def test_the_progress_line_exists_in_the_markup(self, base: str) -> None:
        assert 'id="scanBarFill"' in base
        assert 'class="scan-bar-fill"' in base

    def test_both_writers_switch_the_same_state(self, main_js: str) -> None:
        """Two pollers own this bar; they must not disagree.

        ``dashboard.js`` polls on the dashboard and ``main.js`` streams
        everywhere else, so a state change made in only one of them would make
        the pill's appearance depend on which page you were on.
        """
        # Inside the WRITER (updateGlobalScanBar), not the definition: the
        # calls belong to the poll that decides, and asserting them against
        # setScanBarActive's own body can never pass.
        start = main_js.index("function updateGlobalScanBar(")
        body = _function_body(main_js, start)
        assert re.search(r"setScanBarActive\(bar, true, pct\)", body), (
            "the global stream must switch the bar ON, or the pill only ever "
            "appears on the dashboard"
        )
        assert re.search(r"setScanBarActive\(bar, false, 0\)", body), (
            "and OFF, or a finished scan leaves the pill up forever"
        )

        dash = _read(DASHBOARD_JS)
        assert re.search(r"setActive\(document\.getElementById\('scanStatusBar'\), true, pct\)", dash), (
            "the dashboard's own poller never switches the pill, so the bar "
            "would stay a hairline while a scan ran there"
        )
        assert re.search(r"setActive\(document\.getElementById\('scanStatusBar'\), false, 0\)", dash)

    def test_the_progress_fill_is_actually_written(self, main_js: str) -> None:
        """The hairline/pill switch carries the progress too.

        Class-and-width are one operation: setting the class alone would show
        the PREVIOUS scan's progress on a new one. Asserting the identifier
        appears is not enough — `const fill = null` keeps the name and drops
        the write.
        """
        start = main_js.index("function setScanBarActive(")
        body = _function_body(main_js, start)
        assert re.search(r"fill\.style\.width\s*=", body), (
            "the progress fill must be written, not just looked up"
        )
        assert "getElementById('scanBarFill')" in body

    def test_idle_is_not_announced_as_a_button_on_mobile_only(self, main_js: str) -> None:
        """The bar is two different things at the two breakpoints.

        Above lg it carries text and is the only way to open the log, so it
        stays a button while idle. Below lg the idle state is a 2px line with
        `pointer-events: none`, so it must not be announced as a button — but
        the switch has to be scoped, or it removes the desktop keyboard path.
        """
        start = main_js.index("function setScanBarActive(")
        body = _function_body(main_js, start)
        assert "matchMedia('(max-width: 991.98px)')" in body, (
            "the accessibility switch must be scoped to mobile, or an idle "
            "desktop bar stops being a button"
        )
        assert "removeAttribute('role')" in body
        assert "removeAttribute('tabindex')" in body
        # `aria-hidden` is named in BOTH branches (the active one removes it),
        # so a bare substring check is satisfied by the wrong branch. Pin the
        # idle state's actual write.
        assert re.search(r"setAttribute\('aria-hidden',\s*'true'\)", body), (
            "the idle mobile bar must be hidden from assistive tech, not just "
            "made untappable"
        )



# ---------------------------------------------------------------------------
# 6. Height actually reclaimed
# ---------------------------------------------------------------------------


class TestChromeHeightIsReclaimed:

    def test_the_mobile_navbar_token_shrank(self, css: str) -> None:
        idx = css.index("@media (max-width: 991.98px)")
        block = css[idx : idx + 400]
        assert "--navbar-height: 56px" in block, (
            "the search row is gone from mobile, so the 132px fallback is now "
            "over-reserving ~76px of every phone screen"
        )
        assert "--navbar-height: 132px" not in css, (
            "the stale 132px fallback would still win on a browser that "
            "applies the older rule"
        )

    def test_there_is_no_second_mobile_navbar_value(self, css: str) -> None:
        """One writer per measurement.

        The 575.98px override existed because the search row wrapped
        differently on small screens; with no search row there is one header
        height, and a second value would only create the 68/96/112-style
        disagreement this token was introduced to end.
        """
        assert "--navbar-height: 124px" not in css

    def test_the_header_row_is_one_line(self, css: str) -> None:
        for rule in (".app-header-lead", ".app-header-title"):
            idx = css.index(f"{rule} {{")
            block = css[idx : css.index("}", idx)]
            assert "min-width: 0" in block, (
                f"{rule} needs min-width:0 or a long artist/album name pushes "
                "the header buttons off screen instead of truncating"
            )


# ---------------------------------------------------------------------------
# 7. page_heading — the reduction rules
# ---------------------------------------------------------------------------


class TestPageHeading:
    """Unit rules, because the filter decides what every phone shows."""

    @staticmethod
    def _heading(value):
        from helpers.template_filters import page_heading
        return page_heading(value)

    @pytest.mark.parametrize(
        "title,expected",
        [
            # Brand is context, in either position.
            ("Popularr Dashboard", "Dashboard"),
            ("Dashboard · Popularr", "Dashboard"),
            ("Artists - Popularr", "Artists"),
            # Name, then context. The first segment is the page's own name.
            ("Corrections · +44 · Popularr", "Corrections"),
            ("When Your Heart Stops Beating · +44 · Popularr",
             "When Your Heart Stops Beating"),
            ("Downloads &amp; Monitor · Popularr", "Downloads &amp; Monitor"),
            # A hyphen is a legitimate character in a title, not a separator.
            ("Love - Hate · Popularr", "Love - Hate"),
            ("Love - Hate", "Love - Hate"),
            # Nothing to trim: pass through whole rather than guessing.
            ("Genres & Moods Analytics", "Genres & Moods Analytics"),
            ("Albums Missing Tracks", "Albums Missing Tracks"),
            # Degenerate input must not blank the header.
            ("", ""),
            (None, ""),
            ("· Popularr", "· Popularr"),
        ],
    )
    def test_the_reduction(self, title, expected) -> None:
        assert self._heading(title) == expected

    def test_a_dash_is_not_treated_as_a_joiner(self) -> None:
        """The specific regression: "Love - Hate" is a name, not name + context.

        Splitting on " - " truncated it to "Love", which is a wrong page title
        rather than a shorter one.
        """
        from helpers.template_filters import _HEADING_SEPARATORS
        assert " - " not in _HEADING_SEPARATORS

    def test_an_unclosed_brand_style_leading_rule(self) -> None:
        """CONTROL: only the exact brand is stripped, not any leading word."""
        assert self._heading("Popularr") == "Popularr"
        assert self._heading("Populace") == "Populace"
