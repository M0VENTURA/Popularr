"""The navbar's System menu and the Ctrl+K search shortcut.

Two changes to the rebuilt shell (``test_site/templates/base.html``):

1. **System dropdown.** Logs, Help, Config and Tag Corrections used to be four
   separate top-level entries — three of them icon-only ``px-2`` links plus
   "Tag Corrections" buried in the Artists menu — which made eleven items in one
   navbar. Between the ``lg`` and ``xl`` breakpoints every label carries
   ``d-lg-none d-xl-inline``, so the icon-only utilities sat next to
   *unlabelled* primary links with nothing to distinguish a page from a
   utility. Consolidating them removes three icons while keeping every
   destination reachable.

2. **Ctrl+K / ⌘K.** Bound on ``document`` so it fires from anywhere, labelled
   with the platform's modifier.

These are source-contract assertions — there is no browser harness here — so
they check the markup and the binding rather than rendered output. The
``test_unified_search_enter_submits`` suite already pins the Enter path, and the
pre-existing contract for that path is re-asserted here so this change cannot
have broken it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BASE = REPO_ROOT / "test_site" / "templates" / "base.html"
FLYOUT = REPO_ROOT / "test_site" / "static" / "js" / "ui" / "search-flyout.js"
CSS = REPO_ROOT / "test_site" / "static" / "css" / "popularr.css"


@pytest.fixture(scope="module")
def base() -> str:
    return BASE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def flyout() -> str:
    return FLYOUT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    return CSS.read_text(encoding="utf-8")


class TestSystemDropdown:
    def test_system_dropdown_exists(self, base: str):
        assert 'id="systemDropdown"' in base
        assert "System" in base

    @pytest.mark.parametrize(
        "endpoint",
        ["ui.logs", "ui.help_page", "ui.config_editor", "ui.correcting"],
    )
    def test_every_utility_is_still_reachable(self, base: str, endpoint: str):
        """Consolidation must not drop a destination."""
        assert f"url_for('{endpoint}')" in base, f"{endpoint} lost from the navbar"

    def test_the_utilities_are_inside_the_system_menu(self, base: str):
        """They must be reachable FROM the System dropdown, not merely present.

        Asserting only that the URLs exist would pass on the old markup, where
        each was its own nav item — which is the arrangement being replaced.
        """
        start = base.index('id="systemDropdown"')
        menu = base[start : base.index("</ul>", start)]
        for endpoint in ("ui.logs", "ui.correcting", "ui.help_page", "ui.config_editor"):
            assert f"url_for('{endpoint}')" in menu, (
                f"{endpoint} is not inside the System dropdown menu"
            )

    def test_the_old_icon_only_utility_links_are_gone(self, base: str):
        """The three separate icon-only nav items must not remain.

        They were the crowding: ``nav-link px-2`` with only a ``title``, so
        between lg and xl they rendered as unlabelled icons beside unlabelled
        primary links.
        """
        offenders = re.findall(
            r'<li class="nav-item d-none d-lg-block"><a class="nav-link px-2"[^>]*'
            r'href="\{\{ url_for\(\'ui\.(?:logs|help_page|config_editor)\'\)',
            base,
        )
        assert not offenders, (
            f"{len(offenders)} icon-only utility link(s) are still top-level; "
            "they belong in the System dropdown"
        )

    def test_navbar_entry_count_is_reduced(self, base: str):
        """Fewer top-level items than before (was 10 + search + 3 icons).

        Counted as direct ``<li class="nav-item">`` children of the navbar list;
        dropdown *entries* live in nested ``<li>``s without the class, so this
        measures top-level density rather than total links.
        """
        nav = base[base.index('class="navbar-nav'): base.index("</ul>", base.index('class="navbar-nav'))]
        top_level = len(re.findall(r'<li class="nav-item', nav))
        assert top_level <= 7, (
            f"{top_level} top-level navbar items — the System consolidation "
            "should leave at most 7 (Dashboard, Artists, Downloads, Playlists, "
            "Discover, Missing, System, + user)"
        )


class TestSearchShortcut:
    def test_the_hint_is_rendered_in_the_search_box(self, base: str):
        assert 'id="navSearchKbdHint"' in base
        assert 'id="navSearchKbd"' in base

    def test_the_hint_is_hidden_where_the_navbar_is_tight(self, base: str):
        """Below lg the search row wraps; the badge would crowd its own input."""
        assert 'class="input-group-text bg-dark border-secondary text-muted search-kbd-hint d-none d-lg-flex' in base

    def test_the_input_advertises_the_shortcut(self, base: str):
        assert 'aria-keyshortcuts="Control+K Meta+K"' in base
        assert "aria-keyshortcuts" in base

    def test_the_hint_is_a_sibling_not_an_overlay(self, base: str):
        """It must not be positioned over the input it labels."""
        idx = base.index('id="navSearchKbdHint"')
        tag_start = base.rindex("<", 0, idx)
        hint_attrs = base[tag_start : idx]
        assert "position" not in hint_attrs and "absolute" not in hint_attrs

    def test_the_shortcut_is_bound_on_document(self, flyout: str):
        """A shortcut that only works after clicking into the box saves nothing."""
        handler = flyout[flyout.index("String(e.key || '').toLowerCase() !== 'k'") :]
        assert "document.addEventListener('keydown'" in flyout
        assert "e.preventDefault()" in handler[:600]
        assert "openUnifiedSearch()" in handler[:600]

    def test_both_modifiers_are_accepted(self, flyout: str):
        assert "(e.ctrlKey || e.metaKey)" in flyout

    def test_it_does_not_fire_while_typing_in_a_field(self, flyout: str):
        """Otherwise the browser/OS binding is hijacked inside text inputs."""
        window = flyout[flyout.index("String(e.key || '').toLowerCase() !== 'k'") :]
        window = window[:1400]
        assert "isContentEditable" in window
        assert "'input'" in window and "'textarea'" in window
        # The guard must come BEFORE the open call, or it guards nothing.
        assert window.index("isContentEditable") < window.index("openUnifiedSearch()")

    def test_the_label_follows_the_platform(self, flyout: str):
        assert "navSearchKbd" in flyout
        assert "Mac|iPhone|iPad|iPod" in flyout

    def test_the_hint_styles_exist(self, css: str):
        assert ".search-kbd-hint" in css
        assert ".search-kbd " in css or ".search-kbd {" in css

    def test_the_hint_uses_theme_tokens_not_hardcoded_colours(self, css: str):
        block = css[css.index(".search-kbd-hint") : css.index(".search-kbd-hint") + 700]
        assert "var(--tertiary-bg)" in block
        assert "var(--border-color)" in block


class TestEnterPathStillWorks:
    """The pre-existing banner-Enter contract, re-asserted after the edit."""

    def test_the_banner_keeps_its_mirroring_handler(self, base: str):
        assert 'oninput="syncNavSearchQuery()"' in base
        assert 'id="navSearchInput"' in base
        assert not re.search(r'onkeydown="[^"]*syncNavSearchQuery', base)

    def test_the_submit_entry_point_survives(self, flyout: str):
        assert "global.submitUnifiedSearch =" in flyout
        assert re.search(r"['\"]Enter['\"][\s\S]{0,260}?submitUnifiedSearch\(", flyout)
