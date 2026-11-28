"""The downloads trio is one page.

Three URLs used to serve the same page with a tab removed each time:

    /downloads/monitor   folders + Soulseek + playlists + queue
    /downloads                      Soulseek + playlists + queue
    /downloads/search               Soulseek + playlists

Only the largest was in the navigation, the smaller two carried duplicate
copies of every page script, and their templates had drifted apart. They now
302 into the monitor, keeping the query string (``?q=`` is how every "Search
Soulseek" link arrives) and adding a fragment that monitor.js uses to open the
right tab.

What this pins:

1. the two subset templates are gone, so nobody edits them again;
2. the routes still exist and are REDIRECTS — never 404, never a second render;
3. the fragments they emit name tabs the page actually has (a typo would land
   the user on the default tab with the panel hidden);
4. the one page really carries what the subsets had.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
UI_ROUTES = REPO_ROOT / "routes" / "ui_routes.py"
PAGES = REPO_ROOT / "test_site/templates/Pages/downloads"
MONITOR_TPL = PAGES / "monitor.html"


def _source() -> str:
    return UI_ROUTES.read_text(encoding="utf-8")


def _handler_source(name: str) -> str:
    """The handler's own source, found through the AST.

    Parsed rather than sliced by marker: a marker-based slice silently picks up
    the NEXT function once a comment or a decorator moves.
    """
    src = _source()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            segment = ast.get_source_segment(src, node)
            assert segment is not None
            return segment
    raise AssertionError(f"no handler named {name} in routes/ui_routes.py")


# ---------------------------------------------------------------------------
# 1. The subset templates are gone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["queue.html", "search.html"])
def test_the_subset_template_is_deleted(name: str) -> None:
    assert not (PAGES / name).exists(), (
        f"{name} still exists — it would be a second downloads page nobody "
        "routes to, which is exactly the drift this collapsed"
    )


def test_the_one_page_is_still_there() -> None:
    assert MONITOR_TPL.is_file()


# ---------------------------------------------------------------------------
# 2. The routes are redirects
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    ["downloads_page", "downloads_search", "downloads_search_soulseek",
     "downloads_search_musicbrainz", "downloads_search_playlists"],
)
def test_the_route_redirects_instead_of_rendering(handler: str) -> None:
    body = _handler_source(handler)
    assert "redirect(_monitor_url(" in body, (
        f"{handler} no longer redirects into the monitor"
    )
    assert "render_template" not in body, (
        f"{handler} renders a template again — that is the second page back"
    )


def test_the_plain_downloads_url_keeps_no_fragment() -> None:
    """`/downloads` lands on the default tab, which is the one holding the queue."""
    assert "return redirect(_monitor_url())" in _handler_source("downloads_page")


def test_the_query_string_is_carried_through() -> None:
    """Every "Search Soulseek" link is /downloads/search?q=… — dropping `q`
    would land on an empty search box, which is the bug this app already had
    once (see pages/search.js's header)."""
    body = _handler_source("_monitor_url")
    assert "request.query_string" in body, (
        "_monitor_url stopped forwarding the query string"
    )


# ---------------------------------------------------------------------------
# 3. The fragments name real tabs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fragment", ['"#soulseek"', '"#playlists"'])
def test_the_emitted_fragment_is_a_tab_on_the_page(fragment: str) -> None:
    """A fragment the page does not have silently opens the default tab."""
    used = fragment.strip('"')
    tpl = MONITOR_TPL.read_text(encoding="utf-8")
    assert f'data-bs-target="{used}"' in tpl, (
        f"a redirect sends users to {used}, but monitor.html has no such tab"
    )


def test_the_page_switches_tab_from_the_fragment() -> None:
    """Bootstrap pills ignore the hash, so a redirect alone is not enough."""
    monitor_js = (REPO_ROOT / "test_site/static/js/pages/monitor.js").read_text(
        encoding="utf-8"
    )
    assert "function activateTabFromHash(" in monitor_js
    assert 'data-bs-target="#' in monitor_js, (
        "the activator no longer looks the tab trigger up by its target"
    )
    assert "activateTabFromHash();" in monitor_js, "it is defined but never called"


# ---------------------------------------------------------------------------
# 4. The one page carries what the subsets had
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "needle",
    [
        # the queue (was queue.html's own tab, and monitor's folders tab)
        "components/search/_queue_status.html",
        # the discards section (queue.html was the only page rendering it)
        "components/_discarded_downloads.html",
        "js/pages/discarded-downloads.js",
        # the Soulseek panel and its controller
        "components/search/_soulseek.html",
        "js/pages/soulseek-search.js",
        # the playlist panel and BOTH its controllers
        "components/search/_playlists.html",
        "js/features/csv-import.js",
        # the row actions + ?q= auto-search that search.html used to load
        "js/pages/search.js",
    ],
)
def test_the_monitor_page_kept_every_piece(needle: str) -> None:
    tpl = MONITOR_TPL.read_text(encoding="utf-8")
    assert needle in tpl, f"monitor.html lost {needle} when the pages collapsed"


def test_the_discards_section_sits_above_the_tabs() -> None:
    """"At the top" is the original request: above the strip it shows on every
    tab; inside a pane it would be hidden on most of them."""
    tpl = MONITOR_TPL.read_text(encoding="utf-8")
    assert tpl.index("_discarded_downloads.html") < tpl.index("searchTabs")
