"""The Activity page, and the shared feed module it is built on.

Three things can break here and each needs a different kind of test:

1. The ROUTE — it must exist, be reachable, and NOT 500 for a user on the live
   tree (whose templates have no `pages/activity.html`).
2. The FEED MODULE — the dashboard card and the page must render a scan the
   same way. Verified behaviourally: the module is loaded into a Node harness
   and driven, because a source assertion cannot tell a correct grouping from a
   broken one.
3. The WIRING — the module must be loaded BEFORE the scripts that use it, and
   both templates must use the same row macro rather than their own copies.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TEST_SITE = REPO_ROOT / "test_site"

FEED_JS = TEST_SITE / "static/js/services/activity-feed.js"
DASH_JS = TEST_SITE / "static/js/pages/dashboard.js"
PAGE_JS = TEST_SITE / "static/js/pages/activity.js"
ACTIVITY_TPL = TEST_SITE / "templates/Pages/activity.html"
DASHBOARD_TPL = TEST_SITE / "templates/Pages/dashboard.html"
ROW_PARTIAL = TEST_SITE / "templates/components/_recent_album_row.html"


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------

class TestTheRoute:
    @pytest.fixture(scope="class")
    def rules(self):
        import importlib

        app_mod = importlib.import_module("app")
        return {str(r.rule): r for r in app_mod.app.url_map.iter_rules()}

    def test_it_is_registered(self, rules):
        assert "/activity" in rules

    def test_it_is_a_get(self, rules):
        assert "GET" in rules["/activity"].methods

    def test_the_template_lives_in_the_rebuilt_tree_only(self):
        """The route redirects on the live tree, so there must be no live copy.

        A live `pages/activity.html` would be dead weight — the route's guard
        means it can never be rendered — and its presence would suggest the page
        works in a mode where the route refuses to serve it.
        """
        assert ACTIVITY_TPL.is_file()
        assert not (REPO_ROOT / "templates/pages/activity.html").exists()

    def test_it_redirects_rather_than_erroring_when_the_cutover_is_off(self):
        """⚠️ The page is rebuilt-only; a live-tree user must land somewhere sane.

        Asserted on the guard itself: the alternative is a 500 from a missing
        template, or an empty shell with no navigation. Both are worse than the
        dashboard, which already shows the user the same three feeds.
        """
        source = (REPO_ROOT / "routes/ui_routes.py").read_text(encoding="utf-8")
        start = source.index('async def activity()')
        window = source[start:start + 1600]
        assert "test_site_status" in window, "the cutover accessor is not consulted"
        assert '"active"' in window or "'active'" in window
        assert "redirect(url_for(\"ui.dashboard\"))" in window


# ---------------------------------------------------------------------------
# The shared feed module
# ---------------------------------------------------------------------------

class TestTheFeedModuleIsShared:
    def test_both_pages_load_it(self):
        for template in (DASHBOARD_TPL, ACTIVITY_TPL):
            text = template.read_text(encoding="utf-8")
            assert "js/services/activity-feed.js" in text, f"{template.name} does not load the feed"

    def test_it_loads_before_the_dashboard_script(self):
        """⚠️ Order is load-bearing: dashboard.js reads global.ActivityFeed at
        LOAD time (it re-exports the scan-name map), so a later tag would throw
        on a bare property access."""
        text = DASHBOARD_TPL.read_text(encoding="utf-8")
        assert text.index("activity-feed.js") < text.index("js/pages/dashboard.js")

    def test_it_loads_before_the_page_script(self):
        text = ACTIVITY_TPL.read_text(encoding="utf-8")
        assert text.index("activity-feed.js") < text.index("js/pages/activity.js")

    def test_the_dashboard_no_longer_keeps_its_own_copy(self):
        """The MAP moved; a leftover copy is a second place to fix a label.

        ⚠️ The accessor itself stays — as a one-line DELEGATE. Asserting the
        function name is absent would forbid that, which is the opposite of what
        matters: what must not come back is the DATA (the label table) or a
        second implementation of the grouping.
        """
        code = DASH_JS.read_text(encoding="utf-8")
        assert "const SCAN_TYPE_DISPLAY_NAMES = {" not in code, (
            "dashboard.js has its own copy of the scan-type labels"
        )
        assert "function groupRecentScans(scans) {" not in code, (
            "dashboard.js has its own grouping implementation"
        )
        # The delegate must be a delegate, not a reimplementation.
        start = code.index("function scanDisplayName(type) {")
        assert "global.ActivityFeed.scanDisplayName(type)" in code[start:start + 120]

    def test_the_dashboard_still_exports_the_names_it_did(self):
        """A refactor must not change the dashboard's public surface."""
        code = DASH_JS.read_text(encoding="utf-8")
        assert "scanDisplayName," in code
        assert "formatElapsed," in code


