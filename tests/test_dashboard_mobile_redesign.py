"""Dashboard mobile redesign: bento strip, slim scanner, carousel, activity feed.

The brief, in its own words:

1. Library overview as **one horizontal strip** with `Manage →`.
2. A **slim scanner** — Forced/Restart in the header, the three engines
   side by side.
3. Upcoming Releases as a **swipeable carousel** with artwork, a status pill
   and a direct Queue action.
4. ONE **activity feed** with Recently Added / Recent Scans / Needs Review.
5. **Immediate feedback** after an action (`✓ Queued`).

These are contract assertions over the rebuilt tree (``test_site/``), which is
what ``features.use_test_site`` serves — the live tree keeps its own markup and
is deliberately not touched here.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TS = REPO_ROOT / "test_site"

DASHBOARD_HTML = TS / "templates" / "Pages" / "dashboard.html"
DASHBOARD_CSS = TS / "static" / "css" / "dashboard.css"
DASHBOARD_JS = TS / "static" / "js" / "pages" / "dashboard.js"
RELEASES_JS = TS / "static" / "js" / "services" / "upcoming-releases.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


#: ``{# … #}`` (Jinja) and ``<!-- … -->`` (HTML).
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _strip_html_comments(source: str) -> str:
    """Remove every COMMENT from a template before asserting on its markup.

    ⚠️ Not a nicety — an assertion like ``'col-md-3' not in card`` was met by
    the comment EXPLAINING the change, which quotes the old class name. A test
    that a comment can satisfy describes the documentation, not the code, and
    it fails the moment someone reword the comment. Same reason the negative
    container checks below strip first.
    """
    return _HTML_COMMENT.sub("", _JINJA_COMMENT.sub("", source))


#: A line that is ENTIRELY a comment, and block comments.
_JS_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_JS_LINE_COMMENT = re.compile(r"^[ \t]*//.*$", re.MULTILINE)


def _strip_js_comments(source: str) -> str:
    """Remove full-line and block comments from JavaScript.

    Only WHOLE-line ``//`` comments are removed — an inline ``//`` can sit
    inside a string (a URL), and a regex cannot tell the two apart. Whole-line
    is where the hazard is: a comment naming the function an assertion is
    looking for.
    """
    return _JS_LINE_COMMENT.sub("", _JS_BLOCK_COMMENT.sub("", source))


@pytest.fixture(scope="module")
def html() -> str:
    """The dashboard MARKUP, comments removed (see ``_strip_html_comments``)."""
    return _strip_html_comments(_read(DASHBOARD_HTML))


@pytest.fixture(scope="module")
def raw_html() -> str:
    """The template including comments, for the few checks that want it."""
    return _read(DASHBOARD_HTML)


@pytest.fixture(scope="module")
def css() -> str:
    return _read(DASHBOARD_CSS)


@pytest.fixture(scope="module")
def dash_js() -> str:
    return _strip_js_comments(_read(DASHBOARD_JS))


@pytest.fixture(scope="module")
def releases_js() -> str:
    return _strip_js_comments(_read(RELEASES_JS))


def _block(css: str, selector: str) -> str:
    """The body of the first rule whose selector starts *selector*.

    Brace-counted rather than "up to the next }": the stylesheet uses nested
    rules, so a naive slice would return a fragment that ends inside a child
    rule — which is how an assertion can pass on markup that proves nothing.
    """
    idx = css.index(selector + " {")
    depth = 0
    start = css.index("{", idx) + 1
    for i in range(start, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            if depth == 0:
                return css[start:i]
            depth -= 1
    raise AssertionError(f"unbalanced braces after {selector!r}")


# ---------------------------------------------------------------------------
# 0. The stylesheet must still parse
# ---------------------------------------------------------------------------


class TestTheStylesheetIsWellFormed:
    def test_braces_balance(self, css: str) -> None:
        """A nested-CSS typo silently kills every rule after it.

        The file uses nesting (`.ur-carousel { &::-webkit-scrollbar { … } }`),
        so one missing brace does not fail loudly — it swallows the rest of the
        redesign and the page just looks half-styled.
        """
        assert css.count("{") == css.count("}"), (
            f"{css.count('{')} open vs {css.count('}')} close"
        )

    def test_every_new_block_is_present(self, css: str) -> None:
        for selector in (".library-bento", ".library-bento-item", ".scanner-mode-tabs",
                         ".ur-carousel", ".ur-card", "#recentHistoryTabs"):
            assert selector in css, f"{selector} is missing from dashboard.css"


# ---------------------------------------------------------------------------
# 1. Library overview — one horizontal strip
# ---------------------------------------------------------------------------


class TestTheLibraryStripIsOneRow:

    def test_it_is_a_flex_strip_not_a_wrapping_grid(self, html: str) -> None:
        assert 'class="library-bento"' in html
        # The old presentation: `col-6 col-md-3` cells inside a `row`, which
        # wrapped to 2x2 below md and cost a phone two rows of height.
        idx = html.index('id="libraryStatsCard"')
        end = html.index("</div>\n    </div>", idx)
        card = html[idx:end]
        assert "col-md-3" not in card, (
            "the 3-across grid wrapped to 2x2 on a phone; the strip must not"
        )
        assert card.count("library-bento-item") >= 4

    def test_all_four_metrics_are_present(self, html: str) -> None:
        idx = html.index('class="library-bento"')
        strip = html[idx: html.index("</div>\n        </div>", idx)]
        for label in ("Artists", "Albums", "Tracks", "Avg"):
            assert f">{label}" in strip or f">{label}<" in strip, f"{label} left the strip"

    def test_each_cell_shrinks_equally(self, css: str) -> None:
        """`flex: 1 1 0` + `min-width: 0`, or the cells size to their content."""
        block = _block(css, ".library-bento-item")
        assert "flex: 1 1 0" in block, (
            "without a zero basis a wide number widens its own column and the "
            "row misaligns"
        )
        assert "min-width: 0" in block

    def test_the_label_cannot_wrap_the_strip_to_two_lines(self, css: str) -> None:
        block = _block(css, ".library-bento-label")
        assert "white-space: nowrap" in block
        assert "text-overflow: ellipsis" in block

    def test_the_dividers_do_not_double_the_outer_edge(self, css: str) -> None:
        block = _block(css, ".library-bento-item")
        assert "border-left" in block
        assert ":first-child" in css[css.index(".library-bento-item"):][:600]


# ---------------------------------------------------------------------------
# 2. Slim scanner
# ---------------------------------------------------------------------------


class TestTheScannerIsSlim:

    def test_the_run_options_live_in_the_header(self, html: str) -> None:
        """The brief puts Forced/Restart in the header, not in a pane."""
        idx = html.index('id="scannerControlsCard"')
        header_end = html.index("</div>", html.index('id="scannerStatusBadge"', idx))
        header = html[idx:header_end]
        assert 'id="popScanForce"' in header, "Forced is not in the header"
        assert 'id="popScanRestart"' in header, "Restart is not in the header"

    def test_and_no_longer_in_the_popularity_pane(self, html: str) -> None:
        """CONTROL on the move: present-in-header alone passes if a copy stayed.

        Two elements sharing one id is invalid HTML and the JS would read
        whichever came first — so the move has to be complete.
        """
        assert html.count('id="popScanForce"') == 1
        assert html.count('id="popScanRestart"') == 1

    def test_the_navidrome_force_stays_with_navidrome(self, html: str) -> None:
        """It answers a DIFFERENT question, so hoisting it would conflate them.

        `popScanForce` means "ignore change detection / cache"; `navImportForce`
        means "re-import every artist in full".
        """
        idx = html.index('id="navImportForce"')
        pane = html.index('id="scannerPaneNavidrome"')
        assert pane < idx, "navImportForce drifted out of the Navidrome pane"
        assert idx < html.index('id="scannerPaneEssentia"')

    def test_the_three_engines_stay_visible_side_by_side(self, html: str) -> None:
        """"Side by side" means all three switches are on screen at once."""
        idx = html.index('id="scannerTabs"')
        tabs = html[idx: html.index("</ul>", idx)]
        for target in ("#scannerPanePopularity", "#scannerPaneNavidrome",
                       "#scannerPaneEssentia"):
            assert f'data-bs-target="{target}"' in tabs, f"{target} lost its switch"

    def test_the_essentia_engine_is_retained(self, html: str) -> None:
        """The mock-up dropped Essentia; the brief says keep it."""
        assert "startEssentiaScan()" in html
        assert "Essentia" in html

    def test_the_pane_toolbar_is_one_line(self, html: str) -> None:
        """`mb-2` on the popularity toolbar added a row of dead space; the
        Forced/Restart checkboxes that needed it moved out."""
        idx = html.index('id="scannerPanePopularity"')
        toolbar = html[idx: html.index("</div>", idx) + 400]
        lead = toolbar[: toolbar.index("</select>")]
        assert "mb-2" not in lead, "the control row still reserves a second line"


# ---------------------------------------------------------------------------
# 3. Upcoming releases carousel
# ---------------------------------------------------------------------------


class TestTheReleasesCarousel:

    def test_the_mobile_container_is_a_carousel(self, html: str) -> None:
        assert 'id="upcoming-releases-carousel"' in html
        assert 'id="upcoming-releases-cards"' not in html, (
            "the stacked-card container was replaced, not duplicated"
        )

    def test_the_renderer_exists_and_is_exported(self, releases_js: str) -> None:
        assert re.search(r"function renderCarousel\(", releases_js)
        assert re.search(r"^\s*renderCarousel,", releases_js, re.M), (
            "not exported, so the dashboard cannot call it"
        )

    def test_it_scrolls_on_one_axis_with_snap(self, css: str) -> None:
        block = _block(css, ".ur-carousel")
        assert "overflow-x: auto" in block, "not horizontally scrollable"
        assert "scroll-snap-type: x mandatory" in block
        card = _block(css, ".ur-card")
        assert "scroll-snap-align: start" in card

    def test_the_next_card_peeks(self, css: str) -> None:
        """A full-width card gives no signal that the row scrolls."""
        card = _block(css, ".ur-card")
        assert re.search(r"width:\s*6\d%", card), (
            "the card must be narrower than the viewport so the next one shows"
        )

    def test_artwork_is_only_requested_for_an_owned_album(self, releases_js: str) -> None:
        """⭐ An unowned release's art endpoint is a guaranteed 404.

        Requesting it anyway fires one failing request per card — fifty on a
        full list — to render the placeholder we could have shown for free.
        """
        idx = releases_js.index("function renderCarousel(")
        body = releases_js[idx: releases_js.index("\n  async function queueById", idx)]
        assert "album_in_collection" in body, (
            "the carousel must branch on ownership before requesting art"
        )
        assert "/art?s=200" in body
        # The branch must GUARD the request, so the URL only appears inside it.
        guard = body.index("album_in_collection")
        assert guard < body.index("/art?s=200")

    def test_the_status_pill_is_rendered(self, releases_js: str) -> None:
        idx = releases_js.index("function renderCarousel(")
        body = releases_js[idx: releases_js.index("\n  async function queueById", idx)]
        assert "ur-card-flag" in body
        assert "Out now" in body and "Upcoming" in body, (
            "the pill the brief asks for, from isReleased()"
        )

    def test_a_queued_release_shows_a_disabled_tick(self, releases_js: str) -> None:
        """A settled state, not a second live Queue button."""
        idx = releases_js.index("function renderCarousel(")
        body = releases_js[idx: releases_js.index("\n  async function queueById", idx)]
        assert "disabled" in body
        assert "Queued" in body
        assert "In Queue" in body

    def test_an_empty_list_renders_a_message(self, releases_js: str) -> None:
        idx = releases_js.index("function renderCarousel(")
        body = releases_js[idx: releases_js.index("\n  async function queueById", idx)]
        assert "emptyMessage" in body, (
            "a blank row reads as broken rather than empty"
        )

    def test_the_desktop_table_is_kept(self, html: str) -> None:
        """CONTROL: the carousel must not have replaced the desktop view."""
        assert 'id="upcomingTableWrap"' in html
        assert "d-none d-lg-block" in html


# ---------------------------------------------------------------------------
# 4. One activity feed, three tabs
# ---------------------------------------------------------------------------


class TestTheActivityFeed:

    def test_three_tabs_share_one_card(self, html: str) -> None:
        idx = html.index('id="recentHistoryTabs"')
        tabs = html[idx: html.index("</ul>", idx)]
        assert tabs.count('role="tab"') == 3, "expected exactly three tabs"
        for target in ("#recentAddedPane", "#recentScansPane", "#pendingApprovalPane"):
            assert f'data-bs-target="{target}"' in tabs

    def test_needs_review_replaced_its_own_card(self, html: str) -> None:
        assert 'id="pendingApprovalCard"' not in html, (
            "the separate card was consolidated into the feed's third tab"
        )
        assert html.count('id="pending-approval-body"') == 1

    def test_the_tab_starts_hidden(self, html: str) -> None:
        idx = html.index('id="pendingApprovalTabItem"')
        tag = html[html.rindex("<", 0, idx): html.index(">", idx)]
        assert "d-none" in tag, (
            "an always-visible 'Needs Review 0' is a permanent nag"
        )

    def test_the_pane_carries_its_own_explanation(self, html: str) -> None:
        """The old card's footer moved with its content, not was dropped."""
        idx = html.index('id="pendingApprovalPane"')
        pane = html[idx: html.index("</div>\n\n                </div>", idx)]
        assert "newest first" in pane, "the work-queue explanation was lost"

    def test_the_hint_covers_the_new_tab(self, dash_js: str) -> None:
        assert "pendingApprovalPane:" in dash_js, (
            "switching to Needs Review would blank the hint line"
        )

    def test_an_emptied_active_tab_moves_the_user_off_it(self, dash_js: str) -> None:
        """⭐ The real trap in the consolidation.

        Saving an album clears the stash, so the list can empty WHILE the user
        is looking at it. Hiding the tab under them leaves a blank pane with no
        way back.
        """
        idx = dash_js.index("function renderPendingApproval(")
        body = dash_js[idx: idx + 1600]
        assert "contains('active')" in body
        assert "getOrCreateInstance" in body, (
            "the user is not moved to a visible tab"
        )

    def test_the_tab_visibility_follows_the_count(self, dash_js: str) -> None:
        idx = dash_js.index("function renderPendingApproval(")
        body = dash_js[idx: idx + 1600]
        assert "classList.remove('d-none')" in body
        assert "classList.add('d-none')" in body
        assert "pendingApprovalTabItem" in body


