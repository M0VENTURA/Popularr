"""Downloads & Monitor: mobile folder rows and queue-aware release matching.

REPORTS:
* "on the download queue on mobile, when matching a folder to a download item,
  the view is skewed" — the action cluster squeezed the text column until words
  wrapped one character per line;
* "when matching to a release, it should list the albums that are in the queue
  first to select from, with a search under it".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from db.engine import db_session
from queue_schema_helpers import ensure_queue_table

REPO = Path(__file__).resolve().parent.parent
ITEM_GROUPS = REPO / "test_site" / "static" / "js" / "services" / "item-groups.js"
MB_PICKER = REPO / "test_site" / "static" / "js" / "services" / "musicbrainz-picker.js"
MONITOR_JS = REPO / "test_site" / "static" / "js" / "pages" / "monitor.js"


@pytest.fixture(autouse=True)
def _isolated_queue():
    ensure_queue_table()

    def _wipe() -> None:
        with db_session() as session:
            session.execute(text("DELETE FROM download_queue"))

    _wipe()
    yield
    _wipe()


def _queue_row(
    *,
    album: str,
    artist: str,
    album_artist: str = "",
    status: str = "queued",
    release_mbid: str = "",
    release_group_mbid: str = "",
    release_id: str = "",
    release_source: str = "",
    cover_art_url: str = "",
) -> None:
    with db_session() as session:
        session.execute(
            text(
                "INSERT INTO download_queue "
                "(album, artist, album_artist, status, release_mbid, "
                " musicbrainz_releasegroupid, release_id, release_source, "
                " cover_art_url) "
                "VALUES (:album, :artist, :album_artist, :status, :release_mbid, "
                ":release_group_mbid, :release_id, :release_source, :cover_art_url)"
            ),
            {
                "album": album,
                "artist": artist,
                "album_artist": album_artist,
                "status": status,
                "release_mbid": release_mbid,
                "release_group_mbid": release_group_mbid,
                "release_id": release_id,
                "release_source": release_source,
                "cover_art_url": cover_art_url,
            },
        )


class TestQueueAlbumsEndpoint:
    async def test_groups_by_album_artist_and_album_with_a_usable_mb_id(self, client):
        _queue_row(
            album="2002 - MTV2 Handpicked", artist="Coldplay",
            album_artist="Various Artists", release_mbid="rel-1",
            cover_art_url="https://art/1.jpg",
        )
        _queue_row(
            album="2002 - MTV2 Handpicked", artist="Oasis",
            album_artist="Various Artists", release_group_mbid="rg-1",
        )
        _queue_row(album="Parachutes", artist="Coldplay", release_group_mbid="rg-2")
        # No MusicBrainz id: cannot be associated, so it is not offered.
        _queue_row(album="No MBID Album", artist="Coldplay")
        # Terminal rows are not "in the queue".
        _queue_row(
            album="Deleted Album", artist="Coldplay",
            status="deleted", release_mbid="rel-gone",
        )

        resp = await client.get("/api/downloads/queue/albums")
        assert resp.status_code == 200
        data = await resp.get_json()

        assert data["success"] is True
        albums = data["albums"]
        assert [album["album"] for album in albums] == [
            "Parachutes", "2002 - MTV2 Handpicked",
        ]

        mtv2 = next(a for a in albums if a["album"] == "2002 - MTV2 Handpicked")
        assert mtv2["artist"] == "Various Artists"
        assert mtv2["track_count"] == 2
        assert mtv2["mb_id"] == "rel-1", "a concrete release wins over its group"
        assert mtv2["release_group_mbid"] == "rg-1"
        assert mtv2["cover_art_url"] == "https://art/1.jpg"

    async def test_a_musicbrainz_release_id_is_a_usable_fallback(self, client):
        _queue_row(
            album="Queued Album", artist="Artist",
            release_id="rel-legacy", release_source="musicbrainz",
        )
        _queue_row(
            album="Discogs Album", artist="Artist",
            release_id="12345", release_source="discogs",
        )

        resp = await client.get("/api/downloads/queue/albums")
        data = await resp.get_json()
        albums = data["albums"]

        assert [album["album"] for album in albums] == ["Queued Album"]
        assert albums[0]["mb_id"] == "rel-legacy"

    async def test_no_queue_albums_is_an_empty_list(self, client):
        resp = await client.get("/api/downloads/queue/albums")
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["albums"] == []


class TestFolderRowLayout:
    def test_the_row_wraps_actions_below_the_text(self):
        source = ITEM_GROUPS.read_text(encoding="utf-8")
        assert "d-flex flex-wrap justify-content-between" in source, (
            "without flex-wrap the non-shrinking action cluster squeezes the "
            "text column to one character on a phone"
        )
        assert "min-width:12rem" in source, (
            "a non-collapsible row carries a subtitle; it needs a usable "
            "minimum width so the ACTIONS shrink/wrap instead of the words"
        )
        assert "ms-auto" in source, (
            "a wrapped action cluster must stay aligned to the right"
        )


class TestMatchModalOffersQueueAlbums:
    def test_the_picker_can_render_queue_albums_above_the_search(self):
        source = MB_PICKER.read_text(encoding="utf-8")
        assert "function renderQueueAlbums(" in source
        assert "In your download queue" in source
        assert "queueAlbums" in source
        assert "mbQueueAlbumsSection" in source
        assert "stageSelection({" in source

    def test_the_monitor_loads_queue_albums_before_opening_the_search(self):
        source = MONITOR_JS.read_text(encoding="utf-8")
        assert "/api/downloads/queue/albums" in source
        assert "{ queueAlbums }" in source
