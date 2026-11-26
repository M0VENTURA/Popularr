"""The downloads monitor page's structure and its two orphan pages.

Reported: *"The folders card loads inside the third tab ('Active Queue &
Monitor'), so it's invisible until you click through. It may deserve its own tab
given it's the page's main working surface."* Plus: `search.html` and
`similar_artists.html` "still exist alongside monitor.html; worth checking
whether they're reachable or dead like the duplicate table was."

Findings

* The navbar's Downloads link is `/downloads/monitor#unmatchedFoldersSection`.
  That section lived inside a NON-ACTIVE Bootstrap tab, and a browser cannot
  scroll to an element inside `display: none` — so the app's main Downloads
  destination landed on the page top with the anchor silently doing nothing.

* The Playlists tab renders `components/search/_playlists.html`, whose form
  submits to `importPlaylistFromCSV`. That lives in features/csv-import.js,
  which monitor.html never loaded, so Upload threw `ReferenceError`. The page
  carried its own CSV importer in monitor.js instead — targeting
  `#csvInlineForm` and friends, which exist only in `old_system/`. Dead code
  standing in front of a broken tab.

* `similar_artists.html` is reachable (`/downloads/discover/similar-artists`)
  but its data endpoint `/api/library/artists/similar` existed ONLY in the
  retired `old_system/app.py` and was never ported — so the grid could never
  load. Restored.

* `search.html` is NOT dead: track/album/artist pages navigate to
  `/downloads/search?q=…`, and pages/search.js reads that parameter and drives
  the Soulseek panel. It must stay.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "test_site" / "templates"
MONITOR_HTML = TEMPLATES / "Pages" / "downloads" / "monitor.html"
MONITOR_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "monitor.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _without_comments(text: str) -> str:
    """*text* with Jinja/CSS/JS comments removed.

    The explanatory comments in these files NAME the things they replace, so a
    naive substring search finds the fix documented as though it were the bug.
    """
    import re

    text = re.sub(r"\{#.*?#\}", "", text, flags=re.DOTALL)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return text


# ---------------------------------------------------------------------------
# The folders card is the default, first tab
# ---------------------------------------------------------------------------

class TestTheFoldersCardIsTheDefaultTab:
    def test_the_first_tab_pane_is_the_folders_one(self):
        """The ACTIVE pane must be the one holding the folders section.

        This is the anchor target for the navbar's Downloads link. If the active
        pane is some other tab, the browser cannot scroll to a hidden section and
        the link silently does nothing — the reported bug.
        """
        body = _without_comments(_read(MONITOR_HTML))

        panes = [
            (m.group(1), m.group(2))
            for m in __import__("re").finditer(
                r'<(div|section)[^>]*class="tab-pane[^"]*show active[^"]*"[^>]*id="([^"]+)"',
                body,
            )
        ] or [
            (m.group(1), m.group(2))
            for m in __import__("re").finditer(
                r'<(div|section)[^>]*id="([^"]+)"[^>]*class="tab-pane[^"]*show active[^"]*"',
                body,
            )
        ]
        assert panes, "no active tab pane found"

        active_id = panes[0][1]
        assert active_id != "soulseek", (
            "the Soulseek pane is the default again; the folders card is the "
            "page's main working surface and the navbar anchor target"
        )
        # The folders section must live INSIDE the active pane.
        active_start = body.index(f'id="{active_id}"')
        folder_start = body.index('id="unmatchedFoldersSection"')
        assert folder_start > active_start, "the folders section is before the active pane"
        assert 'id="unmatchedFoldersSection"' in body

    def test_the_nav_anchor_now_lives_in_a_visible_pane(self):
        """The navbar target must not sit in a non-active tab."""
        import re

        body = _without_comments(_read(MONITOR_HTML))
        active_ids = re.findall(
            r'id="([A-Za-z0-9_-]+)"[^>]*class="tab-pane[^"]*show active[^"]*"', body
        ) + re.findall(
            r'class="tab-pane[^"]*show active[^"]*"[^>]*id="([A-Za-z0-9_-]+)"', body
        )
        assert active_ids, "no active tab pane"
        assert "unmatchedFoldersSection" in body
        # The card must precede the closing of the active pane, i.e. appear
        # before the NEXT tab-pane opens.
        next_pane = re.search(r'id="(soulseek|playlists)"', body)
        if next_pane:
            assert body.index('id="unmatchedFoldersSection"') < next_pane.start(), (
                "the folders card is rendered after another pane opens — it is "
                "inside the wrong tab"
            )


# ---------------------------------------------------------------------------
# The Playlists tab must have its controllers
# ---------------------------------------------------------------------------

class TestThePlaylistTabIsWired:
    def test_the_csv_modules_are_loaded(self):
        """`_playlists.html` submits to importPlaylistFromCSV.

        That function lives in features/csv-import.js. Without it the Upload
        button throws ReferenceError, which is exactly what happened when the
        page loaded only monitor.js's dead csvInline copy.
        """
        body = _read(MONITOR_HTML)
        for module in (
            "js/state/import-state.js",
            "js/features/csv-import.js",
            "js/pages/playlist.js",
        ):
            assert module in body, f"{module} is not loaded by the monitor page"

    def test_import_state_loads_before_its_consumers(self):
        """⚠️ ORDER IS LOAD-BEARING.

        All three files touch currentImportData / missingTracksForSearch, and
        when two declared them with `let` the second threw "Identifier has
        already been declared" at PARSE time and defined nothing.
        """
        body = _read(MONITOR_HTML)
        state_at = body.index("js/state/import-state.js")
        csv_at = body.index("js/features/csv-import.js")
        playlist_at = body.index("js/pages/playlist.js")
        assert state_at < csv_at, "import-state.js must precede csv-import.js"
        assert state_at < playlist_at, "import-state.js must precede playlist.js"

    def test_the_dead_csv_importer_is_gone_from_monitor_js(self):
        """The csvInline importer targeted ids that exist only in old_system."""
        live = _without_comments(_read(MONITOR_JS))
        for token in (
            "csvInlineImport",
            "csvInlineReset",
            "csvInlineForm",
            "csvSetProgress",
        ):
            assert token not in live, (
                f"`{token}` is back in monitor.js — it targets ids that exist "
                "nowhere in this tree, so it can never run"
            )


# ---------------------------------------------------------------------------
# The similar-artists page has a backend
# ---------------------------------------------------------------------------

class TestTheSimilarArtistsEndpointExists:
    def test_the_route_is_registered(self):
        """The page calls /api/library/artists/similar.

        It existed only in the retired old_system/app.py, so the grid could
        never load. This asserts the route is back rather than the page being
        quietly broken.
        """
        from app import app

        rules = {str(rule) for rule in app.url_map.iter_rules()}
        assert "/api/library/artists/similar" in rules

    def test_the_service_aggregates_the_whole_library(self):
        from services.metadata import artist_metadata_service as svc

        assert hasattr(svc, "get_library_similar_artists")


# ---------------------------------------------------------------------------
# The navbar anchor must actually scroll
# ---------------------------------------------------------------------------

class TestTheNavbarAnchorScrolls:
    """The card starts `display: none` and is revealed only when folders exist.

    The browser's initial hash scroll therefore finds nothing to scroll to and
    never retries — clicking Downloads landed on the page top even though the
    anchor named a real element. The controller re-honours the hash once the
    card is visible.
    """

    def test_the_hash_is_honoured_after_the_card_becomes_visible(self):
        live = _without_comments(_read(MONITOR_JS))
        assert "honorFolderAnchor" in live, (
            "nothing re-honours #unmatchedFoldersSection after the card is shown"
        )
        assert "scrollIntoView" in live

    def test_it_runs_once_only(self):
        """Re-scrolling on every 30s poll tick would fight the user's scroll."""
        live = _without_comments(_read(MONITOR_JS))
        assert "folderAnchorHonored" in live, (
            "without a latch the poll loop would yank the viewport every tick"
        )

    def test_it_checks_the_specific_hash(self):
        live = _without_comments(_read(MONITOR_JS))
        assert "#unmatchedFoldersSection" in live


# ---------------------------------------------------------------------------
# search.html is NOT dead — the ?q= links depend on it
# ---------------------------------------------------------------------------

class TestSearchPageIsStillNeeded:
    def test_the_q_links_still_target_the_search_page(self):
        """If this page is ever deleted, every "Search Soulseek" link breaks.

        album-soulseek.js and track.js both navigate to /downloads/search?q=…,
        and pages/search.js is what reads `q` and drives the Soulseek panel.
        """
        js = REPO_ROOT / "test_site" / "static" / "js"
        for path in (js / "pages" / "track.js", js / "services" / "album-soulseek.js"):
            assert "/downloads/search" in _read(path), (
                f"{path.name} no longer links to /downloads/search — check "
                "whether the page can be retired before deleting it"
            )

    def test_the_query_parameter_is_consumed(self):
        search_js = _read(REPO_ROOT / "test_site" / "static" / "js" / "pages" / "search.js")
        assert "URLSearchParams" in search_js
        assert "slskdSearchInput" in search_js, (
            "the ?q= handler must target the panel's real input id"
        )
