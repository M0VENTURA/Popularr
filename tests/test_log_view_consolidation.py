"""One log viewer: the page is retired and the modal inherits its controls.

Reported
--------
> The logs page and the progress ticker that brings up the logs are showing
> similar information. The dedicated log page should be removed and some of
> the features added into the modal instead.

Two views of the same files existed side by side:

* **the page** — `/logs`, server-rendered, with the file picker, the filter box,
  the level toggles, the Wrap switch and the scroll-to-bottom FAB;
* **the modal** — `#unifiedLogModal`, opened from the bottom status bar or the
  System menu, with five source tabs and Download / Clear / Pause, but no
  filtering, no file picker and no word wrap.

The modal wins because it preserves the page the user was on: clicking the
ticker to see what the scanner is doing should not rip you off the artist page.

What this suite pins is the CONSOLIDATION ITSELF — the four asks, plus the two
invariants that make it safe:

1. **The page is gone** (in test_site mode): `/logs` redirects to the dashboard
   with `?logs=1`, and base.html opens the modal for that flag. Live mode keeps
   its page — it never got the modal work, so redirecting there would remove
   functionality outright.
2. **Every control moved**: file picker, filter + `.*`/`Aa`, level toggles and a
   Wrap switch that is ON by default.
3. **The header is one row**: the five large tabs and the icon cluster are a
   single ⋮ overflow menu.
4. **No horizontal scrollbar**: `setWrap` writes `pre-wrap` + `break-word`.

Invariants:

* **ONE engine.** The page's controller (`logs.js`) became the modal's, and
  `main.js` no longer ships a second fetch/render loop — two implementations of
  the same viewer is precisely how the duplication started.
* **No eager fetch.** `logs.js` is loaded by `base.html` on every screen, so a
  `loadLog()` in `DOMContentLoaded` would cost a 500-line request per page view
  for a panel that may never open.

Source-contract tests: they assert markup and wiring, not rendered pixels.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODAL = REPO_ROOT / "test_site" / "templates" / "components" / "_log_modal.html"
BASE = REPO_ROOT / "test_site" / "templates" / "base.html"
LOGS_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "logs.js"
MAIN_JS = REPO_ROOT / "test_site" / "static" / "js" / "main.js"
UI_ROUTES = REPO_ROOT / "routes" / "ui_routes.py"
LOGS_ROUTE = REPO_ROOT / "routes" / "logs.py"

#: The five labels the modal's source tabs used to carry. They survive as the
#: picker's option labels — collapsing two controls into one must not lose the
#: meaning the tabs carried.
CURATED_VIEWS = {
    "unified_scan.log": "Scanner",
    "queue.log": "Queue",
    "search.log": "Soulseek",
    "info.log": "Navidrome",
    "error.log": "System",
}


@pytest.fixture(scope="module")
def modal() -> str:
    return MODAL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def base() -> str:
    return BASE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def logs_js() -> str:
    return LOGS_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def main_js() -> str:
    return MAIN_JS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Comment stripping for NEGATIVE assertions
#
# Both files here DOCUMENT what they used to contain — main.js's header names
# LOG_SOURCE_FILES to say it moved, logs.js's init says "NOT loadLog()". A bare
# `token not in source` therefore fails against correct code, and worse, would
# keep passing if the token came BACK as real code.
#
# ⚠️ The stripper must not treat `//` inside a URL string as a comment: these
# files are full of `'/api/…'`, and cutting at the first `//` would delete the
# code the assertion is supposed to see. Hence the `:` guard.
# ---------------------------------------------------------------------------


def js_code(text: str) -> str:
    """The file's lines with comments removed (kept in order)."""
    code: list[str] = []
    in_block = False
    for line in text.splitlines():
        if in_block:
            if "*/" in line:
                line = line.split("*/", 1)[1]
                in_block = False
            else:
                continue
        if line.strip().startswith("/*"):
            if "*/" not in line:
                in_block = True
                continue
            line = line.split("*/", 1)[1]
        # An inline `//` starts a comment unless it is part of a URL (`http://`).
        cut = line.find("//")
        if cut != -1 and (cut == 0 or line[cut - 1] != ":"):
            line = line[:cut]
        code.append(line)
    return "\n".join(code)


def py_code(text: str) -> str:
    """Python lines with ``#`` comments removed."""
    return "\n".join(
        line.split("#", 1)[0]
        for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )


# ===========================================================================
# 1. The page is retired — but only where the modal replaced it
# ===========================================================================


