"""Artist page layout: the action row and the release-row menus.

Two pieces of the design board's artist screen, both running in BOTH trees:

1. **The hero action row** — `[▶ PLAY TOP TRACKS] [⚡ Scan] [⋮]`. It replaced an
   inline `scan-toolbar` form (scan-type `<select>` + Run + Force + Actions) that
   sat ABOVE the Play button. That toolbar was a second scan UI for a question
   the shared scan sheet already answers, and it cost a full hero row to show one
   dropdown.

2. **The release rows** — `[Open|Download] [⌄] [⋮]`, where the ⋮ holds the
   secondary actions. "Albums in library should have a button to open, with edit
   and scan under the three dots… The download button will show on the missing
   albums, with the three dots giving an option to manually search for a release."

⚠️ THE SCAN SHEET HAD TO BE PORTED TO THE LIVE TREE FOR BOTH. It existed only in
the rebuilt tree, so `Scan release` and the hero's Scan button would have been
dead controls there. `test_sheet_is_available_in_both_trees` pins that.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._test_site_render import REPO_ROOT, build_env

TREES = (
    ("templates", "pages/artist_detail_v2.html"),
    ("test_site/templates", "Pages/artist_detail_v2.html"),
)

RELEASE_COMPONENTS = (
    REPO_ROOT / "templates" / "components" / "_release_section.html",
    REPO_ROOT / "test_site" / "templates" / "components" / "_release_section.html",
)

SCAN_SHEET = (
    REPO_ROOT / "templates" / "components" / "_scan_modal.html",
    REPO_ROOT / "test_site" / "templates" / "components" / "_scan_modal.html",
)

SCAN_MODAL_JS = (
    REPO_ROOT / "static" / "js" / "ui" / "scan-modal.js",
    REPO_ROOT / "test_site" / "static" / "js" / "ui" / "scan-modal.js",
)

LIVE_ARTIST_JS = REPO_ROOT / "static" / "js" / "artist-page.js"
REBUILT_ARTIST_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "artist-detail-extras.js"
TARGETED_PROBE = REPO_ROOT / "tests" / "js" / "scan-sheet-targeted-probe.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _stripped(source: str) -> str:
    """JavaScript with comments removed (these files explain themselves by name)."""
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", without_block)


def _payload() -> dict:
    albums = [
        {"album": "Nevermind", "album_year": 1991, "track_count": 12, "avg_stars": 4.5,
         "is_missing": False, "release_id": ""},
        {"album": "Bleach", "album_year": 1989, "track_count": 0, "is_missing": True,
         "release_id": "rid-1", "cover_art_url": ""},
        {"album": "Unreleased Thing", "album_year": 2027, "track_count": 0,
         "is_missing": True, "is_upcoming": True, "release_id": "rid-2", "cover_art_url": ""},
    ]
    return {
        "artist_name": "Test Artist",
        "artist_bio": "A biography.",
        "artist_country": "US",
        "artist_members": [],
        "artist_aliases": [],
        "artist_alias_canonical": "",
        "similar_artists": {"display": []},
        "stats": {"album_count": 3, "track_count": 12, "five_star_count": 1, "avg_stars": 4.0,
                  "musicbrainz_artist_id": "abc", "discogs_artist_id": "", "lastfm_artist_mbid": ""},
        "top_tracks": [{"id": "t1", "title": "Come As You Are", "album": "Nevermind",
                        "stars": 5, "final_score": 88.0, "popularity_score": 88.0}],
        "genres": ["Grunge"],
        "genre_sources": {},
        "album_categories": [{"key": "studio_albums", "section_id": "studio-albums",
                              "icon": "bi-disc", "label": "Studio Albums"}],
        "albums_by_category": {"studio_albums": albums},
        "appears_on_albums": [],
        "artist_playlists": [],
        "scan_active": False,
        "slskd_config": {"enabled": False},
    }


@pytest.fixture(params=TREES, ids=lambda t: t[0])
def rendered(request):
    root, name = request.param
    from jinja2 import FileSystemLoader

    env = build_env("/artist/Test Artist")
    env.loader = FileSystemLoader(str(REPO_ROOT / root))
    return env.get_template(name).render(**_payload())


# ---------------------------------------------------------------------------
# 1. The hero action row
# ---------------------------------------------------------------------------

class TestTheHeroActionRow:
    def test_the_three_controls_are_present(self, rendered):
        assert "playArtistTopTracks()" in rendered, "no Play control"
        assert "openArtistScanSheet()" in rendered, "no Scan control"
        assert "aria-label=\"More artist actions\"" in rendered, "no ⋮ menu"

    def test_the_inline_scan_toolbar_is_gone(self, rendered):
        """CONTROL: two scan UIs on one page is what the sheet removes.

        ⚠️ Asserted on the rendered output, so a leftover select would be caught
        even if it moved elsewhere in the markup.
        """
        assert "artistScanForm" not in rendered
        assert "artistForceScan" not in rendered
        assert 'name="scan_type"' not in rendered, (
            "a scan-type select is still rendered — the sheet owns that choice now"
        )

    def test_play_is_the_primary_and_comes_first(self, rendered):
        """The design's order: Play, Scan, then the overflow."""
        assert rendered.index("playArtistTopTracks()") < rendered.index("openArtistScanSheet()")
        assert rendered.index("openArtistScanSheet()") < rendered.index("More artist actions")

    def test_the_actions_menu_kept_its_entries(self, rendered):
        """CONTROL: trimming the toolbar must not trim the menu."""
        for label in ("Corrections", "Genre Management", "Edit External IDs",
                      "Download Popularity Report", "Force Metadata Refresh"):
            assert label in rendered, f"{label} was lost from the ⋮ menu"

    def test_the_page_includes_the_sheet_the_button_opens(self, rendered):
        assert 'id="scanSheet"' in rendered, (
            "the Scan button opens a sheet the page never rendered"
        )
        assert 'data-scan-scope="artist"' in rendered, (
            "the sheet is included without the artist scope, so Run would scan "
            "the whole library"
        )


