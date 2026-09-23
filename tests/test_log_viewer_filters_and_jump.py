"""Log viewer: scroll-to-bottom FAB, filter modes, match counter, line gutter.

The interactive behaviour is covered by ``tests/js/logs-viewer-probe.js``, which
extracts the REAL functions from ``logs.js`` and exercises them (25 checks, and
mutation-tested — see ``tests/js/mutate-logs-viewer.js``).

This module covers what a source-contract test can hold that the probe cannot:
the markup/JS id agreement, the contracts the JS depends on, and the properties
that were got wrong on the first attempt and must not regress.

Two such properties are called out below because they are silent failures:
a lower-cased regex pattern, and a counter that reports the total as "visible".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "test_site" / "templates" / "Pages" / "logs.html"
JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "logs.js"
CSS = REPO_ROOT / "test_site" / "static" / "css" / "logs.css"

#: Logs is a test_site-only page. The live tree's viewer is an inline IIFE with
#: no stylesheet at all, so a change here cannot be mirrored to it — these
#: assertions are deliberately scoped to the rebuilt tree.
SCOPE = "test_site"


@pytest.fixture(scope="module")
def html() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js() -> str:
    return JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    return CSS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Ids the JS binds must exist in the markup
# ---------------------------------------------------------------------------


class TestIdsAgree:
    """`bind()` silently no-ops on a missing id, so a typo is invisible."""

    @pytest.mark.parametrize(
        "element_id",
        [
            "scrollToBottomBtn",
            "unreadLogBadge",
            "logMatchCount",
            "filterRegexBtn",
            "filterCaseBtn",
        ],
    )
    def test_new_element_is_in_the_template(self, html: str, element_id: str):
        assert f'id="{element_id}"' in html, (
            f"#{element_id} is bound in logs.js but absent from the template — "
            "bind() would silently skip it"
        )

    @pytest.mark.parametrize(
        "element_id",
        [
            "logOutput",
            "logSelector",
            "streamLogBtn",
            "refreshLogBtn",
            "downloadLogBtn",
            "copyLogBtn",
            "clearLogBtn",
            "wrapLinesCheck",
            "logFilterInput",
            "logLevelFilters",
        ],
    )
    def test_existing_ids_still_present(self, html: str, element_id: str):
        assert f'id="{element_id}"' in html

    def test_every_bound_id_exists(self, html: str, js: str):
        """Every `byId('...')` in the module must resolve to a template id."""
        wanted = set(re.findall(r"byId\('([^']+)'\)", js))
        missing = sorted(i for i in wanted if f'id="{i}"' not in html)
        assert not missing, f"logs.js reads ids the template does not render: {missing}"


# ---------------------------------------------------------------------------
# 2. The jump button must not be inside the scrolling container
# ---------------------------------------------------------------------------


class TestJumpButtonPlacement:
    def test_it_is_outside_the_scrolling_pre(self, html: str):
        """A button inside `overflow-y: auto` scrolls away with the content.

        It has to sit in the CARD and float over the output, which is why the
        card carries `position-relative`.
        """
        pre_start = html.index('id="logOutput"')
        pre_end = html.index("</pre>", pre_start)
        body = html[pre_start:pre_end]
        assert 'id="scrollToBottomBtn"' not in body, (
            "the jump button is inside the scrolling <pre>, so it would scroll "
            "out of view exactly when it is needed"
        )

    def test_the_card_is_a_positioning_context(self, html: str):
        """`position: absolute` needs an ancestor that is not `static`."""
        assert "position-relative" in html, (
            "the viewer card lost `position-relative`, so .log-jump-btn "
            "(position: absolute) would escape to the page"
        )

    def test_the_button_starts_hidden(self, html: str):
        """It is revealed by updateJumpButton(), never visible on first paint."""
        idx = html.index('id="scrollToBottomBtn"')
        tag = html[html.rindex("<", 0, idx): html.index(">", idx)]
        assert "d-none" in tag

    def test_the_badge_starts_hidden(self, html: str):
        idx = html.index('id="unreadLogBadge"')
        tag = html[html.rindex("<", 0, idx): html.index(">", idx)]
        assert "d-none" in tag


# ---------------------------------------------------------------------------
# 3. The regex pattern must NOT be lower-cased
# ---------------------------------------------------------------------------


class TestRegexPatternIntegrity:
    def test_the_pattern_is_not_lowercased_for_regex(self, js: str):
        """⚠️ Lower-casing narrows `[A-Z]` to `[a-z]` silently.

        Case-insensitivity in regex mode is the `i` FLAG. The first draft of this
        change lower-cased `filterText` at the input handler, which would have
        made `[A-Z]{3}` match nothing while looking like it worked. Pinned here
        and covered behaviourally by the JS probe.
        """
        # The handler must store the RAW trimmed value.
        assert "filterText = String(this.value || '').trim();" in js, (
            "the filter handler no longer stores the raw pattern — check that "
            "regex mode still works for patterns containing uppercase classes"
        )
        # And the regex must be built from the raw pattern.
        assert "new RegExp(filterText, flags)" in js, (
            "the RegExp is no longer built from the raw pattern"
        )

    def test_case_insensitivity_uses_the_flag(self, js: str):
        assert "filterCaseSensitive ? 'u' : 'iu'" in js, (
            "regex case-insensitivity must come from the i flag, not from "
            "modifying the pattern"
        )

    def test_a_lowercased_copy_feeds_the_substring_path(self, js: str):
        """The substring path still needs folding — via a separate variable."""
        assert "filterTextLower = filterText.toLowerCase();" in js
        assert "line.toLowerCase().indexOf(filterTextLower)" in js


# ---------------------------------------------------------------------------
# 4. ReDoS guards
# ---------------------------------------------------------------------------


class TestRedosGuards:
    def test_invalid_patterns_are_caught_not_thrown(self, js: str):
        """A half-typed `(` is a normal intermediate state while typing."""
        assert re.search(r"new RegExp\([^)]*\)[\s\S]{0,200}?catch", js), (
            "the RegExp constructor is not wrapped in try/catch — a partially "
            "typed pattern would break the whole viewer"
        )

    def test_an_invalid_pattern_matches_nothing_not_everything(self, js: str):
        """Showing every line while the filter says "invalid" reads as ignored."""
        assert "if (!compiledRegex) return false;" in js

    def test_there_is_a_pattern_length_cap(self, js: str, css: str):
        assert "MAX_PATTERN_LENGTH" in js
        assert re.search(r"filterText\.length > MAX_PATTERN_LENGTH", js)

    def test_there_is_a_subject_length_cap(self, js: str):
        """Unbounded input is what makes backtracking catastrophic."""
        assert "MAX_REGEX_LINE_LENGTH" in js
        assert re.search(r"line\.length > MAX_REGEX_LINE_LENGTH", js)

    def test_the_compiled_regex_is_cached(self, js: str):
        """render() runs per SSE frame — rebuilding per line would be waste."""
        assert "compiledRegexKey" in js
        assert re.search(r"if \(key === compiledRegexKey\) return;", js)

    def test_the_cache_key_includes_the_flags(self, js: str):
        """Otherwise toggling case would reuse a regex built with the other flag."""
        assert re.search(r"const key = `\$\{flags\}", js), (
            "the regex cache key omits the flags, so toggling case-sensitivity "
            "would reuse a stale RegExp"
        )


# ---------------------------------------------------------------------------
# 5. The unread counter must count real arrivals
# ---------------------------------------------------------------------------


class TestUnreadCounter:
    def test_unread_counts_the_incoming_lines(self, js: str):
        assert "unreadCount += incoming.length;" in js

    def test_it_counts_raw_arrivals_not_visible_ones(self, js: str):
        """A filter that hides everything must not report "0 new lines".

        Counted from `incoming`, never from `render()`'s return value — the
        number answers "how much arrived", not "how much passed the filter".
        """
        # The whole stream-message handler, so the assertion covers the real
        # increment rather than a clipped window around it.
        start = js.index("streamSource.addEventListener('message'")
        handler = js[start : js.index("streamSource.onerror", start)]
        assert "unreadCount += incoming.length;" in handler
        assert "unreadCount += visible" not in handler
        assert "render()" in handler  # sanity: this IS the render call site

    def test_staying_pinned_clears_the_count(self, js: str):
        """Following the tail means nothing is being missed."""
        assert re.search(r"if \(follow\) \{[^}]*resetUnread\(\)", js, re.S)

    def test_scrolling_back_to_bottom_clears_it(self, js: str):
        """Otherwise the badge means "since the last frame", not "since I looked"."""
        assert re.search(r"addEventListener\('scroll'[\s\S]{0,200}?atBottom\(\)\) resetUnread\(\)", js), (
            "the scroll handler does not clear the unread count at the bottom"
        )

    def test_the_scroll_listener_is_passive(self, js: str):
        """It only reads scrollTop; a blocking listener would cost real frames."""
        assert re.search(r"\{ passive: true \}", js)

    def test_the_counter_is_not_reset_per_frame(self, js: str):
        """A reset inside the stream handler would make it always 0 or 1."""
        stream_block = js[js.index("streamSource.addEventListener('message'"):]
        stream_block = stream_block[:3000]
        assert "unreadCount = 0;" not in stream_block


# ---------------------------------------------------------------------------
# 6. The counter must distinguish visible from total
# ---------------------------------------------------------------------------


class TestMatchCounter:
    def test_it_reports_visible_over_total(self, js: str):
        assert "${visible.toLocaleString()} / ${rawLines.length.toLocaleString()} lines" in js, (
            "the counter must report VISIBLE / TOTAL — reporting the total as "
            "visible makes a filtered result indistinguishable from an "
            "unfiltered one"
        )

    def test_it_stays_blank_with_no_filter(self, js: str):
        """A \"500 / 500\" label on an unfiltered view is noise."""
        assert re.search(r"if \(!filterText && levelFilter === 'all'\)", js)

    def test_it_reports_an_invalid_pattern(self, js: str):
        assert "Invalid pattern" in js

    def test_it_is_live_for_screen_readers(self, html: str):
        idx = html.index('id="logMatchCount"')
        tag = html[html.rindex("<", 0, idx): html.index(">", idx)]
        assert "aria-live" in tag

    def test_updateMatchCount_is_called_by_render(self, js: str):
        assert re.search(r"function render\(\)[\s\S]{0,1200}?updateMatchCount\(visible\)", js)


