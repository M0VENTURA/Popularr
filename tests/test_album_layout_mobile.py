"""Album page layout: the action row, the stats grid, the hero polish.

Three of the four points from the mobile review, running against BOTH trees:

1. **The action row** — `[▶ PLAY ALBUM] [⚡ Scan] [⋮]`, the same shape as the
   artist page's. It replaced an inline `scan-toolbar` (scan-type select + Run +
   Force + Actions) that the rebuilt tree wrapped in a phone-only "Scan Options"
   collapsible while the artist page rendered it raw — the inconsistency the
   review flagged, plus the flex-wrapping, the `⚡` vs `⚡ Run` mismatch and the
   help text squeezed into three vertical lines, all symptoms of that one row.

2. **The stats grid** — a single row of four. It was `col-6 col-md-3`, so a 2x2
   on phones, costing twice the height AND drawing an ORPHANED DIVIDER:
   `border-end` sits on the first three cells, and in a 2x2 the second cell of
   row 1 is that row's right edge, so RATING wore a rule with nothing beside it.

3. **The hero** — the title clamps to two lines, and the art's edit overlay is a
   corner badge instead of a slab across the middle of an 80px thumbnail.
"""

from __future__ import annotations

import re

import pytest
from jinja2 import FileSystemLoader

from tests._test_site_render import REPO_ROOT, build_env, install_real_filters

TREES = (
    ("templates", "pages/album_detail.html", "live"),
    ("test_site/templates", "Pages/album_detail.html", "rebuilt"),
)

PAGES = tuple(REPO_ROOT / root / name for root, name, _ in TREES)

POPULARR_CSS = (
    REPO_ROOT / "static" / "css" / "popularr.css",
    REPO_ROOT / "test_site" / "static" / "css" / "popularr.css",
)


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture(params=PAGES, ids=lambda p: p.parent.parent.parent.name)
def page(request) -> str:
    return _read(request.param)


def _payload() -> dict:
    return {
        "artist_name": "Test Artist",
        "album_name": "Your Heart Stopped Beating On A Very Long Album Name",
        "tracks": [
            {"id": "t1", "title": "Track One", "duration": 200, "stars": 5},
            {"id": "t2", "title": "Track Two", "duration": 210, "stars": 4},
        ],
        "album_data": {
            "album_year": 2006, "avg_stars": 4.2, "track_count": 2,
            "total_duration": 410, "spotify_release_date": "2006-09-18",
            "spotify_album_type": "album", "singles_count": 1,
        },
        "hero_genres": ["Rock"],
        "is_album_favourite": False,
        "slskd_config": {"enabled": False},
        "_show_rel_title": False,
        "_show_rel_year": False,
    }


@pytest.fixture(params=TREES, ids=lambda t: t[2])
def rendered(request):
    root, name, _label = request.param
    env = build_env("/album/Test Artist/An Album")
    env.loader = FileSystemLoader(str(REPO_ROOT / root))
    # The page embeds `escapejs`d values in its onclick handlers, and an
    # unregistered filter makes the whole template fail to COMPILE.
    install_real_filters(env, "escapejs")
    return env.get_template(name).render(**_payload())


# ---------------------------------------------------------------------------
# 1. The action row
# ---------------------------------------------------------------------------

def _button_tag_for(rendered: str, handler: str) -> str:
    """The opening ``<button …>`` tag whose handler is *handler*.

    ⚠️ Counting `btn-lg` across the page is NOT enough: the ⋮ button is also
    `btn-lg`, so demoting Play to `btn-sm` still leaves two and the count still
    passes. The assertion has to be about the specific control.
    """
    at = rendered.index(handler)
    start = rendered.rindex("<button", 0, at)
    return rendered[start: rendered.index(">", at)]