# ---------------------------------------------------------------------------
# The row markup is shared, not copied
# ---------------------------------------------------------------------------

class TestTheRowIsShared:
    def test_the_partial_exists_and_defines_the_macro(self):
        assert ROW_PARTIAL.is_file()
        assert "{% macro recent_album_row(al) %}" in ROW_PARTIAL.read_text(encoding="utf-8")

    def test_both_pages_import_and_call_it(self):
        for template in (DASHBOARD_TPL, ACTIVITY_TPL):
            text = template.read_text(encoding="utf-8")
            assert 'import recent_album_row' in text, f"{template.name} does not import the macro"
            assert "recent_album_row(al)" in text, f"{template.name} does not call the macro"

    def test_neither_page_inlines_its_own_row(self):
        """The dupe this prevents: a copy that misses the truncation fix."""
        for template in (DASHBOARD_TPL, ACTIVITY_TPL):
            text = template.read_text(encoding="utf-8")
            assert 'class="list-group-item bg-transparent border-secondary p-2 d-flex' not in text, (
                f"{template.name} has its own copy of the recent-album row"
            )

    def test_the_row_keeps_its_load_bearing_shape(self):
        """Two details here each came from a reported defect.

        * the wrapper is a DIV, because a `<button>` inside an `<a>` is invalid
          HTML and a click would both play and navigate;
        * the year sits OUTSIDE the truncating title element, or a long album
          name ellipsises the metadata away instead of the title.
        """
        text = ROW_PARTIAL.read_text(encoding="utf-8")
        assert 'class="list-group-item bg-transparent border-secondary' in text
        assert "js-play-album" in text
        # The play button must not be inside the album anchor.
        anchor_start = text.index('<a href="{{ url_for(\'ui.album_detail\'')
        anchor_end = text.index("</a>", anchor_start)
        assert "js-play-album" not in text[anchor_start:anchor_end]


# ---------------------------------------------------------------------------
# The page's own wiring
# ---------------------------------------------------------------------------

class TestThePageScript:
    def test_it_asks_for_more_than_the_card(self):
        """The page exists to show what the card cut along.

        The card caps at 12 albums / 25 scans / 50 reviews. Matching those caps
        here would make "View all" show exactly what the user just left.
        """
        code = PAGE_JS.read_text(encoding="utf-8")
        assert "SCAN_LIMIT = 100" in code
        assert "REVIEW_LIMIT = 200" in code

    def test_it_uses_a_managed_poller(self):
        """A bare setInterval is the leak ui/poller.js exists to prevent."""
        code = PAGE_JS.read_text(encoding="utf-8")
        assert "poller.create(" in code
        assert "setInterval(" not in code

    def test_it_renders_through_the_shared_module(self):
        code = PAGE_JS.read_text(encoding="utf-8")
        assert "global.ActivityFeed.renderScansInto(" in code
        assert "global.ActivityFeed.renderApprovalsInto(" in code

    def test_a_failed_fetch_reports_itself(self):
        """A permanent spinner reads as 'still loading' forever."""
        code = PAGE_JS.read_text(encoding="utf-8")
        assert code.count("Could not load") == 2
        assert "spinner-border" not in code.split("catch")[1]


# ---------------------------------------------------------------------------
# It is reachable
# ---------------------------------------------------------------------------

def _recent_activity_card() -> str:
    """The dashboard's Recent Activity card, with Jinja comments removed.

    ⚠️ COMMENTS ARE STRIPPED, and this is not tidiness. The card's own comment
    explains that the link USED to point at `/downloads/monitor` — so a plain
    "that URL must not appear" assertion is satisfied by the prose documenting
    the fix, not by the markup. (The same trap has bitten five times in this
    codebase; the standing rule is to strip before asserting on absence.)

    Also scoped to the CARD rather than the whole template: `View all` appears
    on the Upcoming Releases card too, and slicing around the first match
    tested the wrong link.
    """
    import re

    text = DASHBOARD_TPL.read_text(encoding="utf-8")
    text = re.sub(r"\{#.*?#\}", " ", text, flags=re.DOTALL)

    start = text.index('id="recentHistoryCard"')
    # The card ends at the next top-level card, or end of template.
    end = text.find('class="card shadow-sm"', start + 10)
    return text[start:end if end != -1 else len(text)]


class TestTheDashboardLinksToIt:
    def test_view_all_points_at_the_activity_page(self):
        card = _recent_activity_card()
        assert "ui.activity" in card, "View all does not link to the Activity page"

    def test_it_no_longer_points_at_the_downloads_page(self):
        """⚠️ The reported gap: 'View all' went to /downloads/monitor, a
        downloads page showing NONE of the three feeds above the link."""
        assert "/downloads/monitor" not in _recent_activity_card()


