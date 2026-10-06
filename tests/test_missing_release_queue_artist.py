"""Queueing a missing track must use the TRACK's artist, not the album's.

Reported:

> When items are added to the queue, they are still adding as album artist.
> When missing releases are added to the album pages, are they added along
> with track artist to the table? **Maybe that's why they aren't adding track
> artist.**

The report's diagnosis was right. Two different shape mismatches, both landing
on "Various Artists" for a compilation:

* ``/api/album/missing-tracks`` rows are keyed **`track_artist`** (named after
  the DB column). The missing-releases page read `.artist` — which that
  response does not carry — so the value was `undefined` and the fallback
  (`|| artist`) supplied the ALBUM artist. The corrections page read
  `.album_artist || .artist`: neither field exists there, so it queued with **no
  artist at all**.
* ``/api/album/title-mismatches`` rows carry BOTH ``artist`` (the performer,
  straight from ``tracks.artist``) and ``album_artist`` — but the queue payload
  preferred ``album_artist``, the wrong one on a compilation.

Everything else already got it right (the album page's missing rows, the
release download path, the CSV playlist import), which is why the bug looked
sporadic: it followed whichever button was clicked.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

MISSING_RELEASES = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "missing-releases.js"
CORRECTIONS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "artist-corrections.js"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestTheMissingReleasesPageQueuesTheTrackArtist:
    """The button in the table, and the bulk 'queue them all' action."""

    def test_the_table_row_carries_the_track_artist(self):
        source = _src(MISSING_RELEASES)

        assert 'data-artist="${esc(t.track_artist || t.artist || artist)}"' in source, (
            "the row's data-artist must read track_artist — the field the "
            "missing-tracks response actually sends"
        )

    def test_the_bulk_queue_post_prefers_the_track_artist(self):
        source = _src(MISSING_RELEASES)

        assert "artist: track.track_artist || track.artist || artist," in source, (
            "reading `.artist` on a response that has none falls through to "
            "the album artist — the reported bug"
        )

    def test_the_album_artist_still_travels_in_its_own_field(self):
        """CONTROL — folder layout needs the album artist, just not AS artist."""
        source = _src(MISSING_RELEASES)

        assert "album_artist: artist," in source


class TestTheCorrectionsPageQueuesTheTrackArtist:
    def test_missing_track_rows_use_the_canonical_field(self):
        source = _src(CORRECTIONS)

        assert "artist: track.track_artist || track.artist || artistName," in source, (
            "these rows come from /api/album/missing-tracks too, and neither "
            "`artist` nor `album_artist` exists on them — the old expression "
            "queued with no artist at all"
        )

    def test_title_mismatch_rows_prefer_the_track_artist(self):
        source = _src(CORRECTIONS)

        assert "artist: m.artist || m.album_artist," in source, (
            "title-mismatch rows carry BOTH fields; preferring album_artist "
            "queued compilations as \"Various Artists\""
        )
        assert "album_artist: m.album_artist || m.artist," in source, (
            "the album artist still travels, in the right field"
        )


class TestTheResponseActuallyCarriesTheFieldTheyRead:
    """The JS can only be right if the API sends what it names."""

    def test_missing_tracks_rows_expose_track_artist(self, db_session):
        from sqlalchemy import text
        from services.metadata import album_missing_service as ams

        # Not part of the test schema — create exactly the columns the service
        # SELECTs so the row shape is the production one.
        db_session.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS missing_album_tracks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    artist_name TEXT, album_name TEXT, title TEXT,
                    track_number TEXT, disc_number INTEGER, track_artist TEXT,
                    year TEXT, release_id TEXT, recording_mbid TEXT,
                    duration REAL, ignored BOOLEAN DEFAULT FALSE
                )
                """
            )
        )
        db_session.execute(text("DELETE FROM missing_album_tracks"))
        db_session.execute(
            text(
                """
                INSERT INTO missing_album_tracks
                    (artist_name, album_name, title, track_number, disc_number, track_artist)
                VALUES (:a, :al, 'Duality', 1, 1, 'Slipknot')
                """
            ),
            {"a": "Various Artists", "al": "MTV2 Headbangers Ball, Volume 2"},
        )
        db_session.commit()

        rows = ams.get_missing_tracks_from_db("Various Artists", "MTV2 Headbangers Ball, Volume 2")

        tracks = rows.get("missing_tracks") or []
        assert tracks, "the endpoint must return the row it was given"
        assert tracks[0].get("track_artist") == "Slipknot", (
            "the field the queue reads must be the recording's own credit"
        )
        assert "artist" not in tracks[0], (
            "documents the trap: there is no `artist` key to fall back to, so "
            "the JS must name `track_artist` explicitly"
        )

    def test_the_queue_payload_sends_a_track_number_and_release_too(self, db_session):
        """CONTROL — the fix must not have dropped the rest of the payload."""
        source = _src(MISSING_RELEASES)

        for field in (
            "title: track.title",
            "track_number: track.track_number || null",
            "release_id: track.release_id || null",
            "recording_mbid: track.recording_mbid || null",
        ):
            assert field in source, f"{field} was dropped from the queue payload"


class TestBothFilesStillParse:
    @pytest.mark.parametrize("path", [MISSING_RELEASES, CORRECTIONS])
    def test_node_accepts_the_file(self, path):
        """A syntax error here would break the whole page at load."""
        result = subprocess.run(
            ["node", "--check", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert result.returncode == 0, result.stderr[:400]
