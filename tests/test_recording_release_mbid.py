"""The recording lookup's release MBID is kept, and cover detection uses it.

Reported: *"Can you confirm that the track release MBID is fetched during the
metadata lookup for each track? Or saved during the popularity matching? Then
that MBID can be used for cover detection rather than a simple artist and track
lookup, as it's creating some false positives."*

Confirmed: the recording lookup FETCHED the releases (its ``inc`` includes
``releases``) and selected one for the release title/year — but it never
returned or saved that release's MBID. Cover detection therefore had no
deterministic route on an unmatched VA compilation and fell back to a broad
artist+title search.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _recording(release: dict) -> dict:
    return {
        "id": "rec-1",
        "title": "The One",
        "artist-credit": [
            {"name": "Bushido", "artist": {"id": "a-bushido", "name": "Bushido"}}
        ],
        "releases": [release],
        "relations": [],
    }


class TestTheLookupReturnsTheRecordingsRelease:
    def test_the_selected_release_mbid_and_group_are_returned(self):
        from services.enrichment.musicbrainz_service import MusicBrainzService

        svc = MusicBrainzService(enabled=True)
        meta = svc._recording_to_metadata(
            _recording(
                {
                    "id": "rel-track",
                    "title": "Single",
                    "date": "2020-01-01",
                    "release-group": {"id": "rg-track", "title": "Single"},
                }
            ),
            "rec-1",
            0.9,
        )

        assert meta["release_mbid"] == "rel-track"
        assert meta["release_group_mbid"] == "rg-track"

    def test_a_recording_without_releases_returns_no_release_id(self):
        from services.enrichment.musicbrainz_service import MusicBrainzService

        svc = MusicBrainzService(enabled=True)
        recording = _recording({})
        recording["releases"] = []
        meta = svc._recording_to_metadata(recording, "rec-1", 0.9)

        assert meta["release_mbid"] is None
        assert meta["release_group_mbid"] is None


class _ReleaseStub:
    def __init__(self) -> None:
        self.get_release_calls: list[str] = []

    def get_release(self, mbid, inc="", **kwargs):
        self.get_release_calls.append(str(mbid))
        return {
            "id": str(mbid),
            "media": [
                {
                    "tracks": [
                        {
                            "title": "The One",
                            "recording": {
                                "id": "rec-from-release",
                                "title": "The One",
                                "artist-credit": [
                                    {"artist": {"id": "a-bushido", "name": "Bushido"}}
                                ],
                            },
                        }
                    ]
                }
            ],
        }

    def search_recordings(self, query, limit=15):
        return []


class TestCoverDetectionPrefersTheTracksOwnRelease:
    def test_the_per_track_release_is_resolved_before_the_album_release(self):
        from services.enrichment.cover_detector_impl import CoverDetector

        stub = _ReleaseStub()
        det = CoverDetector(db_connection=None)
        det.mb = stub

        got = det._resolve_recording_mbid(
            {
                "id": "t1",
                "title": "The One",
                "artist": "Bushido",
                "album_artist": "Various Artists",
                # Both exist; the recording's OWN release must win.
                "recording_release_mbid": "rel-per-track-va",
                "musicbrainz_album_mbid": "rel-album-va",
            },
            "Various Artists",
        )

        assert got == "rec-from-release"
        assert stub.get_release_calls[0] == "rel-per-track-va"

    def test_a_per_track_release_counts_as_a_logged_identity(self):
        from services.enrichment.cover_detector_impl import CoverDetector

        assert CoverDetector._track_has_logged_identity(
            {"recording_release_mbid": "rel-per-track-va"}
        ) is True
