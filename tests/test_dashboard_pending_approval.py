"""The dashboard's "Needs Metadata Approval" panel.

REQUESTED: "Have area on dashboard that shows recently scanned albums needing
metadata approval. These will disappear once approved. List is scrollable
showing all releases from most recently scanned, but will only show items that
have recommendations for adjustments."

⭐ NO NEW STORAGE. The scan was already stashing what a metadata import WOULD
write onto ``tracks.pending_mb_updates`` (with a ``stashed_at`` timestamp)
whenever the Config page's metadata updating is recommend-only. This feature
only surfaces that state — so the two can never disagree.

⭐ "DISAPPEAR ONCE APPROVED" IS ALREADY THE LIFECYCLE. Both the save path
(``routes/ui_routes.py``) and the discard path clear the stash, so an album
leaves the list without anything being re-scanned and without a second
"approved" flag to keep in sync. That is asserted here rather than assumed.

The rendering rules are covered by ``tests/js/pending-approval-probe.js``, which
brace-matches the SHIPPED ``renderPendingApproval`` out of the dashboard module.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest
from sqlalchemy import text

from db.engine import db_session

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "pending-approval-probe.js"
DASHBOARD_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "dashboard.js"
DASHBOARD_HTML = REPO_ROOT / "test_site" / "templates" / "Pages" / "dashboard.html"

PREFIX = "pend-"


def _node_available() -> bool:
    try:
        return subprocess.run(["node", "--version"], capture_output=True).returncode == 0
    except Exception:
        return False


needs_node = pytest.mark.skipif(not _node_available(), reason="node is required")


def _envelope(
    stashed_at: str,
    *,
    album_changes: int = 1,
    track_changes: int = 1,
    release_title: str = "Release",
) -> str:
    """The exact envelope shape ``stash_album_recommendations`` writes."""
    return json.dumps({
        "version": 1,
        "release_mbid": "rel-1",
        "release_group_mbid": "rg-1",
        "release_title": release_title,
        "stashed_at": stashed_at,
        "album_changes": [
            {"field": f"f{i}", "label": f"L{i}", "current": "a", "proposed": "b"}
            for i in range(album_changes)
        ],
        "changes": [
            {"field": f"t{i}", "label": f"T{i}", "current": "a", "proposed": "b"}
            for i in range(track_changes)
        ],
    })


def _seed(*rows) -> None:
    """rows: (suffix, artist, album, payload_or_None, ignored_or_None)"""
    with db_session() as session:
        session.execute(text(f"DELETE FROM tracks WHERE id LIKE '{PREFIX}%'"))
        for suffix, artist, album, payload, ignored in rows:
            session.execute(text("""
                INSERT INTO tracks (id, artist, album_artist, album, title,
                                    pending_mb_updates, mb_ignored_fields)
                VALUES (:id, :artist, :artist, :album, 'T', :p, :ign)
            """), {
                "id": PREFIX + suffix,
                "artist": artist,
                "album": album,
                "p": payload,
                "ign": ignored,
            })
        session.commit()


@pytest.fixture(autouse=True)
def _clean():
    yield
    try:
        with db_session() as session:
            session.execute(text(f"DELETE FROM tracks WHERE id LIKE '{PREFIX}%'"))
            session.commit()
    except Exception:
        pass


class TestAlbumsWithRecommendationsAreListed:
    def test_only_albums_that_have_recommendations_appear(self):
        from services.metadata.pending_update_service import fetch_albums_pending_review

        _seed(
            ("a", "Artist A", "Needs Work", _envelope("2026-09-27T10:00:00"), None),
            ("b", "Artist B", "All Clean", None, None),
        )
        albums = fetch_albums_pending_review(limit=50)
        names = [a["album"] for a in albums]

        assert "Needs Work" in names, (
            "an album with stashed recommendations must be listed"
        )
        assert "All Clean" not in names, (
            "an album with NO recommendations must not appear — the panel only "
            "shows items that have adjustments"
        )

    def test_newest_first(self):
        from services.metadata.pending_update_service import fetch_albums_pending_review

        _seed(
            ("a", "Artist A", "Oldest", _envelope("2026-09-01T08:00:00"), None),
            ("b", "Artist B", "Newest", _envelope("2026-09-27T10:00:00"), None),
            ("c", "Artist C", "Middle", _envelope("2026-09-15T09:00:00"), None),
        )
        names = [a["album"] for a in fetch_albums_pending_review(limit=50)]

        assert names == ["Newest", "Middle", "Oldest"], (
            "the list is ordered by when the recommendations were stashed, "
            "newest first"
        )

    def test_the_change_count_adds_album_and_track_changes(self):
        from services.metadata.pending_update_service import fetch_albums_pending_review

        _seed(
            ("a", "Artist A", "Mixed", _envelope(
                "2026-09-27T10:00:00", album_changes=2, track_changes=3,
            ), None),
        )
        album = fetch_albums_pending_review(limit=50)[0]

        assert album["change_count"] == 5, (
            "album-level and track-level changes must both count, or the badge "
            "understates the work"
        )
        assert album["album_changes"] == 2
        assert album["track_changes"] == 3

    def test_release_title_is_carried_through(self):
        from services.metadata.pending_update_service import fetch_albums_pending_review

        _seed(
            ("a", "Artist A", "Album", _envelope(
                "2026-09-27T10:00:00", release_title="The Deluxe Edition",
            ), None),
        )
        assert fetch_albums_pending_review(limit=50)[0]["release_title"] == "The Deluxe Edition"


class TestTheListIsAWorkQueueNotALog:
    """An entry that would save nothing must not be shown."""

    def test_an_all_ignored_album_is_omitted(self):
        from services.metadata.pending_update_service import fetch_albums_pending_review

        _seed(
            ("a", "Artist A", "Every Field Ignored",
             _envelope("2026-09-27T10:00:00", album_changes=1, track_changes=1),
             json.dumps(["f0", "t0"])),
        )
        assert fetch_albums_pending_review(limit=50) == [], (
            "every proposed field is permanently ignored, so the album offers "
            "the user nothing to approve — showing it would be noise"
        )

    def test_the_limit_caps_albums_not_changes(self):
        from services.metadata.pending_update_service import fetch_albums_pending_review

        _seed(*[
            (f"a{i}", f"Artist {i}", f"Album {i}",
             _envelope(f"2026-09-{10 + i:02d}T10:00:00"), None)
            for i in range(5)
        ])
        assert len(fetch_albums_pending_review(limit=2)) == 2

    def test_a_corrupt_envelope_is_skipped_not_raised(self):
        from services.metadata.pending_update_service import fetch_albums_pending_review

        _seed(
            ("bad", "Artist A", "Corrupt", "{not json", None),
            ("good", "Artist B", "Fine", _envelope("2026-09-27T10:00:00"), None),
        )
        albums = fetch_albums_pending_review(limit=50)
        assert [a["album"] for a in albums] == ["Fine"], (
            "one unreadable row must not take the whole panel down"
        )


class TestApprovingRemovesAnAlbumFromTheList:
    """The requested behaviour, asserted rather than assumed."""

    def test_discarding_clears_the_stash_and_empties_the_list(self):
        from services.metadata.pending_update_service import (
            discard_album_recommendations,
            fetch_albums_pending_review,
        )

        _seed(("a", "Artist A", "Album A", _envelope("2026-09-27T10:00:00"), None))
        assert len(fetch_albums_pending_review(limit=50)) == 1

        result = discard_album_recommendations("Artist A", "Album A")

        assert result["cleared"] == 1
        assert fetch_albums_pending_review(limit=50) == [], (
            "discarding must remove the album from the approval list"
        )

    def test_the_save_path_clears_the_stash(self):
        """Saving a review is how the user APPROVES it.

        If the save path stopped clearing, the album would stay on the
        dashboard forever while its changes had already been applied.
        """
        src = (REPO_ROOT / "routes" / "ui_routes.py").read_text(encoding="utf-8")
        assert "discard_album_recommendations(" in src, (
            "the album save must clear the stashed recommendations it applied"
        )
        idx = src.index("discard_album_recommendations(")
        window = src[max(0, idx - 700): idx]
        assert "_has_pending_recommendations" in window, (
            "the clear is gated on there being pending recommendations AND on "
            "the save having written something"
        )


class TestTheEndpoint:
    async def test_it_returns_the_albums_newest_first(self, client):
        _seed(
            ("a", "Artist A", "Older", _envelope("2026-09-10T10:00:00"), None),
            ("b", "Artist B", "Newer", _envelope("2026-09-27T10:00:00"), None),
        )
        resp = await client.get("/api/metadata/pending-recommendations")
        assert resp.status_code == 200
        body = await resp.get_json()

        assert body["success"] is True
        assert [a["album"] for a in body["albums"]] == ["Newer", "Older"]
        assert body["count"] == 2

    async def test_the_limit_is_clamped_and_survives_junk(self, client):
        # A non-numeric limit must fall back rather than 500.
        resp = await client.get("/api/metadata/pending-recommendations?limit=abc")
        assert resp.status_code == 200
        assert (await resp.get_json())["success"] is True

        resp = await client.get("/api/metadata/pending-recommendations?limit=0")
        assert resp.status_code == 200

    async def test_a_negative_limit_is_clamped_to_at_least_one(self, client):
        _seed(("a", "Artist A", "Album", _envelope("2026-09-27T10:00:00"), None))
        resp = await client.get("/api/metadata/pending-recommendations?limit=-5")
        assert resp.status_code == 200
        assert len((await resp.get_json())["albums"]) == 1


class TestTheCardIsWiredIntoTheDashboard:
    def test_the_card_markup_exists_and_starts_hidden(self):
        html = DASHBOARD_HTML.read_text(encoding="utf-8")
        assert 'id="pendingApprovalCard"' in html
        assert 'id="pending-approval-body"' in html
        assert 'id="pending-approval-count"' in html
        # Starts hidden: an up-to-date library must not show an empty panel.
        card_idx = html.index('id="pendingApprovalCard"')
        assert "d-none" in html[max(0, card_idx - 120): card_idx + 120], (
            "the card must start hidden and be revealed only when there is "
            "something to approve"
        )

    def test_the_list_is_scrollable(self):
        html = DASHBOARD_HTML.read_text(encoding="utf-8")
        idx = html.index('id="pending-approval-body"')
        window = html[max(0, idx - 400): idx]
        assert "max-height" in window and "overflow-y" in window, (
            "the panel must scroll rather than push the page taller"
        )

    def test_the_module_polls_the_endpoint(self):
        src = DASHBOARD_JS.read_text(encoding="utf-8")
        assert "/api/metadata/pending-recommendations" in src, (
            "the card is filled from the endpoint, not server-rendered, so it "
            "refreshes and empties as albums are approved"
        )
        assert "updatePendingApproval()," in src, (
            "the loader must be part of the dashboard's poll cycle, or the list "
            "goes stale"
        )


@needs_node
class TestTheRenderingRules:
    """Driven from the SHIPPED renderer via a Node probe."""

    @staticmethod
    def _probe() -> dict:
        out = subprocess.run(
            ["node", str(PROBE), str(DASHBOARD_JS)],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        assert out.returncode == 0, (out.stdout or "") + (out.stderr or "")
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_an_empty_list_hides_the_card(self):
        assert self._probe()["empty_hidden"] is True

    def test_a_row_links_to_the_album_page(self):
        result = self._probe()
        assert result["one_links_album"] is True, (
            "each row must link to the album, where the review and Save live"
        )
        assert result["one_encodes_segments"] is True, (
            "artist/album segments must be URL-encoded or a slash in a name "
            "breaks the path"
        )

    def test_the_link_omits_an_absent_year(self):
        assert self._probe()["no_year_no_trailing"] is True

    def test_the_row_shows_the_change_count(self):
        result = self._probe()
        assert result["one_shows_change_count"] is True
        assert result["plural_present"] is True

    def test_markup_in_a_title_is_escaped(self):
        result = self._probe()
        assert result["escapes_html"] is True, (
            "an album title is user/tag data and must not be injected as markup"
        )
        assert result["escapes_quotes"] is True, (
            "quotes must be escaped too, or a title can break out of an attribute"
        )
