"""Unified search: recent searches + arrow-key navigation.

Reported in an outside UI evaluation of the rebuilt search flyout, which asked
for arrow-key navigation, recent searches, debouncing and a mobile overlay.
Two of those four were verified wrong before any code was written:

1. **Debouncing contradicts the design.** The module's own contract is "typing
   syncs text only — searches run on Enter or a filter change" (a deliberate
   choice, documented in the file header and in the input handler). Adding a
   debounce means live-searching on every keystroke, which is the thing that
   choice exists to prevent.
2. **The mobile overlay already exists.** `openUnifiedFilterSheet()` builds a
   bottom sheet on small screens and a centred dialog from 768px up, with its
   own CSS. Nothing to add.

The report also located the logic in "base.html … backed by search.js". Both
filenames are wrong, and this suite pins the real ones so the next reader does
not go looking in the same two places:

    markup  test_site/templates/components/_unified_search_modal.html
            (base.html only `{% include %}`s it)
    logic   test_site/static/js/ui/search-flyout.js
            (test_site/static/js/pages/search.js is the /search PAGE and
             contains ZERO references to runSearch)

WHY THIS SPLIT: the behaviour is covered by
``tests/js/search-recents-arrows-probe.js`` (41 checks, mutation-tested by
``tests/js/mutate-search-recents-arrows.js``). That probe extracts FUNCTIONS, so
it cannot reach the ``DOMContentLoaded`` block. Everything asserted here is
wiring or markup that a function-level probe structurally cannot see — asserting
it in the probe would produce a false all-clear.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: This feature is test_site-only. The live tree's equivalent is
#: static/js/unified_search.js, which is a different implementation.
SCOPE = "test_site"

FLYOUT = REPO_ROOT / "test_site" / "static" / "js" / "ui" / "search-flyout.js"
MODAL = REPO_ROOT / "test_site" / "templates" / "components" / "_unified_search_modal.html"
BASE = REPO_ROOT / "test_site" / "templates" / "base.html"
CSS = REPO_ROOT / "test_site" / "static" / "css" / "popularr.css"
PAGE_SEARCH = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "search.js"


@pytest.fixture(scope="module")
def flyout() -> str:
    return FLYOUT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def code() -> str:
    """The module with comments blanked.

    Use this for every "token must (not) appear" assertion. The shipped file
    documents each trap it avoids using the very token, so matching the raw
    text asserts the wrong thing — see strip_js_comments.
    """
    return strip_js_comments(FLYOUT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def modal() -> str:
    return MODAL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    return CSS.read_text(encoding="utf-8")


def _init_block(flyout: str) -> str:
    """The DOMContentLoaded body — the part a function probe cannot reach."""
    start = flyout.index("document.addEventListener('DOMContentLoaded'")
    return flyout[start:]


def strip_js_comments(source: str) -> str:
    """Blank out JS comments, preserving newlines.

    REQUIRED, not tidiness. Several assertions below are of the form "this
    token must NOT appear", and this module deliberately *explains* the
    mistakes it avoids — `setActiveRow` has a comment containing the words
    "aria-selected", and the arrow handler comments on `preventDefault`. A
    naive substring check therefore matches the documentation rather than the
    code and reports the opposite of the truth, in both directions:
    a missing call reads as present, and a correct comment reads as a bug.

    String literals are skipped so a `//` inside a string is not mistaken for a
    comment. Regex literals are not handled; this file has none.
    """
    out = list(source)
    i, n = 0, len(source)

    def blank(start: int, stop: int) -> None:
        for k in range(start, min(stop, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            j = i
            while j < n and source[j] != "\n":
                j += 1
            blank(i, j)
            i = j
        elif ch == "/" and nxt == "*":
            j = source.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif ch in "\"'`":
            j = i + 1
            while j < n:
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == ch:
                    j += 1
                    break
                j += 1
            i = j
        else:
            i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# 1. The real file locations (the report named two wrong ones)
# ---------------------------------------------------------------------------


class TestTheRealFilesAreWhereThisSuiteSays:
    def test_the_markup_is_a_component_not_base_html(self):
        assert MODAL.exists()
        base = BASE.read_text(encoding="utf-8")
        assert "components/_unified_search_modal.html" in base, (
            "base.html must include the component"
        )
        # The distinction matters: a search for the flyout markup must land on
        # the component, not on base.html's include line.
        assert "us-results" in MODAL.read_text(encoding="utf-8")
        assert "us-results" not in base

    def test_pages_search_js_is_a_DIFFERENT_feature(self):
        """`search.js` is the /search page — it must not gain flyout logic.

        Guards the documented dead end: the report sent readers here, and the
        file contains no reference to the flyout's search at all. If that ever
        changes, this assertion tells the next person to revisit the docs.
        """
        source = PAGE_SEARCH.read_text(encoding="utf-8")
        assert "runSearch" not in source
        assert "unifiedSearchInput" not in source

    def test_the_module_is_loaded_globally_by_base(self):
        base = BASE.read_text(encoding="utf-8")
        assert "js/ui/search-flyout.js" in base


# ---------------------------------------------------------------------------
# 2. Recent searches — wiring only (the store itself is probe-covered)
# ---------------------------------------------------------------------------


class TestRecentSearchWiring:
    def test_clearing_the_flyout_input_refreshes_the_recents_view(self, code):
        """Otherwise recents are only reachable by closing and reopening.

        Asserted here, NOT in the JS probe: the handler is registered inside
        DOMContentLoaded and the probe extracts functions, so a mutation of
        this line escapes it.
        """
        init = _init_block(code)
        handler = init[init.index("input.addEventListener('input'"):]
        assert "refreshRecentView()" in handler[:1200], (
            "clearing the box must return to the recent-search list"
        )

    def test_it_issues_no_request(self, flyout):
        """The refresh must not become a live search.

        Re-rendering cached text is what makes this compatible with the
        module's "typing syncs text only" contract. If refreshRecentView ever
        grew a fetch, this would silently become debounce-by-accident.
        """
        start = flyout.index("function refreshRecentView()")
        body = flyout[start:flyout.index("\n  }", start)]
        assert "fetch" not in body
        assert "api." not in body
        assert "runSearch" not in body

    def test_clearing_the_banner_box_refreshes_it_too(self, flyout):
        """The banner box is where the typing happens; it must behave the same."""
        start = flyout.index("function syncNavSearchQuery()")
        body = flyout[start:flyout.index("\n  }", start)]
        assert "refreshRecentView()" in body

    def test_the_empty_state_prefers_recents_over_the_prompt(self, flyout):
        start = flyout.index("if (query.length < MIN_QUERY_LENGTH && !hasAdvanced)")
        body = flyout[start:start + 700]
        assert "readRecentSearches()" in body
        assert "recentSearchSection(recents)" in body
        assert "emptyPromptHtml()" in body, "the prompt must survive as the fallback"

    def test_recents_are_recorded_only_when_a_search_actually_runs(self, flyout):
        """rememberSearch must sit AFTER the short-circuit, not before.

        Before it, every fragment typed on the way to a real query would be
        recorded — the list would fill with "w", "we", "wee"…
        """
        short_circuit = flyout.index("if (query.length < MIN_QUERY_LENGTH && !hasAdvanced)")
        call = flyout.index("rememberSearch(query);")
        assert call > short_circuit, (
            "rememberSearch must run only for a query that passes the "
            "minimum-length gate"
        )

    def test_clicking_a_recent_applies_it(self, flyout):
        init = _init_block(flyout)
        assert "data-us-recent" in init
        assert "applyRecentSearch(" in init

    def test_removing_and_clearing_are_both_handled(self, flyout):
        init = _init_block(flyout)
        assert "data-us-recent-remove" in init
        assert "forgetSearch(" in init
        assert "us-recents-clear" in init
        assert "writeRecentSearches([])" in init

    def test_removing_stops_the_row_click_from_also_firing(self, flyout):
        """The remove button is INSIDE the row, so without stopPropagation a
        removal would both delete the entry and re-run its search."""
        init = _init_block(flyout)
        remove_branch = init[init.index("data-us-recent-remove"):]
        assert "stopPropagation()" in remove_branch[:400]

    def test_the_remove_branch_precedes_the_row_branch(self, flyout):
        init = _init_block(flyout)
        assert init.index("data-us-recent-remove") < init.index("closest('[data-us-recent]')")

    def test_the_store_is_localStorage_not_sessionStorage(self, flyout):
        """Recents must survive closing the tab; sessionStorage would not."""
        assert "RECENTS_KEY = 'popularr.unifiedSearch.recent'" in flyout
        assert "global.localStorage" in flyout

    def test_every_storage_access_is_guarded(self, flyout):
        """Private-mode reads THROW, as does a full quota on write.

        A convenience feature must never be able to break the flyout, so both
        the read and the write are wrapped. Three try/catch blocks: read,
        write, and the JSON.parse inside read.
        """
        start = flyout.index("function readRecentSearches()")
        end = flyout.index("function recentSearchRows(")
        region = flyout[start:end]
        assert region.count("try {") >= 2
        assert region.count("catch") >= 2

    def test_the_recents_section_is_labelled(self, flyout):
        assert "Recent searches" in flyout


# ---------------------------------------------------------------------------
# 3. Arrow-key navigation — wiring only
# ---------------------------------------------------------------------------


class TestArrowKeyWiring:
    def test_arrow_down_and_arrow_up_are_both_bound(self, flyout):
        init = _init_block(flyout)
        assert "'ArrowDown'" in init
        assert "'ArrowUp'" in init

    def test_arrow_keys_are_prevented_from_moving_the_caret(self, code):
        """Without preventDefault, ArrowUp rewrites the caret in a text input
        and the row highlight moves at the same time — which reads as the
        input being reset."""
        init = _init_block(code)
        arrow_branch = init[init.index("if (e.key === 'ArrowDown'"):]
        assert "preventDefault()" in arrow_branch[:400]

    def test_the_banner_box_navigates_too(self, flyout):
        """openUnifiedSearch focuses the banner box back, so it usually holds
        the focus — an arrows-only-in-the-flyout implementation would feel
        broken to the user even though it is correct."""
        init = _init_block(flyout)
        assert "['navSearchInput', 'dashboardTopSearchInput'].forEach" in init
        after = init[init.index("['navSearchInput', 'dashboardTopSearchInput'].forEach"):]
        assert "ArrowDown" in after[:600]

    def test_navigation_is_guarded_on_the_flyout_being_open(self, flyout):
        """The banner box exists on every page, always.

        Unguarded, arrowing in the banner would move a highlight through rows
        the user cannot see AND swallow the keypress.
        """
        assert "const isSearchOpen" in flyout
        start = flyout.index("function moveActiveRow(delta)")
        body = flyout[start:start + 200]
        assert "isSearchOpen()" in body

    def test_enter_acts_on_the_highlighted_row(self, flyout):
        start = flyout.index("global.submitUnifiedSearch = function ()")
        body = flyout[start:start + 900]
        assert "handleEnterKey()" in body
        # It must come BEFORE runSearch, or the highlighted row is ignored and
        # every Enter just re-runs the query.
        assert body.index("handleEnterKey()") < body.index("runSearch()")

    def test_enter_still_searches_when_nothing_is_highlighted(self, flyout):
        """The pre-existing contract: `runSearch()` must stay reachable from
        the submit path. handleEnterKey returns false when there is no
        highlight, so the fall-through is what keeps Enter working."""
        start = flyout.index("global.submitUnifiedSearch = function ()")
        body = flyout[start:start + 900]
        assert "if (handleEnterKey()) return;" in body
        assert "runSearch();" in body

    def test_the_highlight_is_dropped_when_the_list_is_replaced(self, flyout):
        """markRendered runs on every render; the index would otherwise point
        into a DOM list that no longer exists."""
        start = flyout.index("function markRendered(query)")
        body = flyout[start:start + 300]
        assert "resetActiveRow()" in body

    def test_the_highlight_is_dropped_on_open_and_on_close(self, flyout):
        open_start = flyout.index("function openUnifiedSearch(scope, prefill)")
        open_body = flyout[open_start:open_start + 1200]
        assert "resetActiveRow()" in open_body

        close_start = flyout.index("function closeUnifiedSearch()")
        close_body = flyout[close_start:close_start + 600]
        assert "resetActiveRow()" in close_body

    def test_the_active_row_is_announced_accessibly(self, code):
        """aria-current, NOT aria-selected.

        These rows are anchors and plain divs; aria-selected is only valid on
        listbox/tab/grid roles, so using it here would be an accessibility
        regression rather than an improvement.

        Asserts on the comment-stripped source — the real comment in
        setActiveRow names aria-selected in order to explain why it is not
        used, and would otherwise satisfy the negative assertion's own
        precondition in reverse.
        """
        start = code.index("function setActiveRow(index)")
        body = code[start:code.index("\n  }", start) + 4]
        assert "aria-current" in body
        assert "aria-selected" not in body


# ---------------------------------------------------------------------------
# 4. The two rejected items stay rejected
# ---------------------------------------------------------------------------


class TestRejectedItems:
    def test_there_is_no_debounce_timer_in_the_input_handler(self, code):
        """The module searches on Enter, by design.

        A debounce would quietly convert it to type-ahead, which is the exact
        behaviour the "typing syncs text only" rule rules out. If someone adds
        one deliberately they must also update the header and this test.
        """
        init = _init_block(code)
        handler = init[init.index("input.addEventListener('input'"):]
        head = handler[:900]
        assert "setTimeout" not in head
        assert "debounce" not in head.lower()

    def test_the_mobile_filter_sheet_still_exists(self, flyout, css):
        """The report asked for a mobile overlay that is already implemented."""
        assert "function openUnifiedFilterSheet()" in flyout
        assert ".us-filter-sheet" in css
        # Bottom sheet on phones, centred dialog from 768px up.
        assert ".us-filter-sheet.show" in css

    def test_typing_still_does_not_search(self, code):
        init = _init_block(code)
        handler = init[init.index("input.addEventListener('input'"):]
        # The only allowed body is mirroring + the recents refresh.
        assert "runSearch()" not in handler[:handler.index("});")]


# ---------------------------------------------------------------------------
# 5. Markup and styles
# ---------------------------------------------------------------------------


class TestMarkupAndStyles:
    def test_the_footer_advertises_the_arrow_keys(self, modal):
        assert "to navigate" in modal
        assert "&uarr;" in modal or "\u2191" in modal

    def test_the_footer_keeps_the_existing_hints(self, modal):
        assert "Enter" in modal
        assert "Esc" in modal

    def test_the_results_container_is_unchanged(self, modal):
        """The rows are injected into this id; renaming it would blank the
        flyout entirely."""
        assert 'id="unifiedSearchResults"' in modal
        assert 'id="unifiedSearchInput"' in modal

    def test_the_active_row_style_mirrors_hover(self, css):
        """A separate "selected" treatment would imply a second kind of
        emphasis for what is the same thing (the row you are on)."""
        assert ".us-row.us-row-active" in css
        block = css[css.index(".us-row.us-row-active"):]
        block = block[:block.index("}") + 1]
        assert "var(--tertiary-bg)" in block, (
            "the highlight must use the same token as .us-row:hover"
        )

    def test_the_active_row_has_a_non_colour_affordance(self, css):
        """The background tint alone is invisible in forced-colours mode."""
        block = css[css.index(".us-row.us-row-active"):]
        block = block[:block.index("}") + 1]
        assert "outline" in block

    def test_the_remove_button_is_reachable_without_hover(self, css):
        """Touch devices never hover, so a hover-revealed control would be
        permanently unreachable there."""
        assert "@media (hover: none)" in css
        assert "opacity: 1" in css[css.index("@media (hover: none)"):][:200]

    def test_animation_is_disabled_under_reduced_motion(self, css):
        assert "prefers-reduced-motion" in css


# ---------------------------------------------------------------------------
# 6. The behavioural probe (runs the SHIPPED functions)
# ---------------------------------------------------------------------------
#
# Source-contract checks cannot distinguish a working de-duplication rule from
# one that appends forever, nor a clamped arrow index from an unclamped one.
# tests/js/search-recents-arrows-probe.js extracts the real functions by
# brace-matching and drives them against a minimal DOM stub.

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

PROBE = REPO_ROOT / "tests" / "js" / "search-recents-arrows-probe.js"
MUTATOR = REPO_ROOT / "tests" / "js" / "mutate-search-recents-arrows.js"


def _node_available() -> bool:
    try:
        return subprocess.run(
            ["node", "--version"], capture_output=True, shell=True
        ).returncode == 0
    except Exception:
        return False


needs_node = pytest.mark.skipif(not _node_available(), reason="node is required")


def _run(script: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["node", str(script), *args],
        capture_output=True, text=True, cwd=str(REPO_ROOT), shell=True,
        encoding="utf-8",
    )


def _probe_result() -> dict:
    out = _run(PROBE, str(FLYOUT))
    assert out.returncode == 0, f"probe failed:\n{out.stdout}\n{out.stderr}"
    lines = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")]
    assert lines, f"probe produced no JSON:\n{out.stdout}\n{out.stderr}"
    return json.loads(lines[-1])


@needs_node
def test_the_probe_has_the_shipped_functions():
    """A renamed function must fail loudly rather than silently skipping."""
    result = _probe_result()
    assert "error" not in result, result.get("error")


@needs_node
def test_the_probe_passes_all_behavioural_checks():
    result = _probe_result()
    failed = [c["name"] for c in result["checks"] if not c["pass"]]
    assert not failed, f"failed checks: {failed}"
    assert result["total"] >= 30


@needs_node
def test_every_mutation_of_the_shipped_code_is_detected():
    """MUTATION TEST: the probe is only evidence if it fails on broken code.

    M6 in the harness is the bug this probe actually caught in development
    (resetActiveRow zeroing the index while leaving the highlight class on the
    row), so a passing run here means that class of bug stays caught.
    """
    out = _run(MUTATOR, str(FLYOUT))
    assert "ALL MUTATIONS DETECTED" in out.stdout, (
        f"the probe failed to detect a regression:\n{out.stdout}\n{out.stderr}"
    )
    assert "ANCHOR-NOT-FOUND" not in out.stdout, (
        "a mutation anchor no longer matches the source — the harness is "
        f"silently skipping a case:\n{out.stdout}"
    )
