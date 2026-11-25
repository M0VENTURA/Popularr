"""A scan that applies metadata must actually write lyricist, composer and the
release-track id.

Reported: after a forced sync, the album review still listed
``MusicBrainz Release Track ID: old → new`` and ``Lyricist: empty → Brian Fair``
on every track even though the config applies metadata during the scan.

Root causes, all fixed here:
* the release-track id was read back from the RECORDING-id tag, so a file
  never carried a real value and the review re-proposed one forever;
* the album batch never carried the release-track id, and the track stage had
  no writer for it;
* ``_recording_to_metadata`` folded composer and lyricist into ``writer``, so
  neither column could ever be populated by a scan;
* nothing wrote either credit to the FILE.
"""

from __future__ import annotations

import importlib

import pytest


class _StubMbService:
    def __init__(self, roles: dict[str, list[str]]) -> None:
        self._roles = roles

    def get_credit_roles_for_recording(self, recording_mbid: str):
        return self._roles

    def get_composers_for_recording(self, recording_mbid: str):  # pragma: no cover
        return self._roles.get("writer") or []


class TestTheRecordingMetadataCarriesBothCredits:
    def test_composer_and_lyricist_are_separated_not_folded_into_writer(self):
        mbs = importlib.import_module("services.enrichment.musicbrainz_service")
        service = mbs.MusicBrainzService.__new__(mbs.MusicBrainzService)

        recording = {
            "id": "rec-1",
            "title": "Crushing Belial",
            "artist-credit": [{"name": "Shadows Fall", "artist": {"id": "art-1"}}],
            "relations": [
                {
                    "type": "performance",
                    "work": {
                        "id": "work-1",
                        "relations": [
                            {"type": "composer", "artist": {"name": "Jonathan Donais"}},
                            {"type": "lyricist", "artist": {"name": "Brian Fair"}},
                        ],
                    },
                }
            ],
        }

        meta = service._recording_to_metadata(recording, "rec-1", 1.0)

        assert meta["composer"] == "Jonathan Donais"
        assert meta["lyricist"] == "Brian Fair"
        assert "Brian Fair" in meta["writer"]


class TestTheAlbumBatchCarriesTheReleaseTrackId:
    def test_a_release_track_id_reaches_the_track_payload(self):
        mbs = importlib.import_module("services.enrichment.musicbrainz_service")
        service = mbs.MusicBrainzService.__new__(mbs.MusicBrainzService)

        release = {
            "id": "release-1",
            "title": "Of One Blood",
            "artist-credit": [{"name": "Shadows Fall", "artist": {"id": "art-1"}}],
            "release-group": {"id": "group-1", "title": "Of One Blood"},
            "media": [
                {
                    "position": 1,
                    "track-count": 1,
                    "tracks": [
                        {
                            "id": "releasetrack-1",
                            "position": 1,
                            "title": "Pain Glass Vision",
                            "recording": {
                                "id": "rec-1",
                                "title": "Pain Glass Vision",
                                "length": 48000,
                                "artist-credit": [
                                    {"name": "Shadows Fall", "artist": {"id": "art-1"}}
                                ],
                            },
                        }
                    ],
                }
            ],
        }

        flattened = mbs._flatten_release(release, "release-1")

        assert flattened["tracks"][0]["release_track_mbid"] == "releasetrack-1"
        assert flattened["tracks"][0]["mb_recording_mbid"] == "rec-1"


class TestTheExtractorNeverUsesTheRecordingId:
    def test_the_release_track_id_is_not_read_from_the_recording_tag(self):
        extractor = importlib.import_module("services.scanning.metadata_extractor")
        source = (
            extractor.extract_track_metadata.__doc__ or ""
        )
        # The call site is what matters; assert on the real source line.
        import inspect

        code = inspect.getsource(extractor)
        line = next(
            l for l in code.splitlines()
            if '"musicbrainz_releasetrackid": get_tag_value(' in l
        )
        assert "get_tag_value(\"musicbrainz_trackid\"," not in line.replace(" ", ""), (
            "the recording id must never be read as the release-track id"
        )


class TestTheScanWritesBothCredits:
    def test_the_track_stage_writes_composer_and_lyricist(self):
        import inspect

        stage = importlib.import_module("services.popularity.stages.track_stage")
        source = inspect.getsource(stage)

        assert 'for _role_field in ("composer", "lyricist"):' in source
        assert 'payload["musicbrainz_releasetrackid"] = _release_track_mbid' in source


class TestTheFilePassWritesTheLyricist:
    def test_lyricist_is_a_candidate_and_has_its_own_frame(self):
        sync = importlib.import_module("services.metadata.album_tag_sync_service")
        tfs = importlib.import_module("services.metadata.tag_file_service")

        candidates = sync._db_tag_candidates(
            {"id": "t1", "title": "Song", "lyricist": "Brian Fair"},
            perfect=False,
        )
        assert candidates.get("lyricist") == "Brian Fair"

        # A dedicated ID3 branch: the extended allow-list would have written
        # TXXX:LYRICIST while the WRITER branch deletes that desc.
        assert tfs._txxx_desc_for_field("lyricist") == "LYRICIST"
