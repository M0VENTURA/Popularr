"""Enter in the banner search must START a search, in both trees.

Reported: "Typing an entry into the universal search on the banner and pressing
enter doesn't search. I need to select Library, All or External before a search
is initiated. I want enter to start the search too."

Cause: the banner input (``#navSearchInput``) is where the typing actually
happens — ``openUnifiedSearch`` focuses it back, so the flyout's own input
(whose Enter handler is correct) rarely holds focus. The banner's INLINE handler
only called ``syncNavSearchQuery()``, which copies the text into the flyout and
does nothing else, and ``runSearch`` is private to the search module's closure,
so the markup had no way to reach it. Clicking a Library/All/External tab worked
because THAT click called ``runSearch`` — which is exactly the reported
"must select a scope first" behaviour.

These are source-contract assertions (there is no browser harness here): the
markup must not carry the half-handler, and each search module must expose a
submit entry point and bind Enter on the banner box. Both trees are checked —
the live tree is the default, so a fix that only lands in ``test_site`` would not
be seen.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

BASE_TEMPLATES = [
    "templates/base.html",
    "test_site/templates/base.html",
]

SEARCH_MODULES = [
    "static/js/unified_search.js",
    "test_site/static/js/ui/search-flyout.js",
]


def _read(relative_path: str) -> str:
    path = REPO_ROOT / relative_path
    assert path.exists(), f"{relative_path} is missing"
    return path.read_text(encoding="utf-8")


class TestBannerMarkupHasNoHalfHandler:
    """The bug itself: an onkeydown that only mirrors the text."""

    @pytest.mark.parametrize("template", BASE_TEMPLATES)
    def test_the_banner_input_has_no_two_step_enter_handler(self, template: str):
        source = _read(template)
        # The precise defect: Enter that calls syncNavSearchQuery() and stops.
        assert not re.search(r'onkeydown="[^"]*syncNavSearchQuery', source), (
            "the banner input's Enter handler mirrors the text without searching"
        )

    @pytest.mark.parametrize("template", BASE_TEMPLATES)
    def test_the_banner_input_still_mirrors_on_typing(self, template: str):
        """The fix must not remove the text mirroring that keeps the two in sync."""
        source = _read(template)
        assert "oninput=\"syncNavSearchQuery()\"" in source
        assert 'id="navSearchInput"' in source


class TestSearchModuleOwnsEnter:
    @pytest.mark.parametrize("module_path", SEARCH_MODULES)
    def test_it_exposes_a_submit_entry_point(self, module_path: str):
        source = _read(module_path)
        assert re.search(r"function\s+submitUnifiedSearch|submitUnifiedSearch\s*=", source), (
            "the search module must define a submit entry point"
        )
        assert re.search(r"(?:window|global)\.submitUnifiedSearch\s*=", source), (
            "the submit entry point must be reachable from the markup/other modules"
        )

    @pytest.mark.parametrize("module_path", SEARCH_MODULES)
    def test_the_submit_path_actually_searches(self, module_path: str):
        source = _read(module_path)
        assert re.search(r"submitUnifiedSearch[\s\S]{0,700}?runSearch\(\)", source), (
            "submitting must call runSearch — that call is the whole fix"
        )

    @pytest.mark.parametrize("module_path", SEARCH_MODULES)
    def test_the_enter_handler_calls_the_submit_entry_point(self, module_path: str):
        """The discriminating check: Enter must reach the code that searches.

        The bug was that Enter reached something that only COPIED the text, so a
        guard that merely finds "Enter" and "navSearchInput" in the same file
        would have passed on the broken tree — which is why the binding is
        asserted through to the submit call rather than by proximity alone.
        """
        source = _read(module_path)
        assert re.search(r"['\"]Enter['\"][\s\S]{0,260}?submitUnifiedSearch\(", source), (
            "the Enter handler does not call the submit entry point"
        )

    @pytest.mark.parametrize("module_path", SEARCH_MODULES)
    def test_enter_bypasses_the_same_query_short_circuit(self, module_path: str):
        """Re-pressing Enter must re-run the search, not hit openUnifiedSearch's
        "same query as last time" guard."""
        source = _read(module_path)
        submit = source[source.find("submitUnifiedSearch") :]
        assert "runSearch()" in submit[:900]