# ---------------------------------------------------------------------------
# 5. Queue feedback
# ---------------------------------------------------------------------------


class TestQueueFeedback:

    def test_the_tick_is_shown_before_the_refresh(self, releases_js: str) -> None:
        """⭐ Order matters: `onRefresh()` re-renders the list.

        Showing the tick AFTER it would act on a detached button, so the
        confirmation the brief asks for would never appear.
        """
        idx = releases_js.index("async function queueById(")
        body = releases_js[idx: idx + 1200]
        assert "setDone" in body, "the toggled tick is the requested feedback"
        assert "Queued" in body
        assert body.index("setDone") < body.index("onRefresh"), (
            "the tick must be set before the refresh replaces the button"
        )

    def test_a_failure_leaves_the_action_available(self, releases_js: str) -> None:
        """The brief: on failure retain the action and show the error."""
        idx = releases_js.index("async function queueById(")
        body = releases_js[idx: idx + 1200]
        assert "catch" in body
        assert "notifyError" in body

    def test_the_probe_still_drives_the_real_function(self) -> None:
        """The JS probe harness must run against the shipped source.

        It extracts ``renderPendingApproval`` by name and stubs only the DOM, so
        a signature change breaks it loudly rather than silently skipping.
        """
        probe = REPO_ROOT / "tests" / "js" / "pending-approval-probe.js"
        result = subprocess.run(
            ["node", str(probe), str(DASHBOARD_JS)],
            capture_output=True, text=True, timeout=60,
        )
        assert result.stdout.strip(), f"probe produced nothing: {result.stderr[:400]}"
        data = json.loads(result.stdout)
        assert "error" not in data, data.get("error")
        assert data["empty_hidden"] is True
        assert data["one_visible"] is True
        assert data["escapes_html"] is True


