"""Detect a remix via the MusicBrainz 'remix of' recording relation.

REPORT (2026-10-21): 'Can we add the following to detect whether a song is a
remix of an original song? Currently the track is coming up as the original
version. Many tracks on New Old Songs by Limp Bizkit are remixes. The tracks
show "Remix of".'

The app already fetched the recording-level relations (the recording ``inc``
superset includes ``recording-rels``) but never read the 'remix of' relation.
These tests pin that the relation is now read and persisted.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest


def _recording_credit(*names: str) -> List[Dict[str, Any]]:
    return [{"artist": {"id": f"mbid-{n}", "name": n}, "name": n} for n in names]


class TestRemixOfRelationHelpers:
    def test_remix_of_points_at_original(self):
        from services.enrichment.musicbrainz_service import remix_of_recording_id

        recording = {
            "id": "remix-mbid",
            "title": "Break Stuff (DJ Lethal Remix)",
            "relations": [
                {
                    "type": "remix of",
                    "recording": {"id": "orig-mbid", "title": "Break Stuff"},
                },
            ],
        }
        assert remix_of_recording_id(recording) == "orig-mbid"

    def test_original_has_no_remix_of(self):
        from services.enrichment.musicbrainz_service import remix_of_recording_id

        recording = {
            "id": "orig-mbid",
            "title": "Break Stuff",
            "relations": [
                {"type": "performance", "work": {"id": "work-mbid"}},
            ],
        }
        assert remix_of_recording_id(recording) == ""

    def test_plain_title_is_not_a_remix(self):
        from services.enrichment.musicbrainz_service import remix_of_recording_id

        recording = {"id": "x", "title": "Break Stuff (Remix)", "relations": []}
        assert remix_of_recording_id(recording) == ""

    def test_ignores_other_relation_types(self):
        from services.enrichment.musicbrainz_service import remix_of_recording_id

        recording = {
            "id": "x",
            "title": "Track",
            "relations": [
                {"type": "performance", "work": {"id": "w"}},
                {"type": "remixer", "artist": {"id": "a", "name": "DJ"}},
            ],
        }
        assert remix_of_recording_id(recording) == ""

    def test_remixers_extracted(self):
        from services.enrichment.musicbrainz_service import remixers_of_recording

        recording = {
            "id": "remix-mbid",
            "relations": [
                {"type": "remixer", "artist": {"id": "a1", "name": "DJ Lethal"}},
            ],
        }
        assert remixers_of_recording(recording) == ["DJ Lethal"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
