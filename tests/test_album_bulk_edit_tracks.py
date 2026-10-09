"""Bulk "Edit Tracks" on the album page — the Edit Album identity fields, scoped.

Reported
--------
> It would be good to have an option on the multiple select to select multiple
> tracks and select Edit Tracks (next to rename selected) which would give
> access to the items that are in the edit album page for those tracks to
> realign to a different album if needed.

Where "Rename Selected" MOVES FILES, this edits the ROWS: the selected ticks
get the album/artist/year/title fields the Edit Album page offers, so a few
tracks can be pushed onto a different album without disturbing the rest of the
tracklist.

Contracts pinned here
---------------------
* only the SELECTED ids are written — a track outside the selection must not
  move;
* a blank field means "leave unchanged": the modal posts only what was typed,
  so the update is partial by construction;
* anything outside the allow-list is REFUSED (400), never silently dropped —
  a field the caller believes it set must never look applied;
* both trees render the button + modal, and both scripts define and export the
  handlers the inline ``onclick`` calls;
* the DB write is followed by the file fan-out (the album-save rule: a metadata
  change reaches the rows AND the tags Navidrome reads).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_TEMPLATE = REPO_ROOT / "templates" / "pages" / "album_detail.html"
REBUILT_TEMPLATE = REPO_ROOT / "test_site" / "templates" / "Pages" / "album_detail.html"
LIVE_JS = REPO_ROOT / "static" / "js" / "album_detail.js"
REBUILT_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "album.js"
TRACK_ROUTES = REPO_ROOT / "routes" / "track_routes.py"

ARTIST = "Sirenia"
ORIGIN = "Origin Album"


def _seed(db_session, track_id: str, *, album: str = ORIGIN) -> None:
    from sqlalchemy import text

    db_session.execute(
        text("""
            INSERT INTO tracks (id, artist, album, title, file_path, year)
            VALUES (:id, :artist, :album, :title, :file_path, '2007')
            ON CONFLICT DO NOTHING
        """),
        {
            "id": track_id,
            "artist": ARTIST,
            "album": album,
            "title": f"Track {track_id}",
            "file_path": f"/music/{ARTIST}/{album}/{track_id}.mp3",
        },
    )
    db_session.commit()


def _albums(db_session, ids: list[str]) -> dict[str, str]:
    """``{id: album}`` for the given ids (ids are the test's own literals)."""
    from sqlalchemy import text

    listing = ",".join(f"'{i}'" for i in ids)
    rows = db_session.execute(
        text(f"SELECT id, album FROM tracks WHERE id IN ({listing})")
    ).fetchall()
    return {str(r[0]): str(r[1]) for r in rows}


class TestTheEndpoint:
    def test_the_route_is_registered(self):
        src = TRACK_ROUTES.read_text(encoding="utf-8")
        assert '@track_bp.route("/bulk-update", methods=["POST"])' in src

    async def test_it_requires_ids_and_fields(self, client):
        resp = await client.post("/api/track/bulk-update", json={"fields": {"album": "x"}})
        assert resp.status_code == 400

        resp = await client.post("/api/track/bulk-update", json={"track_ids": ["a"]})
        assert resp.status_code == 400

    async def test_an_unsupported_field_is_refused(self, client, db_session):
        _seed(db_session, "be-x")
        resp = await client.post(
            "/api/track/bulk-update",
            json={"track_ids": ["be-x"], "fields": {"musicbrainz_albumid": "1"}},
        )
        assert resp.status_code == 400, (
            "an unknown field must be refused, not silently dropped"
        )
        payload = await resp.get_json()
        assert payload["success"] is False
        assert "musicbrainz_albumid" in payload["error"]

    async def test_only_the_selected_tracks_move(self, client, db_session):
        """THE POINT: realign a few tracks onto a different album."""
        _seed(db_session, "be-1")
        _seed(db_session, "be-2")
        _seed(db_session, "be-3")

        resp = await client.post(
            "/api/track/bulk-update",
            json={
                "track_ids": ["be-1", "be-2"],
                "fields": {"album": "Destination Album"},
                "sync_to_file": False,
            },
        )
        payload = await resp.get_json()
        assert resp.status_code == 200, payload
        assert payload["success"] is True
        assert payload["tracks_requested"] == 2
        assert payload["updated"] == ["album"]

        got = _albums(db_session, ["be-1", "be-2", "be-3"])
        assert got["be-1"] == "Destination Album"
        assert got["be-2"] == "Destination Album"
        assert got["be-3"] == ORIGIN, "a track outside the selection moved"

    async def test_a_blank_field_leaves_that_column_alone(self, client, db_session):
        """The modal posts only what was typed, so blank must be a no-op."""
        _seed(db_session, "be-4")

        resp = await client.post(
            "/api/track/bulk-update",
            json={
                "track_ids": ["be-4"],
                "fields": {"album": "Renamed Album", "year": "   "},
                "sync_to_file": False,
            },
        )
        assert resp.status_code == 200, await resp.get_json()

        from sqlalchemy import text

        row = db_session.execute(
            text("SELECT album, year FROM tracks WHERE id = 'be-4'")
        ).fetchone()
        assert row[0] == "Renamed Album"
        assert str(row[1]) == "2007", "the blank year must not have been written"

    async def test_every_identity_field_is_accepted(self, client, db_session):
        _seed(db_session, "be-5")
        resp = await client.post(
            "/api/track/bulk-update",
            json={
                "track_ids": ["be-5"],
                "fields": {
                    "album": "A", "album_artist": "Sirenia", "year": "1999",
                    "release_year": "2011", "track_number": "3", "disc_number": "2",
                    "title": "Renamed", "artist": "Someone", "writer": "Composer",
                },
                "sync_to_file": False,
            },
        )
        assert resp.status_code == 200, await resp.get_json()
        payload = await resp.get_json()
        assert payload["success"] is True
        assert len(payload["updated"]) == 9


class TestTheAlbumPageRendersIt:
    def test_both_trees_have_the_button_next_to_rename(self):
        for path in (LIVE_TEMPLATE, REBUILT_TEMPLATE):
            html = path.read_text(encoding="utf-8")
            assert 'id="editTracksBtn"' in html, f"{path.name}: no Edit Tracks button"
            assert "openEditTracksModal()" in html, (
                f"{path.name}: the button does not open the modal"
            )
            assert html.index('id="editTracksBtn"') < html.index('id="renameSelectedBtn"'), (
                f"{path.name}: Edit Tracks must sit beside Rename Selected"
            )

    def test_both_trees_render_the_identity_fields(self):
        expected = [
            "btAlbum", "btAlbumArtist", "btYear", "btReleaseYear",
            "btTrackNumber", "btDiscNumber", "btTitle", "btArtist", "btWriter",
        ]
        for path in (LIVE_TEMPLATE, REBUILT_TEMPLATE):
            html = path.read_text(encoding="utf-8")
            assert 'id="editTracksModal"' in html, f"{path.name}: no modal"
            for field in expected:
                assert f'id="{field}"' in html, f"{path.name}: missing {field}"
            assert 'id="editTracksApplyBtn"' in html
            assert "applyEditTracks(this)" in html


class TestBothScriptsWireIt:
    def test_the_handlers_are_defined_and_exported(self):
        for path in (LIVE_JS, REBUILT_JS):
            src = path.read_text(encoding="utf-8")
            assert "openEditTracksModal" in src, f"{path.name}: opener missing"
            assert "applyEditTracks" in src, f"{path.name}: apply handler missing"
            export = (
                "window.openEditTracksModal" if path == LIVE_JS
                else "global.openEditTracksModal"
            )
            assert export in src, f"{path.name}: {export} not exported for the onclick"

    def test_it_posts_to_the_bulk_endpoint_with_the_selection(self):
        for path in (LIVE_JS, REBUILT_JS):
            src = path.read_text(encoding="utf-8")
            assert "/api/track/bulk-update" in src, f"{path.name}: no endpoint call"
            # The ids come from the SELECTION helper, not from the whole album.
            ids = "_getSelectedTrackIds()" if path == LIVE_JS else "selectedTrackIds()"
            assert ids in src, f"{path.name}: {ids} not used to build track_ids"
            assert "sync_to_file: true" in src, (
                f"{path.name}: the file fan-out was dropped — a metadata change "
                "must reach the rows AND the tags Navidrome reads"
            )
