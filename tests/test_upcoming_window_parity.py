"""The dashboard must show the SAME upcoming releases as the dedicated page.

⚠️ THE REPORTED BUG.

> "The upcoming releases on the dashboard is only showing singles from
> MusicBrainz. But looking at the MusicBrainz Daily Collection under
> /downloads/discover/upcoming, there are a lot more listed that don't seem to
> be getting displayed in the dashboard."

``static/js/dashboard.js`` sent ``window: 7``. The API applies ``window`` as a
**SYMMETRIC** clause (±N days), so it did not mean "this week" — it silently
REPLACED the configured display window, narrowing it from the configured
``[-28, +14]`` (43 days) to ``[-7, +7]`` (15 days).

Two consequences, and both were reported:

* the dashboard listed far fewer rows than the dedicated page, which sends no
  ``window`` and therefore gets the full configured span — two views of ONE
  table disagreeing about what "upcoming" means;
* the Config page's "Show Last / Show Next (days)" governed the dedicated page
  but NOT the dashboard, so no amount of configuring could make a future album
  appear there. Albums are announced weeks to months ahead, so a ±7-day span kept
  only near-term releases — exactly why the table "only showed singles".

``get_release_window()``'s docstring already stated the intent: *"Its
``loadUpcomingReleasesTable()`` sends no ``window`` parameter, so the separate
'tight' clause ... never applies — this function alone governs what the dashboard
shows."* The rebuilt dashboard (``test_site/static/js/pages/dashboard.js``) has
always sent none. These tests pin the live copy to the same contract.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIVE_DASHBOARD = ROOT / "static" / "js" / "dashboard.js"
REBUILT_DASHBOARD = ROOT / "test_site" / "static" / "js" / "pages" / "dashboard.js"
API_ROUTE = ROOT / "routes" / "upcoming_releases_routes.py"


def _upcoming_fetch_block(path: Path) -> str:
    """The ``fetchReleases({...})`` call inside ``loadUpcomingReleasesTable``."""
    src = path.read_text(encoding="utf-8")
    start = src.index("loadUpcomingReleasesTable")
    fetch = src.index("fetchReleases({", start)
    end = src.index("});", fetch)
    return src[fetch:end]


# ---------------------------------------------------------------------------
# The dashboard must not narrow the configured window
# ---------------------------------------------------------------------------


def test_the_dashboard_sends_no_window_parameter() -> None:
    """THE REGRESSION. ``window: 7`` collapsed the configured window to ±7 days."""
    block = _upcoming_fetch_block(LIVE_DASHBOARD)

    assert not re.search(r"\bwindow\s*:", block), (
        "the dashboard must not pass `window` — the API applies it as a symmetric "
        "±N clause, which silently replaces the configured display window and is "
        "why the dashboard listed a fraction of the dedicated page's releases"
    )


def test_the_dashboard_still_asks_for_queue_annotations() -> None:
    """The control: removing ``window`` must not remove the other parameters."""
    block = _upcoming_fetch_block(LIVE_DASHBOARD)

    assert "include_queue" in block, "the queue badge data is still requested"
    assert "filter" in block, "the In Collection filter is still passed"


def test_both_ui_trees_agree_on_the_fetch() -> None:
    """⚠️ The live and rebuilt dashboards must ask for the same thing.

    They had drifted: the live copy sent ``window: 7`` and the rebuilt one sent
    nothing, so the same page listed different releases depending on which tree
    was serving. Parity here is the point — not which value is "right".
    """
    live = _upcoming_fetch_block(LIVE_DASHBOARD)
    rebuilt = _upcoming_fetch_block(REBUILT_DASHBOARD)

    assert ("window" in live) == ("window" in rebuilt), (
        "the two dashboards disagree about whether to pass a `window` parameter"
    )


def test_the_dedicated_page_never_narrowed_the_window() -> None:
    """The dedicated page is the reference: it never sent ``window``.

    If this ever changes, the two surfaces can diverge again silently.
    """
    src = (ROOT / "templates" / "pages" / "downloads" / "upcoming.html").read_text(
        encoding="utf-8"
    )
    fetch = src.index("fetchReleases({")
    end = src.index("});", fetch)
    assert not re.search(r"\bwindow\s*:", src[fetch:end]), (
        "the dedicated page relies on the configured window; passing `window` "
        "here would narrow it the same way the dashboard was narrowed"
    )


# ---------------------------------------------------------------------------
# The API's count must describe the same query as the rows it returns
# ---------------------------------------------------------------------------


def test_total_is_snapshotted_after_the_window_clause() -> None:
    """``total`` must not be counted over a WIDER window than the returned rows.

    It used to be snapshotted before the tight ``window`` clause was appended, so
    a ``window``-passing caller got rows for ±N days but a ``total`` for the full
    configured window — the footer advertised "View All 137 Releases" above a
    table of 20, and a client paginating on ``total`` would page into rows the
    filter excludes.
    """
    src = API_ROUTE.read_text(encoding="utf-8")

    snapshot = src.index("total_where_sql = where_sql")
    window_clause = src.index("if window_days > 0:")
    library_clause = src.index("if hide_in_library:")
    count_query = src.index("SELECT COUNT(*) FROM upcoming_releases")

    assert snapshot > window_clause, (
        "`total` is snapshotted before the `window` clause is applied, so the "
        "count describes a different query than the rows"
    )
    assert snapshot > library_clause, (
        "`total` is snapshotted before the library filter is applied, so the "
        "count includes releases the rows exclude"
    )
    assert snapshot < count_query, "the snapshot must precede the COUNT query"


def test_there_is_exactly_one_snapshot() -> None:
    """A second, earlier snapshot is how the mismatch got reintroduced once already."""
    src = API_ROUTE.read_text(encoding="utf-8")

    assert src.count("total_where_sql = where_sql") == 1, (
        "more than one `total_where_sql` snapshot means an earlier one wins and "
        "the count silently stops matching the rows"
    )


def test_the_count_and_the_page_use_one_where_clause() -> None:
    """Both statements must be built from the same clause, by construction."""
    src = API_ROUTE.read_text(encoding="utf-8")

    assert "FROM upcoming_releases{total_where_sql}" in src
    assert "FROM upcoming_releases{where_sql} " in src
    # And the snapshot sits immediately before the query block, so no clause can
    # be appended to `where_sql` afterwards without also reaching the count.
    assert src.index("total_where_sql = where_sql") < src.index(
        "SELECT COUNT(*) FROM upcoming_releases"
    )


@pytest.mark.parametrize("key", ["include_queue"])
def test_the_api_still_honours_the_window_parameter(key: str) -> None:
    """Removing the dashboard's use of ``window`` must not remove the capability.

    The tight clause stays available for callers that genuinely want it; the
    dashboard simply stops using it to narrow the configured window.
    """
    src = API_ROUTE.read_text(encoding="utf-8")

    assert "window_days = max(0, request.args.get(\"window\", 0, type=int))" in src
    assert "if window_days > 0:" in src
    assert key in src
