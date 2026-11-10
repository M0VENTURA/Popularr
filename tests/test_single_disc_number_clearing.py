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

from services.scanning.payload_builder import build_track_payload


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


def test_the_scan_pass_does_not_stage_a_disc_clear():
    """The scan offers what it could NOT apply; the disc clear is applied by
    the import payload, so offering it here flagged every single-disc album."""
    local = _local(disc_number="1")
    metadata = _metadata(1)
    out = _track_proposals(
        [local], _comparison(), metadata, include_disc_clear=False
    )

    changes = out[0]["changes"] if out else []
    assert not any(c["field"] == "disc_number" for c in changes)


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


def _payload(disc_total, disc_number=1):
    return build_track_payload(
        track={
            "id": "t1",
            "title": "Song",
            "artist": "Artist",
            "albumArtist": "Artist",
        },
        album_name="Album",
        album_artist_value="Artist",
        canonical_artist_name="Artist",
        extracted={
            "disc_number": disc_number,
            "disctotal": disc_total,
            "title": "Song",
        },
        is_new_track=False,
    )


def test_a_single_disc_import_clears_the_disc_number():
    """This is the APPLY path — without it the scan offers a disc clear on
    every single-disc album forever."""
    payload = _payload("1")

    assert payload.get("disc_number") is None


def test_a_multi_disc_import_keeps_the_disc_number():
    payload = _payload("2", disc_number=2)

    assert payload.get("disc_number") == 2


def test_an_unknown_disc_total_does_not_clear_the_disc_number():
    payload = _payload(None, disc_number=1)

    assert payload.get("disc_number") == 1