# ---------------------------------------------------------------------------
# 6. The activity feed's own rows — art, truncation, the add button
# ---------------------------------------------------------------------------


#: An album name carrying BOTH characters that break naive interpolation: an
#: apostrophe ends a single-quoted JS literal, a double quote ends an HTML
#: attribute. Real library names contain both.
TRICKY_ALBUM = '"Heroes" & Other Rarities'
TRICKY_ARTIST = "O'Brien & Sons"


@pytest.fixture(scope="module")
def recent_row() -> str:
    """Render the REAL dashboard and return one Recently Added row.

    ⭐ A source-text assertion cannot answer the questions this section asks —
    whether the year survives a long title, whether the button ended up outside
    the anchor, whether a name with a quote in it encodes safely. So the shipped
    template is rendered and the markup read back.
    """
    import sys
    from datetime import datetime

    sys.path.insert(0, str(REPO_ROOT))
    from tests._test_site_render import build_env

    env = build_env("/dashboard")
    html = env.get_template("Pages/dashboard.html").render(
        recent_albums=[{
            "artist": TRICKY_ARTIST,
            "album": TRICKY_ALBUM,
            "album_year": "1977",
            "album_type": "Album",
            "track_count": 10,
            "added_at": datetime(2026, 10, 14),
        }],
        recent_scans=[],
        stats={"artists_c": 1, "ac": 2, "tc": 3, "avg_stars": 4.5},
        album_categories=[],
        albums_by_category={},
        top_tracks=[],
        error=None,
        scan_active=False,
        launch_on_startup=False,
        first_full_scan_done=True,
        perpetual=False,
        forced=False,
        nav_users=[],
    )

    marker = html.index("js-play-album")
    start = html.rindex('<div class="list-group-item', 0, marker)
    end = html.index("</button>", marker) + len("</button>")
    return html[start:end]


