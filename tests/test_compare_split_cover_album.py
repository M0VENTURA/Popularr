"""The MusicBrainz Compare must match a split/covers EP by SONG, not position.

REPORTED

> "Trying to map this album, looking it up and it's matching the wrong files
>  and trying to rename them."

``Career Suicide`` is a split covers EP. The local titles carry the performer
in the title ("Trike Kill - Sk8r Boi (Avril Lavigne cover)") as well as in the
artist column, so the name match against MB's clean "Sk8r Boi" missed. The
matcher then fell back to TRACK NUMBER and paired each file with whatever the
release had at that position — proposing a rename of a file it had matched to
the wrong song.

These tests pin the two halves of the fix:

* the credited-artist prefix is stripped from the MATCH key (display title and
  rename diff keep the original), so the song is found by name;
* the name-only fuzzy fallback is rejected when both lengths are known and
  disagree, so a coincidental token overlap cannot become a rename.
"""

from __future__ import annotations

from services.enrichment.musicbrainz_service import _match_mb_tracks_to_library


def _mb(number, title, duration_ms, *, disc=1, mbid=""):
    return {
        "mb_track_number": str(number),
        "mb_disc_number": disc,
        "mb_title": title,
        "mb_recording_mbid": mbid or f"rec-{number}",
        "mb_duration": duration_ms,
        "artist": "Orpheus Omega / Trike Kill",
    }


def _local(track_id, title, artist, number, duration, *, cover=True):
    return {
        "id": track_id,
        "title": title,
        "artist": artist,
        "album_artist": "Orpheus Omega / Trike Kill",
        "disc_number": "1",
        "track_number": str(number),
        "duration": duration,
        "is_cover": 1 if cover else 0,
        "mbid": "",
        "mb_ignored_fields": "",
    }


def _by_title(comparison, title):
    return next(entry for entry in comparison if entry["mb_title"] == title)


class TestASplitCoversEpMatchesBySong:
    def _release(self):
        return [
            _mb(1, "Stacy's Mom", 304000),
            _mb(2, "All The Small Things", 224000),
            _mb(3, "Sk8r Boi", 225000),
            _mb(4, "Basket Case", 185000),
            _mb(5, "Misery Business", 205000),
            _mb(6, "Prisoner of Society", 201000),
        ]

    def _library(self):
        return [
            _local("L3", "Trike Kill - Sk8r Boi (Avril Lavigne cover)", "Trike Kill", 3, 225),
            _local("L4", "Orpheus Omega - Basket Case (Green Day cover)", "Orpheus Omega", 4, 185),
            _local("L5", "Trike Kill - Misery Business (Paramore cover)", "Trike Kill", 5, 205),
            _local("L6", "Orpheus Omega - Prisoner of Society (The Living End cover)", "Orpheus Omega", 6, 201),
        ]

    def test_the_performer_prefix_is_not_a_rename(self):
        comparison, extra = _match_mb_tracks_to_library(self._release(), self._library())

        assert extra == []
        for mb_title, local_id in (
            ("Sk8r Boi", "L3"),
            ("Basket Case", "L4"),
            ("Misery Business", "L5"),
            ("Prisoner of Society", "L6"),
        ):
            entry = _by_title(comparison, mb_title)
            assert entry["library_track_id"] == local_id, (
                f"{mb_title} was not matched to {local_id}"
            )
            assert "title" not in entry["diff_fields"], (
                f"{mb_title}: the credited-artist prefix / cover annotation was "
                "reported as a rename"
            )

    def test_a_wrong_position_match_is_not_used_when_the_name_is_elsewhere(self):
        """THE REPORTED CASE: release order differs from the file numbers."""
        release = [
            _mb(1, "All The Small Things", 224000),
            _mb(2, "Basket Case", 185000),
            _mb(3, "Stacy's Mom", 304000),
            _mb(4, "Prisoner of Society", 201000),
            _mb(5, "Sk8r Boi", 225000),
            _mb(6, "Misery Business", 205000),
        ]
        library = [
            _local("L3", "Trike Kill - Sk8r Boi (Avril Lavigne cover)", "Trike Kill", 3, 225),
        ]

        comparison, extra = _match_mb_tracks_to_library(release, library)

        stacys = _by_title(comparison, "Stacy's Mom")
        assert stacys["matched"] is False, (
            "position 3 paired Sk8r Boi's file with Stacy's Mom"
        )
        sk8r = _by_title(comparison, "Sk8r Boi")
        assert sk8r["library_track_id"] == "L3"
        assert "title" not in sk8r["diff_fields"]
        assert extra == []

    def test_a_fuzzy_overlap_with_conflicting_length_does_not_match(self):
        """A name-only candidate whose duration clearly differs is not a match."""
        release = [_mb(1, "Stacy's Mom", 304000)]
        library = [
            # Similar enough tokens to pass 0.55, but NOT the same title (so it
            # reaches the fuzzy fallback) and a different recording: 4:05 vs 3:04.
            _local("L1", "Trike Kill - Stacy's Momma (Fountains of Wayne cover)",
                   "Trike Kill", 1, 405),
        ]

        comparison, extra = _match_mb_tracks_to_library(release, library)

        assert comparison[0]["matched"] is False
        assert extra and extra[0]["library_track_id"] == "L1"
