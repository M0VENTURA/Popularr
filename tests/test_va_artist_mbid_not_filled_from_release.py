"""The per-track artist_mbid must NOT be filled from the release-level
artist-credit on a Various Artists compilation.

REPORT (2026-10-21): "The corrections page is incorrectly matching track
artists as having the same MBID with other artists when they have the
album artist as Various Artists."

The shape of the defect
-----------------------
``_flatten_release`` built a per-track payload whose ``artist_mbid`` was

    credit_artist_mbid(Recording.get("artist-credit"))
    or credit_artist_mbid(Release.get("artist-credit"))

The fallback is the bug. MusicBrainz release tracklists frequently omit
the per-recording artist-credit (it is often empty), so the ``or`` fell
through to the release-level credit. On a Various Artists compilation
that release-level credit is the "Various Artists" MBID, and it was
written to *every* track of the album.

The duplicate-artist feature then queried

    SELECT artist, COUNT(*) FROM tracks WHERE musicbrainz_artistid = :mbid
    GROUP BY artist

with that one "Various Artists" MBID and returned every real performer on
the compilation as a "duplicate artist" sharing the same ID.

The rule these tests pin
------------------------
The per-track ``artist_mbid`` comes ONLY from the recording-level credit.
The release-level credit is the ALBUM artist and is NEVER written to the
track. When the recording-level credit is absent, ``artist_mbid`` is NULL.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest


def _recording_credit(*names: str) -> List[Dict[str, Any]]:
    """A MusicBrainz artist-credit list for a recording."""
    return [{"artist": {"id": f"mbid-{n}", "name": n}, "name": n} for n in names]


def _release_credit(*names: str) -> List[Dict[str, Any]]:
    """A MusicBrainz artist-credit list for a release."""
    return [{"artist": {"id": f"rg-mbid-{n}", "name": n}, "name": n} for n in names]


class TestArtistMbidComesFromRecordingOnly:
    def _flatten_one(
        self,
        recording_credits: List[Dict[str, Any]] | None,
        release_credits: List[Dict[str, Any]] | None,
        recording_id: str = "rec-mbid-1",
    ) -> Dict[str, Any]:
        """Drive the REAL flatten for one track and return its payload."""
        from services.enrichment.musicbrainz_service import _flatten_release

        release: Dict[str, Any] = {
            "id": "release-mbid",
            "title": "Test Album",
            "artist-credit": release_credits or [],
            "media": [
                {
                    "position": 1,
                    "track-count": 1,
                    "tracks": [
                        {
                            "position": 1,
                            "number": "1",
                            "title": "Test Track",
                            "length": 180000,
                            "recording": {
                                "id": recording_id,
                                "title": "Test Track",
                                "length": 180000,
                                "artist-credit": recording_credits or [],
                                "genres": [],
                            },
                        }
                    ],
                }
            ],
        }
        flat = _flatten_release(release, "release-mbid")
        return flat["tracks"][0]

    def test_recording_credit_wins(self):
        """A real recording-level credit is written as the per-track MBID."""
        track = self._flatten_one(
            recording_credits=_recording_credit("Creed"),
            release_credits=_release_credit("Various Artists"),
        )
        assert track["artist_mbid"] == "mbid-Creed"

    def test_missing_recording_credit_leaves_null(self):
        """THE REPORTED CASE: when the recording credit is missing, the
        release-level 'Various Artists' MBID must NOT be written to the
        per-track column."""
        track = self._flatten_one(
            recording_credits=None,
            release_credits=_release_credit("Various Artists"),
        )
        assert track["artist_mbid"] is None

    def test_empty_recording_credit_leaves_null(self):
        track = self._flatten_one(
            recording_credits=[],
            release_credits=_release_credit("Various Artists"),
        )
        assert track["artist_mbid"] is None

    def test_no_credits_at_all(self):
        track = self._flatten_one(
            recording_credits=None,
            release_credits=None,
        )
        assert track["artist_mbid"] is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