class TestTheActivityRowShowsArtwork:

    def test_real_art_is_requested(self, recent_row: str) -> None:
        assert "/art?s=80" in recent_row, (
            "the row still shows a placeholder instead of the album's artwork"
        )

    def test_the_request_is_lazy(self, recent_row: str) -> None:
        """Twelve albums x one request each is a slow first paint otherwise."""
        assert 'loading="lazy"' in recent_row

    def test_there_is_a_fallback_for_an_album_with_no_image(self, recent_row: str) -> None:
        """The endpoint returns a placeholder SVG rather than 404-ing, but an
        album with no art at all should still fall back to the disc glyph rather
        than showing a broken image."""
        assert "onerror=" in recent_row
        assert "bi-disc" in recent_row, "the fallback glyph was removed"

    def test_the_name_is_percent_encoded_for_the_url(self, recent_row: str) -> None:
        """⭐ `path_segment` double-encodes so the ASGI server's `%2F` decoding
        cannot split the path. Asserted through a name containing an apostrophe,
        a quote, an ampersand and spaces — a raw `O'Brien & Sons` in a URL is a
        different request from the encoded one."""
        assert "O%2527Brien" in recent_row, (
            "the artist segment is not percent-encoded — a name with a quote or "
            "an ampersand in it produces a broken art URL"
        )
        assert "O'Brien &" not in recent_row.split('src="')[1].split('"')[0], (
            "raw characters reached the URL"
        )


