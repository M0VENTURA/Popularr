"""Choose which EDITION of a release group the album is linked to.

REPORTED

> Next to release on identifiers and linking, can we add a search that will
> bring up the different releases from the release group to select the best
> one. It could back in to the existing release modal that's used elsewhere.

The album page's **Identifiers & Linking** block holds a concrete
*MusicBrainz Release ID* and a *Release Group ID* as two free-text inputs, and
the only way to bind a different pressing of the same album was to paste an id
by hand. The releases in between — the 12-track CD, the 18-track deluxe, the
promo — were reachable from nowhere on that page.

WHAT WAS BUILT (backing into the existing modal)
------------------------------------------------

* ``GET /api/musicbrainz/release-picker`` gained **``mode=select``**. The same
  slideover the queue already uses, the same ``browse_releases_for_group``
  payload, the same edition-grouping — plus a *Use this release* button on each
  card. Queue mode is untouched: it auto-selects (one release, or every edition
  sharing a track count) and is driven by the caller, so it needs no button.
* ``openAlbumReleaseSelector()`` on the album page (both trees) opens that
  slideover, preferring ``#album_release_group_mbid`` and falling back to
  ``#album_mbid`` — the endpoint resolves a concrete release to its own group
  when the browse of a non-group id comes back empty.
* The card buttons are server-rendered HTML, so they are handled by a
  **delegated** document listener (the pattern the rest of the app uses for
  rows a module does not build itself) that runs ``applyAlbumMbid(id)`` —
  the same function a Lookup-MBID pick uses, so nothing is written until the
  form's own **Save Metadata**.

Nothing is persisted by choosing: the id lands in the input and the user saves,
so a wrong pick is undone by reloading — the same contract as every other
lookup on that page.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

ROUTE = REPO_ROOT / "routes" / "musicbrainz_routes.py"
ALBUM_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "album.js"
LIVE_ALBUM_JS = REPO_ROOT / "static" / "js" / "album_detail.js"
ALBUM_HTML = REPO_ROOT / "templates" / "pages" / "album_detail.html"      # live tree
TEST_SITE_ALBUM_HTML = REPO_ROOT / "test_site" / "templates" / "Pages" / "album_detail.html"


def _route_source() -> str:
    return ROUTE.read_text(encoding="utf-8")


def _inline_picker_template() -> str:
    """The Jinja string the release-picker route renders for the slideover."""
    source = _route_source()
    start = source.index("return await _render(")
    open_at = source.index('"""', start) + 3
    close_at = source.index('"""', open_at)
    return source[open_at:close_at]


def _release(**overrides) -> dict:
    row = {
        "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "title": "Fingerprints",
        "status": "Official",
        "disambiguation": "",
        "country": "AU",
        "date": "1999-11-16",
        "track_count": 13,
        "format": "CD",
        "artist": "Powderfinger",
        "edition_count": 1,
    }
    row.update(overrides)
    return row


def _render(select_mode: bool, releases: list[dict] | None = None) -> str:
    from jinja2 import Environment

    template = Environment().from_string(_inline_picker_template())
    return template.render(
        releases=releases if releases is not None else [_release()],
        album="Fingerprints",
        artist="Powderfinger",
        select_mode=select_mode,
    )


# ===========================================================================
# 1. The shared slideover gains a SELECT mode
# ===========================================================================


