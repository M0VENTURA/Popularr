"""Cover detection for derivative works and huge-work originals.

Reported:
* Fall Out Boy's "We Didn't Start the Fire" is modelled in MusicBrainz as a
  NEW work ``based on`` Billy Joel's work, not as a cover. The detector missed
  the work-work relation and returned not-a-cover.
* "My Heart Will Go On" is a huge work; the first page of recordings did not
  contain Céline Dion, so the writer's/score recording or a later cover won.
"""

from __future__ import annotations

from services.enrichment.cover_detector_impl import CoverDetector


def _recording(mbid, artist, *, year="2004", title="We Didn’t Start the Fire"):
    return {
        "id": mbid,
        "title": title,
        "artist-credit": [{"name": artist, "artist": {"id": artist, "name": artist}}],
        "first-release-date": year,
        "releases": [{"date": year, "title": "Release"}],
    }


class TestWorkBaseRelations:
    def test_same_titled_based_on_work_is_followed(self):
        detector = CoverDetector()
        detector.mb = type("FakeMB", (), {
            "get_work": lambda self, work_id, inc=None: {
                "id": work_id,
                "title": "We Didn’t Start the Fire",
                "work-relation-list": [{
                    "type": "based on",
                    "work": {
                        "id": "billy-work",
                        "title": "We Didn’t Start the Fire",
                    },
                }],
            }
        })()

        assert detector._work_base_ids("fob-work") == [
            {"id": "billy-work", "title": "We Didn’t Start the Fire"}
        ]

    def test_raw_json_relations_shape_is_followed(self):
        detector = CoverDetector()
        detector.mb = type("FakeMB", (), {
            "get_work": lambda self, work_id, inc=None: {
                "id": work_id,
                "title": "We Didn’t Start the Fire",
                "relations": [{
                    "type": "based on",
                    "target-type": "work",
                    "work": {
                        "id": "billy-work",
                        "title": "We Didn’t Start the Fire",
                    },
                }],
            }
        })()

        assert detector._work_base_ids("fob-work") == [
            {"id": "billy-work", "title": "We Didn’t Start the Fire"}
        ]

    def test_a_different_titled_base_is_not_treated_as_a_cover(self):
        detector = CoverDetector()
        detector.mb = type("FakeMB", (), {
            "get_work": lambda self, work_id, inc=None: {
                "id": work_id,
                "title": "New Song",
                "work-relation-list": [{
                    "type": "based on",
                    "work": {"id": "sample-source", "title": "Old Song"},
                }],
            }
        })()

        assert detector._work_base_ids("new-work") == []

    def test_derivative_work_returns_the_base_work_original(self):
        detector = CoverDetector()
        detector.mb = type("FakeMB", (), {})()
        detector._work_base_cache["fob-work"] = [
            {"id": "billy-work", "title": "We Didn’t Start the Fire"}
        ]
        detector._work_recordings = lambda work_id, limit=100: [
            _recording("fob-rec", "Fall Out Boy", year="2023")
        ]
        detector._earliest_across_pages = lambda *a, **k: {
            "artist": "Billy Joel", "year": 1989, "confidence": "medium"
        }

        result = detector._detect_via_work_id(
            "fob-work", "Fall Out Boy", exclude_mbid="fob-rec",
        )

        assert result is not None
        assert result["artist"] == "Billy Joel"
        assert result["source"] == "musicbrainz_work_based_on"
        assert result["work"] == "We Didn’t Start the Fire"

    def test_base_work_writer_recording_beats_a_later_cover(self):
        """Elton John Band covered the song later; Billy Joel is the base writer."""
        detector = CoverDetector()
        detector.mb = type("FakeMB", (), {})()
        detector._work_base_cache["fob-work"] = [
            {"id": "billy-work", "title": "We Didn’t Start the Fire"}
        ]
        detector._work_writers = lambda work_id: (
            "We Didn’t Start the Fire",
            [{"id": "billy", "name": "Billy Joel"}],
        )
        detector._work_recordings = lambda work_id, limit=100: [
            _recording("elton", "Elton John Band", year="2020"),
            _recording("billy-rec", "Billy Joel", year="1989"),
        ]
        detector._earliest_across_pages = lambda *a, **k: {
            "artist": "Elton John Band", "year": 2020, "confidence": "medium"
        }
        detector._find_original_recording = lambda *a, **k: {
            "artist": "Elton John Band", "year": 2020, "confidence": "medium"
        }

        result = detector._detect_via_work_id(
            "fob-work", "Fall Out Boy", exclude_mbid="fob-rec",
        )

        assert result is not None
        assert result["artist"] == "Billy Joel"
        assert result["source"] == "musicbrainz_work_based_on"


class TestHugeWorkTitleSearch:
    def test_title_search_finds_the_performed_original(self):
        detector = CoverDetector()
        detector.mb = type("FakeMB", (), {})()
        detector._work_base_cache["mh-work"] = []
        detector._work_recordings = lambda work_id, limit=100: [
            _recording("james", "James Horner", year="2004"),
            _recording("deja", "Deja Vu", year="2004"),
        ]
        detector._work_writers = lambda work_id: (
            "My Heart Will Go On",
            [{"id": "jh", "name": "James Horner"}, {"id": "wj", "name": "Will Jennings"}],
        )
        detector._find_original_recording = lambda *a, **k: {
            "artist": "Céline Dion", "year": 1997, "confidence": "medium"
        }
        detector._original_by_writer = lambda *a, **k: None

        result = detector._detect_via_work_id(
            "mh-work", "DragonForce", exclude_mbid="dragon-rec",
            allow_writer_check=False,
        )

        assert result is not None
        assert result["artist"] == "Céline Dion"
        assert result["source"] == "musicbrainz_work"
