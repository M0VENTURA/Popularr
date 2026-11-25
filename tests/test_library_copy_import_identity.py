"""A copied library file must not adopt the SOURCE album's track row.

Reported: "New tracks that are added to the queue, but copied from existing
files seem to be just updating the metadata of the original files". The import
resolved ``recording_mbid`` to the source album's row (the same recording), so
the target album's metadata was written onto the original track and the copied
file never became its own row.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

RECORDING = "36fb7353-f882-4924-a7f6-4d3507db1da3"
ALBUM = "Greatest Hits"


@pytest.fixture(autouse=True)
def _empty_tracks(db_session):
    db_session.execute(text("DELETE FROM tracks"))
    db_session.commit()


def _seed(db_session, track_id: str, file_path: str, *, album: str = ALBUM):
    db_session.execute(
        text("""
            INSERT INTO tracks (
                id, title, artist, album, album_artist, file_path,
                recording_mbid
            ) VALUES (
                :id, 'Freak on a Leash', 'Korn', :album, 'Korn', :path,
                :recording
            )
            ON CONFLICT DO NOTHING
        """),
        {
            "id": track_id,
            "album": album,
            "path": file_path,
            "recording": RECORDING,
        },
    )
    db_session.commit()


def _resolve(**kwargs):
    from services.downloads.download_completion_service import (
        _resolve_track_id_for_import,
    )

    return _resolve_track_id_for_import(**kwargs)


def test_a_live_copy_on_another_album_is_not_adopted(db_session, tmp_path):
    source = tmp_path / "music" / "source.mp3"
    source.parent.mkdir()
    source.write_bytes(b"audio")
    _seed(db_session, "source-row", str(source), album="Live & Rare")

    copied = tmp_path / "downloads" / "copy.mp3"
    copied.parent.mkdir()
    copied.write_bytes(b"audio")

    got = _resolve(
        file_path=str(copied),
        recording_mbid=RECORDING,
        release_mbid=None,
        artist="Korn",
        title="Freak on a Leash",
        album=ALBUM,
    )
    assert got is None, (
        "the copy adopted the source album's row, so the import would rewrite "
        "the ORIGINAL file's metadata instead of creating the new album's row"
    )


def test_a_stale_row_for_a_missing_file_is_still_adopted(db_session, tmp_path):
    """The row exists for this track but its file is gone — adopt it."""
    _seed(db_session, "stale-row", str(tmp_path / "gone.mp3"), album="Live & Rare")

    copied = tmp_path / "downloads" / "copy.mp3"
    copied.parent.mkdir(parents=True)
    copied.write_bytes(b"audio")

    got = _resolve(
        file_path=str(copied),
        recording_mbid=RECORDING,
        release_mbid=None,
        artist="Korn",
        title="Freak on a Leash",
        album=ALBUM,
    )
    assert got == "stale-row"


def test_the_same_path_is_always_the_same_row(db_session, tmp_path):
    target = tmp_path / "music" / "target.mp3"
    target.parent.mkdir()
    target.write_bytes(b"audio")
    _seed(db_session, "target-row", str(target), album=ALBUM)

    got = _resolve(
        file_path=str(target),
        recording_mbid=RECORDING,
        release_mbid=None,
        artist="Korn",
        title="Freak on a Leash",
        album=ALBUM,
    )
    assert got == "target-row"


def test_a_redownload_still_adopts_the_row_it_replaces(db_session, tmp_path):
    old = tmp_path / "music" / "old.mp3"
    old.parent.mkdir()
    old.write_bytes(b"old audio")
    _seed(db_session, "replaced-row", str(old), album=ALBUM)

    new = tmp_path / "downloads" / "new.mp3"
    new.parent.mkdir()
    new.write_bytes(b"new audio")

    got = _resolve(
        file_path=str(new),
        recording_mbid=RECORDING,
        release_mbid=None,
        artist="Korn",
        title="Freak on a Leash",
        album=ALBUM,
        replace_track_id="replaced-row",
    )
    assert got == "replaced-row"


def test_a_renamed_file_on_the_same_album_still_adopts_its_row(db_session, tmp_path):
    old = tmp_path / "music" / "old-name.mp3"
    old.parent.mkdir()
    old.write_bytes(b"audio")
    _seed(db_session, "renamed-row", str(old), album=ALBUM)

    new = tmp_path / "music" / "new-name.mp3"
    new.write_bytes(b"audio")

    got = _resolve(
        file_path=str(new),
        recording_mbid=RECORDING,
        release_mbid=None,
        artist="Korn",
        title="Freak on a Leash",
        album=ALBUM,
    )
    assert got == "renamed-row", (
        "a move within the same album must update the existing row, not insert "
        "a duplicate"
    )


def test_the_artist_title_fallback_stays_on_the_same_album(db_session, tmp_path):
    other = tmp_path / "music" / "other.mp3"
    other.parent.mkdir()
    other.write_bytes(b"audio")
    _seed(db_session, "other-row", str(other), album="Live & Rare")

    copied = tmp_path / "downloads" / "copy2.mp3"
    copied.parent.mkdir()
    copied.write_bytes(b"audio")

    got = _resolve(
        file_path=str(copied),
        recording_mbid=None,
        release_mbid=None,
        artist="Korn",
        title="Freak on a Leash",
        album=ALBUM,
    )
    assert got is None
