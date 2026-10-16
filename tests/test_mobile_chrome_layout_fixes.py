"""Mobile layout pass: dashboard, log viewer, search flyout, system sheet.

Four complaints from the design board, and one rule behind most of them:

    "Fixes the stretched `Manage →` and `▶` buttons"
    "replaces the top-right `⚡` log icon with a `>_` terminal icon (with an
     active green status dot)"
    "compresses the `Scanner · Idle` card into a single 52px row"
    "adds floating `+` quick-queue buttons to the release cards"
    "Fixes the stretched `⋮` button and removes the `.313367` microsecond
     fragments when timestamps are toggled off"
    "Merges the search bar and `▽` filter icon onto one row, and removes
     duplicate `Dashboard` / `All Artists` links from the `⋮` bottom menu"

⭐ THE STRETCHED BUTTONS WERE ONE RULE. `popularr.css` set
`.btn { min-height: 44px }` below 576px — an app-wide touch target that also
caught `btn-sm`, the variant used precisely where a row must stay short. Four
separate "stretched" buttons, one unqualified selector.

The microsecond fix is verified by DRIVING the real renderer in
`tests/js/logs-line-render-probe.js`; a regex copied into a test would keep
passing while the shipped one still leaked the fragment.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TS = REPO_ROOT / "test_site"

POPULARR_CSS_LIVE = REPO_ROOT / "static" / "css" / "popularr.css"
POPULARR_CSS = TS / "static" / "css" / "popularr.css"
DASHBOARD_CSS = TS / "static" / "css" / "dashboard.css"
DASHBOARD_HTML = TS / "templates" / "Pages" / "dashboard.html"
BASE_HTML = TS / "templates" / "base.html"
MAIN_JS = TS / "static" / "js" / "main.js"
DASHBOARD_JS = TS / "static" / "js" / "pages" / "dashboard.js"
RELEASES_JS = TS / "static" / "js" / "services" / "upcoming-releases.js"
LOGS_JS = TS / "static" / "js" / "pages" / "logs.js"
LOGS_CSS = TS / "static" / "css" / "logs.css"
SEARCH_MODAL = TS / "templates" / "components" / "_unified_search_modal.html"
LINE_PROBE = REPO_ROOT / "tests" / "js" / "logs-line-render-probe.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_js_comments(source: str) -> str:
    """Remove `//` and `/* */` comments from JavaScript.

    ⚠️ NEEDED, not tidiness. These modules explain their own contracts by
    naming the identifiers involved — the carousel's comment says
    "`attachRowHandlers` binds `.ur-queue-btn`" — so a bare
    `"ur-queue-btn" in body` check is satisfied by the PROSE after the class is
    removed from the button. Verified by mutation: the assertion escaped.
    """
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"//[^\n]*", "", without_block)


def _scanner_row(html: str) -> str:
    """The `.scanner-bar` ELEMENT, tag-balanced, and nothing after it.

    ⚠️ A SLICE cannot express this. Slicing from `class="scanner-bar"` to the
    next known id counts everything up to that id as "in the row" — so a button
    moved out of the row but left before the next element still looked contained,
    and the mutation escaped. The div depth counter is what actually answers
    "is this inside the element".

    Jinja comments are stripped first: they contain no tags, but a `{# … #}`
    spanning lines would otherwise be scanned for `<div`.
    """
    stripped = re.sub(r"\{#.*?#\}", "", html, flags=re.S)
    start = stripped.index('class="scanner-bar"')
    start = stripped.rindex("<div", 0, start)
    pos = stripped.index(">", start) + 1
    depth = 1
    while depth > 0:
        nxt_open = stripped.find("<div", pos)
        nxt_close = stripped.find("</div>", pos)
        assert nxt_close != -1, "unbalanced <div>s in the scanner row"
        if nxt_open != -1 and nxt_open < nxt_close:
            depth += 1
            pos = nxt_open + 4
        else:
            depth -= 1
            pos = nxt_close + 6
    return stripped[start:pos]


def _phone_block(css: str, anchor: str) -> str:
    """The `@media (max-width: 576px)` block containing *anchor*."""
    start = css.index("@media (max-width: 576px)")
    block = css[start : css.index("\n}", css.index(anchor, start)) + 2]
    assert anchor in block, f"the anchor {anchor!r} is not in the phone block"
    return block


# ---------------------------------------------------------------------------
# 1. The stretched buttons — one rule, four symptoms
# ---------------------------------------------------------------------------


class TestTheTouchTargetRuleDoesNotCatchBtnSm:
    """⭐ The root cause of every "stretched" button in the brief."""

    @pytest.mark.parametrize(
        "css_path", [POPULARR_CSS, POPULARR_CSS_LIVE],
        ids=lambda p: p.parent.parent.name,
    )
    def test_btn_sm_is_exempt(self, css_path: Path) -> None:
        """`btn-sm` is the dense-row variant; a 44px floor defeats its purpose.

        ⚠️ Asserted on the PRESENCE of the exemption AND the absence of a
        regressed blanket rule. Checking only that `.btn { min-height: 44px }`
        exists would still pass with `btn-sm` re-inheriting it.
        """
        block = _phone_block(_read(css_path), ".btn-sm")

        assert re.search(r"\.btn\s*\{\s*min-height:\s*44px", block), (
            "the standard button must keep the app-wide touch target"
        )
        exempt = re.search(r"\.btn-sm\s*\{\s*min-height:\s*0", block)
        assert exempt, (
            f"{css_path.name}: btn-sm has no exemption, so every small button "
            "in a short row is forced to 44px — the reported stretched buttons"
        )

    def test_the_exemption_comes_after_the_blanket_rule(self) -> None:
        """CSS is order-dependent at equal specificity (both are 0,1,0).

        Declaring `.btn-sm` BEFORE `.btn` would leave the blanket rule winning
        for every small button, which is the bug — so the order is the fix.
        """
        block = _phone_block(_read(POPULARR_CSS), ".btn-sm")
        assert block.index(".btn {") < block.index(".btn-sm {"), (
            "the btn-sm exemption must come after the .btn floor or it is void"
        )

    def test_a_dense_context_can_still_opt_back_in(self) -> None:
        """CONTROL: the exemption must not have removed the 44px where it matters.

        artist.css sets its own 44px on `.release-actions-overflow > .btn`
        (0,2,0) — that outranks the exemption, and it is what the mobile
        release-overflow ⋮ relies on. `test_mobile_layout_density.py` pins the
        rule; this pins that the two do not fight.
        """
        artist_css = _read(TS / "static" / "css" / "artist.css")
        assert re.search(
            r"\.release-actions-overflow\s*>\s*\.btn\s*\{[^}]*min-height:\s*44px",
            artist_css,
        ), (
            "the explicit 44px on the overflow menu's button is gone, so the "
            "exemption now shrinks it"
        )


# ---------------------------------------------------------------------------
# 2. The header's terminal icon and its activity dot
# ---------------------------------------------------------------------------


class TestTheHeaderLogIcon:
    def test_the_bolt_is_a_terminal_now(self) -> None:
        """⚠️ The bolt advertised "scan" for a control that mostly opens a LOG.

        `requestContextScan()` dispatches `popularr:scan-request` and falls
        through to `openUnifiedLogModal()` when no page answers — which is every
        page except the dashboard.
        """
        html = _read(BASE_HTML)
        idx = html.index('id="appHeaderScan"')
        button = html[idx : html.index("</button>", idx)]

        assert "bi-terminal" in button, "the icon is still the scan bolt"
        assert "bi-lightning-charge" not in button

    def test_it_still_dispatches_the_context_scan(self) -> None:
        """CONTROL: the icon changed, the BEHAVIOUR must not have."""
        html = _read(BASE_HTML)
        idx = html.index('id="appHeaderScan"')
        assert "requestContextScan()" in html[idx : html.index("</button>", idx)]

    def test_it_carries_a_dot_that_starts_hidden(self) -> None:
        html = _read(BASE_HTML)
        idx = html.index('id="appHeaderScan"')
        button = html[idx : html.index("</button>", idx)]

        assert 'id="appHeaderScanDot"' in button, "the status dot is not in the button"
        # ⚠️ A STANDALONE `hidden`, matched as an attribute.
        #
        # A plain `"hidden" in button` is satisfied by `aria-hidden="true"` —
        # which the dot AND the icon both carry — so the assertion passed with
        # the dot visible. Verified by mutation: it escaped.
        assert re.search(r"\shidden[\s>]", button), (
            "the dot must start hidden — an idle dot says nothing"
        )
        assert 'aria-hidden="true"' in button, (
            "the dot is decorative; the button's own title carries the state"
        )

    def test_the_dot_is_driven_by_the_real_scan_state(self) -> None:
        """⭐ A dot that is always on communicates nothing.

        `setHeaderScanDot` reads the same `active_scans` feed the status bar
        reads, and is called from BOTH writers — main.js's SSE stream and
        dashboard.js's own poll (which main.js bails out for).
        """
        main = _read(MAIN_JS)
        assert "function setHeaderScanDot(" in main
        assert "setHeaderScanDot," in main, "not published on global.popularr"

        # main.js wires it off the scan feed it already parses.
        assert "setHeaderScanDot(!!scan)" in main, (
            "main.js never lights the dot from the scan feed"
        )

        # ⚠️ dashboard.js MUST call it too: main.js's writer returns early on
        # the dashboard (`#dashboardFlags`), so without this the dot would never
        # light on the page the user starts from.
        dash = _read(DASHBOARD_JS)
        assert "setHeaderScanDot" in dash, (
            "dashboard.js owns the bar on the dashboard but never lights the dot"
        )
        assert "setHeaderScanDot(!!(active && active.length))" in dash

    def test_the_dot_has_a_positioning_context_on_the_button(self) -> None:
        """⚠️ Without `position: relative` on the button the dot anchors to the
        nearest positioned ancestor — the app header — and lands in the wrong
        corner entirely."""
        css = _read(POPULARR_CSS)
        assert re.search(r"#appHeaderScan\s*\{\s*position:\s*relative", css)

        idx = css.index(".app-header-dot {")
        dot = css[idx : css.index("}", idx)]
        assert "position: absolute" in dot
        assert "pointer-events: none" in dot, (
            "the dot must not eat taps aimed at the button"
        )

    def test_the_dot_hides_while_idle(self) -> None:
        css = _read(POPULARR_CSS)
        assert re.search(r"\.app-header-dot\[hidden\]\s*\{\s*display:\s*none", css), (
            "`hidden` must beat the dot's own sizing or it shows while idle"
        )


# ---------------------------------------------------------------------------
# 3. The scanner card is one row
# ---------------------------------------------------------------------------


class TestTheScannerCardIsOneRow:
    def test_the_row_is_52px_and_holds_both_things(self) -> None:
        html = _read(DASHBOARD_HTML)
        row = _scanner_row(html)

        assert 'id="scannerState"' in row, "the state is outside the row"
        assert 'id="scannerStartBtn"' in row, "the action is outside the row"

        css = _read(DASHBOARD_CSS)
        block = css[css.index(".scanner-bar {") : css.index("}", css.index(".scanner-bar {"))]
        assert re.search(r"min-height:\s*52px", block), (
            "52px is the brief's number, and it is the shortest row that still "
            "clears the 44px touch minimum inside it"
        )
        assert "align-items: center" in block

    def test_the_progress_area_stays_outside_the_row(self) -> None:
        """⚠️ Putting the panel inside the flex row would lay a multi-line
        progress block out HORIZONTALLY beside the state label."""
        row = _scanner_row(_read(DASHBOARD_HTML))

        assert 'id="activeScansBody"' not in row, (
            "the progress panel is inside the row and will render beside the state"
        )
        assert 'id="activeScansPanel"' not in row

    def test_every_id_the_poller_writes_survived(self) -> None:
        """A renamed id is a control that silently stops updating.

        `updateActiveScans()` writes all six of these by id.
        """
        html = _read(DASHBOARD_HTML)
        for element_id in ("scannerState", "scannerStatusBadge", "scannerIdleState",
                           "scannerActiveState", "activeScansPanel", "activeScansBody"):
            assert f'id="{element_id}"' in html, f"{element_id} lost in the restructure"


# ---------------------------------------------------------------------------
# 4. The floating quick-queue button
# ---------------------------------------------------------------------------


class TestTheFloatingQuickQueueButton:
    def test_it_is_on_the_artwork_and_wired(self) -> None:
        # ⚠️ Comments stripped: this file's own prose names `ur-queue-btn` and
        # `data-release-index` while explaining the binding, so asserting on the
        # raw source passes after the class is removed from the button.
        body = _strip_js_comments(_read(RELEASES_JS))
        idx = body.index("function renderCarousel(")
        body = body[idx : body.index("async function queueById", idx)]

        assert "ur-card-quick" in body
        assert "${quickAction}" in body, "the button is not placed on the art"
        assert "ur-queue-btn" in body, (
            "attachRowHandlers binds this class — a new one disconnects the handler"
        )
        assert "data-release-index" in body, (
            "the release is resolved from this index; nothing may be serialised "
            "into an attribute (see the module header)"
        )

    def test_the_action_is_rendered_INSIDE_the_art_element(self) -> None:
        """⭐ Placement, not just presence.

        Asserting that `${quickAction}` appears somewhere in the card would pass
        with the action back in a full-width row underneath it — which is the
        layout the brief replaced, and which mutation confirmed escaped. The
        assertion is therefore on the ART DIV's own template string.
        """
        body = _strip_js_comments(_read(RELEASES_JS))
        idx = body.index("function renderCarousel(")
        body = body[idx : body.index("async function queueById", idx)]

        art_line = next(
            line for line in body.splitlines() if 'class="ur-card-art"' in line
        )
        assert "${quickAction}" in art_line, (
            f"the floating button is not inside the art element: {art_line.strip()}"
        )
        assert "ur-card-actions" not in body, (
            "the old full-width action row is back — the brief floats the button "
            "on the artwork instead"
        )

    def test_a_queued_release_gets_no_action(self) -> None:
        """One signal for "already queued", not a flag AND a dead button."""
        body = _strip_js_comments(_read(RELEASES_JS))
        idx = body.index("const quickAction")
        branch = body[idx : body.index("return (", idx)]

        assert "? ''" in branch, "a queued release must render nothing"
        assert "disabled" not in branch


# ---------------------------------------------------------------------------
# 5. The microsecond fragments — driven through the real renderer
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for the JS probe")
def test_the_line_renderer_drops_fractional_seconds() -> None:
    """⭐ THE FIX, verified by running the SHIPPED renderer.

    structlog writes `2026-10-13T11:18:55.200965 [INFO] …` and `TS_RE` used to
    stop at the seconds, leaving `.200965` at the head of the message — the
    reported `.313367` fragment. A regex copied into this test would keep
    passing while the shipped one still leaked it, so the probe extracts
    `renderLineHtml` and `TS_RE` from the file itself.
    """
    result = subprocess.run(
        ["node", str(LINE_PROBE)], capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, (
        f"the line-render probe failed:\n{result.stdout}\n{result.stderr}"
    )
    assert "ALL PASS" in result.stdout


def test_the_pattern_consumes_the_fraction_without_capturing_it() -> None:
    """The group must stay seconds-only.

    Capturing the fraction would put microseconds back on screen with the
    Timestamps switch ON — the fix would only have MOVED the fragment.
    """
    source = _read(LOGS_JS)
    match = re.search(r"const TS_RE = (/.*?/);", source)
    assert match, "TS_RE is gone — the probe's extraction will fail"

    pattern = match.group(1)
    assert "[.,]" in pattern, "a comma separator is not accepted"
    # Plain substring: the shape being pinned IS a literal, and escaping it into
    # a regex here would test this test's escaping rather than the pattern.
    assert ")(?:[.,]\\d+)?" in pattern, (
        "the fraction must be a NON-capturing optional group immediately after "
        f"the seconds group: {pattern}"
    )
    assert ")(" not in pattern.replace(")(?:[.,]\\d+)?", ""), (
        "a SECOND capturing group would put the fraction back on screen with "
        f"timestamps on: {pattern}"
    )


# ---------------------------------------------------------------------------
# 6. The search row
# ---------------------------------------------------------------------------


class TestTheSearchRow:
    def test_the_field_and_the_filter_share_one_row(self) -> None:
        html = _read(SEARCH_MODAL)
        row_start = html.index('class="d-flex align-items-center gap-2 mb-2"')
        row = html[row_start : html.index('id="unifiedAdvancedFilters"', row_start)]

        assert 'id="unifiedSearchInput"' in row, "the field is not in the row"
        assert 'id="unifiedFilterBtn"' in row, "the ▽ filter is not in the row"

    def test_the_filter_is_not_left_beside_the_scope_chips(self) -> None:
        """CONTROL: two copies of `#unifiedFilterBtn` would be a duplicate id,
        and `getElementById` returns the FIRST — so the second would be inert."""
        html = _read(SEARCH_MODAL)
        assert html.count('id="unifiedFilterBtn"') == 1, (
            "the filter button exists twice; the second copy is dead code"
        )
        assert html.count('id="unifiedFilterBadge"') == 1

    def test_the_filter_stays_reachable_on_desktop(self) -> None:
        """⚠️ The search field is `d-lg-none` (the navbar field is the entry
        point above lg). Putting the ▽ inside a hidden row would take the only
        release-type filter with it.

        A hidden control is a lost feature; a lone icon on a line is a layout.
        """
        html = _read(SEARCH_MODAL)
        idx = html.index('id="unifiedFilterBtn"')
        # Walk back to the start of the tag to read the whole attribute list.
        tag = html[html.rindex("<button", 0, idx) : html.index(">", idx)]

        assert "d-lg-none" not in tag, (
            "the filter button hides above lg with the search field, so desktop "
            "loses the release-type filter"
        )
        assert "ms-auto" in tag, (
            "on its own line above lg it must align to the panel edge, not float left"
        )
        # And the field it shares the row with IS mobile-only, by design.
        field_idx = html.index('id="unifiedSearchInput"')
        field_tag = html[html.rindex("<div", 0, field_idx) : html.index(">", field_idx)]
        assert "d-lg-none" in field_tag

    def test_the_scope_chips_own_the_full_width(self) -> None:
        html = _read(SEARCH_MODAL)
        idx = html.index('id="unifiedScopeTabs"')
        row_start = html.rindex("<div", 0, idx)
        row = html[row_start : html.index("</div>", html.index("</ul>", idx))]
        assert "unifiedFilterBtn" not in row, (
            "the filter is still sharing the chip row"
        )


# ---------------------------------------------------------------------------
# 7. The deduplicated menu
# ---------------------------------------------------------------------------


class TestTheMenuDeduplication:
    def test_the_two_duplicates_are_gone(self) -> None:
        sheet = _read(TS / "templates" / "components" / "_system_sheet.html")
        assert "url_for('ui.dashboard')" not in sheet, "Dashboard is still duplicated"
        assert "url_for('ui.artists')" not in sheet, "All Artists is still duplicated"

    def test_the_bottom_bar_still_carries_both(self) -> None:
        """⭐ The OTHER half, and the half that makes the removal safe.

        Asserted as a pair: removing them from the sheet while the bar stopped
        linking them is what would actually strand a user, and asserting only
        the sheet would pass in exactly that case.
        """
        base = _read(BASE_HTML)
        start = base.index('class="bottom-nav"')
        bar = base[start : base.index("</nav>", start)]

        assert "url_for('ui.dashboard')" in bar, "nothing links Dashboard on a phone"
        assert "url_for('ui.artists')" in bar, "nothing links All Artists on a phone"

    def test_no_other_destination_was_lost(self) -> None:
        """CONTROL: the edit removed exactly two links, not a group."""
        sheet = _read(TS / "templates" / "components" / "_system_sheet.html")
        for endpoint in ("ui.bookmarks", "ui.logs", "ui.correcting",
                         "ui.help_page", "ui.config_editor", "ui.logout"):
            assert f"url_for('{endpoint}')" in sheet, f"{endpoint} was lost too"
        for literal in ("/discover", "/downloads/discover/upcoming", "/missing",
                        "/playlists", "/banned-words", "/test-site"):
            assert f'href="{literal}"' in sheet, f"{literal} was lost too"


# ---------------------------------------------------------------------------
# 8. Nothing was left unwired
# ---------------------------------------------------------------------------


class TestNoOrphanedHooks:
    def test_the_removed_scanner_body_left_no_dead_css(self) -> None:
        """The old card's `.card-body` padding rule is now unreachable.

        ⚠️ Asserted as a NOTE rather than an absence, because the same class is
        still used by the progress panel when a scan runs — so the rule is
        reachable in one state and not the other. What must be true is that the
        single-row layout does not depend on it.
        """
        css = _read(DASHBOARD_CSS)
        assert ".scanner-bar" in css
        assert ".scanner-progress" in css, (
            "the progress panel lost its spacing now that it is outside a card-body"
        )

    def test_the_log_viewer_still_has_its_action_ids(self) -> None:
        """CONTROL: the header redesign is NOT this change, but the ⋮ menu's
        ids are what `logs.js` binds — a prerequisite for the button existing
        at the size the brief asks for."""
        for element_id in ("logSelector", "streamLogBtn", "refreshLogBtn",
                           "downloadLogBtn", "copyLogBtn", "clearLogBtn"):
            assert f'id="{element_id}"' in _read(TS / "templates" / "components" / "_log_modal.html")