class TestTheRowDoesNotClipItsMetadata:

    def test_the_year_is_outside_the_truncating_title(self, recent_row: str) -> None:
        """⭐ The reported truncation bug.

        The title div is `text-truncate` (nowrap + ellipsis + hidden). With the
        year and the type badge INSIDE it, a long album name ellipsised the
        METADATA away instead of the title — the user lost the year rather than
        seeing a shortened name.
        """
        title_start = recent_row.index("text-truncate text-light")
        title_span = recent_row[title_start: recent_row.index("</span>", title_start)]
        assert "1977" not in title_span, (
            "the year is inside the truncating element, so a long title clips it"
        )
        # ⚠️ Asserted on the BADGE MARKUP, not on the label text. A mutation that
        # re-nested `<span class="badge">{{ al.album_type }}</span>` put "Album"
        # inside the title span but WITHOUT a `</span>` after it, so a check for
        # `"Album</span>" not in title_span` passed and the mutation escaped —
        # the assertion was reading the wrong thing. The markup is what nests.
        assert 'class="badge' not in title_span, (
            "the type badge is inside the truncating element"
        )
        assert "text-muted" not in title_span, (
            "another metadata span is inside the truncating element"
        )

    def test_the_metadata_is_still_present(self, recent_row: str) -> None:
        """CONTROL: moving it must not drop it."""
        assert "1977" in recent_row
        assert ">Album<" in recent_row

    def test_the_art_cannot_be_squeezed_by_a_long_title(self, recent_row: str) -> None:
        """`flex-shrink-0` on the art: without it a long title compresses the
        thumbnail instead of ellipsising."""
        img = recent_row[recent_row.index("<img"): recent_row.index(">", recent_row.index("<img"))]
        assert "flex-shrink-0" in img


