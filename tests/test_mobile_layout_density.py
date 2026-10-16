"""Mobile density: the tracklist and the album list come first on a phone.

Reported (mobile review of the Album and Artist pages)
-----------------------------------------------------
* The Album page's Full / Restart / Run / Force / Actions block claimed the
  first screen, pushing the songs below the fold.
* The tracklist table showed too much metadata per row (Duration + Rating
  columns and a monospace file path), and its Play / Edit / Rename / Delete
  buttons were ~24px tall — far below the 44px touch minimum.
* The Artist page's hero biography rendered at full length ("a wall of text
  just to see the albums") because the rebuilt tree's ``artist.css`` never
  carried the ``bio-clamp`` rules the live tree has.
* The category chips wrapped into three lines above the albums, the green
  "In Library" badge stated the obvious on every row, and three tightly
  clustered action buttons invited mis-taps.

The change is CSS-first and phone-only (``@media (max-width: 767.98px)``):
markup moves values inline, the toolbar collapses behind one button, and the
release actions collapse into a single ``⋮`` menu whose items FORWARD their
click to the already-bound inline buttons — so the shared release module
stays untouched and both trees keep behaving identically.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

TS = REPO_ROOT / "test_site"
LIVE = REPO_ROOT

ALBUM_PAGE = TS / "templates" / "Pages" / "album_detail.html"
ALBUM_JS = TS / "static" / "js" / "pages" / "album.js"
ALBUM_CSS = TS / "static" / "css" / "popularr.css"
ARTIST_PAGE = TS / "templates" / "Pages" / "artist_detail_v2.html"
RELEASE_COMPONENT = TS / "templates" / "components" / "_release_section.html"

ARTIST_CSS = TS / "static" / "css" / "artist.css"
#: Both trees are SERVED — the live one is the default — so a fade applied to
#: only one of them is missing for whoever is on the other.
ARTIST_CSS_COPIES = [ARTIST_CSS, LIVE / "static" / "css" / "artist.css"]
EXTRAS_JS = TS / "static" / "js" / "pages" / "artist-detail-extras.js"
#: The SHARED release module. It is served by BOTH trees and pinned
#: byte-identical by test_shared_module_copies_do_not_drift, so this change
#: must NOT touch it — the phone ⋮ menu is wired from a test_site-only file
#: instead (artist-detail-extras.js).
SHARED_RELEASE_JS_COPIES = [
    TS / "static" / "js" / "pages" / "artist-releases.js",
    LIVE / "static" / "js" / "artist-releases.js",
]

#: The phone breakpoint every rule in this change shares.
PHONE_MQ = "@media (max-width: 767.98px)"


def _read(path: Path) -> str:
    assert path.is_file(), f"{path.relative_to(REPO_ROOT)} is missing"
    return path.read_text(encoding="utf-8")


def _phone_block(css: str, marker: str) -> str:
    """The phone block CONTAINING ``marker``.

    Both stylesheets declare several ``767.98px`` queries, so the first one
    is not necessarily this change's — assertions must be scoped to the block
    that actually holds the rule under test.
    """
    offset = 0
    while True:
        start = css.find(PHONE_MQ, offset)
        if start == -1:
            raise AssertionError(f"no {PHONE_MQ} block containing {marker!r}")
        depth = 0
        end = None
        for index in range(css.index("{", start), len(css)):
            if css[index] == "{":
                depth += 1
            elif css[index] == "}":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        assert end is not None, "unbalanced media query"
        block = css[start:end]
        if marker in block:
            return block
        offset = end


# ===========================================================================
# ===========================================================================
# 1. Album: the scan controls are the SHARED SHEET, not an inline toolbar
# ===========================================================================


class TestTheAlbumScanControls:
    """⚠️ THIS REPLACES the "scan controls collapse behind one button" design.

    The album page used to render a `scan-toolbar` form (scan-type <select> +
    Run + Force + an Actions dropdown), wrapped on phones in a collapsible
    "Scan Options" panel. The artist page rendered the same form RAW — the
    inconsistency between the two pages, plus the wrapping and the squeezed
    helper text, is what the review flagged.

    Both pages now open the SHARED SCAN SHEET instead: seven types with
    descriptions, Force and Restart with explanations, the mode key, and a
    header that costs one row. The collapsible wrapper is therefore gone, along
    with the button and handler it needed.

    ⚠️ `popularr.css` KEEPS the `.scan-toolbar` / `.scan-toolbar-controls`
    rules for the same reason `archive()` keeps a `deprecated=` note: the
    LIVE tree's `album_detail.html` is the only file that still needs them
    while the two trees coexist, and deleting a rule a served template uses
    would collapse that page's layout. What must be true for the REBUILT page
    is asserted below.
    """

    def test_the_inline_toolbar_is_gone(self):
        html = _read(ALBUM_PAGE)

        assert "albumScanForm" not in html, (
            "the inline scan form is back — it is a second scan UI for a "
            "question the shared sheet answers"
        )
        assert "albumForceScan" not in html, "the old Force checkbox is back"
        assert "scan-toolbar-toggle" not in html, (
            "the collapsible wrapper is back; the sheet replaced it"
        )

    def test_the_hero_offers_play_scan_and_the_overflow(self):
        html = _read(ALBUM_PAGE)

        assert "playAlbum()" in html, "no Play control"
        assert "openAlbumScanSheet()" in html, "no Scan control"
        assert 'aria-label="More album actions"' in html, "no ⋮ menu"

    def test_the_actions_menu_kept_its_entries(self):
        """CONTROL: trimming the toolbar must not trim the menu."""
        html = _read(ALBUM_PAGE)
        for label in ("Lookup on MusicBrainz", "Download Missing Tracks",
                      "Rename Files", "Auto-Link MBIDs", "Change Album Art",
                      "Download Popularity Report"):
            assert label in html, f"{label} was lost from the ⋮ menu"

    def test_the_page_includes_the_sheet_with_the_album_scope(self):
        """⚠️ Asserts the INCLUDE, not the sheet's contents.

        This test reads the page's SOURCE, and `id="scanSheet"` lives in the
        included component — so asserting it here would fail however correct the
        page is. (A test that RENDERS the page can assert the id; see
        `tests/test_artist_layout_completion.py`.) What must be true in the
        source is that the include is present and carries the right scope.
        """
        html = _read(ALBUM_PAGE)

        assert "components/_scan_modal.html" in html, (
            "the Scan button opens a sheet the page never included"
        )
        assert "scan_scope_kind = 'album'" in html, (
            "the sheet is included without the album scope, so Run would scan "
            "the whole library"
        )
        assert "js/ui/scan-modal.js" in html, "the sheet's controller is not loaded"

    def test_the_module_opens_the_sheet_in_the_album_scope(self):
        js = _read(ALBUM_JS)

        assert "function openAlbumScanSheet(" in js
        assert "global.openAlbumScanSheet = openAlbumScanSheet" in js, (
            "inline onclick handlers resolve by GLOBAL name"
        )
        assert "scope: 'album'" in js, (
            "without the scope, Run would scan the whole library"
        )
        # The context comes from the page's own JSON block, not a new global.
        assert "_pageData" in js

    def test_the_scoped_dispatch_belongs_to_the_sheet(self):
        """CONTROL — the page must not post to the *-custom route itself.

        That route reads `request.form`, and the sheet is what builds the form.
        A second poster here would be the duplicate scan UI this removes.
        """
        html = _read(ALBUM_PAGE)
        assert "scan/album-custom" not in html



# ===========================================================================
# 2. Album: the tracklist sheds its columns and grows its touch targets
# ===========================================================================


class TestTheTracklistDropsColumnsOnPhones:
    def test_the_table_is_classed(self):
        html = _read(ALBUM_PAGE)

        assert 'class="table table-hover table-dark align-middle mb-0 tracklist-table"' in html, (
            "the column rules are scoped to .tracklist-table"
        )

    def test_duration_and_rating_are_classed_on_headers_and_cells(self):
        html = _read(ALBUM_PAGE)

        for column in ("col-track-duration", "col-track-rating"):
            occurrences = html.count(column)
            assert occurrences >= 2, (
                f"{column} appears {occurrences}x; both the <th> and the <td> "
                "need it or the header would outlive its column"
            )

    def test_the_values_move_inline_next_to_the_title(self):
        html = _read(ALBUM_PAGE)

        assert 'class="track-inline-duration"' in html, "duration must travel with the title"
        assert 'class="track-inline-rating text-warning"' in html, (
            "the stars must render compactly UNDER the title"
        )

    def test_the_file_path_is_classed_in_both_variants(self):
        """The path div AND its 'No file path recorded' fallback hide together."""
        html = _read(ALBUM_PAGE)

        assert 'text-break track-file-path' in html, "the path div is unclassed"
        assert 'fst-italic track-file-path' in html, (
            "the fallback text would keep showing after the real path hid"
        )
        # The click-to-copy/data consumers must survive the hide.
        assert 'data-track-file-path=' in html

    def test_the_injected_missing_row_sheds_the_same_columns(self):
        """THE ALIGNMENT TRAP: JS-injected rows have their own cell markup,
        and a row showing five cells beside three shifts every column left —
        the exact bug the six-cell row once caused."""
        js = _read(ALBUM_JS)

        build = js[js.index("function buildMissingRow"):]
        build = build[: build.index("\n  }")]

        assert 'col-track-duration' in build, "missing row keeps a Duration cell on phones"
        assert 'col-track-rating' in build, "missing row keeps a Rating cell on phones"
        # Still exactly five cells — only their VISIBILITY changes.
        assert build.count("<td") == 5, (
            "the five-cell contract is pinned by test_album_missing_rows_line_up; "
            "this suite only adds classes to those cells"
        )

    def test_the_inline_duration_rides_along_in_the_missing_row(self):
        js = _read(ALBUM_JS)

        build = js[js.index("function buildMissingRow"):]
        build = build[: build.index("\n  }")]

        assert 'class="track-inline-duration"' in build

    def test_the_phone_block_hides_the_columns_and_the_path(self):
        block = _phone_block(_read(ALBUM_CSS), ".tracklist-table .col-track-duration")

        assert ".tracklist-table .col-track-duration,\n    .tracklist-table .col-track-rating { display: none; }" in block
        assert ".track-file-path { display: none; }" in block
        assert ".track-inline-duration" in block and "display: inline-block" in block
        assert ".track-inline-rating" in block and "display: block" in block

    def test_the_scan_controls_hide_until_opened(self):
        block = _phone_block(_read(ALBUM_CSS), ".scan-toolbar .scan-toolbar-controls")

        assert ".scan-toolbar .scan-toolbar-controls { display: none; width: 100%; }" in block
        assert ".scan-toolbar.is-open .scan-toolbar-controls { display: flex; }" in block

    def test_row_actions_meet_the_touch_minimum(self):
        """44x44 is the same bar test_player_transport_controls_meet_touch_target
        holds the player to — these buttons are tapped just as often."""
        block = _phone_block(_read(ALBUM_CSS), ".tracklist-table .btn-group > .btn")

        assert ".tracklist-table .btn-group > .btn {" in block
        assert "min-width: 44px" in block and "min-height: 44px" in block

    def test_desktop_keeps_everything(self):
        """CONTROL — the hiding rules exist exactly ONCE, and NOT inside the
        phone query this change added, so a desktop keeps the toolbar, both
        columns and the path exactly as before."""
        css = _read(ALBUM_CSS)

        assert css.count(".tracklist-table .col-track-duration") == 1, (
            "the column rule must live only inside the phone media query"
        )
        assert css.count(".track-file-path { display: none; }") == 1
        assert css.count(".scan-toolbar .scan-toolbar-controls { display: none;") == 1

        # The inline duplicates are HIDDEN by base CSS until the query shows
        # them — otherwise desktop would render duration twice. Assert the
        # declaration sits OUTSIDE this change's own block, not merely
        # "somewhere before the first 767.98px query in the file" (there are
        # older ones).
        base_rule = ".track-inline-duration,\n.track-inline-rating { display: none; }"
        base_inline = css.index(base_rule)
        block = _phone_block(css, ".tracklist-table .col-track-duration")
        block_start = css.index(block)
        assert not (block_start <= base_inline <= block_start + len(block)), (
            "the base (desktop-visible) rule landed inside the phone query"
        )
        assert css.count(base_rule) == 1


# ===========================================================================
# 3. Artist: the biography is clamped — in BOTH css copies
# ===========================================================================


class TestTheBiographyIsClamped:
    def test_the_hero_uses_the_four_line_clamp(self):
        html = _read(ARTIST_PAGE)

        assert "bio-clamp-4" in html, (
            "the hero bio must be clamped — it rendered at full length because "
            "the rebuilt tree never defined the class it used (bio-clamp-2)"
        )
        assert "[more]" in html, "the expand affordance must stay"

    def test_every_clamp_class_is_defined(self):
        """The rebuilt tree's own stylesheet must define what its template
        emits — this is the half that was missing."""
        css = _read(ARTIST_CSS)

        for name in ("bio-clamp", "bio-clamp-2", "bio-clamp-4"):
            # ``re`` so a selector LIST (``.bio-clamp,\n.bio-clamp > p {``)
            # counts as a definition of each name in it.
            assert re.search(rf"\.{name}[,\s{{]", css), (
                f"{ARTIST_CSS.relative_to(REPO_ROOT)} does not define .{name}"
            )

    def test_the_about_tab_release_works(self):
        """The About tab's Read More toggles `.expanded` — a release rule
        without it makes the button decorative."""
        css = _read(ARTIST_CSS)

        assert ".bio-clamp.expanded" in css
        assert "-webkit-line-clamp: unset" in css

    def test_the_child_paragraph_is_clamped_too(self):
        """-webkit-line-clamp does not apply THROUGH a block <p> child, so
        clamping only the wrapper div was a no-op in the About tab."""
        css = _read(ARTIST_CSS)

        assert ".bio-clamp > p" in css, (
            f"{ARTIST_CSS.relative_to(REPO_ROOT)} clamps only the wrapper"
        )


# ===========================================================================
# 4. Artist: release rows shed chrome on phones
# ===========================================================================


class TestTheReleaseRowsShedChromeOnPhones:
    """The row is [primary][expand][⋮] at EVERY width, sized for a thumb on a phone.

    ⚠️ THIS SUPERSEDES the phone-only ⋮. The row used to render its secondary
    actions inline and hide them behind a `d-md-none` dropdown on phones, with
    the inline copies marked `.release-inline-only`. The documented design is
    now one primary action plus a ⋮ menu on every screen, so there is nothing
    to hide and no inline variant to mark.

    What still matters, and is asserted here:
      * the ⋮ carries the actions the shared module binds, by CLASS;
      * each of those classes appears exactly ONCE, so `querySelector` finds
        the menu item and no forwarding layer is needed;
      * the row's controls are 44px targets on a phone, which is NOT automatic —
        popularr.css exempts `btn-sm` from its app-wide minimum.
    """

    def test_the_phone_block_covers_the_chips_badge_and_targets(self):
        block = _phone_block(_read(ARTIST_CSS), ".release-badge-library")

        # 1. One swipeable chip row (the old rule only started below 576px).
        assert ".release-jump-bar" in block and "overflow-x: auto" in block
        assert "scroll-snap-type" in block
        # 2. The green badge hides; only the library variant is targeted.
        assert ".release-badge-library { display: none; }" in block
        # 3. ⭐ The row's controls grow to the touch minimum. This is the rule the
        #    `btn-sm` exemption in popularr.css makes NECESSARY: without it the
        #    three controls sit at Bootstrap's ~24px.
        assert ".release-actions > .btn" in block, (
            "the primary action is not sized for a thumb"
        )
        assert ".release-actions-overflow > .btn" in block
        assert block.count("min-width: 44px") >= 1 and "min-height: 44px" in block

    def test_the_component_marks_the_library_badge(self):
        html = _read(RELEASE_COMPONENT)

        assert 'class="badge bg-success release-status-badge release-badge-library"' in html, (
            "the success badge must carry the class the phone rule targets"
        )
        # The warning badge must NOT carry it, or "Missing" would hide too.
        assert 'class="badge bg-warning text-dark release-status-badge"' in html, (
            "the Missing badge must stay unclassed — hiding it would remove "
            "the one badge that carries information"
        )

    def test_the_menu_is_not_phone_only(self):
        """⭐ The ⋮ is the design, not a phone fallback.

        `d-md-none` would hide it on desktop and leave the secondary actions
        unreachable there — they no longer have inline copies.
        """
        html = _read(RELEASE_COMPONENT)

        assert "release-actions-overflow" in html
        assert 'class="dropdown release-actions-overflow"' in html, (
            "the ⋮ must be rendered at every width"
        )
        assert "d-md-none" not in html, (
            "a breakpoint class on the menu would strand the secondary actions "
            "at the widths it hides"
        )

    def test_the_inline_variant_is_gone(self):
        """CONTROL: the old mechanism must not linger, or it would double-render.

        `.release-inline-only` was how the inline copies were hidden on phones.
        With the actions now living ONLY in the menu, an inline copy would show
        twice and the forwarder would recurse.
        """
        html = _read(RELEASE_COMPONENT)
        assert "release-inline-only" not in html

    @pytest.mark.parametrize("action,label", [
        ("release-edit-btn", "Edit release"),
        ("release-scan-btn", "Scan release"),
        ("release-mb-search-btn", "Search for a release"),
    ])
    def test_the_menu_actions_are_present_and_unique(self, action, label):
        """ONE element per class — that is what removes the forwarding layer.

        The old markup had two of each (inline + menu item) and a handler that
        forwarded a menu click to the inline twin. `querySelector` returns the
        FIRST match, so with one element the menu item IS the bound node.
        """
        html = _read(RELEASE_COMPONENT)
        dropdown = html[html.index("release-actions-overflow"):]

        assert action in dropdown, f"{action} missing from the ⋮ menu"
        assert label in dropdown, f"the menu item for {action} has no label"
        assert html.count(action) == 1, (
            f"{action} appears {html.count(action)} times; a second copy would "
            "leave the first bound and the second dead"
        )

    @pytest.mark.parametrize("primary", ["release-open-btn", "release-import-btn"])
    def test_the_primary_action_is_outside_the_menu(self, primary):
        """⭐ The one button the brief asks for, and it must be the VISIBLE one.

        Asserted by POSITION: it has to appear BEFORE the ⋮ menu in the row, or
        "a button to open" would be buried in the overflow.
        """
        html = _read(RELEASE_COMPONENT)
        row = html[html.index('class="release-actions"'):]
        row = row[: row.index("release-tracklist")]

        assert primary in row
        assert row.index(primary) < row.index("release-actions-overflow"), (
            f"{primary} is rendered after the ⋮ menu — it is the primary action "
            "and must sit beside it, not inside it"
        )
        assert html.count(primary) == 1

    def test_the_dropdown_button_is_a_target(self):
        html = _read(RELEASE_COMPONENT)

        assert "aria-label=\"More release actions\"" in html
        assert 'data-bs-toggle="dropdown"' in html


# ===========================================================================
# ===========================================================================
# 5. The ⋮ menu needs NO forwarding layer
# ===========================================================================


class TestTheOverflowMenuNeedsNoForwarding:
    """The row renders each action ONCE, so the shared module binds it directly.

    ⚠️ THIS REPLACES the old "forward a ⋮ click to an inline twin" design.

    That indirection existed for two reasons, and BOTH are gone:

      * the ⋮ was a PHONE-ONLY addition, so desktop kept inline buttons that
        the shared module had already bound — it now renders at every width;
      * there were TWO elements per action (inline + menu item), and
        `querySelector` returns the FIRST, so the menu item could not be bound
        without stealing the inline copy's listener — there is now one each.

    The forwarder was left in place as a no-op for a while (its
    `.release-inline-only` lookup could never match, so it returned early every
    time), which is worse than absent: a handler that silently does nothing reads
    as working wiring. These tests pin its REMOVAL, because re-introducing it
    alongside the new markup would double-fire every menu action.
    """

    def test_the_extras_module_has_no_forwarder(self):
        js = _read(EXTRAS_JS)

        assert ".release-actions-overflow .dropdown-item" not in js, (
            "the forwarder is back; with one element per action it would "
            "double-fire (the menu item is already bound)"
        )
        assert "twin.click()" not in js
        assert "'.release-inline-only.' + inlineClass" not in js

    def test_the_shared_module_binds_the_menu_items(self):
        """The binding lives where the buttons are, in BOTH trees."""
        for path in SHARED_RELEASE_JS_COPIES:
            body = _read(path)
            assert "release-edit-btn" in body, path.name
            assert "release-scan-btn" in body, (
                f"{path.name}: the Scan action is not bound"
            )
            assert "release-mb-search-btn" in body, path.name

    def test_the_shared_module_feature_detects_the_scan_sheet(self):
        """⚠️ `ScanModal` is not loaded by every page that renders these rows.

        `pages/artist_detail_v2.html` includes the sheet in both trees, but the
        legacy `pages/artist_detail.html` imports the release macro and does not.
        A bare call would throw there and take the whole row's wiring with it —
        so the module checks, and REMOVES the item when the sheet is absent
        rather than leaving a menu entry that does nothing.
        """
        for path in SHARED_RELEASE_JS_COPIES:
            body = _read(path)
            assert "global.ScanModal" in body, f"{path.name} calls the sheet unguarded"
            assert "scanItem || scanBtn).remove()" in body, (
                f"{path.name}: an unopenable Scan item is left in the menu"
            )

    def test_the_shared_copies_stay_identical(self):
        """CONTROL — the change above had to be applied to BOTH copies.

        ⚠️ Compared from the first `(function`, i.e. the module BODY. The two
        files carry different header comments naming their own path, so a whole
        -file compare fails on that alone — which is exactly why the canonical
        guard (`test_artist_page_contract.py::test_shared_module_copies_do_not_drift`)
        strips the header before comparing. This mirrors it rather than
        duplicating its helper, and it is the header-INSENSITIVE half that
        matters here: both trees must run the same code.
        """
        bodies = []
        for path in SHARED_RELEASE_JS_COPIES:
            body = _read(path)
            bodies.append(body[body.index("(function"):])

        assert bodies[0] == bodies[1], (
            "the two copies of artist-releases.js have drifted; both trees serve "
            "this module, so a difference appears in only one mode"
        )


# ===========================================================================
# The clipped chip row says so
# ===========================================================================

def _rule(block: str, selector: str) -> str:
    """The declarations of *selector*'s rule inside a media block."""
    match = re.search(rf"^\s*{re.escape(selector)}\s*\{{", block, re.M)
    assert match, f"no rule for `{selector}` in the block"
    body = block[match.end():]
    return body[: body.index("}")]


