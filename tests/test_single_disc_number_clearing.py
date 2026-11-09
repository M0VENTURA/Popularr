"""A one-disc release must clear disc numbers, not preserve a bogus "1".

REPORTED
--------
"When updating metadata, please ensure disk numbers are updated. A single disk
album should have all disk numbers removed from tracks if they have any already
on them."

The proposal used to map a missing local disc to "1" and MusicBrainz's
``mb_disc_number`` (also 1) to "1", so the review saw "no change" and the save
kept the bogus position. It now stages an explicit CLEAR, and the album-level
``disctotal`` rides along so the save path knows the release is single-disc.
"""

from __future__ import annotations

from services.metadata.metadata_proposal_service import (
    _album_level_proposals,
    _track_proposals,
)


def _local(**overrides):
    row = {
        "id": "t1",
        "title": "Song",
        "track_number": "1",
        "disc_number": "1",
        "mbid": "",
        "writer": "",
        "musicbrainz_genres": "",
        "artist": "Artist",
        "album_artist": "Artist",
        "album": "Album",
        "musicbrainz_releasegroupid": "",
        "disctotal": "",
        "mb_ignored_fields": None,
    }
    row.update(overrides)
    return row


def _comparison(mb_disc=1):
    return [{
        "library_track_id": "t1",
        "matched": True,
        "mb_title": "Song",
        "mb_track_number": 1,
        "mb_disc_number": mb_disc,
        "mb_recording_mbid": "rec-1",
    }]


def _metadata(disc_count, mb_disc=1):
    return {
        "disc_count": disc_count,
        "disctotal": str(disc_count),
        "tracks": [{
            "mb_title": "Song",
            "mb_recording_mbid": "rec-1",
            "mb_disc_number": mb_disc,
            "mb_track_number": 1,
            "artist": "Artist",
            "writer": "",
            "musicbrainz_genres": "",
        }],
    }


def _disc_change(local, metadata, mb_disc=1):
    out = _track_proposals([local], _comparison(mb_disc), metadata)
    if not out:
        return None
    return next(
        (c for c in out[0]["changes"] if c["field"] == "disc_number"),
        None,
    )


def test_single_disc_stages_a_clear_for_an_existing_disc_number():
    change = _disc_change(_local(disc_number="1"), _metadata(1))

    assert change is not None, "a stale disc # on a one-disc release must be proposed for clearing"
    assert change["current"] == "1"
    assert change["proposed"] == ""
    assert change["clear"] is True


def test_single_disc_does_not_stage_a_clear_when_already_empty():
    assert _disc_change(_local(disc_number=""), _metadata(1)) is None


def test_multi_disc_still_proposes_the_real_position():
    change = _disc_change(_local(disc_number="1"), _metadata(2, mb_disc=2), mb_disc=2)

    assert change is not None
    assert change["proposed"] == "2"


def test_the_release_disc_total_reaches_the_form():
    """The save path's single/multi decision needs the looked-up total."""
    proposals = _album_level_proposals([_local()], {"disctotal": "1"})

    disc_total = next(
        (p for p in proposals if p["field"] == "album_disctotal"),
        None,
    )
    assert disc_total is not None
    assert disc_total["proposed"] == "1"