class TestTheLowProfileAddButton:

    def test_the_button_exists_and_says_what_it_does(self, recent_row: str) -> None:
        assert "js-play-album" in recent_row
        assert 'aria-label="Play' in recent_row, (
            "an icon-only button needs an accessible name"
        )
        assert 'title="Play this album"' in recent_row

    def test_it_is_outside_the_album_link(self, recent_row: str) -> None:
        """⭐ A `<button>` inside an `<a>` is INVALID HTML.

        The browser reparses it, and a click fires both the button and the link
        — so playing an album would also navigate away from the dashboard. The
        row had to become a div with the link restricted to the art + text.
        """
        assert not re.search(
            r"<a\b[^>]*>(?:(?!</a>).)*js-play-album", recent_row, re.DOTALL
        ), "the play button is nested inside the album link"
        # And the link still wraps the part that SHOULD navigate.
        assert re.search(r"<a\b[^>]*>", recent_row)
        assert recent_row.count("<a ") == 1

    def test_the_names_survive_in_data_attributes(self, recent_row: str) -> None:
        """⭐ Why `data-` rather than an inline onclick.

        An apostrophe in the album name would terminate a JS string literal in
        the attribute and produce a syntax error on the page. Jinja escapes the
        quotes to entities, which the browser decodes when reading `dataset`.
        """
        artist_attr = re.search(r'data-artist="([^"]*)"', recent_row).group(1)
        album_attr = re.search(r'data-album="([^"]*)"', recent_row).group(1)

        # No RAW quote/`<`/`>` — the attribute value must be entity-escaped.
        for value in (artist_attr, album_attr):
            assert '"' not in value
            assert "<" not in value and ">" not in value
        assert "&#34;" in album_attr or "&quot;" in album_attr, (
            "the album's own double quotes were not escaped"
        )
        assert "&#39;" in artist_attr or "&apos;" in artist_attr, (
            "the apostrophe in the artist name was not escaped"
        )
        assert "onclick" not in recent_row, (
            "an inline onclick would break on a name containing a quote"
        )


class TestThePlayHandler:

    def test_it_uses_the_endpoint_the_album_page_already_uses(self, dash_js: str) -> None:
        """No new API, so this cannot drift from what the album page shows."""
        assert "/api/album/tracklist?" in dash_js

    def test_it_renames_the_track_id_for_the_player(self, dash_js: str) -> None:
        """`/api/album/tracklist` returns `track_id`; `Player.playQueue` wants
        `id`. Mapping it explicitly is what stops the queue receiving undefined
        ids."""
        idx = dash_js.index("function playRecentAlbum(")
        body = dash_js[idx: idx + 2200]
        assert "t.track_id" in body
        assert "id:" in body

    def test_an_empty_album_says_so(self, dash_js: str) -> None:
        """Playing nothing reads as a broken button."""
        idx = dash_js.index("function playRecentAlbum(")
        body = dash_js[idx: idx + 2200]
        assert "tracks.length" in body
        assert "toast.warning" in body

    def test_a_failure_is_reported_and_the_button_recovers(self, dash_js: str) -> None:
        """⭐ Bounded to the CATCH BLOCK, not a character window.

        `playRecentAlbum` contains TWO `toast.error` calls — the player-missing
        guard and the failure report. A window assertion of `"toast.error" in
        body` was therefore satisfied by the guard, so deleting the failure
        report still passed. The slice now starts at the `catch`.
        """
        idx = dash_js.index("function playRecentAlbum(")
        body = dash_js[idx: idx + 2200]
        catch = body[body.index("} catch (error)"):]
        assert "toast.error" in catch, (
            "a failed play reports nothing — the button looks like it did nothing"
        )
        assert "error.message" in catch, "the reason is swallowed"
        assert "finally" in body and "restore()" in body, (
            "the button is left in its busy state after a failure"
        )

    def test_the_handler_is_delegated(self, dash_js: str) -> None:
        """⭐ The pane is SERVER-RENDERED, so its buttons exist before this
        module runs — but a re-render would drop per-button listeners. One
        listener on the ancestor survives any innerHTML replacement."""
        idx = dash_js.index("function initRecentlyAddedActions(")
        body = dash_js[idx: idx + 900]
        assert "addEventListener('click'" in body
        assert "closest('.js-play-album')" in body, (
            "the listener must resolve the button by class from the event target"
        )

    def test_the_handler_is_wired_at_init(self, dash_js: str) -> None:
        """An unbound handler is a dead button."""
        assert "initRecentlyAddedActions();" in dash_js
        # And inside the DOMContentLoaded block, not merely defined.
        idx = dash_js.index("document.addEventListener('DOMContentLoaded'")
        assert "initRecentlyAddedActions()" in dash_js[idx:], (
            "the bind never runs"
        )

    def test_it_does_not_navigate_as_well_as_playing(self, dash_js: str) -> None:
        idx = dash_js.index("function initRecentlyAddedActions(")
        body = dash_js[idx: idx + 900]
        assert "preventDefault()" in body

