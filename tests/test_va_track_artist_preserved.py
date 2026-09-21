"""A Various Artists compilation must not stamp the ALBUM artist onto tracks.

Reported: "Track artist is being incorrectly overwritten on Various Artists
compilations as Various Artist. It's meant to be set as the Track Artist of the
song."

Cause: the scan's identity pass (``scan_hooks.prepare_track_context``) derives its
local ``artist`` with a LOOKUP fallback —

    artist = track["artist"] or track["album_artist"] or album_context["artist"]

— and the identity result was then written back onto the raw track dict. On a VA
compilation that stamped ``"Various Artists"`` (the ALBUM artist) onto every track
that had no artist of its own. Worse, because the metadata pass only fills an
artist that is EMPTY, it also blocked MusicBrainz from supplying the song's real
track artist — so the wrong value became permanent.

The fallback must stay strictly local to the lookups: the row's ``artist`` is only
written when the track HAS an artist of its own.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _album_context(track: dict) -> dict:
    return {
        "album": "Now That's Music",
        "artist": "Various Artists",
        "album_artist": "Various Artists",
        "is_va_compilation": True,
        "tracks": [track],
    }


class TestVaCompilationKeepsTheTrackArtist:
    def test_an_empty_track_artist_is_not_filled_with_the_album_artist(self):
        from services.popularity import scan_hooks

        track = {
            "id": "t1",
            "title": "Song One",
            "artist": "",
            "album": "Now That's Music",
            "album_artist": "Various Artists",
        }
        scan_hooks.prepare_track_context(track, _album_context(track))

        assert not track.get("artist"), (
            "an empty track artist must stay empty so MusicBrainz can fill the "
            "song's own artist — not be stamped with the album artist"
        )

    def test_a_missing_artist_key_stays_missing(self):
        from services.popularity import scan_hooks

        track = {"id": "t2", "title": "Song Two", "album": "Now That's Music",
                 "album_artist": "Various Artists"}
        scan_hooks.prepare_track_context(track, _album_context(track))

        assert not track.get("artist")

    def test_the_albums_own_artist_is_never_blanked(self):
        from services.popularity import scan_hooks

        track = {"id": "t3", "title": "Song Three", "artist": "",
                 "album": "Now That's Music", "album_artist": "Various Artists"}
        scan_hooks.prepare_track_context(track, _album_context(track))

        assert track.get("album_artist") == "Various Artists"

    def test_a_real_track_artist_is_preserved(self):
        from services.popularity import scan_hooks

        track = {"id": "t4", "title": "Song Four", "artist": "Disturbed",
                 "album": "Now That's Music", "album_artist": "Various Artists"}
        scan_hooks.prepare_track_context(track, _album_context(track))

        assert track.get("artist") == "Disturbed", (
            "the track's own artist is the track artist and must survive"
        )

    def test_a_featured_credit_does_not_fabricate_an_artist(self):
        """No own artist + a title credit must not become "Various Artists feat. X"."""
        from services.popularity import scan_hooks

        track = {"id": "t5", "title": "Song Five feat. Someone", "artist": "",
                 "album": "Now That's Music", "album_artist": "Various Artists"}
        scan_hooks.prepare_track_context(track, _album_context(track))

        assert not track.get("artist"), (
            "the album artist must not be used as the base for a moved credit"
        )
        assert track.get("featured_artist") == "Someone", (
            "the credit is still recorded, so nothing is lost"
        )


class TestTheFallbackStaysLocalToLookups:
    def test_the_lookup_artist_still_uses_the_fallback(self):
        """The fix must not break the lookups the fallback exists for."""
        from services.popularity import scan_hooks

        track = {"id": "t6", "title": "Song Six", "artist": "",
                 "album": "Now That's Music", "album_artist": "Various Artists"}
        context = scan_hooks.prepare_track_context(track, _album_context(track))
        assert context["artist"] == "Various Artists"

    def test_the_rule_is_documented_in_the_code(self):
        import inspect

        from services.popularity import scan_hooks

        source = inspect.getsource(scan_hooks.prepare_track_context)
        assert "_own_artist" in source
        assert "Various Artists" in source, (
            "the reason the fallback must not be written back should be recorded"
        )