class TestThePageRouteIsRetired:
    async def test_test_site_mode_redirects_to_the_modal(self, client, monkeypatch):
        """THE ASK: the dedicated route must stop serving a second viewer.

        ``?logs=1`` is the hand-off — base.html reads it and opens the modal,
        so an old bookmark still lands on the logs rather than on nothing.
        """
        monkeypatch.setattr(
            "helpers.test_site_mode.config_enables_test_site", lambda: True
        )

        response = await client.get("/logs")

        assert response.status_code == 302, "the page must not render a second viewer"
        location = response.headers["Location"]
        assert "logs=1" in location, (
            "the redirect must carry the flag that opens the modal, or an old "
            "bookmark lands on a dashboard with no logs in sight"
        )

    async def test_live_mode_still_serves_its_page(self, client, monkeypatch):
        """CONTROL — the modal work is test_site scoped, so live mode keeps the
        page it has. Redirecting there too would remove the file picker and
        filters with nothing to replace them."""
        monkeypatch.setattr(
            "helpers.test_site_mode.config_enables_test_site", lambda: False
        )

        response = await client.get("/logs")

        assert response.status_code == 200
        body = await response.get_data(as_text=True)
        assert "Application Logs" in body, "the live page must still render"

    def test_the_redirect_is_gated_on_the_cutover_helper(self):
        """Not a raw read of ``features.use_test_site``: the helper is the one
        the cutover itself uses, and it fails SAFE to disabled."""
        source = UI_ROUTES.read_text(encoding="utf-8")
        block = source[source.index('@ui_bp.route("/logs")'):]
        block = block[: block.index('@ui_bp.route("/help")')]
        # Comments out: this file's own docstring names features.use_test_site
        # to say it does NOT read it.
        code = py_code(block)

        assert "config_enables_test_site()" in code
        assert "features.use_test_site" not in code, (
            "the route re-read features.use_test_site directly"
        )


class TestTheSystemMenuStillReachesLogs:
    def test_the_menu_item_keeps_its_real_href(self, base):
        """A real ``href`` is the JS-off fallback — and it lands on the SAME
        viewer, because the route redirects to ``?logs=1``."""
        menu_start = base.index('id="systemDropdown"')
        menu = base[menu_start : base.index("</ul>", menu_start)]

        assert "url_for('ui.logs')" in menu

    def test_clicking_it_opens_the_modal_in_place(self, base):
        """Navigating would defeat the point: the modal exists to keep the
        artist/album page the user was browsing."""
        menu_start = base.index('id="systemDropdown"')
        menu = base[menu_start : base.index("</ul>", menu_start)]

        assert "openUnifiedLogModal()" in menu, (
            "the Logs item navigates instead of opening the modal in place"
        )
        assert "event.preventDefault()" in menu


# ===========================================================================
# 2. Every control the page owned is now in the modal
# ===========================================================================


class TestThePageControlsMovedIntoTheModal:
    @pytest.mark.parametrize(
        "element_id",
        [
            "logSelector",       # the file picker (was the page's dropdown)
            "logFilterInput",    # "Filter output…"
            "filterRegexBtn",    # .*
            "filterCaseBtn",     # Aa
            "logMatchCount",
            "logLevelFilters",   # All / Errors / Warnings / Info / Debug
            "wrapLinesCheck",
            "logTimestampToggle",
            "logOutput",
            "scrollToBottomBtn",
            "unreadLogBadge",
            "tailBannerText",
            "loadFullLogBtn",
            "streamLogBtn",
            "refreshLogBtn",
            "downloadLogBtn",
            "copyLogBtn",
            "clearLogBtn",
            "streamLogLabel",
        ],
    )
    def test_the_control_exists_in_the_modal(self, modal, element_id):
        assert f'id="{element_id}"' in modal, f"#{element_id} never moved into the modal"

    def test_the_level_toggles_are_the_same_five(self, modal):
        """The page's level filter, verbatim — it is a filter over the same
        lines, so its vocabulary must not drift."""
        for level in ("all", "error", "warning", "info", "debug"):
            assert f'data-level="{level}"' in modal

    def test_wrap_is_on_by_default(self, modal):
        """THE FOURTH ASK. ``checked`` in the markup so the <pre> is wrapped
        from the first paint rather than after the first change event."""
        start = modal.index('id="wrapLinesCheck"')
        tag = modal[modal.rindex("<", 0, start) : modal.index(">", start)]

        assert "checked" in tag, "Wrap must default to ON"

    def test_the_stream_is_wrapped_in_javascript_not_only_markup(self, logs_js):
        """The checkbox alone does nothing: `setWrap` has to write the
        white-space rules, and it has to be CALLED on init (a `checked`
        checkbox never fires `change`)."""
        assert "output.style.whiteSpace = checked ? 'pre-wrap' : 'pre'" in logs_js
        assert "output.style.wordWrap = checked ? 'break-word' : 'normal'" in logs_js
        assert "setWrap(!!wrapBox.checked)" in logs_js, (
            "the default is bound but never applied — the pre stays un-wrapped"
        )

    def test_the_picker_is_populated_from_the_api(self, logs_js):
        """The page's options came from its route's render context; the modal
        is global, so it asks the API."""
        assert "/api/logs/list" in logs_js
        assert "function populatePicker()" in logs_js

    def test_a_failed_picker_fetch_retries(self, logs_js):
        """Otherwise one transient 500 leaves an empty dropdown for the whole
        session and the panel looks permanently broken."""
        assert "pickerPromise = null" in logs_js


