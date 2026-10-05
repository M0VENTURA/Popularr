"""The MBID lookup must surface BOTH the track artist and the album artist.

Requested: *"MBID should have both the track and album artist available."*

The album artist was already there — `_ALBUM_FIELD_SPECS` has carried
`("album_artist", "Album Artist", "artist")` from the start. The TRACK artist
never was: `_TRACK_FIELD_SPECS` was `title, track_number, disc_number, mbid,
writer, musicbrainz_genres`, so a lookup could show who released the record but
never who performs each recording.

The data was always fetched: `_flatten_release` writes a per-track `artist`
("the recording's own credit when it has one, otherwise the release's joined
credit — NOT the primary only, a featured credit belongs on the track"). It was
simply never surfaced.

## Why this needed a guard, not just a field

Because of that fallback. On a compilation whose recording carries no credit of
its own, `Track_artist` falls back to the RELEASE credit — so a naive proposal
would offer `Various Artists` for every track and, on Apply, stamp it over each
performer. That is precisely the reported bug the suite already guards:

    tests/test_va_track_artist_preserved.py
    "Track artist is being incorrectly overwritten on Various Artists
     compilations as Various Artist. It's meant to be set as the Track Artist
     of the song."

So a proposed track artist **equal to the album artist is never offered**: it
is either the fallback (harmful to apply) or nothing to change (already caught
by the equality check). A genuine per-track credit differs from the album
credit and is proposed normally.
"""
from __future__ import annotations

from services.metadata.metadata_proposal_service import (
    _ALBUM_FIELD_SPECS,
    _TRACK_FIELD_SPECS,
    _track_proposals,
)


def _local(**overrides):
    """A library row as ``_load_local_tracks`` (SELECT *) returns it."""
    row = {
        "id": "t1",
        "title": "Song",
        "track_number": "1",
        "disc_number": "1",
        "mbid": "",
        "writer": "",
        "musicbrainz_genres": "",
        "artist": "Old Performer",
        "album_artist": "Old Performer",
        "mb_ignored_fields": None,
    }
    row.update(overrides)
    return row


def _comparison(mb_title="Song", rec="rec-1"):
    return [{
        "library_track_id": "t1",
        "matched": True,
        "mb_title": mb_title,
        "mb_track_number": 1,
        "mb_disc_number": 1,
        "mb_recording_mbid": rec,
    }]


def _metadata(artist: str, mb_title: str = "Song", rec: str = "rec-1"):
    """Release metadata whose single track carries *artist*."""
    return {
        "tracks": [{
            "mb_title": mb_title,
            "mb_recording_mbid": rec,
            "mb_disc_number": 1,
            "mb_track_number": 1,
            "musicbrainz_genres": "",
            "writer": "",
            "artist": artist,
        }],
    }


def _changes(local, metadata):
    out = _track_proposals([local], _comparison(), metadata)
    return out[0]["changes"] if out else []


def _artist_changes(local, metadata):
    return [c for c in _changes(local, metadata) if c["field"] == "artist"]


class TestBothArtistsAreAvailable:
    def test_the_album_artist_is_still_proposed(self):
        """The album half must not regress while adding the track half."""
        assert any(
            field == "album_artist" for field, _label, _key in _ALBUM_FIELD_SPECS
        ), "the album artist was already available and must stay so"

    def test_the_track_artist_is_now_a_staged_field(self):
        assert any(
            field == "artist" for field, _label, _key in _TRACK_FIELD_SPECS
        ), (
            "a lookup that can show the album credit but not the performer "
            "cannot answer 'who is actually playing on this track'"
        )


class TestTheTrackArtistIsProposed:
    def test_a_differing_track_artist_raises_a_bar(self):
        changes = _artist_changes(
            _local(artist="Old Performer", album_artist="The Band"),
            _metadata(artist="New Performer"),
        )

        assert changes, "a real performer change must be proposed"
        assert changes[0]["current"] == "Old Performer"
        assert changes[0]["proposed"] == "New Performer"
        assert changes[0]["label"] == "Track Artist"

    def test_the_same_artist_raises_nothing(self):
        """CONTROL — equality already suppressed it before this field existed."""
        assert _artist_changes(
            _local(artist="The Band", album_artist="The Band"),
            _metadata(artist="The Band"),
        ) == []


class TestTheCompilationGuard:
    """The fallback must never reach the bar."""

    def test_the_album_credit_is_not_offered_as_a_track_artist(self):
        changes = _artist_changes(
            _local(artist="Midnight Oil", album_artist="Various Artists"),
            _metadata(artist="Various Artists"),
        )

        assert changes == [], (
            "on a compilation whose recording had no credit, MusicBrainz "
            "returns the RELEASE credit — proposing it is how 'Various "
            "Artists' gets stamped onto every track"
        )

    def test_a_real_compilation_performer_is_still_offered(self):
        """The guard must not swallow the case the feature exists for."""
        changes = _artist_changes(
            _local(artist="Old Name", album_artist="Various Artists"),
            _metadata(artist="Midnight Oil"),
        )

        assert [c["proposed"] for c in changes] == ["Midnight Oil"], (
            "a compilation must still be able to correct a track's performer"
        )

    def test_a_proposal_that_BLANKS_the_artist_is_skipped(self):
        """No credit came back — proposing '' would wipe a real value."""
        assert _artist_changes(
            _local(artist="Midnight Oil", album_artist="Various Artists"),
            _metadata(artist=""),
        ) == []
