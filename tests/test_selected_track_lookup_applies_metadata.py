"""Lookup MBID for SELECTED tracks must also write their metadata.

REPORTED

> "when you select multiple tracks on the album page and lookup mbid for the
>  tracks, it attaches the mbid for them, but should also update the metadata
>  for those tracks only."

The rebuilt album page's "Lookup MBID for Selected" resolved a concrete
release and then posted ONLY the release identity to ``/api/track/bulk-update``.
The selected rows were bound to the release while keeping their old title and
track/disc numbers, so the lookup looked half-applied.

The link endpoint already matches local rows against a release tracklist. With
``track_ids`` + ``apply_metadata`` it now writes that match's title, track/disc
number and MBIDs onto the SELECTED rows only.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from db.engine import db_session


RELEASE_ID = "11111111-1111-1111-1111-111111111111"
GROUP_ID = "22222222-2222-2222-2222-222222222222"


class _StubMB:
    def get_release(self, release_id, inc="", timeout=0):
        return {
            "id": release_id,
            "title": "Untouchables",
            "media": [
                {
                    "position": 1,
                    "tracks": [
                        {
                            "position": 1,
                            "number": "1",
                            "title": "Here to Stay",
                            "recording": {"id": "rec-1"},
                        },
                        {
                            "position": 2,
                            "number": "2",
                            "title": "Make Believe",
                            "recording": {"id": "rec-2"},
                        },
                    ],
                }
            ],
        }


@pytest.fixture()
def library():
    with db_session() as session:
        session.execute(text("DELETE FROM tracks"))
        session.execute(
            text(
                "INSERT INTO tracks (id, title, artist, album_artist, album, "
                "track_number, disc_number, file_path, musicbrainz_albumid, "
                "musicbrainz_album_mbid, musicbrainz_releasegroupid, "
                "recording_mbid, musicbrainz_trackid) VALUES "
                "('t1', 'Old One', 'Korn', 'Korn', 'Untouchables', '1', '1', "
                "'/music/korn/01.mp3', 'old-rel', 'old-rel', 'old-rg', "
                "'old-rec', 'old-rec'), "
                "('t2', 'Old Two', 'Korn', 'Korn', 'Untouchables', '2', '1', "
                "'/music/korn/02.mp3', 'old-rel', 'old-rel', 'old-rg', "
                "'old-rec', 'old-rec')"
            )
        )
        session.commit()
    yield


def _row(track_id):
    with db_session() as session:
        return dict(
            session.execute(
                text(
                    "SELECT title, track_number, disc_number, recording_mbid, "
                    "musicbrainz_albumid, musicbrainz_album_mbid, "
                    "musicbrainz_releasegroupid FROM tracks WHERE id = :id"
                ),
                {"id": track_id},
            ).mappings().one()
        )


def _install(monkeypatch):
    import routes.musicbrainz_routes as mb_routes
    import routes.track_routes as track_routes

    monkeypatch.setattr(mb_routes, "_get_mb_client", lambda: _StubMB())
    synced: list[list[str]] = []
    monkeypatch.setattr(
        track_routes,
        "_start_bulk_file_sync",
        lambda ids: synced.append(list(ids)) or True,
    )
    return synced


class TestTheSelectedLookup:
    async def test_it_updates_metadata_for_the_selection_only(
        self, client, library, monkeypatch
    ) -> None:
        synced = _install(monkeypatch)

        resp = await client.post(
            "/api/musicbrainz/link-album-mbids",
            json={
                "artist": "Korn",
                "album": "Untouchables",
                "release_id": RELEASE_ID,
                "release_group_mbid": GROUP_ID,
                "track_ids": ["t1"],
                "apply_metadata": True,
            },
        )

        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["success"] is True, body
        assert body["linked"] == 1
        assert body["metadata_applied"] is True

        one = _row("t1")
        assert one["title"] == "Here to Stay"
        assert one["track_number"] == "1"
        assert one["recording_mbid"] == "rec-1"
        assert one["musicbrainz_albumid"] == RELEASE_ID
        assert one["musicbrainz_album_mbid"] == RELEASE_ID
        assert one["musicbrainz_releasegroupid"] == GROUP_ID

        two = _row("t2")
        assert two["title"] == "Old Two", "an unselected track was changed"
        assert two["recording_mbid"] == "old-rec"
        assert two["musicbrainz_albumid"] == "old-rel"

        assert synced == [["t1"]], "the file sync must cover only the selection"

    async def test_a_single_disc_release_does_not_add_a_bogus_disc_number(
        self, client, library, monkeypatch
    ) -> None:
        _install(monkeypatch)
        with db_session() as session:
            session.execute(
                text("UPDATE tracks SET disc_number = '' WHERE id = 't1'")
            )
            session.commit()

        resp = await client.post(
            "/api/musicbrainz/link-album-mbids",
            json={
                "artist": "Korn",
                "album": "Untouchables",
                "release_id": RELEASE_ID,
                "track_ids": ["t1"],
                "apply_metadata": True,
            },
        )
        assert resp.status_code == 200
        assert _row("t1")["disc_number"] in (None, "")

    async def test_apply_metadata_without_ids_is_refused(
        self, client, library, monkeypatch
    ) -> None:
        """An unscoped metadata overwrite must not be possible."""
        _install(monkeypatch)
        resp = await client.post(
            "/api/musicbrainz/link-album-mbids",
            json={
                "artist": "Korn",
                "album": "Untouchables",
                "release_id": RELEASE_ID,
                "apply_metadata": True,
            },
        )
        assert resp.status_code == 400
        assert _row("t1")["title"] == "Old One"

    async def test_the_album_wide_auto_link_still_writes_no_metadata(
        self, client, library, monkeypatch
    ) -> None:
        """CONTROL: the old button's contract must not change."""
        _install(monkeypatch)
        with db_session() as session:
            # Both already linked → the auto-link path has nothing to do.
            session.execute(
                text(
                    "UPDATE tracks SET recording_mbid = 'x', "
                    "musicbrainz_trackid = 'x'"
                )
            )
            session.commit()

        resp = await client.post(
            "/api/musicbrainz/link-album-mbids",
            json={
                "artist": "Korn",
                "album": "Untouchables",
                "release_id": RELEASE_ID,
            },
        )
        body = await resp.get_json()
        assert body["linked"] == 0
        assert _row("t1")["title"] == "Old One"


class TestTheMatchingCarriesMetadata:
    def test_a_position_match_returns_the_release_values(self) -> None:
        from routes.musicbrainz_routes import _match_release_tracklist

        local = [{"id": "t1", "title": "Old", "track_number": "3", "disc_number": "1"}]
        mb = [{
            "position": 3, "number": "3", "disc": 1,
            "title": "New Title", "recording_mbid": "rec-3",
        }]

        match = _match_release_tracklist(local, mb)[0]

        assert match["mb_title"] == "New Title"
        assert match["mb_track_number"] == "3"
        assert match["mb_disc"] == 1