class TestThePickerHasASelectMode:
    def test_the_mode_query_parameter_is_read(self):
        assert 'request.args.get("mode")' in _route_source(), (
            "the route must accept mode=select"
        )
        assert 'select_mode = (request.args.get("mode")' in _route_source()

    def test_the_select_button_is_rendered_only_in_select_mode(self):
        html = _render(select_mode=True)
        assert "js-pick-release" in html, "no way to choose a release"
        assert 'data-release-id="' in html
        assert "Use this release" in html

    def test_queue_mode_is_untouched(self):
        """CONTROL — the queue flow must not grow a button it never asked for."""
        html = _render(select_mode=False)
        assert "js-pick-release" not in html
        assert "Use this release" not in html

    def test_the_header_says_which_action_it_is(self):
        assert "Select the version this album should be linked to" in _render(True)
        assert "Select the exact version you want to queue" in _render(False)

    def test_the_edition_grouping_still_applies_in_select_mode(self):
        """CONTROL — folding identical pressings is what makes the list usable."""
        rows = [_release(id="r1"), _release(id="r2", track_count=0)]
        html = _render(True, rows)
        assert html.count("js-pick-release") == len(rows)

    def test_the_select_mode_flag_is_passed_to_the_renderer(self):
        assert "select_mode=select_mode" in _route_source()


# ===========================================================================
# 2. The album page opens it, and the choice reaches the form
# ===========================================================================


@pytest.mark.parametrize("path", [ALBUM_JS, LIVE_ALBUM_JS], ids=["test_site", "live"])
class TestTheAlbumPageOpensTheSelector:
    def test_the_opener_exists(self, path):
        source = path.read_text(encoding="utf-8")
        assert "openAlbumReleaseSelector" in source, f"{path.name} defines no opener"

    def test_the_opener_is_exported_for_the_template(self, path):
        """The button's ``onclick`` is a bare global name."""
        source = path.read_text(encoding="utf-8")
        assert re.search(
            r"(global|window)\.openAlbumReleaseSelector\s*=", source
        ), f"{path.name} never publishes the opener"

    def test_the_opener_requests_select_mode(self, path):
        source = path.read_text(encoding="utf-8")
        assert "mode=select" in source, "the slideover would open in queue mode"

    def test_the_release_group_is_preferred_over_the_concrete_id(self, path):
        source = path.read_text(encoding="utf-8")
        opener = source[
            source.index("openAlbumReleaseSelector")
            : source.index("openAlbumReleaseSelector") + 1400
        ]
        assert opener.index("album_release_group_mbid") < opener.index("album_mbid"), (
            "the GROUP is what lists editions — the concrete id is only a fallback"
        )

    def test_a_row_without_any_id_says_what_to_do_first(self, path):
        source = path.read_text(encoding="utf-8")
        assert "Lookup MBID" in source, (
            "with neither id set the button must explain the next step, "
            "not open an empty list"
        )

    def test_the_click_is_delegated_and_runs_apply_album_mbid(self, path):
        """Server-rendered HTML cannot be bound at load — delegate."""
        source = path.read_text(encoding="utf-8")
        assert ".js-pick-release" in source
        assert "addEventListener('click'" in source or 'addEventListener("click"' in source
        assert re.search(
            r"(window\.)?applyAlbumMbid\(\s*releaseId", source
        ), "the choice must land through applyAlbumMbid, the Lookup-MBID path"

    def test_the_slideover_is_closed_after_a_choice(self, path):
        source = path.read_text(encoding="utf-8")
        assert "detailSlideOver" in source


# ===========================================================================
# 3. The button sits next to the release identifiers, in both trees
# ===========================================================================


@pytest.mark.parametrize("path", [ALBUM_HTML, TEST_SITE_ALBUM_HTML], ids=["live", "test_site"])
class TestTheButtonIsNextToTheReleaseId:
    def test_the_button_is_present(self, path):
        source = path.read_text(encoding="utf-8")
        assert 'onclick="openAlbumReleaseSelector()"' in source

    def test_it_follows_the_release_id_input(self, path):
        source = path.read_text(encoding="utf-8")
        at = source.index('id="album_mbid"')
        window = source[at: at + 700]
        assert "openAlbumReleaseSelector" in window, (
            "the button belongs beside the Release ID, not somewhere else on the page"
        )

    def test_it_does_not_submit_the_form(self, path):
        """CONTROL — picking a release must not save; Save Metadata does."""
        source = path.read_text(encoding="utf-8")
        at = source.index("openAlbumReleaseSelector()")
        button = source[max(0, at - 300): at + 60]
        assert 'type="button"' in button