# ---------------------------------------------------------------------------
# 2. The release rows
# ---------------------------------------------------------------------------

class TestTheReleaseRowMenus:
    @pytest.mark.parametrize("component", RELEASE_COMPONENTS, ids=lambda p: p.parent.parent.name)
    def test_the_menu_holds_the_secondary_actions(self, component):
        html = _read(component)

        assert "release-edit-btn" in html and "Edit release" in html
        assert "release-scan-btn" in html and "Scan release" in html
        assert "release-mb-search-btn" in html and "Search for a release" in html

    @pytest.mark.parametrize("component", RELEASE_COMPONENTS, ids=lambda p: p.parent.parent.name)
    def test_each_action_appears_exactly_once(self, component):
        """⚠️ A second copy would leave the first bound and the second dead.

        `artist-releases.js` binds by `querySelector`, which returns the FIRST
        match — so duplicated classes are the exact hazard the previous
        inline-plus-menu design needed a forwarding layer to survive.
        """
        html = _read(component)
        for action in ("release-open-btn", "release-import-btn", "release-edit-btn",
                       "release-scan-btn", "release-mb-search-btn", "release-toggle-btn"):
            assert html.count(action) == 1, (
                f"{action} appears {html.count(action)} times in {component.name}"
            )

    @pytest.mark.parametrize("component", RELEASE_COMPONENTS, ids=lambda p: p.parent.parent.name)
    def test_an_unreleased_album_gets_no_actions(self, component):
        """There is nothing to download, edit or expand for a release that does
        not exist yet, so the row must not offer those actions.

        ⚠️ RENDERED, not counted. This used to assert
        `html.count("is_missing and is_upcoming") >= 2`, which counts GUARD
        STRINGS — a mutation that relaxed one of the three guards to `{% elif
        True %}` left the other two in place and the count unchanged, so the
        assertion passed while an unreleased album gained a Download button.
        Verified by mutation. The row's own output is the only honest check.
        """
        # A page whose category holds ONLY the not-yet-released album.
        payload = _payload()
        upcoming_only = [
            a for a in payload["albums_by_category"]["studio_albums"] if a.get("is_upcoming")
        ]
        assert upcoming_only, "the fixture lost its upcoming album"
        payload["albums_by_category"] = {"studio_albums": upcoming_only}

        root = "test_site/templates" if "test_site" in str(component) else "templates"
        name = "Pages/artist_detail_v2.html" if "test_site" in str(component) else "pages/artist_detail_v2.html"
        from jinja2 import FileSystemLoader

        env = build_env("/artist/Test Artist")
        env.loader = FileSystemLoader(str(REPO_ROOT / root))
        rendered = env.get_template(name).render(**payload)

        assert "Unreleased Thing" in rendered, "the upcoming album did not render at all"
        for action in ("release-import-btn", "release-open-btn", "release-edit-btn",
                       "release-scan-btn", "release-mb-search-btn",
                       "release-actions-overflow", "release-toggle-btn"):
            assert action not in rendered, (
                f"an unreleased album renders {action} — there is nothing to "
                "download, edit or expand for a release that does not exist yet"
            )
        # CONTROL: the badge that explains WHY it has no actions must still show.
        assert "Upcoming" in rendered

    def test_the_menu_dispatch_is_bound_in_the_shared_module(self):
        """Both copies, because both trees serve this module."""
        paths = (REPO_ROOT / "static" / "js" / "artist-releases.js",
                 REPO_ROOT / "test_site" / "static" / "js" / "pages" / "artist-releases.js")
        for path in paths:
            code = _stripped(_read(path))
            assert "release-scan-btn" in code, f"{path.name} does not bind Scan release"
            assert "global.ScanModal" in code, f"{path.name} calls the sheet unguarded"
            assert "scope: 'album'" in code, (
                f"{path.name} opens the sheet without the album scope"
            )


