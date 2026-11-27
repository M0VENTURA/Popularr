"""Album-page CSV tracklist headers.

REPORTED: the user asked for the album page to show the required CSV headers and
the columns that can be added (Track Artist, Album Artist, Track Name, Length…).

The bug behind the ask: ``Track Name`` — Exportify's own title header, and the one
a user is most likely to have — mapped to NOTHING in ``_parse_csv_tracklist``. The
header row was still detected (another cell is a known field), so every row lost
its title, was skipped, and the import reported ``success: true`` with 0 rows and
no error. These tests pin the aliases and the units.
"""

from __future__ import annotations

from services.metadata.album_missing_service import _parse_csv_tracklist


def _one(csv_text: str) -> dict:
    rows, errors = _parse_csv_tracklist(csv_text)
    assert errors == [], errors
    assert len(rows) == 1, rows
    return rows[0]


class TestTitleAliases:
    def test_track_name_is_a_title(self):
        """Exportify's header must work — it used to import zero rows."""
        row = _one("Track Name,Artist\nNever Tear Us Apart,INXS\n")
        assert row["title"] == "Never Tear Us Apart"
        assert row["artist"] == "INXS"

    def test_other_title_spellings(self):
        for header in ("Title", "Name", "Song", "Song Name", "Track Title"):
            row = _one(f"{header},Artist\nSong,INXS\n")
            assert row["title"] == "Song", header


class TestColumnsThatLandInTheTrackList:
    def test_track_artist_number_disc_and_length(self):
        row = _one(
            "Disc Number,Track Number,Title,Artist,Length,Year\n"
            "1,3,Never Tear Us Apart,INXS,3:52,1987\n"
        )
        assert row["title"] == "Never Tear Us Apart"
        assert row["artist"] == "INXS"
        assert str(row["track_number"]) == "3"
        assert int(row["disc_number"]) == 1
        assert int(row["duration"]) == 232  # 3:52
        assert row["year"] == "1987"

    def test_artist_name_s_variant(self):
        row = _one("Track Name,Artist Name(s)\nSong,INXS\n")
        assert row["artist"] == "INXS"

    def test_exportify_duration_ms_is_not_misread_as_seconds(self):
        """``Duration (ms)`` must NOT be treated as seconds (would be 184586 s)."""
        row = _one("Track Name,Artist Name(s),Duration (ms)\nSong,INXS,184586\n")
        assert row["title"] == "Song"
        # Unmapped on purpose; a null length is honest, a wrong one is not.
        assert row.get("duration") in (None, "")

    def test_album_and_album_artist_columns_are_tolerated(self):
        """They are ignored, but must not stop the title from parsing."""
        row = _one(
            "Track Name,Album Name,Album Artist,Artist\n"
            "Song,Kick,INXS,INXS\n"
        )
        assert row["title"] == "Song"
        assert row["artist"] == "INXS"

    def test_columns_in_any_order(self):
        row = _one("Length,Title,Artist\n3:52,Song,INXS\n")
        assert row["title"] == "Song"
        assert int(row["duration"]) == 232


class TestPositionalStillWorks:
    def test_disc_track_title(self):
        # NOTE: the title must not be a bare header word ("Song"/"Name"/...), or
        # the first row is mistaken for a header — a pre-existing quirk of the
        # liberal parser, unrelated to the alias fix.
        row = _one("1,3,Never Tear Us Apart\n")
        assert row["title"] == "Never Tear Us Apart"
        assert str(row["track_number"]) == "3"

    def test_exportify_hash_cell(self):
        row = _one("#5 - Song Five\n")
        assert row["title"] == "Song Five"
        assert str(row["track_number"]) == "5"
