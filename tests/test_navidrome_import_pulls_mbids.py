"""The Navidrome import must PULL MusicBrainz IDs — and never wipe them.

Report: *"I don't think the Navidrome import is pulling in the mbid for all
the tracks, which is what is causing the issues."*

Root cause (two halves, both proven against Navidrome's own source):

1. **Never pulled in.** Navidrome sends the recording MBID as
   ``musicBrainzId`` on every song child
   (``server/subsonic/helpers.go::osChildFromMediaFile`` →
   ``child.MusicBrainzId = mf.MbzRecordingID``) and the release MBID as
   ``musicBrainzId`` on the *album* (``AlbumID3``). The old system read it
   (``t.get("musicBrainzId", "") — Navidrome uses musicBrainzId field``);
   the new-system port lost that line, so the extractor never matched the
   key Navidrome actually sends. On top of that, ``build_track_payload``
   never mapped anything to the canonical ``recording_mbid`` column.

2. **Actively wiped.** The upsert writes ``col=EXCLUDED.col`` for every
   payload key, and the payload carried ``""`` for every MBID identity field
   whenever extraction came up empty — so each import OVERWROTE the MBIDs
   the popularity scan / download import had stored. (The old upsert used
   ``COALESCE(EXCLUDED.…, tracks.…)`` — that preserve semantics was lost in
   the port; see ``_POPULARITY_PROTECTED_COLUMNS`` for the same trap being
   fixed for scoring columns.)

This suite pins both halves at three levels: the extractor, the payload, and
a real ``scan_artist_to_db`` run against the shared test database.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from db.engine import db_session
from db.repositories.popularity_repository import save_to_db
from services.scanning.metadata_extractor import extract_track_metadata
from services.scanning.navidrome_import import scan_artist_to_db
from services.scanning.payload_builder import build_track_payload

REC_UUID = "11111111-2222-3333-4444-555555555555"
ALB_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture(autouse=True)
def _isolated_tracks():
    """Empty the shared in-memory ``tracks`` table around every test."""

    def _wipe() -> None:
        with db_session() as session:
            session.execute(text("DELETE FROM tracks"))

    _wipe()
    yield
    _wipe()


def _navidrome_song(**overrides) -> dict:
    """A song shaped exactly as Navidrome's Subsonic response sends it."""
    song = {
        "id": "nb-song",
        "title": "Song One",
        "artist": "Mbid Artist",
        "albumArtist": "Mbid Artist",
        "album": "Mbid Album",
        "path": "Mbid Artist/Mbid Album/01 - Song One.flac",
        "duration": 180,
        "track": 1,
        "disc": 1,
        "year": 1999,
        "userRating": 5,
    }
    song.update(overrides)
    return song


def _payload(track: dict, *, extracted=None, album_mbid=None, is_new_track=False) -> dict:
    return build_track_payload(
        track=track,
        album_name="Mbid Album",
        album_artist_value="Mbid Artist",
        canonical_artist_name="Mbid Artist",
        extracted=extracted,
        is_new_track=is_new_track,
        album_mbid=album_mbid,
    )


def _row(track_id: str) -> dict:
    with db_session() as session:
        row = session.execute(
            text(
                "SELECT recording_mbid, mbid, musicbrainz_trackid, "
                "musicbrainz_albumid, musicbrainz_album_mbid "
                "FROM tracks WHERE id = :tid"
            ),
            {"tid": track_id},
        ).fetchone()
    assert row is not None, f"track {track_id} not found"
    return dict(row._mapping)


def _seed(track_id: str) -> None:
    with db_session() as session:
        session.execute(
            text(
                "INSERT INTO tracks (id, artist, album, album_artist, title, "
                "recording_mbid, mbid, musicbrainz_trackid, musicbrainz_albumid, "
                "musicbrainz_album_mbid) "
                "VALUES (:id, :artist, :album, :album_artist, :title, "
                ":recording_mbid, :mbid, :trackid, :albumid, :album_mbid)"
            ),
            {
                "id": track_id,
                "artist": "Mbid Artist",
                "album": "Mbid Album",
                "album_artist": "Mbid Artist",
                "title": "Song One",
                "recording_mbid": "keep-rec",
                "mbid": "keep-mbid",
                "trackid": "keep-tid",
                "albumid": "keep-albumid",
                "album_mbid": "keep-alb-mbid",
            },
        )