# ---------------------------------------------------------------------------
# 7. Accessibility
# ---------------------------------------------------------------------------


class TestAccessibility:
    def test_mode_toggles_expose_pressed_state(self, html: str):
        """`.active` on an outline button conveys nothing to a screen reader."""
        for element_id in ("filterRegexBtn", "filterCaseBtn"):
            idx = html.index(f'id="{element_id}"')
            tag = html[html.rindex("<", 0, idx): html.index(">", idx)]
            assert 'aria-pressed="false"' in tag, f"#{element_id} lacks aria-pressed"

    def test_the_js_keeps_aria_pressed_in_sync(self, js: str):
        assert re.search(r"setAttribute\('aria-pressed', next \? 'true' : 'false'\)", js)

    def test_the_badge_is_aria_hidden(self, html: str):
        """The button's own aria-label already reports the count.

        Without this, a screen reader announces the number twice.
        """
        idx = html.index('id="unreadLogBadge"')
        tag = html[html.rindex("<", 0, idx): html.index(">", idx)]
        assert 'aria-hidden="true"' in tag

    def test_the_button_has_an_accessible_name(self, html: str):
        idx = html.index('id="scrollToBottomBtn"')
        tag = html[html.rindex("<", 0, idx): html.index(">", idx)]
        assert "aria-label" in tag

    def test_the_js_updates_the_label_with_the_count(self, js: str):
        assert re.search(r"Jump to newest lines, \$\{unreadCount\} new", js)


