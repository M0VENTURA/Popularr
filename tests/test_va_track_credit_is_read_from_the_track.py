"""A Various-Artists track keeps ITS OWN credit — not the release's.

REPORTED

> Some tracks are still being added to the download queue as Various Artists.
> This is from the missing releases table on the album page.

ROOT CAUSE (probed against the shipped flatten)
-----------------------------------------------

``_flatten_release`` built the per-track artist as::

    recording artist-credit  →  release's joined credit  →  primary credit

and read **only** ``medium.tracks[].recording["artist-credit"]``. On a
Various-Artists release MusicBrainz puts the band on
``medium.tracks[].artist-credit`` and frequently leaves the RECORDING's credit
unset — so the chain fell straight through to ``Joined_artist_credit``
(``"Various Artists"``) for every track of the compilation.

Measured with the real function, one track, release credited "Various Artists":

    recording credit only                       artist='Jimmy Eat World'   ✅
    TRACK credit only (MB's usual VA shape)     artist='Various Artists'   ❌ ← the bug
    neither                                     artist='Various Artists'   (honest fallback)

``artist`` is what every consumer keys on: ``_match_mb_tracks_to_library``
copies it into ``mb_artist``, ``get_missing_tracks`` stores it as
``missing_album_tracks.track_artist``, ``persist_missing_from_comparison``
reads ``entry["mb_artist"]``, and the album page's queue button sends
``track_artist`` to ``/api/queue/add``. One wrong value at the flatten, and
Soulseek was asked for a band that is a label — which is why so many of those
rows show *Backed off*.

THE FIX
--------

Read the TRACK's own credit between the recording's and the release's. The
release credit stays the **last** resort: it is the right answer for a
single-artist album, and a queue row still needs a non-empty artist to search
with.

Existing ``missing_album_tracks`` rows heal themselves —
``_persist_missing_tracks`` refreshes a surviving row's ``track_artist`` when
the newly computed value differs and is not a placeholder. Queue rows already
created keep the artist they were queued with.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.enrichment import musicbrainz_service as mbs  # noqa: E402


def _release(recording_credit: str | None, track_credit: str | None) -> dict:
    """A one-track Various-Artists release shaped as MusicBrainz returns it."""
    recording = {"id": "rec-1", "title": "Lonely Day", "length": 221000}
    if recording_credit:
        recording["artist-credit"] = [
            {"name": recording_credit, "artist": {"id": "r", "name": recording_credit}}
        ]
    track = {"position": 6, "title": "Lonely Day", "length": 221000, "recording": recording}
    if track_credit:
        track["artist-credit"] = [
            {"name": track_credit, "artist": {"id": "t", "name": track_credit}}
        ]
    return {
        "id": "rel-1",
        "title": "MTV2 Handpicked, Volume 2",
        "date": "2004-01-01",
        "status": "Official",
        "artist-credit": [
            {"name": "Various Artists",
             "artist": {"id": "va", "name": "Various Artists"}},
        ],
        "media": [{"position": 1, "format": "CD", "tracks": [track]}],
        "release-group": {"id": "rg-1", "primary-type": "Album",
                          "secondary-types": ["Compilation"]},
    }


def _flat(recording_credit: str | None, track_credit: str | None) -> dict:
    return mbs._flatten_release(_release(recording_credit, track_credit), "rel-1")


class TestTheTrackCreditIsRead:
    def test_a_track_credit_wins_over_the_release_credit(self):
        """THE REPORT — MB's usual VA shape: band on the track, not the recording."""
        assert _flat(None, "Jimmy Eat World")["tracks"][0]["artist"] == "Jimmy Eat World"

    def test_the_recording_credit_still_wins_when_it_exists(self):
        """CONTROL — the more specific credit is the more correct one."""
        assert (
            _flat("Jimmy Eat World", None)["tracks"][0]["artist"]
            == "Jimmy Eat World"
        )

    def test_the_track_credit_wins_over_a_recording_credit_that_is_the_label(self):
        assert (
            _flat("Various Artists", "Jimmy Eat World")["tracks"][0]["artist"]
            == "Jimmy Eat World"
        )

    def test_the_release_credit_is_the_last_resort(self):
        """CONTROL — a queue row still needs a non-empty artist to search with."""
        assert _flat(None, None)["tracks"][0]["artist"] == "Various Artists"

    def test_the_album_artist_is_still_the_release_credit(self):
        """CONTROL — folder layout keys on the ALBUM artist, unchanged."""
        assert _flat(None, "Jimmy Eat World")["artist"] == "Various Artists"


class TestTheCreditReachesTheQueuePayloads:
    def test_the_comparison_entry_carries_it_as_mb_artist(self):
        """``get_missing_tracks`` and ``persist_missing_from_comparison`` read this."""
        flat = _flat(None, "Jimmy Eat World")
        comparison, _extra = mbs._match_mb_tracks_to_library(
            flat["tracks"],
            [{"id": "other", "title": "Something Else", "track_number": 1,
              "disc_number": 1, "mbid": "", "duration": 1}],
        )
        assert comparison[0]["mb_artist"] == "Jimmy Eat World"

    def test_the_missing_track_entry_keeps_it(self):
        """``missing_album_tracks.track_artist`` — what the album page queues.

        ``get_missing_tracks`` stores ``mt.get("artist") or artist``, so the
        flatten's value has to be a REAL performer — and
        ``_persist_missing_tracks`` only refreshes a surviving row when the new
        value is not a placeholder, so a placeholder here would also stop the
        stored rows from ever healing.
        """
        from helpers.normalization_service import is_track_artist_placeholder

        track = _flat(None, "Jimmy Eat World")["tracks"][0]

        assert (track.get("artist") or "Various Artists") == "Jimmy Eat World"
        assert not is_track_artist_placeholder(track.get("artist")), (
            "a placeholder would neither queue correctly nor let the stored "
            "row refresh itself"
        )


class TestTheChainIsInTheShippedSource:
    def test_the_track_credit_is_read_between_the_two(self):
        import inspect

        source = inspect.getsource(mbs._flatten_release)
        recording = source.index('Recording.get("artist-credit")')
        track = source.index('track.get("artist-credit")')
        release = source.index("or Joined_artist_credit or Primary_artist")
        assert recording < track < release, (
            "the chain must be recording → track → release; reading only the "
            "recording is what handed 'Various Artists' to every track of a "
            "compilation"
        )