# ---------------------------------------------------------------------------
# 1. The extractor reads the key Navidrome actually sends
# ---------------------------------------------------------------------------

class TestExtractorReadsNavidromeMusicBrainzId:
    def test_song_musicbrainzid_fills_recording_mbid_keys(self):
        """The regression itself: the old system read ``musicBrainzId``
        (``# Navidrome uses musicBrainzId field``); the port lost the line."""
        extracted = extract_track_metadata(
            _navidrome_song(musicBrainzId=REC_UUID)
        )

        assert extracted["musicbrainz_trackid"] == REC_UUID
        assert extracted["mbid"] == REC_UUID

    @pytest.mark.parametrize("alias", [
        "musicbrainzId",           # case variant
        "musicbrainz_recordingid", # file-tag style key
        "musicbrainz_trackid",
        "mbid",
    ])
    def test_legacy_aliases_still_work(self, alias):
        extracted = extract_track_metadata(_navidrome_song(**{alias: REC_UUID}))
        assert extracted["musicbrainz_trackid"] == REC_UUID

    def test_song_without_mbids_stays_empty(self):
        extracted = extract_track_metadata(_navidrome_song())
        assert extracted["musicbrainz_trackid"] == ""
        assert extracted["mbid"] == ""

    def test_musicbrainzid_is_never_read_as_the_album_mbid(self):
        """On a SONG, musicBrainzId is the recording id — it must not leak
        into the album column."""
        extracted = extract_track_metadata(_navidrome_song(musicBrainzId=REC_UUID))
        assert extracted["musicbrainz_albumid"] == ""


# ---------------------------------------------------------------------------
# 2. The payload: emit recording_mbid, PRESERVE stored MBIDs
# ---------------------------------------------------------------------------

class TestPayloadMusicBrainzIdentity:
    def test_recording_mbid_column_is_emitted_when_navidrome_provides_one(self):
        extracted = extract_track_metadata(_navidrome_song(musicBrainzId=REC_UUID))
        payload = _payload(_navidrome_song(), extracted=extracted)

        assert payload.get("recording_mbid") == REC_UUID
        assert payload.get("musicbrainz_trackid") == REC_UUID

    def test_empty_mbid_fields_are_omitted_not_sent_as_empty(self):
        """Omitted ⇒ the upsert's ``col=EXCLUDED.col`` never runs for that
        column ⇒ the stored MBID survives."""
        extracted = extract_track_metadata(_navidrome_song())
        payload = _payload(_navidrome_song(), extracted=extracted)

        for key in ("recording_mbid", "mbid", "musicbrainz_trackid",
                    "musicbrainz_albumid", "musicbrainz_album_mbid"):
            assert key not in payload, (
                f"{key}='' would wipe the stored value on every import"
            )

    def test_album_object_mbid_fills_both_album_columns(self):
        extracted = extract_track_metadata(_navidrome_song())
        payload = _payload(_navidrome_song(), extracted=extracted, album_mbid=ALB_UUID)

        assert payload.get("musicbrainz_album_mbid") == ALB_UUID
        assert payload.get("musicbrainz_albumid") == ALB_UUID

    def test_track_level_album_mbid_wins_over_the_album_object(self):
        extracted = extract_track_metadata(
            _navidrome_song(tags={"musicbrainz_albumid": REC_UUID})
        )
        payload = _payload(_navidrome_song(), extracted=extracted, album_mbid=ALB_UUID)

        assert payload.get("musicbrainz_album_mbid") == REC_UUID

    def test_stored_mbids_survive_a_navidrome_sync_without_mbids(self):
        """Real upsert: a sync whose Navidrome response carries no MBIDs must
        leave every stored MBID untouched."""
        _seed("nb-preserve")

        extracted = extract_track_metadata(_navidrome_song())
        assert save_to_db(_payload({"id": "nb-preserve", "title": "Song One"},
                                   extracted=extracted)) is True

        row = _row("nb-preserve")
        assert row["recording_mbid"] == "keep-rec"
        assert row["mbid"] == "keep-mbid"
        assert row["musicbrainz_trackid"] == "keep-tid"
        assert row["musicbrainz_albumid"] == "keep-albumid"
        assert row["musicbrainz_album_mbid"] == "keep-alb-mbid"

    def test_navidrome_sync_writes_the_mbids_it_does_provide(self):
        _seed("nb-write")

        extracted = extract_track_metadata(
            _navidrome_song(musicBrainzId=REC_UUID)
        )
        payload = _payload(
            {"id": "nb-write", "title": "Song One"},
            extracted=extracted,
            album_mbid=ALB_UUID,
        )
        assert save_to_db(payload) is True

        row = _row("nb-write")
        assert row["recording_mbid"] == REC_UUID
        assert row["mbid"] == REC_UUID
        assert row["musicbrainz_trackid"] == REC_UUID
        assert row["musicbrainz_album_mbid"] == ALB_UUID
        assert row["musicbrainz_albumid"] == ALB_UUID


