"""Navidrome import collects the raw tags the API actually exposes — and
preserves the ones it never exposes.

Report: *"These are the raw tags on Navidrome, but not all of these are being
collected during a Navidrome import"* (Boysetsfire — Savage Blood example).

Two halves:

1. **Collect** — the Subsonic/OpenSubsonic response carries these tags under
   keys the extractor never read: `moods` (array), per-role `contributors`
   (mixer/producer/composer), `displayComposer`, and the ALBUM object's
   `recordLabels` / `releaseTypes` / `originalReleaseDate` (songs never
   carry them — that is why they were missing).
2. **Preserve** — the rest of the pasted list (catalognumber, media, script,
   releasecountry, releasestatus, artistwebpage, the sort tags, …) exists in
   Navidrome's UI but is NOT on the API wire at all; an import must keep the
   stored value instead of blanking it (the upsert writes ``col=EXCLUDED.col``
   for every payload key, so an empty string wipes).
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from db.engine import db_session
from db.models import Track
from services.scanning.metadata_extractor import (
    extract_album_metadata,
    extract_track_metadata,
)
from services.scanning.navidrome_import import scan_artist_to_db
from services.scanning.payload_builder import build_track_payload

REC_UUID = "917af4fe-cafb-4aa5-9c19-54e29e850ae1"
ALB_UUID = "4b6b21f7-d6be-4014-87a4-cc43791fd5da"


@pytest.fixture(autouse=True)
def _isolated_tracks():
    def _wipe() -> None:
        with db_session() as session:
            session.execute(text("DELETE FROM tracks"))

    _wipe()
    yield
    _wipe()


def _song(**overrides) -> dict:
    song = {
        "id": "nb-song",
        "title": "Savage Blood",
        "artist": "Boysetsfire",
        "albumArtist": "Boysetsfire",
        "path": "Boysetsfire/2015 - Boysetsfire/01 - Boysetsfire - Savage Blood.mp3",
        "duration": 180,
        "track": 1,
        "disc": 1,
        "year": 2015,
        "userRating": 5,
    }
    song.update(overrides)
    return song


# ---------------------------------------------------------------------------
# 1. Song-level: keys the extractor never used to read
# ---------------------------------------------------------------------------

class TestSongLevelRawTags:
    def test_moods_array_becomes_the_mood_string(self):
        extracted = extract_track_metadata(_song(
            moods=["Heavy", "Energetic", "Melodic"],
        ))
        assert extracted["mood"] == "Heavy; Energetic; Melodic"

    def test_scalar_mood_falls_back(self):
        extracted = extract_track_metadata(_song(mood="Calm"))
        assert extracted["mood"] == "Calm"

    def test_contributor_roles_reach_the_writer_credits(self):
        import json as _json

        extracted = extract_track_metadata(_song(contributors=[
            {"role": "mixer", "artist": {"id": "n1", "name": "Lou Giordano"}},
            {"role": "producer", "artist": {"id": "n2", "name": "Chad Istvan"}},
            {"role": "composer", "artist": {"id": "n3", "name": "Some Composer"}},
        ]))
        writers = _json.loads(extracted["writer"])
        # mixer/producer/conductor have no tracks column of their own — the
        # writer credits JSONB is where per-role people are collected. Before
        # WRITER_ROLES covered them, these raw tags were dropped entirely.
        assert "Lou Giordano" in writers
        assert "Chad Istvan" in writers
        assert "Some Composer" in writers

    def test_display_composer_alias(self):
        extracted = extract_track_metadata(_song(displayComposer="Jane Doe"))
        assert extracted["composer"] == "Jane Doe"

    def test_isrc_array_is_normalised(self):
        extracted = extract_track_metadata(_song(isrc=["DESG40800085"]))
        assert extracted["isrc"] == "DESG40800085"

    def test_no_contributors_is_empty_not_broken(self):
        extracted = extract_track_metadata(_song())
        assert extracted["mixer"] == ""
        assert extracted["producer"] == ""
        assert extracted["mood"] == ""


# ---------------------------------------------------------------------------
# 2. Album-level: AlbumID3 tags songs never carry
# ---------------------------------------------------------------------------

class TestAlbumObjectMetadata:
    def test_record_labels_fill_the_label_column(self):
        meta = extract_album_metadata({
            "recordLabels": [
                {"name": "Bridge Nine Records"},
                {"name": "End Hits Records"},
                {"name": "Bridge Nine Records"},   # deduped
            ],
        })
        assert meta["recordlabel"] == "Bridge Nine Records; End Hits Records"
        # There is NO `label` column anywhere — only recordlabel is storable.
        assert "label" not in meta

    def test_release_types_become_the_composite_form(self):
        meta = extract_album_metadata({"releaseTypes": ["album"]})
        assert meta["releasetype"] == "album"

        meta = extract_album_metadata({"releaseTypes": ["album", "live"]})
        assert meta["releasetype"] == "album+live"

    def test_original_release_date_splits_year_and_date(self):
        meta = extract_album_metadata({
            "originalReleaseDate": {"year": 2015, "month": 9, "day": 25},
        })
        assert meta["originalyear"] == "2015"
        assert meta["originaldate"] == "2015-09-25"

    def test_year_only_original_date(self):
        meta = extract_album_metadata({"originalReleaseDate": {"year": 2015}})
        assert meta["originalyear"] == "2015"
        assert meta["originaldate"] == "2015"

    def test_missing_or_malformed_album_is_safe(self):
        assert extract_album_metadata({}) == {}
        assert extract_album_metadata(None) == {}  # type: ignore[arg-type]
        assert extract_album_metadata({"recordLabels": "nope"}) == {}


# ---------------------------------------------------------------------------
# 3. Payload: album tags fill the empty slots, song tags always win
# ---------------------------------------------------------------------------

def _payload(song: dict, *, album_tags: dict | None = None, is_new: bool = False) -> dict:
    return build_track_payload(
        track=song,
        album_name="2015 - Boysetsfire",
        album_artist_value="Boysetsfire",
        canonical_artist_name="Boysetsfire",
        album_tags=album_tags,
        is_new_track=is_new,
    )


class TestPayloadAlbumTags:
    def test_album_tags_fill_what_the_song_lacks(self):
        album_tags = extract_album_metadata(
            {"recordLabels": [{"name": "Bridge Nine Records"}]}
        )
        payload = _payload(_song(), album_tags=album_tags)

        assert payload.get("recordlabel") == "Bridge Nine Records"

    def test_song_level_value_always_wins(self):
        # The extractor got the label from the song's own tags…
        extracted = extract_track_metadata(_song(
            tags={"recordlabel": "Song Level Label"},
        ))
        payload = build_track_payload(
            track=_song(),
            album_name="X",
            album_artist_value="Boysetsfire",
            canonical_artist_name="Boysetsfire",
            extracted=extracted,
            album_tags={"recordlabel": "Album Level Label"},
            is_new_track=False,
        )
        assert payload.get("recordlabel") == "Song Level Label"


# ---------------------------------------------------------------------------
# 4. Preserve: tags the API never sends must survive an import
# ---------------------------------------------------------------------------

def _seed(track_id: str) -> None:
    with db_session() as session:
        session.execute(
            text(
                "INSERT INTO tracks (id, artist, album, album_artist, title, "
                "releasecountry, catalognumber, media, script, "
                "mood, originalyear) "
                "VALUES (:id, 'Boysetsfire', 'Savage Blood', 'Boysetsfire', "
                "'Savage Blood', 'Germany', 'B9R236', '12\" Vinyl', 'Latn', "
                "'Heavy; Energetic; Melodic', '2015')"
            ),
            {"id": track_id},
        )


def _row(track_id: str) -> dict:
    with db_session() as session:
        row = session.execute(
            text(
                "SELECT releasecountry, catalognumber, media, script, "
                "mood, originalyear FROM tracks WHERE id = :id"
            ),
            {"id": track_id},
        ).fetchone()
    assert row is not None
    return dict(row._mapping)


class TestImportPreservesUnreachableTags:
    def test_import_cannot_reach_them_so_it_must_not_blank_them(self):
        """Navidrome's response carries NONE of these — the payload must omit
        them (``col=EXCLUDED.col`` would wipe the stored values otherwise)."""
        _seed("nb-preserve")

        payload = _payload(_song(musicBrainzId=REC_UUID), is_new=False)
        for col in ("releasecountry", "catalognumber", "media", "script",
                    "mood", "originalyear"):
            assert col not in payload, (
                f"{col}='' would wipe the stored value on every import"
            )

        from db.repositories.popularity_repository import save_to_db
        payload["id"] = "nb-preserve"
        assert save_to_db(payload) is True

        row = _row("nb-preserve")
        assert row["releasecountry"] == "Germany"
        assert row["catalognumber"] == "B9R236"
        assert row["media"] == '12" Vinyl'
        assert row["script"] == "Latn"
        assert row["mood"] == "Heavy; Energetic; Melodic"
        assert row["originalyear"] == "2015"


# ---------------------------------------------------------------------------
# 5. End-to-end: scan_artist_to_db collects the full raw-tag set
# ---------------------------------------------------------------------------

class _FakeImportClient:
    def __init__(self, albums, album_tracks):
        self.albums = albums
        self.album_tracks = album_tracks

    def fetch_artist_albums(self, artist_id):
        return self.albums

    def fetch_album_tracks(self, album_id):
        return {"tracks": self.album_tracks.get(album_id, []),
                "artist": "", "artistId": "", "name": "", "id": album_id}

    def get_song(self, song_id):
        return {}


class TestEndToEndRawTagCollection:
    def test_scan_collects_song_and_album_raw_tags(self):
        artist = "Boysetsfire"
        client = _FakeImportClient(
            albums=[{
                "id": "al-bsf",
                "name": "2015 - Boysetsfire",
                "songCount": 1,
                "musicBrainzId": ALB_UUID,
                "recordLabels": [{"name": "Bridge Nine Records"}],
                "releaseTypes": ["album"],
                "originalReleaseDate": {"year": 2015, "month": 9, "day": 25},
            }],
            album_tracks={
                "al-bsf": [_song(
                    id="nb-e2e",
                    musicBrainzId=REC_UUID,
                    moods=["Heavy", "Energetic", "Melodic"],
                    isrc=["DESG40800085"],
                    contributors=[
                        {"role": "mixer", "artist": {"id": "n1", "name": "Lou Giordano"}},
                        {"role": "producer", "artist": {"id": "n2", "name": "Chad Istvan"}},
                    ],
                )],
            },
        )

        scan_artist_to_db(artist, "ar-bsf", diff_mode=False, client=client)

        with db_session() as session:
            row = session.execute(text(
                "SELECT mood, writer, isrc, recordlabel, "
                "releasetype, originalyear, originaldate, recording_mbid, "
                "musicbrainz_album_mbid "
                "FROM tracks WHERE id = 'nb-e2e'"
            )).fetchone()
        assert row is not None, "the scan did not persist the track"
        got = dict(row._mapping)

        # From the SONG child:
        assert got["mood"] == "Heavy; Energetic; Melodic"
        assert got["isrc"] == "DESG40800085"
        assert got["recording_mbid"] == REC_UUID
        # mixer/producer credits collect into the writer JSONB (no own column):
        writers = str(got.get("writer") or "")
        assert "Lou Giordano" in writers, writers
        assert "Chad Istvan" in writers, writers
        # From the ALBUM object:
        assert got["recordlabel"] == "Bridge Nine Records"
        assert got["releasetype"] == "album"
        assert got["originalyear"] == "2015"
        assert got["originaldate"] == "2015-09-25"
        assert got["musicbrainz_album_mbid"] == ALB_UUID
