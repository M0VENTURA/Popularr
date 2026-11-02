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
        detector._work_recordings = lambda work_id, limit=100: (
            [_recording("fob-rec", "Fall Out Boy", year="2023")]
            if work_id == "fob-work"
            else [_recording("billy-rec", "Billy Joel", year="1989")]
        )
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
        detector._work_recordings = lambda work_id, limit=100: (
            [_recording("fob-rec", "Fall Out Boy", year="2023")]
            if work_id == "fob-work"
            else [
                _recording("elton", "Elton John Band", year="2020"),
                _recording("billy-rec", "Billy Joel", year="1989"),
            ]
        )
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

    def test_a_reciprocal_relation_does_not_cover_the_original(self):
        """RHCP's Scar Tissue work is not a cover of NTO's later remix work."""
        detector = CoverDetector()
        detector.mb = type("FakeMB", (), {})()
        detector._work_base_cache["rhcp-work"] = [
            {"id": "nto-work", "title": "Scar Tissue"}
        ]

        def _recordings(work_id, limit=100):
            if work_id == "rhcp-work":
                return [_recording("rhcp-rec", "Red Hot Chili Peppers", year="1999",
                                   title="Scar Tissue")]
            return [_recording("nto-rec", "NTO", year="2021", title="Scar Tissue")]

        detector._work_recordings = _recordings
        detector._performer_wrote_work = lambda *a, **k: True

        result = detector._detect_via_work_id(
            "rhcp-work", "Red Hot Chili Peppers", exclude_mbid="rhcp-rec",
        )

        assert result == {}, "a later work must not become the original artist"


class TestCoverActTitleHints:
    def test_kidz_bop_suffix_is_not_an_original_artist_hint(self):
        detector = CoverDetector()
        assert detector._detect_cover_hint_from_text(
            "Hey Ya! (Kidz Bop Cover)"
        ) is None

    def test_a_real_original_artist_suffix_is_still_used(self):
        detector = CoverDetector()
        assert detector._detect_cover_hint_from_text(
            "Hey Ya! (Outkast Cover)"
        ) == "Outkast"


class TestHugeWorkTitleSearch:
    def test_an_earlier_performed_original_beats_a_later_writer_score(self):
        """Céline Dion (1997, no writer relation) beats James Horner (2004)."""
        detector = CoverDetector()

        celine = _recording("celine", "Céline Dion", year="1997")
        celine["first-release-date"] = "1997-11-24"
        james = _recording("james", "James Horner", year="2004")
        james["first-release-date"] = "2004-01-01"
        james["artist-relation-list"] = [
            {"type": "writer", "artist": {"name": "James Horner"}}
        ]

        detector._search_recordings_by_title = lambda title, limit=10: [
            celine, james,
        ]
        detector._get_recording = lambda rid, inc: (
            celine if rid == "celine" else james
        )

        result = detector._find_original_recording(
            "My Heart Will Go On",
            "James Horner",
            album_artist="DragonForce",
            track_year=2019,
        )

        assert result is not None
        assert result["artist"] == "Céline Dion"

    def test_a_performed_recording_excludes_the_writer(self):
        detector = CoverDetector()
        detector._work_recordings = lambda work_id: [
            _recording("james", "James Horner", year="2004"),
            _recording("celine", "Céline Dion", year="1997"),
        ]

        result = detector._earliest_performed_recording(
            "mh-work", "DragonForce", ["James Horner", "Will Jennings"]
        )

        assert result is not None
        assert result["artist"] == "Céline Dion"

    def test_a_writer_alias_contributes_its_band_identity(self):
        """André Benjamin -> André 3000 alias -> OutKast band."""
        detector = CoverDetector()

        def _bands(artist_id):
            if artist_id == "andre":
                return [{"id": "andre3000", "name": "André 3000", "kind": "alias"}]
            if artist_id == "andre3000":
                return [{"id": "outkast", "name": "OutKast", "kind": "band"}]
            return []

        detector._artist_bands = _bands

        ranked = detector._ranked_writer_candidates(
            [{"id": "andre", "name": "André Benjamin"}]
        )

        assert ("outkast", "OutKast") in ranked

    def test_a_wrong_existing_cover_suffix_is_rebuilt(self):
        detector = CoverDetector()
        track = {
            "id": "t",
            "title": "Hey Ya! (Kidz Bop Cover)",
            "artist": "Various Artists",
            "is_cover": 1,
            "original_cover_artist": "OutKast",
        }
        update = detector._suffix_repair_update(track)
        assert update is not None
        assert update["title"] == "Hey Ya!"
        assert update["original_artist"] == "OutKast"

    def test_a_matching_cover_suffix_is_not_rewritten(self):
        detector = CoverDetector()
        track = {
            "id": "t",
            "title": "Hey Ya! (OutKast Cover)",
            "artist": "Various Artists",
            "is_cover": 1,
            "original_cover_artist": "OutKast",
        }
        assert detector._suffix_repair_update(track) is None

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
