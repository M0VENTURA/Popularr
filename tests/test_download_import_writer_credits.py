"""A download import must fill Writer / Composer / Lyricist from MusicBrainz.

REPORTED

> "When downloading a song from soulseek via the download queue, the following
>  fields aren't filled in when the tracks are moved into the collection:
>  Writer, Composer, Lyricist."

The import only ever copied ``writer`` from the queue row's stored metadata and
its release refresh asked for isrc/tracktotal/artist-id only. Composer and
lyricist were never read, fetched or persisted.

``_apply_stored_metadata`` now:

* reads all three roles from the queue row's stored metadata;
* fills any missing role from the release tracklist (no extra request);
* falls back to the ROLE-AWARE recording relations when a single-song download
  has a recording id but no release id (a flat "composers" list could not tell
  composer from lyricist);
* persists all three to the track row and writes them into the file tags.
"""

from __future__ import annotations

import json

import pytest

import services.downloads.download_completion_service as dcs
import services.enrichment.musicbrainz_service as mbs
import services.metadata.tag_file_service as tag_service
from db.repositories import tracks as tracks_repo

CREDITS = {
    "writer": "Tommi Lalli, Olli Matela",
    "composer": "Alex Mattson, Niko Moilanen",
    "lyricist": "Joonas Parkkonen, Joonas Porko",
}

#: The recording-relations API returns LISTS per role; the release tracklist
#: carries the same values as comma-joined strings.
CREDIT_LISTS = {
    "writer": ["Tommi Lalli", "Olli Matela"],
    "composer": ["Alex Mattson", "Niko Moilanen"],
    "lyricist": ["Joonas Parkkonen", "Joonas Porko"],
}


class _StubMBService:
    def __init__(self, roles):
        self.roles = roles
        self.calls = 0

    def get_credit_roles_for_recording(self, recording_mbid):
        self.calls += 1
        return self.roles


@pytest.fixture()
def captured(monkeypatch):
    """Capture the file tags and the DB payload the import would write."""
    file_tags: list[dict] = []
    db_payload: list[dict] = []
    mb_calls: list[str] = []

    monkeypatch.setattr(dcs, "_resolve_album_level_metadata", lambda item: {})
    monkeypatch.setattr(dcs, "_resolve_track_id_for_import", lambda **kw: "t1")
    monkeypatch.setattr(dcs, "_fetch_import_art", lambda item: None)
    monkeypatch.setattr(
        tag_service, "update_file_metadata",
        lambda path, meta: file_tags.append(dict(meta)) or True,
    )
    monkeypatch.setattr(
        tracks_repo, "insert_or_update_track",
        lambda track_id, payload: db_payload.append(dict(payload)),
    )
    monkeypatch.setattr(
        mbs, "fetch_musicbrainz_release_metadata",
        lambda release_id: {
            "tracks": [{
                "recording_mbid": "rec-1",
                "title": "Song",
                "isrc": "USRC17607839",
                "tracktotal": "12",
                "artist_mbid": "artist-1",
                "writer": CREDITS["writer"],
                "composer": CREDITS["composer"],
                "lyricist": CREDITS["lyricist"],
            }]
        },
    )
    store = {"roles": {k: list(v) for k, v in CREDIT_LISTS.items()}, "calls": 0}

    def _shared():
        store["calls"] += 1
        return _StubMBService(store["roles"])

    monkeypatch.setattr(mbs, "get_shared_mb_service", _shared)
    return {"file_tags": file_tags, "db_payload": db_payload, "store": store}


def _item(**overrides):
    item = {
        "id": 1,
        "title": "Song",
        "artist": "Band",
        "album": "Album",
        "album_artist": "Band",
    }
    item.update(overrides)
    return item


class TestTheImportFillsCredits:
    def test_the_release_tracklist_fills_all_three_roles(self, captured):
        ok = dcs._apply_stored_metadata(
            _item(recording_mbid="rec-1", release_mbid="rel-1"),
            "/music/Band/Album/01.mp3",
        )
        assert ok is True

        meta = captured["file_tags"][0]
        payload = captured["db_payload"][0]
        for role, value in CREDITS.items():
            assert meta.get(role) == value, f"{role} not written to the file tags"
            assert payload.get(role) == value, f"{role} not persisted to the row"
        # The release path already had the values, so no recording request.
        assert captured["store"]["calls"] == 0

    def test_a_single_song_uses_the_role_aware_recording_relations(self, captured):
        ok = dcs._apply_stored_metadata(
            _item(recording_mbid="rec-2"),  # no release id
            "/music/Band/Album/02.mp3",
        )
        assert ok is True
        assert captured["store"]["calls"] == 1

        meta = captured["file_tags"][0]
        payload = captured["db_payload"][0]
        for role, value in CREDITS.items():
            assert meta.get(role) == value
            assert payload.get(role) == value

    def test_stored_composer_and_lyricist_survive(self, captured):
        item = _item(metadata=json.dumps({
            "writer": "Stored Writer",
            "composer": "Stored Composer",
            "lyricist": "Stored Lyricist",
        }))
        dcs._apply_stored_metadata(item, "/music/Band/Album/03.mp3")

        payload = captured["db_payload"][0]
        assert payload["writer"] == "Stored Writer"
        assert payload["composer"] == "Stored Composer"
        assert payload["lyricist"] == "Stored Lyricist"
