"""Album page: the extended release fields that previously had no inputs.

The backend already read ``album_<field>`` for these; only the form was missing
them. This pins the write path so a future template edit cannot silently drop
one again.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import text

from db.engine import db_session, get_engine


def _register_regexp_replace() -> None:
    """Make the album loader's Postgres ``regexp_replace`` call work on SQLite.

    The shared in-memory engine already holds its one StaticPool connection, so
    the conftest listener (registered for FUTURE connections) never fires here.
    Register the function on the live connection directly.
    """
    conn = get_engine().raw_connection()
    try:
        def _regexp_replace(value, pattern, repl, flags=""):
            if value is None:
                return None
            return re.sub(
                pattern, repl, str(value),
                flags=re.IGNORECASE if "i" in str(flags) else 0,
            )

        conn.driver_connection.create_function("regexp_replace", -1, _regexp_replace)
    finally:
        conn.close()


@pytest.fixture()
def album():
    _register_regexp_replace()
    with db_session() as session:
        session.execute(text("DELETE FROM tracks"))
        for track_id, number in (("ext-1", "1"), ("ext-2", "2")):
            session.execute(
                text(
                    "INSERT INTO tracks (id, title, artist, album, album_artist, "
                    "track_number, year, file_path) VALUES "
                    "(:id, :title, 'Korn', 'Untouchables', 'Korn', :num, '2002', :path)"
                ),
                {
                    "id": track_id,
                    "title": f"Track {number}",
                    "num": number,
                    "path": f"/music/Korn/Untouchables/{number}.mp3",
                },
            )
        session.commit()
    yield


def _row(track_id: str) -> dict:
    with db_session() as session:
        return dict(
            session.execute(
                text(
                    "SELECT releasestatus, asin, script, originaldate, tracktotal, "
                    "discsubtitle, language, explicitstatus, copyright "
                    "FROM tracks WHERE id = :id"
                ),
                {"id": track_id},
            ).mappings().one()
        )


class TestTheExtendedAlbumFields:
    async def test_the_form_writes_every_added_field(self, client, album) -> None:
        resp = await client.post(
            "/album/Korn/Untouchables",
            form={
                "album_title": "Untouchables",
                "album_artist": "Korn",
                "album_releasestatus": "Official",
                "album_asin": "B000066HQC",
                "album_script": "Latn",
                "album_originaldate": "2002-06-10",
                "album_tracktotal": "15",
                "album_discsubtitle": "Disc 1: The Storm",
                "album_language": "eng",
                "album_explicitstatus": "Explicit",
                "album_copyright": "2002 Sony Music",
            },
        )
        assert resp.status_code in (200, 302)

        for track_id in ("ext-1", "ext-2"):
            row = _row(track_id)
            assert row["releasestatus"] == "Official"
            assert row["asin"] == "B000066HQC"
            assert row["script"] == "Latn"
            assert row["originaldate"] == "2002-06-10"
            assert row["tracktotal"] == "15"
            assert row["discsubtitle"] == "Disc 1: The Storm"
            assert row["language"] == "eng"
            assert row["explicitstatus"] == "Explicit"
            assert row["copyright"] == "2002 Sony Music"