class TestTheAlbumActionRow:
    def test_the_three_controls_are_present(self, rendered):
        assert "playAlbum(" in rendered, "no Play control"
        assert "openAlbumScanSheet(" in rendered, "no Scan control"
        assert "More album actions" in rendered, "no ⋮ menu"

    def test_the_inline_scan_toolbar_is_gone(self, rendered):
        """CONTROL: two scan UIs on one page is exactly what the sheet removes."""
        assert "albumScanForm" not in rendered
        assert "albumForceScan" not in rendered
        assert "scan-toolbar-toggle" not in rendered
        assert 'name="scan_type"' not in rendered, (
            "a scan-type select is still rendered — the sheet owns that choice now"
        )

    def test_play_is_first_then_scan_then_the_overflow(self, rendered):
        """The design's order, with Play as the primary."""
        assert rendered.index("playAlbum(") < rendered.index("openAlbumScanSheet(")
        assert rendered.index("openAlbumScanSheet(") < rendered.index("More album actions")

    def test_it_matches_the_artist_page_shape(self, rendered):
        """⭐ The two pages must offer the same three controls.

        That consistency IS the change: the review's first observation was that
        the scan control block differed between the two pages.
        """
        for handler in ("playAlbum(", "openAlbumScanSheet("):
            tag = _button_tag_for(rendered, handler)
            assert "btn-lg" in tag, (
                f"the {handler.strip('(')} control is not the large size the "
                f"artist page uses: {tag}"
            )

    def test_the_sheet_is_rendered_in_the_album_scope(self, rendered):
        assert 'id="scanSheet"' in rendered, (
            "the Scan button opens a sheet the page never rendered"
        )
        assert 'data-scan-scope="album"' in rendered, (
            "the sheet is not in the album scope, so Run would scan the whole library"
        )

    def test_the_actions_menu_kept_its_entries(self, rendered):
        """CONTROL: trimming the toolbar must not trim the menu."""
        for label in ("Lookup on MusicBrainz", "Download Missing Tracks",
                      "Auto-Link MBIDs", "Change Album Art"):
            assert label in rendered, f"{label} was lost from the ⋮ menu"


# ---------------------------------------------------------------------------
# 2. The stats grid
# ---------------------------------------------------------------------------

def _metric_cells(page: str) -> list[str]:
    """The class list of each cell in the hero's metrics row.

    Scoped to the row by walking its own ``<div>``s, not by a fixed character
    window: the page holds a SECOND four-field 2x2 grid (the edit-metadata
    modal's Original Year / Release Year / Album Type / ...), and a window that
    ran past the row would count its cells as metric cells.
    """
    box = page.index("hero-metrics-box")
    row_open = page.rfind("<div", 0, page.index('class="row', box))

    depth = 0
    cursor = row_open
    while cursor != -1:
        nxt_open = page.find("<div", cursor)
        nxt_close = page.find("</div>", cursor)
        if nxt_close == -1:
            break
        if nxt_open != -1 and nxt_open < nxt_close:
            depth += 1
            cursor = nxt_open + 4
        else:
            depth -= 1
            cursor = nxt_close + 6
            if depth == 0:
                break

    return re.findall(r'<div class="(col-[^"]*)"', page[row_open:cursor])


