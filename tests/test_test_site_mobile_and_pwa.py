"""test_site mobile/touch, PWA and dashboard-usability contract.

These pin the 2026-10-13 test_site UI pass:

1. The FIXED navbar no longer covers main's scrollbar. `main` reserves the
   gutter (`scrollbar-gutter: stable`) and the bar's right edge is pulled in by
   `--scrollbar-width`, measured from `main` in main.js. The bar is
   `position: fixed; left:0; right:0`, so before this it sat over the top of
   the scrollbar and the scrollbar could not be grabbed there.
2. PWA shell — `viewport-fit=cover` (WITHOUT it iOS reports the safe-area
   insets as 0, making the existing `env(safe-area-inset-bottom)` a no-op),
   theme-color, manifest + icons, apple-* meta.
3. Search inputs are `type="search"` with the mobile keyboard hints.
4. The dashboard's Upcoming Releases renders into BOTH the desktop wrapper and
   the mobile card container. Rendering only into `#upcomingTableWrap`
   (`d-none d-lg-block`) left `#upcoming-releases-cards` (`d-lg-none`)
   permanently empty — a blank card on a phone.
5. Dashboard status regions are live regions and the icon-only stop buttons
   have accessible names.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TS = REPO_ROOT / "test_site"

BASE_HTML = TS / "templates" / "base.html"
DASHBOARD_HTML = TS / "templates" / "Pages" / "dashboard.html"
DASHBOARD_JS = TS / "static" / "js" / "pages" / "dashboard.js"
MAIN_JS = TS / "static" / "js" / "main.js"
POPULARR_CSS = TS / "static" / "css" / "popularr.css"
UNIFIED_MODAL = TS / "templates" / "components" / "_unified_search_modal.html"
SOULSEEK = TS / "templates" / "components" / "search" / "_soulseek.html"
MANIFEST = TS / "static" / "manifest.webmanifest"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. The navbar must not cover the scrollbar
# ---------------------------------------------------------------------------

class TestNavbarDoesNotCoverTheScrollbar:

    def test_main_reserves_the_scrollbar_gutter(self) -> None:
        css = _read(POPULARR_CSS)
        idx = css.index("main.container-fluid {")
        window = css[idx: idx + 700]
        assert "scrollbar-gutter: stable" in window, (
            "main must reserve the scrollbar gutter, or content shifts sideways "
            "the moment a page grows past one screen"
        )

    def test_the_navbar_right_edge_uses_the_measured_width(self) -> None:
        css = _read(POPULARR_CSS)
        assert ".navbar.fixed-top {" in css
        idx = css.index(".navbar.fixed-top {")
        window = css[idx: idx + 200]
        assert "right: var(--scrollbar-width" in window, (
            "the fixed navbar must pull its right edge in by the scrollbar "
            "width, or it sits over the scrollbar"
        )

    def test_main_js_measures_the_scrollbar_from_main(self) -> None:
        js = _read(MAIN_JS)
        assert "function syncScrollbarWidth()" in js
        idx = js.index("function syncScrollbarWidth()")
        window = js[idx: idx + 700]
        # Must measure the SCROLL CONTAINER, not the document: body is
        # overflow:hidden, so the document's client width says nothing.
        assert "main.container-fluid" in window, (
            "the scrollbar width must be measured from main (the scroll "
            "container), not the document"
        )
        assert "offsetWidth - main.clientWidth" in window, (
            "the gutter width is offsetWidth - clientWidth"
        )
        assert "'--scrollbar-width'" in window, (
            "the measured width must be written to the --scrollbar-width token"
        )

    def test_the_measurement_runs_on_init_and_resize(self) -> None:
        js = _read(MAIN_JS)
        assert "syncScrollbarWidth();" in js, "must run once at init"
        assert "addEventListener('resize', syncScrollbarWidth)" in js, (
            "the scrollbar can appear/disappear with the window width"
        )

    def test_it_is_exported_for_the_probes(self) -> None:
        js = _read(MAIN_JS)
        idx = js.index("global.popularr = {")
        assert "syncScrollbarWidth," in js[idx: idx + 200]


# ---------------------------------------------------------------------------
# 2. PWA shell
# ---------------------------------------------------------------------------

class TestPwaShell:

    def test_viewport_fit_cover_is_present(self) -> None:
        base = _read(BASE_HTML)
        assert "viewport-fit=cover" in base, (
            "without viewport-fit=cover iOS reports env(safe-area-inset-*) as "
            "0, making the bottom sheet's safe-area padding a no-op"
        )

    def test_theme_color_and_apple_meta(self) -> None:
        base = _read(BASE_HTML)
        assert '<meta name="theme-color"' in base
        assert "apple-mobile-web-app-capable" in base
        assert "apple-mobile-web-app-title" in base

    def test_the_manifest_is_linked_and_parses(self) -> None:
        base = _read(BASE_HTML)
        assert "manifest.webmanifest" in base
        assert MANIFEST.is_file(), "the manifest file must exist"
        data = json.loads(_read(MANIFEST))
        assert data["name"] == "Popularr"
        assert data["display"] == "standalone"
        assert data["start_url"]
        assert data["icons"], "a manifest without icons is not installable"

    def test_manifest_icons_exist_on_disk(self) -> None:
        data = json.loads(_read(MANIFEST))
        for icon in data["icons"]:
            rel = icon["src"].lstrip("/")  # "/static/img/icon.svg" -> static/img/...
            # The src is served by the static view, which resolves test_site
            # first; assert the file is really there.
            assert (TS / rel).is_file(), f"manifest icon missing: {icon['src']}"
            assert icon["type"] == "image/svg+xml"

    def test_a_maskable_icon_is_declared(self) -> None:
        data = json.loads(_read(MANIFEST))
        purposes = {i.get("purpose") for i in data["icons"]}
        assert "maskable" in purposes, (
            "Android masks the icon; a maskable variant avoids clipping"
        )

    def test_favicon_and_apple_touch_icon(self) -> None:
        base = _read(BASE_HTML)
        assert 'rel="icon"' in base
        assert 'rel="apple-touch-icon"' in base


# ---------------------------------------------------------------------------
# 3. Mobile keyboard hints on the search inputs
# ---------------------------------------------------------------------------

SEARCH_INPUTS = (
    ("base.html nav search", BASE_HTML, "navSearchInput"),
    ("unified search modal", UNIFIED_MODAL, "unifiedSearchInput"),
    ("soulseek search", SOULSEEK, "slskdSearchInput"),
)


class TestSearchInputsAreMobileFriendly:

    @pytest.mark.parametrize("label,path,field_id", SEARCH_INPUTS)
    def test_type_is_search(self, label, path, field_id) -> None:
        body = _read(path)
        idx = body.index(f'id="{field_id}"')
        # Look back a little for the tag's attributes.
        window = body[max(0, idx - 200): idx + 400]
        assert 'type="search"' in window, (
            f"{label}: a text input gives a plain Return key and no clear button"
        )

    @pytest.mark.parametrize("label,path,field_id", SEARCH_INPUTS)
    def test_mobile_keyboard_hints(self, label, path, field_id) -> None:
        body = _read(path)
        idx = body.index(f'id="{field_id}"')
        window = body[max(0, idx - 200): idx + 500]
        for attr in ('enterkeyhint="search"', 'inputmode="search"',
                     'autocapitalize="off"', 'autocorrect="off"'):
            assert attr in window, f"{label}: missing {attr}"

    def test_autocorrect_off_stops_name_mangling(self) -> None:
        # The whole point: an artist typed lower-case must not be "corrected".
        for _label, path, field_id in SEARCH_INPUTS:
            body = _read(path)
            idx = body.index(f'id="{field_id}"')
            window = body[max(0, idx - 200): idx + 500]
            assert 'spellcheck="false"' in window


# ---------------------------------------------------------------------------
# 4. The dashboard Upcoming card must not be blank on mobile
# ---------------------------------------------------------------------------

class TestUpcomingRendersOnBothBreakpoints:

    def test_both_containers_exist_in_the_template(self) -> None:
        html = _read(DASHBOARD_HTML)
        assert 'id="upcomingTableWrap"' in html
        assert 'id="upcoming-releases-cards"' in html
        # The desktop wrapper is hidden below lg and the mobile one above it,
        # so BOTH must be populated for either breakpoint to show anything.
        assert "d-none d-lg-block" in html
        assert "d-lg-none" in html

    def test_the_loader_renders_into_both(self) -> None:
        js = _read(DASHBOARD_JS)
        idx = js.index("async function loadUpcomingReleasesTable()")
        window = js[idx: idx + 2200]
        assert "getElementById('upcomingTableWrap')" in window
        assert "getElementById('upcoming-releases-cards')" in window, (
            "the mobile container must be fetched, or it stays empty"
        )
        # And rendered into each — the mobile one is the regression.
        assert "renderTable('upcomingTableWrap'" in window
        assert "renderTable('upcoming-releases-cards'" in window, (
            "rendering only into the desktop wrapper left the mobile card blank"
        )

    def test_a_loading_state_is_shown(self) -> None:
        js = _read(DASHBOARD_JS)
        idx = js.index("async function loadUpcomingReleasesTable()")
        window = js[idx: idx + 1200]
        assert "spinner-border" in window, (
            "without a loading state the card reads as 'no releases' until the "
            "fetch resolves"
        )

    def test_an_error_is_shown_in_both_containers(self) -> None:
        js = _read(DASHBOARD_JS)
        idx = js.index("async function loadUpcomingReleasesTable()")
        window = js[idx: idx + 2200]
        catch_idx = window.index("} catch (error) {")
        tail = window[catch_idx: catch_idx + 500]
        assert "desktop.innerHTML = msg" in tail
        assert "mobile.innerHTML = msg" in tail


# ---------------------------------------------------------------------------
# 5. Dashboard accessibility
# ---------------------------------------------------------------------------

class TestDashboardAccessibility:

    LIVE_REGIONS = ("pop-status", "nav-status", "ess-status", "scannerStatusBadge",
                    "pending-approval-count")

    @pytest.mark.parametrize("field_id", LIVE_REGIONS)
    def test_status_regions_are_live(self, field_id) -> None:
        html = _read(DASHBOARD_HTML)
        idx = html.index(f'id="{field_id}"')
        window = html[max(0, idx - 200): idx + 200]
        assert 'aria-live="polite"' in window, (
            f"{field_id}: scan/approval state changes are never announced "
            "without a live region"
        )

    def test_progress_bars_have_progressbar_semantics(self) -> None:
        js = _read(DASHBOARD_JS)
        idx = js.index('role="progressbar"')
        window = js[idx: idx + 300]
        assert "aria-valuenow" in window
        assert "aria-valuemin" in window
        assert "aria-valuemax" in window

    def test_stop_buttons_have_accessible_names(self) -> None:
        html = _read(DASHBOARD_HTML)
        # Every stopAllScans() button is icon-only, so `title` alone is not a
        # reliable accessible name.
        count_stop = html.count("onclick=\"stopAllScans()\"")
        count_label = html.count('aria-label="Stop all scans"')
        assert count_stop >= 3, "expected the three per-pane stop buttons"
        assert count_label == count_stop, (
            f"{count_stop} stop buttons but {count_label} accessible names — "
            "every icon-only control needs an aria-label"
        )


# ---------------------------------------------------------------------------
# 6. Navbar: Missing folded into the Discover menu
# ---------------------------------------------------------------------------

class TestMissingFoldedIntoDiscover:

    def test_missing_is_not_a_top_level_item(self) -> None:
        base = _read(BASE_HTML)
        assert '<li class="nav-item"><a class="nav-link" href="/missing">' not in base, (
            "Missing should no longer compete with Artists/Downloads in the bar"
        )

    def test_missing_is_inside_the_discover_dropdown(self) -> None:
        base = _read(BASE_HTML)
        idx = base.index('id="discoverDropdown"')
        window = base[idx: idx + 1400]
        assert 'href="/missing"' in window, (
            "the link must still exist, inside the Discover menu"
        )

    def test_discover_still_lists_its_own_destinations(self) -> None:
        base = _read(BASE_HTML)
        idx = base.index('id="discoverDropdown"')
        window = base[idx: idx + 1400]
        assert 'href="/discover"' in window
        assert "/downloads/discover/upcoming" in window
