"""Saving album metadata must bring you back to the tab you saved from.

Reported
--------
> Currently saving metadata asks to save twice. It saves the album metadata,
> then switches to the tracklist with another save button. Is this required or
> should one save be sufficient …

One save **is** sufficient — the album fields and the staged per-track review
travel in the same POST (``staged_track_updates`` is a hidden input inside
``albumMetadataForm``), so there is nothing left to save afterwards. What made
it look like two steps:

1. The save POSTs from the **Edit Album** tab, but the handler redirected to
   the album URL with no fragment, and ``tab-tracks`` is
   ``class="nav-link active"`` by default — so the reload always landed on the
   **tracklist**. The user's context had silently changed under them.
2. The tracklist is where the sticky save bar lives (``form[data-sticky-save]``
   → ``#stickySaveBar``, "You have unsaved metadata changes" + its own
   **Save Metadata**), so the very next thing on screen looked like a *second*
   save.

Bootstrap 5 does **not** restore a tab from the URL, so appending the fragment
server-side is only half the fix — both album pages have to select the tab
themselves.

Asserted here
-------------
* the redirect carries the fragment, located from ``return redirect(`` (so a
  comment that merely mentions the fragment cannot satisfy it);
* both trees read ``location.hash === '#tab-details'`` and show the tab —
  that exact expression appears only in code, never in the comments written
  with this change;
* the premise itself: ``tab-tracks`` really is the default active tab, so the
  fragment is load-bearing rather than decorative.
"""

from __future__ import annotations

import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
UI_ROUTES = REPO_ROOT / "routes" / "ui_routes.py"
LIVE_JS = REPO_ROOT / "static" / "js" / "album_detail.js"
REBUILT_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "album.js"
LIVE_TEMPLATE = REPO_ROOT / "templates" / "pages" / "album_detail.html"
REBUILT_TEMPLATE = REPO_ROOT / "test_site" / "templates" / "Pages" / "album_detail.html"

FRAGMENT = '"#tab-details"'
HASH_CHECK = "location.hash === '#tab-details'"


class TestTheSaveReturnsToTheTabItCameFrom:
    def test_the_redirect_carries_the_edit_tab_fragment(self):
        src = UI_ROUTES.read_text(encoding="utf-8")
        # Anchor on the SAVE's own redirect: ui_routes has ~19 of them (login,
        # setup, …), so the first `return redirect(` is the wrong one.
        anchor = src.index("redirect_artist = new_artist or artist_name")
        start = src.index("return redirect(", anchor)
        window = src[start: start + 400]

        assert FRAGMENT in window, (
            "the save POSTs from the Edit Album tab but redirects to the bare "
            "album URL, so the reload lands on the default TRACKS tab and the "
            "next Save affordance the user sees looks like a second save"
        )

    @pytest.mark.parametrize("tree", [LIVE_JS, REBUILT_JS], ids=["live", "test_site"])
    def test_the_page_selects_the_tab_from_the_hash(self, tree: pathlib.Path):
        src = tree.read_text(encoding="utf-8")

        assert HASH_CHECK in src, (
            f"{tree.name} does not read the fragment — Bootstrap 5 will not "
            "restore a tab from the URL on its own, so the server-side "
            "fragment alone leaves the user on the tracklist"
        )
        assert "bootstrap.Tab.getOrCreateInstance" in src, (
            f"{tree.name} reads the hash but never shows the tab"
        )

    @pytest.mark.parametrize("template", [LIVE_TEMPLATE, REBUILT_TEMPLATE], ids=["live", "test_site"])
    def test_the_tracklist_is_the_default_tab(self, template: pathlib.Path):
        """Premise — if the default ever becomes Edit Album, revisit this."""
        html = template.read_text(encoding="utf-8")
        assert 'id="tab-tracks-btn" data-bs-toggle="tab"' in html
        assert 'class="nav-link active fw-bold" id="tab-tracks-btn"' in html, (
            "the tracklist is no longer the default tab — the fragment may no "
            "longer be needed (and this assertion should be updated)"
        )
