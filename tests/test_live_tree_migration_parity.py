"""Areas that existed in the rebuilt tree but not the live one — migrated.

The live tree is the DEFAULT UI (`helpers/test_site_mode.config_enables_test_site`
returns False unless `features.use_test_site` is truthy), so anything that
existed only under `test_site/` was invisible to a normal install. This suite
pins the three classes of gap that were migrated:

1. **Stylesheets a live page emits classes for but never defines.**
   `templates/pages/logs.html` has always wrapped its output in `.log-line` /
   `.log-level-*` / `.log-ts` / `.log-tag`, and NOTHING in the live tree defined
   any of them — the log viewer rendered flat monochrome. Same for the sandbox's
   `.sandbox-delta` / `.score-*` and the metadata-compare page.
2. **The Activity page.** `/activity` redirected to the dashboard whenever the
   cutover was off, i.e. the feature did not exist on the default UI.
3. **The shared `activity-feed` module**, which the dashboard card and the
   Activity page must both use so one scan cannot render two ways.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_STATIC = REPO_ROOT / "static"
TEMPLATES = REPO_ROOT / "templates"


#: (live page, stylesheet it must link, a class that page emits)
MIGRATED_PAGE_SHEETS: tuple[tuple[str, str, str], ...] = (
    ("templates/pages/logs.html", "css/logs.css", "log-line"),
    ("templates/pages/sandbox.html", "css/sandbox.css", "sandbox-delta"),
    ("templates/pages/metadata_compare.html", "css/metadata-compare.css", "metadata-compare-panel"),
)


def read(rel: str | Path) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def css_classes(path: Path) -> set[str]:
    css = re.sub(r"/\*.*?\*/", " ", path.read_text(encoding="utf-8", errors="replace"), flags=re.S)
    return set(re.findall(r"\.([A-Za-z_][A-Za-z0-9_-]*)", css))


# ---------------------------------------------------------------------------
# 1. Page stylesheets
# ---------------------------------------------------------------------------


class TestLivePagesCarryTheirStylesheets:
    @pytest.mark.parametrize(("page", "sheet", "emitted_class"), MIGRATED_PAGE_SHEETS)
    def test_the_stylesheet_exists_in_the_live_tree(
        self, page: str, sheet: str, emitted_class: str
    ) -> None:
        assert (LIVE_STATIC / sheet).is_file(), f"{sheet} was never migrated into static/"

    @pytest.mark.parametrize(("page", "sheet", "emitted_class"), MIGRATED_PAGE_SHEETS)
    def test_the_stylesheet_defines_the_class_the_page_emits(
        self, page: str, sheet: str, emitted_class: str
    ) -> None:
        """⭐ The reported defect: the class was emitted and defined nowhere."""
        defined = css_classes(LIVE_STATIC / sheet)
        assert emitted_class in defined, (
            f"{sheet} does not define .{emitted_class}, which {page} emits"
        )

    @pytest.mark.parametrize(("page", "sheet", "emitted_class"), MIGRATED_PAGE_SHEETS)
    def test_the_page_actually_links_it(
        self, page: str, sheet: str, emitted_class: str
    ) -> None:
        """Copying the file without linking it changes nothing on screen."""
        text = read(page)
        assert sheet in text, f"{page} never links {sheet}"
        assert "{% block styles %}" in text, (
            f"{page} must declare its stylesheet in a block styles override"
        )

    @pytest.mark.parametrize(("page", "sheet", "emitted_class"), MIGRATED_PAGE_SHEETS)
    def test_the_defining_rule_is_not_shadowed_by_an_inline_block(
        self, page: str, sheet: str, emitted_class: str
    ) -> None:
        """An inline rule for the same class would win on source order.

        None of these pages defined the class inline (that was the bug), and
        re-adding one while the stylesheet is linked would recreate the
        duplicate-ownership problem this migration removed.
        """
        text = read(page)
        inline = " ".join(re.findall(r"<style[^>]*>(.*?)</style>", text, re.S))
        assert f".{emitted_class}" not in inline, (
            f"{page} defines .{emitted_class} BOTH inline and in {sheet}"
        )


class TestTheBaseTemplateExposesAStylesBlock:
    def test_the_block_exists(self) -> None:
        """Without it a page has no way to request a stylesheet at all."""
        assert "{% block styles %}{% endblock %}" in read("templates/base.html")

    def test_it_sits_in_the_head(self) -> None:
        text = read("templates/base.html")
        head_end = text.index("</head>")
        assert text.index("{% block styles %}") < head_end


class TestMigratedStylesheetsHaveTheirTokens:
    """A copied stylesheet whose `var(--token)` is undefined renders unstyled."""

    #: Tokens the rebuilt tree consolidated and its page sheets reference.
    REQUIRED_TOKENS = (
        "--radius-card",
        "--radius-control",
        "--radius-sm",
        "--radius-pill",
        "--status-failed",
        "--status-downloading",
        "--status-queued",
        "--status-complete",
        "--status-pending",
        "--status-info",
        "--status-danger",
        "--brand-wikipedia",
        "--bottom-nav-height",
    )

    def test_every_required_token_is_defined_in_live(self) -> None:
        declared: set[str] = set()
        for path in (LIVE_STATIC / "css").glob("*.css"):
            declared |= set(
                re.findall(r"(--[A-Za-z0-9_-]+)\s*:", path.read_text(encoding="utf-8", errors="replace"))
            )
        missing = [t for t in self.REQUIRED_TOKENS if t not in declared]
        assert not missing, f"undefined in the live tree: {missing}"

    def test_status_warning_keeps_the_live_value(self) -> None:
        """⚠️ The rebuilt tree uses a different orange; adopting it restyles
        every existing warning, which is a design change, not a migration."""
        text = read("static/css/popularr.css")
        declarations = re.findall(r"--status-warning:\s*([^;]+);", text)
        assert declarations, "--status-warning must stay declared"
        assert len(declarations) == 1, (
            f"declared {len(declarations)} times — a second declaration in the same "
            "block wins or loses by source order, which is not a decision"
        )


# ---------------------------------------------------------------------------
# 2. The Activity page
# ---------------------------------------------------------------------------


class TestActivityPageIsAvailableOnTheLiveUi:
    def test_the_template_exists(self) -> None:
        assert (TEMPLATES / "pages/activity.html").is_file()

    def test_the_route_no_longer_redirects_by_cutover(self) -> None:
        """⭐ It rendered ONLY when test_site was active, i.e. never by default."""
        source = read("routes/ui_routes.py")
        start = source.index("async def activity()")
        body = source[start:source.index("\n@ui_bp.route", start)]
        assert "test_site_status" not in body, (
            "the route must not gate the page on the UI cutover any more"
        )
        assert "redirect(" not in body
        assert 'render_template(\n        "pages/activity.html"' in body or \
               "pages/activity.html" in body

    def test_the_dashboard_links_to_it(self) -> None:
        assert "ui.activity" in read("templates/pages/dashboard.html")

    def test_both_scripts_exist_in_the_live_tree(self) -> None:
        assert (LIVE_STATIC / "js/activity-feed.js").is_file()
        assert (LIVE_STATIC / "js/pages/activity.js").is_file()

    def test_the_template_loads_both_scripts_in_order(self) -> None:
        """The feed module defines `ActivityFeed`; the page script uses it."""
        text = read("templates/pages/activity.html")
        feed = text.index("js/activity-feed.js")
        page = text.index("js/pages/activity.js")
        assert feed < page, "activity-feed.js must load before activity.js"

    def test_the_template_renders_the_containers_the_script_targets(self) -> None:
        text = read("templates/pages/activity.html")
        script = read("static/js/pages/activity.js")
        for element_id in re.findall(r"byId\('([a-z-]+)'\)", script):
            assert f'id="{element_id}"' in text, (
                f"activity.js targets #{element_id}, which the template does not render"
            )


def strip_jinja_comments(source: str) -> str:
    """Drop {# … #} blocks.

    ⭐ A comment-matching trap that has now bitten three separate assertions in
    this file: the templates deliberately NAME the paths they no longer load, in
    a comment explaining why. Scanning raw text makes an explanatory note look
    like a live reference.
    """
    return re.sub(r"\{#.*?#\}", " ", source, flags=re.S)


def script_srcs(source: str) -> list[str]:
    return re.findall(r"versioned_static\('([^']+)'\)", strip_jinja_comments(source))


def strip_js_comments(source: str) -> str:
    """Drop /* */ and // comments so prose about the absent layer cannot pass
    or fail an assertion about the code. (A comment-matching trap: the module
    deliberately EXPLAINS the globals it no longer calls.)"""
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    return re.sub(r"(^|\s)//[^\n]*", " ", source)


class TestTheMigratedActivityScriptHasNoMissingDependencies:
    """The rebuilt script called `global.api` / `global.poller`, which the live
    tree has never had — a straight copy would throw on the first request."""

    @pytest.mark.parametrize("script", ["static/js/pages/activity.js", "static/js/activity-feed.js"])
    def test_it_does_not_call_the_rebuilt_utils_layer(self, script: str) -> None:
        source = strip_js_comments(read(script))
        for absent in ("global.api.", "global.poller", "global.toast("):
            assert absent not in source, (
                f"{script} calls {absent}; that layer does not exist in the live tree"
            )

    def test_it_uses_a_global_the_live_tree_publishes(self) -> None:
        """`escapeHtml` is the one the rebuilt module relied on that live has."""
        assert "window.escapeHtml" in read("static/js/main.js")
        assert "global.ActivityFeed" in read("static/js/activity-feed.js")

    def test_the_feed_module_is_published_as_a_global(self) -> None:
        assert "global.ActivityFeed =" in read("static/js/activity-feed.js")


class TestActivityFeedIsSharedWithTheDashboardCard:
    def test_the_card_loads_the_same_module(self) -> None:
        """⭐ Two copies of a renderer is how the same scan renders two ways."""
        text = read("templates/pages/dashboard.html")
        assert "js/activity-feed.js" in text, (
            "the dashboard activity card must use the shared feed module"
        )


class TestNoLivePageAsksForAnAssetThatDoesNotExist:
    """⭐ THE 404 THAT HID THIS WHOLE MIGRATION.

    The live dashboard had been copied from the rebuilt template and asked for
    `js/services/activity-feed.js`, `js/services/scan-preflight.js` and friends.
    The live tree keeps its scripts FLAT (`js/<name>.js`), so five of the six
    did not exist. A missing `<script>` is SILENT — no error the user sees, no
    page failure — so five dashboard features simply vanished, which reads as
    "this was never migrated" rather than "these files are missing".

    Only ROUTED templates are checked: a template no route renders (the unrouted
    `artist_detail.html`, `smart_playlists.html`, …) may reference anything.
    """

    @staticmethod
    def _rendered_templates() -> set[str]:
        rendered: set[str] = set()
        for p in (REPO_ROOT / "routes").rglob("*.py"):
            text = p.read_text(encoding="utf-8", errors="replace")
            # Skip commented-out routes: a route that was intentionally removed
            # leaves its render_template line behind in a comment.
            body = "\n".join(
                line for line in text.splitlines() if not line.lstrip().startswith("#")
            )
            rendered |= set(
                re.findall(r"""["']((?:pages|playlists|auth)/[A-Za-z0-9_/.-]+\.html)["']""", body)
            )
        return rendered

    def test_every_asset_a_rendered_page_asks_for_exists(self) -> None:
        missing: dict[str, str] = {}
        for rel in sorted(self._rendered_templates()):
            page = TEMPLATES / rel
            if not page.is_file():
                continue
            text = page.read_text(encoding="utf-8", errors="replace")
            for asset in re.findall(r"versioned_static\('([^']+)'\)", text):
                if not (LIVE_STATIC / asset).is_file():
                    missing[asset] = rel
        assert not missing, (
            "these assets are requested by a rendered page but do not exist in "
            f"static/ (a silent 404): {missing}"
        )

    def test_the_dashboard_uses_flat_script_paths(self) -> None:
        """Checked on the <script> src values, not on the whole file: the block
        deliberately NAMES the path it removed, in a comment."""
        text = read("templates/pages/dashboard.html")
        srcs = script_srcs(text)
        offending = [s for s in srcs if s.startswith("js/services/")]
        assert not offending, (
            "the live tree keeps scripts flat (js/<name>.js); js/services/ does "
            f"not exist here, so these would 404 silently: {offending}"
        )

    def test_the_status_badge_module_was_migrated(self) -> None:
        assert (LIVE_STATIC / "js/ui/status-badge.js").is_file()
        source = strip_js_comments(read("static/js/ui/status-badge.js"))
        assert "global.api." not in source, (
            "status-badge must stay self-contained; it only needs escapeHtml"
        )


# ---------------------------------------------------------------------------
# 3. What is deliberately NOT migrated (a ratchet, so it cannot be forgotten)
# ---------------------------------------------------------------------------


#: Rebuilt-tree pieces that depend on the `utils/ui` layer the live tree does
#: not have. Migrating them is the CUTOVER, not a copy — recorded here so the
#: remaining gap is visible instead of silent.
NOT_MIGRATED_YET: tuple[str, ...] = (
    "js/services/musicbrainz-queue.js",
    "js/services/slskd.js",
    "js/services/genres.js",
    "js/ui/toast.js",
    "js/ui/modal.js",
    "js/ui/confirm.js",
    "js/ui/button-state.js",
    "js/ui/poller.js",
    "js/utils/api.js",
    "js/utils/dom.js",
)


class TestTheRemainingGapIsRecorded:
    def test_they_are_not_loaded_by_a_ROUTED_live_page(self) -> None:
        """⭐ A module that throws on load is WORSE than a silent 404.

        Each of these calls `global.api` / `global.toast` / `global.modal` at
        startup, so pointing a routed live template at one would replace a
        missing feature with an uncaught error on every page that loads it.

        ⚠️ Scoped to ROUTED templates on purpose. `templates/pages/
        artist_detail.html` still references several of them — it is NOT
        rendered (`ui_routes.artist_detail` serves `artist_detail_v2.html`), so
        the references are inert. Repairing that dead template would mean
        porting the utils/ui layer it was written against, which is the cutover.
        """
        rendered = TestNoLivePageAsksForAnAssetThatDoesNotExist._rendered_templates()
        for rel in sorted(rendered):
            page = TEMPLATES / rel
            if not page.is_file():
                continue
            # Only the ACTUAL src values — not the prose that explains the removal.
            srcs = script_srcs(page.read_text(encoding="utf-8", errors="replace"))
            for absent in NOT_MIGRATED_YET:
                assert absent not in srcs, (
                    f"{rel} loads {absent}, which needs the rebuilt utils/ui layer "
                    "the live tree does not have"
                )

    def test_the_live_tree_still_has_its_own_equivalents(self) -> None:
        """The gap is a missing FEATURE, not a missing file with no substitute."""
        for present in (
            "js/main.js",
            "js/scan-preflight.js",
            "js/scan-resume-picker.js",
            "js/upcoming_releases.js",
            "js/artist-page.js",
            "js/artist-releases.js",
        ):
            assert (LIVE_STATIC / present).is_file(), f"{present} is missing from static/"