# ===========================================================================
# 3. The header is one thin row
# ===========================================================================


class TestTheHeaderIsCondensed:
    def test_the_five_source_tabs_are_gone(self, modal):
        """THE THIRD ASK. Their labels survive as picker options — see
        ``services.log_service.LOG_VIEW_LABELS`` — so no destination is lost."""
        assert "log-source-tab" not in modal, "the tab row still exists"
        assert "switchLogSource" not in modal, "the tabs' handler is still bound"

    def test_the_utilities_are_behind_one_overflow_menu(self, modal):
        """Download / Clear / Pause and friends must not sit on the title row."""
        assert 'data-bs-toggle="dropdown"' in modal
        for element_id in ("downloadLogBtn", "copyLogBtn", "clearLogBtn", "streamLogBtn"):
            start = modal.index(f'id="{element_id}"')
            menu_start = modal.rindex('<ul class="dropdown-menu', 0, start)
            assert start > menu_start, f"#{element_id} is not inside the ⋮ menu"

    def test_the_old_icon_cluster_is_not_inline_any_more(self, modal):
        """CONTROL — the previous markup had these three as siblings of the
        title, which is the second row the ask called out."""
        for gone in ("Last 1hr", 'onclick="clearUnifiedLog()"', "pauseLogBtn"):
            assert gone not in modal, f"{gone!r} is still inline in the header"

    def test_the_modal_markup_is_a_component_not_inline_in_the_shell(self, base):
        """base.html already carries the navbar, the player and two modals;
        a ~130-line viewer does not belong inline in the global shell."""
        assert '{% include "components/_log_modal.html" %}' in base
        assert 'id="unifiedLogModal"' not in base, (
            "the modal was left inline in base.html as well as included"
        )


# ===========================================================================
# 4. One engine, loaded by the shell
# ===========================================================================


class TestThereIsExactlyOneLogEngine:
    def test_the_shell_loads_the_engine_and_its_colours(self, base):
        """Both are GLOBAL now: the modal can be opened from any page, and the
        level colours live in logs.css (nothing else defines them).

        Needles are the versioned_static CALLS, not bare substrings: base.html
        names ``js/pages/logs.js`` inside a comment above the script tags, so a
        substring search would compare against prose and report the wrong
        order."""
        assert "versioned_static('js/pages/logs.js')" in base, (
            "the engine is not loaded by the shell"
        )
        assert "versioned_static('css/logs.css')" in base, (
            "the level colours are not loaded by the shell"
        )
        assert (
            base.index("versioned_static('js/main.js')")
            < base.index("versioned_static('js/pages/logs.js')")
        ), "main.js opens the modal, so it must load first"

    def test_main_js_no_longer_ships_a_second_viewer(self, main_js):
        """THE INVARIANT. Two fetch/render loops for one panel is exactly the
        duplication this consolidation removed."""
        code = js_code(main_js)
        for gone in ("LOG_SOURCE_FILES", "LOG_TAG_CLASSES", "formatLogLine",
                     "renderLogLines", "updateUnifiedLog", "setLogTabDot"):
            assert gone not in code, f"main.js still carries {gone}"

    def test_main_js_exports_only_the_opener(self, main_js):
        """The other handlers were inline onclick targets in the old markup;
        the buttons are bound by id now, so a second export would be a second
        implementation."""
        code = js_code(main_js)
        assert "global.openUnifiedLogModal = openUnifiedLogModal" in code
        for gone in ("global.toggleLogPause", "global.clearUnifiedLog",
                     "global.switchLogSource", "global.downloadActiveLogLastHour",
                     "global.toggleLogTimestamps"):
            assert gone not in code, f"{gone} is still exported"

    def test_the_modal_lifetime_hands_off_to_the_engine(self, main_js):
        """Load/close must follow visibility: fetching a log nobody can see, or
        leaving SSE open behind a dismissed panel, is the background-traffic
        problem the old bare setInterval was fixed for."""
        assert "logsPage.onOpen()" in main_js
        assert "logsPage.onClose()" in main_js

    def test_the_engine_exports_the_lifecycle(self, logs_js):
        assert "onOpen" in logs_js
        assert "onClose" in logs_js

    def test_the_engine_does_not_fetch_on_every_page(self, logs_js):
        """It is loaded by base.html EVERYWHERE now. A tail load in
        DOMContentLoaded would be a 500-line request per page view for a panel
        that may never open."""
        start = logs_js.index("document.addEventListener('DOMContentLoaded'")
        block = logs_js[start: logs_js.index("});", start)]
        code = js_code(block)

        assert "loadLog(" not in code, "the engine fetches before the modal opens"
        assert "initToolbar();" in code, "the controls must still be bound"

    def test_the_stream_stops_when_the_modal_closes(self, logs_js):
        assert "function onClose()" in logs_js
        assert "stopStream();" in logs_js.split("function onClose()")[1]