def _declares(rule: str, prop: str) -> bool:
    """Is CSS property *prop* declared in *rule*?

    ⚠️ A plain `prop in rule` answers a different question. ``-webkit-mask-image``
    CONTAINS ``mask-image``, so ``"mask-image" in rule`` is true when only the
    prefixed property is present, and ``"-webkit-mask-image" in rule`` is true
    when only the ``.at-end`` SIBLING declares ``-webkit-mask-image: none``. Both
    directions let a deleted declaration escape, so this splits on the colon and
    compares the property name exactly.
    """
    return any(
        line.split(":", 1)[0].strip() == prop
        for line in rule.splitlines()
        if ":" in line and not line.strip().startswith(("/*", "*", "//"))
    )


def _value(rule: str, prop: str) -> str:
    """The declared value of *prop* in *rule*."""
    for line in rule.splitlines():
        if ":" in line and line.split(":", 1)[0].strip() == prop:
            return line.split(":", 1)[1].strip().rstrip(";").strip()
    raise AssertionError(f"`{prop}` is not declared in:\n{rule}")


class TestTheJumpBarAdmitsItClips:
    """The chip row scrolls, so its last chip is cut at the card edge.

    Reported as "pills get slightly clipped on the right edge" — with nothing to
    say the row continued, a clipped chip reads as a broken one.

    The affordance is a fade at the right edge that comes OFF once the reader has
    scrolled to the end (there the clipped chip is the one under their thumb, and
    fading it would hide what they scrolled to reach). Two halves must agree: the
    stylesheet declares the fade, and the script — which is the only thing that
    can know the scroll POSITION — marks the end. A fade with no script is dead
    CSS; a script with no rule is a no-op.
    """

    def test_the_phone_stylesheet_fades_the_row(self):
        for css_path in ARTIST_CSS_COPIES:
            tree = css_path.parent.parent.name
            rule = _rule(_phone_block(_read(css_path), "mask-image"), ".release-jump-bar")

            assert _declares(rule, "mask-image"), (
                f"{tree}: the row is not masked, so nothing fades"
            )
            assert "linear-gradient" in _value(rule, "mask-image")
            assert "transparent" in _value(rule, "mask-image"), (
                f"{tree}: the fade does not reach transparent, so it ends on a "
                "hard edge"
            )
            assert _declares(rule, "--fade"), (
                f"{tree}: the fade has no width token, so its size is not adjustable"
            )
            assert _declares(rule, "-webkit-mask-image"), (
                f"{tree}: Safari (and every iOS browser) needs the -webkit- "
                "prefix; without it the fade exists only on Android and desktop"
            )

    def test_the_fade_is_a_mask_on_the_row_not_a_scrolling_child(self):
        """⚠️ The bar IS the scroll container.

        An absolutely positioned overlay child scrolls WITH the chips, so it
        covers the first chip (and never the clipped one) as soon as the reader
        moves — the exact opposite of the intent. A mask applies to the
        element's own box, so it stays pinned to the visible right edge.
        """
        for css_path in ARTIST_CSS_COPIES:
            block = _phone_block(_read(css_path), "mask-image")

            assert "position: absolute" not in block, (
                f"{css_path.parent.parent.name}: an overlay would scroll with "
                "the chips; use the mask"
            )

    def test_arriving_at_the_end_removes_the_fade(self):
        for css_path in ARTIST_CSS_COPIES:
            tree = css_path.parent.parent.name
            block = _phone_block(_read(css_path), "mask-image")

            assert ".release-jump-bar.at-end" in block, (
                f"{tree}: nothing lifts the fade at the end of the row"
            )
            rule = _rule(block, ".release-jump-bar.at-end")
            assert _value(rule, "mask-image") == "none", tree
            assert _value(rule, "-webkit-mask-image") == "none", (
                f"{tree}: the prefixed mask is still applied on iOS, so the "
                "fade stays over the last chip there"
            )

    def test_the_script_marks_the_end(self):
        """The other half of the contract: `.at-end` must be reachable."""
        for path in SHARED_RELEASE_JS_COPIES:
            body = _read(path)
            assert "at-end" in body, (
                f"{path.name}: the stylesheet's `.at-end` rule is dead — no "
                "script ever sets it"
            )
            assert "classList.toggle('at-end'" in body, path.name

    def _fade_source(self, path) -> str:
        body = _read(path)
        fade = body[body.index("function wireJumpBarFade"):]
        return fade[: fade.index("\n  }")]

    def test_the_end_is_measured_with_slack(self):
        """⚠️ Exact equality never fires on a scaled display.

        `scrollLeft` and `clientWidth` are integers after rounding, so on a
        fractional device-pixel ratio the sum lands a fraction short of
        `scrollWidth` at the true end and the fade stays on over a fully
        scrolled row.
        """
        # ⚠️ Asserted as the whole COMPARISON. `"SLACK" in fade` is true even
        # when the end test stops using it — the token also appears in the
        # `scrollable` line and in its own declaration, so that check survived
        # replacing `>=` with `===`.
        expected = re.compile(
            r"atEnd\s*=\s*bar\.scrollLeft\s*\+\s*bar\.clientWidth\s*>=\s*"
            r"bar\.scrollWidth\s*-\s*SLACK\s*;"
        )
        for path in SHARED_RELEASE_JS_COPIES:
            fade = self._fade_source(path)

            assert "var SLACK" in fade, f"{path.name}: no slack constant"
            assert expected.search(fade), (
                f"{path.name}: the end test does not allow for a sub-pixel "
                "scroll position, so the fade stays on at the true end"
            )

    def test_a_row_that_does_not_scroll_gets_no_fade(self):
        """A wide screen shows every chip, so there is nothing to advertise.

        The rule is phone-only, but the script runs at every width and the bar
        can also be short enough on a phone — so it has to check, not assume.
        """
        for path in SHARED_RELEASE_JS_COPIES:
            fade = self._fade_source(path)

            assert "scrollWidth > bar.clientWidth" in fade, (
                f"{path.name}: a non-scrolling row is marked `at-end` by "
                "default; assert the check"
            )

    def test_the_marker_is_recomputed_on_scroll_and_on_resize(self):
        """Scroll is the obvious one; resize is not optional.

        A rotation or a window drag changes how much of the row fits, so a
        one-shot measurement on load leaves the fade wrong afterwards.
        """
        for path in SHARED_RELEASE_JS_COPIES:
            fade = self._fade_source(path)

            assert "'scroll'" in fade, (
                f"{path.name}: the fade is never updated as the row scrolls"
            )
            assert "resize" in fade, f"{path.name}: the fade is stale after a rotation"

    def test_it_is_wired_into_init(self):
        """CONTROL: an unwired function is the failure mode this whole class guards."""
        for path in SHARED_RELEASE_JS_COPIES:
            body = _read(path)
            # `.index` raises if the function is gone, which is the first half of
            # the control; the call site is the half that is easy to forget.
            assert body.index("function wireJumpBarFade") < body.index("function init"), (
                f"{path.name}: wireJumpBarFade is defined after init()"
            )
            assert "wireJumpBarFade();" in body[body.index("function init"):], (
                f"{path.name}: init() never calls it, so the fade never updates"
            )

