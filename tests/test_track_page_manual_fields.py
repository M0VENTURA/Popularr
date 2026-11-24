"""Track page save: the newly exposed per-track identity and override fields.

The page now posts ``recording_mbid`` (the canonical column) instead of the
legacy ``mbid``, and exposes ``iswc``, ``original_title``, ``original_cover_artist``,
``lyricist``, ``album_context_live``, ``exclude_from_stats`` and
``popularity_frozen``. Each must reach the database; the recording id must also
reach its legacy aliases so the file tag and every reader agree.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from db.engine import db_session


@pytest.fixture()
def seeded():
    with db_session() as session:
        session.execute(text("DELETE FROM tracks"))
        session.execute(
            text(
                "INSERT INTO tracks (id, title, artist, album, album_artist, year, "
                "file_path, recording_mbid, mbid, musicbrainz_trackid) VALUES "
                "('tp-1', 'Old Title', 'Korn', 'Untouchables', 'Korn', '2002', "
                "'/music/korn/01.mp3', '', '', '')"
            )
        )
        session.commit()
    yield


def _row() -> dict:
    with db_session() as session:
        return dict(
            session.execute(
                text(
                    "SELECT recording_mbid, mbid, musicbrainz_trackid, iswc, "
                    "original_title, original_cover_artist, lyricist, "
                    "album_context_live, exclude_from_stats, popularity_frozen, "
                    "popularity_frozen_at FROM tracks WHERE id = 'tp-1'"
                )
            ).mappings().one()
        )


class TestTheTrackPageSave:
    async def test_the_new_fields_persist(self, client, seeded) -> None:
        resp = await client.post(
            "/track/tp-1",
            form={
                "title": "Old Title",
                "artist": "Korn",
                "album": "Untouchables",
                "recording_mbid": "rec-canonical",
                "iswc": "T-345246800-1",
                "original_title": "Original Title",
                "original_cover_artist": "Someone Else",
                "lyricist": "A Writer",
                "album_context_live": "on",
                "exclude_from_stats": "on",
                "popularity_frozen": "on",
            },
        )
        assert resp.status_code in (200, 302)

        row = _row()
        assert row["recording_mbid"] == "rec-canonical"
        assert row["mbid"] == "rec-canonical", "legacy alias left stale"
        assert row["musicbrainz_trackid"] == "rec-canonical"
        assert row["iswc"] == "T-345246800-1"
        assert row["original_title"] == "Original Title"
        assert row["original_cover_artist"] == "Someone Else"
        assert row["lyricist"] == "A Writer"
        assert bool(row["album_context_live"])
        assert bool(row["exclude_from_stats"])
        assert bool(row["popularity_frozen"])
        assert row["popularity_frozen_at"], "freezing must stamp when it happened"
