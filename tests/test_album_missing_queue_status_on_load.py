"""Album page: the download tick must survive a page refresh.

REPORTED

> "when selecting to download a track, it shows a tick to show it's added to the
> queue, but when the page is refreshed, it doesn't show it's in the queue. …
> It should show in the download queue while it's in there, then once imported
> into the release, the banner disappears."

CAUSE

``queue_status`` was only computed by the LIVE ``refresh=1`` path. The page-load
read (``get_missing_tracks_from_db``) never selected it, so a reload always
rendered the row as "Missing" with the download button re-enabled — the tick was
pure DOM state. It is now read from ``download_queue`` on every page load.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from db.engine import db_session
from services.metadata import album_missing_service as ams
from tests.queue_schema_helpers import ensure_queue_table


@pytest.fixture()
def album():
    ensure_queue_table()
    tag = uuid.uuid4().hex[:8]
    artist = f"Test Artist {tag}"
    name = f"Album {tag}"
    with db_session() as session:
        # The suite's shared in-memory DB does not create this table for every
        # test (it is not in the bootstrap registry), so ensure it here.
        session.execute(text(
            "CREATE TABLE IF NOT EXISTS missing_album_tracks ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  artist_name TEXT NOT NULL, album_name TEXT NOT NULL,"
            "  title TEXT NOT NULL, track_number TEXT, disc_number INTEGER DEFAULT 1,"
            "  track_artist TEXT, year TEXT, release_id TEXT, recording_mbid TEXT,"
            "  duration INTEGER, ignored BOOLEAN NOT NULL DEFAULT 0,"
            "  is_bonus BOOLEAN NOT NULL DEFAULT 0,"
            "  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        ))
        session.execute(
            text("DELETE FROM missing_album_tracks WHERE artist_name = :a"),
            {"a": artist},
        )
        session.execute(
            text("DELETE FROM download_queue WHERE artist = :a"), {"a": artist}
        )
        session.execute(
            text(
                "INSERT INTO missing_album_tracks "
                "(artist_name, album_name, title, track_number, disc_number, ignored) "
                "VALUES (:a, :al, 'Never Tear Us Apart', '1', 1, FALSE),"
                "       (:a, :al, 'New Sensation', '2', 1, FALSE)"
            ),
            {"a": artist, "al": name},
        )
        session.commit()
    yield artist, name


def _queue_row(artist, album, title, number, status):
    with db_session() as session:
        session.execute(
            text(
                "INSERT INTO download_queue "
                "(artist, title, album, album_artist, track_number, disc_number, "
                " status, import_type) "
                "VALUES (:a, :t, :al, :a, :n, 1, :s, 'song')"
            ),
            {"a": artist, "t": title, "al": album, "n": number, "s": status},
        )
        session.commit()


def _missing(artist, album):
    return ams.get_missing_tracks_from_db(artist, album)["missing_tracks"]


class TestQueueStatusSurvivesAReload:
    def test_a_queued_track_reports_its_status_on_page_load(self, album):
        artist, name = album
        _queue_row(artist, name, "Never Tear Us Apart", "1", "queued")

        rows = {r["title"]: r for r in _missing(artist, name)}
        assert rows["Never Tear Us Apart"].get("queue_status") == "queued"
        assert not rows["New Sensation"].get("queue_status"), (
            "a track that is not queued must stay unbadged"
        )

    @pytest.mark.parametrize("status", ["searching", "downloading", "processing", "moving"])
    def test_in_flight_states_all_show(self, album, status):
        artist, name = album
        _queue_row(artist, name, "Never Tear Us Apart", "1", status)
        rows = {r["title"]: r for r in _missing(artist, name)}
        assert rows["Never Tear Us Apart"].get("queue_status") == status

    @pytest.mark.parametrize("status", ["imported", "in_collection", "completed"])
    def test_an_imported_track_is_no_longer_queued(self, album, status):
        """Once it is in the library the row leaves the missing list / badge."""
        artist, name = album
        _queue_row(artist, name, "Never Tear Us Apart", "1", status)
        rows = {r["title"]: r for r in _missing(artist, name)}
        assert not rows["Never Tear Us Apart"].get("queue_status")

    def test_a_failed_row_is_not_reported_as_queued(self, album):
        artist, name = album
        _queue_row(artist, name, "Never Tear Us Apart", "1", "failed")
        rows = {r["title"]: r for r in _missing(artist, name)}
        assert not rows["Never Tear Us Apart"].get("queue_status")