# ---------------------------------------------------------------------------
# 8. Styling
# ---------------------------------------------------------------------------


class TestStyling:
    def test_the_counter_scope_targets_the_real_element(self, css: str):
        """⚠️ `#logOutput` is the id. A `.log-output` class does not exist."""
        assert "#logOutput" in css
        assert re.search(r"#logOutput\s*\{\s*counter-reset:\s*logline;", css), (
            "the line counter is not scoped to #logOutput, so the gutter numbers "
            "would count everything on the page"
        )

    def test_the_gutter_number_is_not_selectable(self, css: str):
        """A line number must never land in a copy-paste of the log."""
        block = css[css.index(".log-line::before"):]
        block = block[:block.index("}")]
        assert "user-select: none" in block

    def test_the_gutter_number_ignores_the_pointer(self, css: str):
        block = css[css.index(".log-line::before"):]
        block = block[:block.index("}")]
        assert "pointer-events: none" in block

    def test_duplicate_log_line_blocks_do_not_exist(self, css: str):
        """Two `.log-line {` blocks would silently override one another."""
        count = len(re.findall(r"^\.log-line \{", css, re.M))
        assert count == 1, f".log-line is declared {count} times"

    def test_error_tinting_wins_over_zebra(self, css: str):
        """The level rules must come AFTER the striping rule.

        Both set background-color, so source order decides. Error rows are the
        ones that must stay visible.
        """
        stripe = css.index("nth-of-type(odd)")
        level = css.index(".log-line.log-level-error")
        assert stripe < level, (
            "the zebra rule now follows the level rules, so striping would "
            "override the error tint"
        )

    def test_the_jump_button_meets_the_touch_target_minimum(self, css: str):
        block = css[css.index(".log-jump-btn"):]
        block = block[:block.index("}")]
        assert "44px" in block, "the jump button is below the app's 44px touch target"

    def test_level_filter_glyphs_do_not_swallow_clicks(self, css: str):
        """The whole button is the target, including the <code> glyph."""
        block = css[css.index(".log-filter-mode code"):]
        block = block[:block.index("}")]
        assert "pointer-events: none" in block


# ---------------------------------------------------------------------------
# 9. Scope
# ---------------------------------------------------------------------------


class TestScope:
    def test_logs_js_is_test_site_only(self):
        """Asserted so nobody assumes a live-tree mirror exists.

        The live log viewer is an inline IIFE inside templates/pages/logs.html
        with no stylesheet at all — there is nothing to mirror into.
        """
        assert JS.is_file()
        assert not (REPO_ROOT / "static" / "js" / "pages" / "logs.js").exists(), (
            "a live-tree logs.js appeared — re-check whether this change should "
            "be mirrored there"
        )