# ===========================================================================
# 5. The API behind the picker
# ===========================================================================


class TestTheLogFileListEndpoint:
    def test_the_endpoint_exists_and_is_registered(self, logs_route_source):
        assert "@logs_bp.route(\"/api/logs/list\"" in logs_route_source

    def test_it_returns_the_shared_helper_not_a_new_list(self, logs_route_source):
        """One allow-list. The page's route reads the same helper, so the two
        surfaces cannot disagree about which files exist."""
        assert "list_log_files()" in logs_route_source

    def test_the_helper_orders_views_first_then_alpha(self, tmp_path, monkeypatch):
        """The shape contract: curated views lead, the rest follow in name
        order. The directory is supplied so the assertion does not depend on
        whatever happens to be in the test environment's own log dir."""
        import services.log_service as log_service

        for name in ("debug.log", "access.log", "unified_scan.log", "queue.log"):
            (tmp_path / name).write_text("x", encoding="utf-8")
        monkeypatch.setattr(log_service, "resolve_log_dir", lambda: str(tmp_path))

        names = [entry["name"] for entry in log_service.list_log_files()]

        assert names[:2] == ["unified_scan.log", "queue.log"], (
            "the Scanner/Queue views must lead"
        )
        assert names[2:] == sorted(names[2:]), "the rest must be alphabetical"

    def test_the_helper_only_offers_allow_listed_files(self, tmp_path, monkeypatch):
        """The picker must not enumerate — or leak — a file the app never meant
        to show. A stray .log in the directory is simply not offered."""
        import services.log_service as log_service

        for name in ("unified_scan.log", "queue.log", "access.log", "not-yours.log"):
            (tmp_path / name).write_text("x" * 10, encoding="utf-8")

        monkeypatch.setattr(log_service, "resolve_log_dir", lambda: str(tmp_path))

        names = [entry["name"] for entry in log_service.list_log_files()]

        assert "not-yours.log" not in names, "an unlisted file was offered to the UI"
        assert set(names) <= set(log_service.LOG_FILE_ALLOWLIST)

    def test_the_curated_views_keep_their_tab_labels(self, tmp_path, monkeypatch):
        """Collapsing the five tabs into one picker must not lose what they
        meant — that is the whole reason the labels exist."""
        import services.log_service as log_service

        for name in CURATED_VIEWS:
            (tmp_path / name).write_text("line\n", encoding="utf-8")

        monkeypatch.setattr(log_service, "resolve_log_dir", lambda: str(tmp_path))

        labels = {e["name"]: e["label"] for e in log_service.list_log_files()}

        for name, label in CURATED_VIEWS.items():
            assert labels.get(name) == label, f"{name} lost its {label!r} label"

    def test_an_unlabelled_file_is_labelled_with_its_own_name(self, tmp_path, monkeypatch):
        import services.log_service as log_service

        (tmp_path / "client.log").write_text("line\n", encoding="utf-8")
        monkeypatch.setattr(log_service, "resolve_log_dir", lambda: str(tmp_path))

        entries = log_service.list_log_files()

        assert entries[0]["label"] == "client.log"

    def test_the_views_come_before_the_rest(self, tmp_path, monkeypatch):
        """Presentation order lives here so the route and the API cannot pick
        different orders for the same files."""
        import services.log_service as log_service

        (tmp_path / "unified_scan.log").write_text("a\n", encoding="utf-8")
        (tmp_path / "access.log").write_text("b\n", encoding="utf-8")
        monkeypatch.setattr(log_service, "resolve_log_dir", lambda: str(tmp_path))

        names = [entry["name"] for entry in log_service.list_log_files()]

        assert names[0] == "unified_scan.log", "the Scanner view must lead"

    def test_a_missing_directory_is_not_an_error(self, tmp_path, monkeypatch):
        """The modal opens on a host whose log dir may not exist yet."""
        import services.log_service as log_service

        monkeypatch.setattr(
            log_service, "resolve_log_dir", lambda: str(tmp_path / "nope")
        )

        assert log_service.list_log_files() == []


@pytest.fixture(scope="module")
def logs_route_source() -> str:
    return LOGS_ROUTE.read_text(encoding="utf-8")