class TestTheAlbumStatsGrid:
    def test_it_is_one_row_of_four(self, page):
        """⭐ A single row of four `col-3` cells, not `col-6 col-md-3`.

        The 2x2 cost twice the height of the artist page's box, and it is what
        produced the orphaned divider (see the module docstring).
        """
        cells = _metric_cells(page)
        assert len(cells) == 4, f"expected 4 metric cells, found {len(cells)}: {cells}"

        for index, classes in enumerate(cells):
            assert "col-6" not in classes, (
                f"metric cell {index + 1} is `{classes}` — a col-6 cell makes the "
                "grid two rows on a phone again"
            )
            assert "col-3" in classes, f"metric cell {index + 1} is not a col-3: {classes}"

    def test_the_orphaned_divider_is_impossible_by_construction(self, page):
        """⭐ The real assertion: dividers BETWEEN cells, never trailing.

        A `border-end` on the LAST cell draws a rule at the row's right edge with
        nothing after it — the reported orphan. Asserted structurally rather than
        by counting classes, so it holds however the cells are ordered.
        """
        cells = _metric_cells(page)
        assert len(cells) == 4, f"expected 4 metric cells, found {len(cells)}: {cells}"

        for index, classes in enumerate(cells):
            has_divider = "border-end" in classes
            is_last = index == len(cells) - 1
            assert has_divider != is_last, (
                f"cell {index + 1} ({classes!r}) has "
                f"{'a' if has_divider else 'no'} divider; the others must have one "
                "and the last must have none"
            )

    def test_the_order_is_facts_then_verdict(self, page):
        """YEAR | TRACKS | DURATION | RATING — the artist page's shape too, with
        the counts before the rating."""
        grid = page[page.index("hero-metrics-box"):]
        order = [grid.index(label) for label in (">Year<", ">Tracks<",
                                                ">Duration<", ">Rating<")]
        assert order == sorted(order), (
            "the metric order is not YEAR | TRACKS | DURATION | RATING"
        )

    def test_every_metric_still_renders(self, rendered):
        """CONTROL: the restructure must not drop a value."""
        for label in ("YEAR", "TRACKS", "DURATION", "RATING"):
            assert label in rendered.upper(), f"{label} is missing"
        assert "2006" in rendered, "the year value is not rendered"


# ---------------------------------------------------------------------------
# 3. The hero
# ---------------------------------------------------------------------------

def _css_rule(css: str, selector: str) -> str:
    """The declarations of *selector*'s first rule.

    ⚠️ Matched with a regex anchored on a line-start ``{``, not ``str.index``.
    The selector's FIRST appearance in this file is inside the comment that
    explains the change, so ``css.index(selector)`` returns a slice of PROSE and
    the assertion that follows passes or fails for reasons that have nothing to
    do with the rule — the same trap as asserting on a comment elsewhere.
    """
    match = re.search(rf"^\s*{re.escape(selector)}\s*\{{", css, re.M)
    assert match, f"no rule for `{selector}`"
    body = css[match.end():]
    return body[: body.index("}")]


class TestTheAlbumHeroPolish:
    def test_the_title_can_wrap_to_two_lines(self, page):
        """`text-truncate` alone is `white-space: nowrap` — one line, always."""
        assert "album-hero-title" in page
        assert "text-truncate" in page, (
            "the class is still wanted for the ellipsis; the CSS overrides only "
            "the nowrap half"
        )

    @pytest.mark.parametrize("css_path", POPULARR_CSS, ids=lambda p: p.parent.parent.name)
    def test_the_clamp_is_declared_and_can_actually_wrap(self, css_path):
        """⚠️ `white-space: normal` is the line that makes the clamp do anything.

        `text-truncate` sets `nowrap`, so a clamp without this has nothing to
        wrap and every title stays a single truncated line — the declaration
        would look present and do nothing at all.
        """
        rule = _css_rule(_read(css_path), ".album-hero-title")
        assert "white-space: normal" in rule, (
            "without this the nowrap from text-truncate wins and the clamp never applies"
        )
        assert "-webkit-line-clamp: 2" in rule

    @pytest.mark.parametrize("css_path", POPULARR_CSS, ids=lambda p: p.parent.parent.name)
    def test_the_art_overlay_is_a_corner_badge_on_phones(self, css_path):
        """It was centred on the art, covering the thumbnail it edits."""
        rule = _css_rule(_read(css_path), ".album-art-change-btn")
        assert "right:" in rule, "the badge is not anchored to a corner"

    def test_the_overlay_undoes_the_centring_it_inherits(self, page):
        """⚠️ `start-50` + `translate-middle-x` are Bootstrap utilities.

        So the override has to undo BOTH — a rule that only moved `bottom` would
        leave the badge horizontally centred over the art.
        """
        assert "start-50" in page, (
            "the control markup changed; this test's premise needs re-checking"
        )
        rule = _css_rule("".join(_read(p) for p in POPULARR_CSS), ".album-art-change-btn")
        assert "left: auto" in rule, "`start-50` is not undone"
        assert "transform: none" in rule, "`translate-middle-x` is not undone"
