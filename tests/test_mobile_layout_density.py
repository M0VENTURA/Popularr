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
# 1. Album: the scan controls collapse behind one button
# ===========================================================================


class TestTheScanControlsCollapseOnPhones:
    def test_the_toggle_button_exists(self):
        html = _read(ALBUM_PAGE)

        assert 'class="btn btn-sm btn-outline-secondary w-100 d-md-none scan-toolbar-toggle"' in html, (
            "the phone-only Scan Options button is missing"
        )
        assert 'onclick="toggleScanToolbar(this)"' in html
        assert 'aria-expanded="false"' in html, "the toggle must start closed"

    def test_the_controls_are_wrapped_in_the_collapsible_div(self):
        html = _read(ALBUM_PAGE)

        wrapper_start = html.index('class="scan-toolbar-controls')
        form_end = html.index("</form>", wrapper_start)
        wrapper = html[wrapper_start:form_end]

        assert wrapper.count("</div><!-- /.scan-toolbar-controls -->") == 1, (
            "the wrapper must be closed explicitly — an unclosed div would "
            "swallow the Play Album CTA into the form"
        )
        # Everything that was in the toolbar must now be INSIDE the wrapper.
        assert "_scan_selector.html" in wrapper, "the scan type select moved out"
        assert 'title="Run Scan"' in wrapper, "the Run button moved out"
        assert "dropdown-menu" in wrapper, "the Actions dropdown moved out"
        assert "</form>" not in wrapper

    def test_the_toggle_is_exported_as_a_global(self):
        """Inline onclick handlers resolve by GLOBAL name."""
        js = _read(ALBUM_JS)

        assert "function toggleScanToolbar(btn)" in js
        assert "global.toggleScanToolbar = toggleScanToolbar" in js, (
            "templates have no compiler — an unexported handler is a dead button"
        )

    def test_the_toggle_reads_the_class_off_the_form(self):
        """The button sits OUTSIDE the controls it reveals, so the class must
        be found through closest('.scan-toolbar')."""
        js = _read(ALBUM_JS)

        body = js[js.index("function toggleScanToolbar"):]
        body = body[: body.index("\n  }")]

        assert "closest('.scan-toolbar')" in body
        assert "classList.toggle('is-open')" in body
        assert "aria-expanded" in body


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
    def test_the_phone_block_covers_the_chips_badge_and_actions(self):
        block = _phone_block(_read(ARTIST_CSS), ".release-badge-library")

        # 1. One swipeable chip row (the old rule only started below 576px).
        assert ".release-jump-bar" in block and "overflow-x: auto" in block
        assert "scroll-snap-type" in block
        # 2. The green badge hides; only the library variant is targeted.
        assert ".release-badge-library { display: none; }" in block
        # 3. Inline buttons hide, the ⋮ grows to the touch minimum.
        assert ".release-inline-only { display: none; }" in block
        assert ".release-actions-overflow > .btn" in block and "min-width: 44px" in block

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

    def test_the_component_has_both_action_variants(self):
        html = _read(RELEASE_COMPONENT)

        assert "release-actions-overflow" in html
        assert 'class="dropdown release-actions-overflow d-md-none"' in html, (
            "the ⋮ menu must be phone-only (d-md-none) so desktop keeps its "
            "inline buttons"
        )
        assert html.count("release-inline-only") >= 3, (
            "every inline action is marked, or one button would show twice"
        )

    @pytest.mark.parametrize("variant,label", [
        ("release-open-btn", "Open album"),
        ("release-edit-btn", "Edit release"),
        ("release-toggle-btn", "Show tracklist"),
    ])
    def test_the_dropdown_carries_the_owned_row_actions(self, variant, label):
        """Same CLASSES and data attributes as the inline copies — that is
        what lets one set of bindings serve both."""
        html = _read(RELEASE_COMPONENT)
        dropdown = html[html.index("release-actions-overflow"):]

        assert variant in dropdown, f"{variant} missing from the ⋮ menu"
        assert label in dropdown

    @pytest.mark.parametrize("variant,label", [
        ("release-import-btn", "Import release"),
        ("release-mb-search-btn", "Search MusicBrainz"),
    ])
    def test_the_dropdown_carries_the_missing_row_actions(self, variant, label):
        html = _read(RELEASE_COMPONENT)
        dropdown = html[html.index("release-actions-overflow"):]

        assert variant in dropdown
        assert label in dropdown

    def test_the_dropdown_button_is_a_target(self):
        html = _read(RELEASE_COMPONENT)

        assert "aria-label=\"More release actions\"" in html
        assert 'data-bs-toggle="dropdown"' in html