# ---------------------------------------------------------------------------
# 3. End-to-end: scan_artist_to_db pulls the IDs out of a Navidrome response
# ---------------------------------------------------------------------------

class _FakeImportClient:
    """Minimal client for ``scan_artist_to_db`` (mirrors the removal suite)."""

    def __init__(self, albums, album_tracks):
        self.albums = albums
        self.album_tracks = album_tracks

    def fetch_artist_albums(self, artist_id):
        return self.albums

    def fetch_album_tracks(self, album_id):
        tracks = self.album_tracks.get(album_id, [])
        return {"tracks": tracks, "artist": "", "artistId": "", "name": "",
                "id": album_id}

    def get_song(self, song_id):
        return {}


class TestScanArtistToDbPullsMbids:
    def test_end_to_end_recording_and_album_mbids_land_in_the_db(self):
        artist = "Mbid Artist"
        client = _FakeImportClient(
            albums=[{
                "id": "al-mb",
                "name": "Mbid Album",
                "songCount": 1,
                "musicBrainzId": ALB_UUID,   # AlbumID3 → release MBID
            }],
            album_tracks={
                "al-mb": [{
                    "id": "nb-e2e",
                    "title": "Song One",
                    "artist": artist,
                    "path": "Mbid Artist/Mbid Album/01 - Song One.flac",
                    "musicBrainzId": REC_UUID,   # song child → recording MBID
                }],
            },
        )

        scan_artist_to_db(artist, "ar-mb", diff_mode=False, client=client)

        row = _row("nb-e2e")
        assert row["recording_mbid"] == REC_UUID, (
            "the import never stored the recording MBID Navidrome sent"
        )
        assert row["musicbrainz_trackid"] == REC_UUID
        assert row["musicbrainz_album_mbid"] == ALB_UUID, (
            "the album's musicBrainzId was never propagated to the tracks"
        )

    def test_end_to_end_import_does_not_erase_previously_stored_mbids(self):
        """Same album, but the files carry no MBID tags: the previously
        stored IDs must survive the import (the wipe regression).

        ⚠️ Navidrome must report MORE tracks than the DB holds, otherwise
        ``should_skip_cached_album`` skips the album entirely and the test
        passes without ever exercising the upsert (vacuous pass).
        """
        artist = "Mbid Artist"
        with db_session() as session:
            session.execute(
                text(
                    "INSERT INTO tracks (id, artist, album, album_artist, title, "
                    "recording_mbid, musicbrainz_album_mbid) "
                    "VALUES (:id, :artist, :album, :aa, :title, :rec, :alb)"
                ),
                {"id": "nb-old", "artist": artist, "album": "Mbid Album",
                 "aa": artist, "title": "Song One",
                 "rec": "stored-rec", "alb": "stored-alb"},
            )

        client = _FakeImportClient(
            albums=[{"id": "al-mb", "name": "Mbid Album", "songCount": 2}],
            album_tracks={
                "al-mb": [
                    {
                        "id": "nb-old",
                        "title": "Song One",
                        "artist": artist,
                        "path": "Mbid Artist/Mbid Album/01 - Song One.flac",
                        # no musicBrainzId anywhere — Navidrome has no tag to send
                    },
                    {   # forces the album to be processed (additions path)
                        "id": "nb-new",
                        "title": "Song Two",
                        "artist": artist,
                        "path": "Mbid Artist/Mbid Album/02 - Song Two.flac",
                    },
                ],
            },
        )

        scan_artist_to_db(artist, "ar-mb", diff_mode=False, client=client)

        row = _row("nb-old")
        assert row["recording_mbid"] == "stored-rec", (
            "the import wiped the stored recording MBID with an empty value"
        )
        assert row["musicbrainz_album_mbid"] == "stored-alb"

        fresh = _row("nb-new")
        assert fresh["recording_mbid"] in (None, ""), (
            "a brand-new track without MBID tags must insert cleanly"
        )
