"""Composer corruption and MBID field aliases.

REPORTED

1. A track's composer had become
   ``"Dion Rambo, Jacques Richmond, D Rambo, J Richmond • ['Dion Rambo', 'Jacques Richmond'], ..."``
   — a human string, then a Python list repr, then an escaped copy — and the
   local-correction guard then PROTECTED that garbage from Navidrome's clean
   value ("winner='existing'").

2. ``recording_mbid`` held the RELEASE-TRACK id while ``musicbrainz_trackid``
   held a different value, because the file tag ``musicbrainz_trackid`` was
   accepted as an alias for the recording id.

CAUSE (1)

The composer credit had no normaliser of its own: a Python list reaching the tag
writer was written as ``str(list)`` into the TCOM frame, and the read-side
splitter split that repr on its own commas (``_CREDIT_SPLIT_RE`` does not know
about ``'``/``[``/``]``), so the debris was written back and re-split on the next
import — nesting a level deeper every scan.
"""

from __future__ import annotations

import pytest

from services.metadata.tag_file_service import _writer_names_text
from services.scanning import reconciliation as rec
from services.scanning.metadata_extractor import split_credit_names


CORRUPTED = (
    "Dion Rambo, Jacques Richmond, D Rambo, J Richmond "
    "• ['Dion Rambo', 'Jacques Richmond', 'D Rambo', 'J Richmond']"
)


class TestDebrisDetection:
    @pytest.mark.parametrize("value", [
        CORRUPTED,
        "['Dion Rambo', 'Jacques Richmond']",
        "\\'Dion Rambo\\'",
        'Names • ["A", "B"]',
    ])
    def test_serialised_debris_is_flagged(self, value):
        assert rec._looks_like_serialised_debris(value) is True

    @pytest.mark.parametrize("value", [
        "Dion Rambo, Jacques Richmond",
        "AC/DC",
        'Tyler "T-Bone" Spencer',   # balanced quotes are a real name
        "[Unknown]",                # balanced brackets are a real name
        "",
    ])
    def test_real_values_are_not_flagged(self, value):
        assert rec._looks_like_serialised_debris(value) is False


class TestAMalformedLocalValueIsNotProtected:
    def test_a_corrupted_composer_does_not_win_the_conflict(self):
        existing = {"composer": CORRUPTED, "metadata_dirty_for_navidrome": True}
        payload = {"composer": "Dion Rambo, Jacques Richmond"}
        conflicts = rec.local_authority_conflicts(existing, payload)
        assert "composer" not in conflicts, (
            "the corrupted value must not be protected as a local correction"
        )

    def test_a_genuine_local_correction_is_still_protected(self):
        """CONTROL: a real user edit must survive the import."""
        existing = {"composer": "Dion Rambo, Jacques Richmond"}
        payload = {"composer": "Someone Else"}
        assert "composer" in rec.local_authority_conflicts(existing, payload)


class TestComposerIsRepairedOnRead:
    @pytest.mark.parametrize("value", [
        CORRUPTED,
        "['Dion Rambo', 'Jacques Richmond']",
    ])
    def test_a_list_repr_splits_into_clean_names(self, value):
        names = split_credit_names(value)
        assert names[:2] == ["Dion Rambo", "Jacques Richmond"]
        assert not any("[" in n or "'" in n for n in names), names


class TestComposerIsRepairedOnWrite:
    def test_a_python_list_is_written_as_names_not_a_repr(self):
        text = _writer_names_text(["Dion Rambo", "Jacques Richmond"])
        assert text == "Dion Rambo, Jacques Richmond"
        assert "[" not in text and "'" not in text

    def test_a_repr_string_is_repaired(self):
        text = _writer_names_text("['Dion Rambo', 'Jacques Richmond']")
        assert text == "Dion Rambo, Jacques Richmond"

    def test_the_reported_corrupted_value_heals(self):
        text = _writer_names_text(CORRUPTED)
        assert text == "Dion Rambo, Jacques Richmond, D Rambo, J Richmond", text


class TestRecordingMbidIsNotTheReleaseTrackId:
    def test_the_file_tag_trackid_is_not_read_as_a_recording_id(self):
        """``musicbrainz_trackid`` is the RELEASE-TRACK id under Picard.

        The extractor returns the recording id under ``mbid`` and
        ``musicbrainz_trackid`` (``payload_builder`` derives ``recording_mbid``
        from those); it must NOT be the release-track id.
        """
        from services.scanning import metadata_extractor as me

        extracted = me.extract_track_metadata(
            {
                "id": "song-1", "title": "Song", "artist": "Artist",
                "musicBrainzId": "rec-1",
                "musicbrainz_trackid": "releasetrack-1",
            },
            get_song=None,
        )
        assert extracted["mbid"] == "rec-1"
        assert extracted["musicbrainz_trackid"] == "rec-1"
        assert "releasetrack-1" not in (
            extracted.get("mbid"), extracted.get("musicbrainz_trackid")
        )
