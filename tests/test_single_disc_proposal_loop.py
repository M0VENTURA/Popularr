"""A metadata scan must not keep re-proposing a disc number it just cleared.

REPORTED

> "After doing a metadata scan, this is still showing as areas needing saving to
> fix the metadata"
>     Disc #: empty -> 1        (on every track of a single-disc album)

WHY IT LOOPED

``_track_proposals`` computed

    single_disc = include_disc_clear AND _disc_count(metadata) == 1

and then offered

    "disc_number": "" if single_disc else mb_disc_number

The scan passes ``include_disc_clear=False`` to suppress the *clear* offer, so
``single_disc`` became False and the expression fell through to
``mb_disc_number`` — which is 1 on a one-disc release. So the scan proposed
"empty -> 1" while the IMPORT path (``payload_builder``) CLEARS ``disc_number``
whenever ``disctotal == 1``.

Save wrote 1 → import cleared it → next scan proposed 1 again. Forever.

The flag was answering two different questions at once. They are now separate:
"is this a one-disc release?" is decided by MusicBrainz alone, and the flag only
controls whether the CLEAR edit is *offered*.
"""

from __future__ import annotations

import pytest

from services.metadata.metadata_proposal_service import _track_proposals


def _metadata(disc_count: int | None, mb_disc: int = 1) -> dict:
    md: dict = {"tracks": [
        {"mb_track_number": 1, "mb_disc_number": mb_disc, "mb_title": "Song",
         "mb_recording_mbid": "rec-1"},
    ]}
    if disc_count is not None:
        md["disc_count"] = disc_count
        md["disctotal"] = str(disc_count)
    return md


def _local(disc=None) -> list[dict]:
    return [{"id": "t1", "title": "Song", "track_number": "1",
             "disc_number": disc, "mbid": "rec-1"}]


_COMPARISON = [{
    "library_track_id": "t1", "matched": True, "mb_track_number": 1,
    "mb_disc_number": 1, "mb_title": "Song", "mb_recording_mbid": "rec-1",
}]


def _disc_changes(metadata, local, *, include_disc_clear: bool):
    proposals = _track_proposals(
        local, _COMPARISON, metadata, include_disc_clear=include_disc_clear
    )
    return [
        change
        for proposal in proposals
        for change in proposal.get("changes", [])
        if change.get("field") == "disc_number"
    ]


class TestAScanNeverProposesADiscOnASingleDiscAlbum:
    """THE REPORT — "Disc #: empty -> 1" must never be offered by a scan."""

    def test_the_scan_does_not_offer_empty_to_one(self) -> None:
        changes = _disc_changes(
            _metadata(1), _local(None), include_disc_clear=False
        )
        assert changes == [], (
            "the scan proposed a disc number on a one-disc album; the import "
            f"clears it, so this repeats forever: {changes}"
        )

    def test_the_clear_is_still_offered_by_the_manual_review(self) -> None:
        """CONTROL — the manual review's clear must survive the fix."""
        changes = _disc_changes(_metadata(1), _local("1"), include_disc_clear=True)
        assert len(changes) == 1
        assert changes[0]["proposed"] == ""
        assert changes[0].get("clear") is True

    def test_the_scan_does_not_offer_the_clear_either(self) -> None:
        """CONTROL — that is what the flag exists for."""
        changes = _disc_changes(_metadata(1), _local("1"), include_disc_clear=False)
        assert changes == []

    def test_unknown_disc_count_keeps_the_musicbrainz_value(self) -> None:
        """CONTROL — silence from MB is not the same as "one disc".

        With no media information we cannot claim the release is single-disc, so
        the real position must still be offered.
        """
        changes = _disc_changes(
            _metadata(None), _local(""), include_disc_clear=False
        )
        assert len(changes) == 1
        assert changes[0]["proposed"] == "1"

    def test_a_multi_disc_release_still_proposes_its_position(self) -> None:
        """CONTROL — disc 2 of a 2-CD set must still be proposed as disc 2."""
        metadata = {"disc_count": 2, "disctotal": "2", "tracks": [
            {"mb_track_number": 1, "mb_disc_number": 2, "mb_title": "Song",
             "mb_recording_mbid": "rec-1"},
        ]}
        comparison = [{
            "library_track_id": "t1", "matched": True, "mb_track_number": 1,
            "mb_disc_number": 2, "mb_title": "Song", "mb_recording_mbid": "rec-1",
        }]
        proposals = _track_proposals(
            _local("1"), comparison, metadata, include_disc_clear=False
        )
        changes = [
            change
            for proposal in proposals
            for change in proposal.get("changes", [])
            if change.get("field") == "disc_number"
        ]
        assert len(changes) == 1
        assert changes[0]["proposed"] == "2"

    def test_the_save_and_import_agree(self) -> None:
        """THE INVARIANT THAT WAS BROKEN.

        Whatever the review saves for a one-disc album must be what the import
        would store — otherwise the two fight and the review re-appears.
        ``payload_builder`` clears ``disc_number`` when ``disctotal == 1``, so
        every proposed value for a one-disc release must be empty.
        """
        for include_clear in (True, False):
            for local_disc in (None, "", "1", "2"):
                changes = _disc_changes(
                    _metadata(1), _local(local_disc),
                    include_disc_clear=include_clear,
                )
                for change in changes:
                    assert change["proposed"] == "", (
                        "a one-disc album proposed a non-empty disc number "
                        f"({change['proposed']!r}, include_clear={include_clear}, "
                        f"stored={local_disc!r}) — the import clears it, so the "
                        "review can never stay satisfied"
                    )