class TestTheRecentAlbumsQuery:
    """The extracted helper — one query, two surfaces at different widths."""

    def _helper_source(self) -> str:
        return self._helper_code()

    def _helper_code(self) -> str:
        """The helper's body with its DOCSTRING removed.

        ⚠️ The docstring explains this very defect and therefore QUOTES the
        retired `INTERVAL '14 days'` and `LIMIT 12`. Asserting on the raw source
        would have the prose satisfy (or break) the check instead of the code —
        the sixth time this trap has appeared in this codebase.
        """
        import re

        text = (REPO_ROOT / "routes/ui_routes.py").read_text(encoding="utf-8")
        start = text.index("def _fetch_recent_albums(")
        helper = text[start:text.index("@ui_bp.route(\"/activity\")", start)]
        # The FIRST triple-quoted block is the docstring; the SQL literals after
        # it are code and must stay.
        return re.sub(r'""".*?"""', " ", helper, count=1, flags=re.DOTALL)

    def test_the_activity_page_asks_for_a_wider_window_than_the_card(self):
        """⚠️ The page exists to show what the card cut along.

        Matching the card's window (14 days / 12 albums) would make "View all"
        show exactly what the user just left. Checked on the CALL rather than on
        a constant, because the two call sites are the thing that can drift.
        """
        text = (REPO_ROOT / "routes/ui_routes.py").read_text(encoding="utf-8")
        activity = text.index('async def activity()')
        window = text[activity:activity + 1600]
        assert "_fetch_recent_albums(days=30, limit=50)" in window, (
            "the Activity page no longer asks for a wider window than the card"
        )
        dashboard = text.index('async def dashboard()')
        dash_window = text[dashboard:dashboard + 4000]
        assert "_fetch_recent_albums(days=14, limit=12)" in dash_window

    def test_the_cutoff_is_computed_in_python_not_in_sql(self):
        """⚠️ The portability defect this replaced.

        The cutoff was `CURRENT_TIMESTAMP - INTERVAL '14 days'`, which is
        PostgreSQL-only. On SQLite the statement raises, the caller's `except`
        swallows it, and the list comes back EMPTY — so the failure mode was
        identical to "you added nothing recently", and the query was untestable.
        """
        helper = self._helper_source()
        assert "INTERVAL" not in helper
        assert "timedelta(" in helper
        assert ":cutoff" in helper

    def test_the_limit_is_bound_not_interpolated(self):
        """It was a literal `LIMIT 12` in the SQL text, so a caller wanting a
        different length had to edit the string."""
        helper = self._helper_source()
        assert ":limit" in helper
        assert "LIMIT 12" not in helper

    def test_it_returns_empty_rather_than_raising(self):
        """The card must render even when the query cannot run."""
        helper = self._helper_source()
        assert "except Exception" in helper
        assert "return []" in helper


# ---------------------------------------------------------------------------
# The feed module, driven
# ---------------------------------------------------------------------------

PROBE = REPO_ROOT / "tests" / "js" / "activity-feed-probe.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for the JS probe")
class TestTheFeedModuleBehaves:
    def _run(self, scenario: str) -> dict:
        result = subprocess.run(
            ["node", str(PROBE), scenario, str(FEED_JS)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(REPO_ROOT), shell=False,
        )
        lines = (result.stdout or "").strip().splitlines()
        assert lines, f"probe produced no output: {result.stderr[:400]}"
        return json.loads(lines[-1])

    def test_one_row_per_album_with_its_phases(self):
        """A scan pass writes one row per PHASE; the feed collapses them."""
        data = self._run("grouping")
        assert "error" not in data, data.get("error")
        assert data["entries"] == 1
        assert data["types"] == ["Metadata Scan", "Popularity Scan"]

    def test_the_worst_status_wins(self):
        """A completed phase must not overwrite a failed one.

        Other way round, a half-finished album reads as a success — which is the
        failure mode that makes a scan log untrustworthy.
        """
        data = self._run("status")
        assert data["status"] == "failed"

    def test_in_progress_scans_pin_to_the_top(self):
        data = self._run("in_progress")
        assert data["first_is_in_progress"] is True
        assert data["entries"] == 2

    def test_unknown_types_fall_back_to_the_raw_name(self):
        """Better a raw `scan_type` than a blank label."""
        data = self._run("labels")
        assert data["known"] == "Full Scan"
        assert data["unknown"] == "some_future_scan"

    def test_unparseable_timestamps_do_not_render_nan(self):
        data = self._run("timestamps")
        assert data["bad"] == "unknown"
        assert "NaN" not in data["bad"]

    def test_scan_rows_escape_and_encode(self):
        data = self._run("escaping")
        assert data["escapes_html"] is True
        assert data["encodes_path"] is True