# ===========================================================================
# 5. The ⋮ menu is wired from a test_site-only file, by FORWARDING
# ===========================================================================


class TestTheOverflowMenuIsForwarded:
    """The shared release module must stay untouched.

    ``artist-releases.js`` is served by BOTH trees and pinned byte-identical
    by ``test_shared_module_copies_do_not_drift``, so wiring the new phone
    menu from inside it would either drift the trees apart or force a live-tree
    edit — and this change is scoped to ``test_site``.

    Instead ``artist-detail-extras.js`` (test_site-only, already loaded on the
    artist page) FORWARDS a dropdown click to the matching INLINE button, which
    the shared module has already bound. Same classes, same data attributes,
    one set of bindings.
    """

    def test_the_shared_module_is_untouched(self):
        """CONTROL — the reason for the forwarding indirection. Both copies
        must remain identical, and neither may carry the mobile wiring."""
        bodies = []
        for path in SHARED_RELEASE_JS_COPIES:
            assert path.is_file(), f"{path.relative_to(REPO_ROOT)} is missing"
            body = _read(path)
            assert "release-actions-overflow" not in body, (
                f"{path.relative_to(REPO_ROOT)} was changed for the phone menu; "
                "the shared module must stay byte-identical across trees"
            )
            bodies.append(body)

    def test_the_extras_module_forwards_dropdown_clicks(self):
        js = _read(EXTRAS_JS)

        assert ".release-actions-overflow .dropdown-item" in js, (
            "the delegated handler must target the ⋮ menu's items"
        )
        assert "closest('.release-item')" in js, (
            "the twin lives inside the SAME release row"
        )

    def test_it_looks_up_the_inline_twin_by_class(self):
        """Each ⋮ item carries the same class as its inline twin — that is the
        contract that makes the lookup possible."""
        js = _read(EXTRAS_JS)

        for cls in (
            "release-open-btn",
            "release-edit-btn",
            "release-import-btn",
            "release-mb-search-btn",
            "release-toggle-btn",
        ):
            assert cls in js, f"the forwarding list omits {cls}"

        assert "'.release-inline-only.' + inlineClass" in js, (
            "the twin is the INLINE copy — without .release-inline-only the "
            "lookup would match the dropdown item itself and recurse"
        )

    def test_it_clicks_the_twin(self):
        js = _read(EXTRAS_JS)

        assert "twin.click()" in js, (
            "forwarding is the whole point: the shared module's bindings do "
            "the work"
        )
        assert "e.preventDefault()" in js, (
            "the <a> twin would otherwise navigate AND let the dropdown's own "
            "href through"
        )

    def test_the_forwarding_is_explained(self):
        """A reader must not 'simplify' this into a direct call — the handlers
        are module-private, and importing them would touch the shared file."""
        js = _read(EXTRAS_JS)

        assert "artist-releases.js" in js
        assert "byte-identical" in js or "test_shared_module_copies_do_not_drift" in js, (
            "the comment must name the constraint that forces the indirection"
        )

    def test_the_forwarding_survives_a_missing_twin(self):
        """CONTROL — a row without the inline copy must not throw; the handler
        returns quietly."""
        js = _read(EXTRAS_JS)
        handler = js[js.index(".release-actions-overflow .dropdown-item"):]
        handler = handler[: handler.index("});")]

        assert "if (!twin) return;" in handler