# ---------------------------------------------------------------------------
# 3. The sheet is available in BOTH trees (it is what makes Scan work)
# ---------------------------------------------------------------------------

class TestTheSheetIsAvailableInBothTrees:
    @pytest.mark.parametrize("path", SCAN_SHEET, ids=lambda p: p.parent.parent.parent.name)
    def test_the_sheet_exists(self, path):
        assert path.is_file(), f"{path.relative_to(REPO_ROOT)} is missing"

    @pytest.mark.parametrize("path", SCAN_MODAL_JS, ids=lambda p: p.parent.parent.parent.name)
    def test_the_module_exists_and_parses(self, path):
        assert path.is_file(), f"{path.relative_to(REPO_ROOT)} is missing"
        result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, f"{path.name} does not parse: {result.stderr}"

    def test_the_live_copy_shims_the_helpers_it_cannot_rely_on(self):
        """⚠️ The live tree has no `global.api`, `global.toast` or `buttonState`.

        The port therefore carries local `postJson` / `toast` shims. The shim must
        delegate to `global.api` — a blanket rewrite during the port made it call
        ITSELF, so every POST would have recursed instead of failing.
        """
        code = _stripped(_read(SCAN_MODAL_JS[0]))

        assert "function postJson(" in code
        assert "function toast(" in code
        assert "global.api.postJson(url" in code, (
            "the shim no longer delegates to the shared helper"
        )
        assert "return postJson(url" not in code, (
            "the shim calls itself — infinite recursion on every POST"
        )
        assert "global.api.postJson('/api" not in code, (
            "a call site still goes straight to global.api, which the live tree lacks"
        )

    def test_the_two_copies_agree_structurally(self):
        """They are the same module; only the shim may differ.

        ⚠️ Compared by STRUCTURE, not by text. A text diff after stripping the
        shim's comments was too fragile to be meaningful — it failed on the live
        copy's own header before reaching anything real. What actually matters is
        that both copies define the same functions with the same dispatch, so a
        change applied to one tree cannot leave the other behaving differently.
        """
        def functions(path: Path) -> set[str]:
            return set(re.findall(r"\bfunction\s+(\w+)\s*\(", _stripped(_read(path))))

        live, rebuilt = functions(SCAN_MODAL_JS[0]), functions(SCAN_MODAL_JS[1])
        assert live, "the function scan found nothing — the slicer is wrong"

        # The shim adds `postJson` and `toast` to the live copy only.
        extra = live - rebuilt
        assert extra == {"postJson", "toast"}, (
            f"the copies have drifted beyond the documented shim: {sorted(extra)}"
        )
        assert not (rebuilt - live), (
            f"the rebuilt copy has functions the live one lacks: {sorted(rebuilt - live)}"
        )

        # And the dispatch tables must match — that is the part a divergence
        # would actually break.
        def scopes(path: Path) -> str:
            text = _read(path)
            block = text[text.index("const SCOPES = {"):]
            return re.sub(r"\s+", "", block[: block.index("\n  };")])

        assert scopes(SCAN_MODAL_JS[0]) == scopes(SCAN_MODAL_JS[1]), (
            "the two copies declare different scan scopes"
        )

    @pytest.mark.skipif(shutil.which("node") is None, reason="node is required for the JS probe")
    def test_the_targeted_dispatch_is_exercised(self):
        """⭐ Runs the probe: submits a form and reads the fields back.

        Source assertions cannot prove the dispatch WORKS — and two bugs here
        (a self-recursive shim and a temporal-dead-zone `force`) were invisible
        in the source and would have broken every artist/album scan.

        ⚠️ `encoding='utf-8'` is required on Windows: the probe prints box-drawing
        characters, and the default cp1252 codec raises inside subprocess's reader
        thread — which surfaces as `stdout is None` rather than a decode error.
        """
        result = subprocess.run(
            ["node", str(TARGETED_PROBE)], capture_output=True, text=True,
            encoding="utf-8", errors="replace", cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "ALL PASS" in (result.stdout or "")
