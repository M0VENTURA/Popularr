"""The downloads scan must not re-log a dedupe it already knows about.

REPORTED (queue log): `Duplicate skipped: already in queue
artist='Professor Green' title='Read All About It' queue_id=5732` — every two
to five minutes, all day, for the same row.

The dedupe itself was correct; the problem was asking it LATE. The discovery
loop has two pre-checks, and both look at the **file**:

* ``find_existing_discovered_file`` — by ``file_path`` / ``found_filename``
* ``_queue_has_active_match`` — by parsed metadata

A file whose TRACK is already queued under a path neither of them recognised
fell through both and reached ``insert_queue_item``, which dedupes on
``(artist, title)`` and logs at **INFO** when it hits. Every scan. Forever.

The fix extracts that decision into :func:`find_blocking_queue_item` so there
is ONE definition of "is this track already queued?", used by the insert *and*
by the scan — the scan now counts it as ``already_in_queue`` instead of
reaching the insert at all.

The status list stays ``BLOCKING_REQUEUE_STATUSES``: ``completed`` /
``imported`` / ``failed`` deliberately do NOT block, so re-downloading a
finished track still works.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from db.repositories.queue import find_blocking_queue_item, insert_queue_item
from services.downloads import download_scan_service as dss

ARTIST = "Professor Green"
TITLE = "Read All About It"


@pytest.fixture(autouse=True)
def _queue_table(db_session):
    """Make sure ``download_queue`` exists in the test database.

    It is a mapped model, but its ``metadata`` column is JSONB, which SQLite
    cannot render — so ``DownloadQueue.__table__.create()`` raises here, which
    is why six other tests hand-write this table instead. Columns are exactly
    what ``insert_queue_item`` INSERTs.

    Without the table, the helper's own ``except`` would swallow "no such
    table" and return ``None`` — making ``test_nothing_queued_returns_none``
    pass for entirely the wrong reason.
    """
    db_session.execute(text("""
        CREATE TABLE IF NOT EXISTS download_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artist TEXT, title TEXT, album TEXT, album_artist TEXT,
            status TEXT DEFAULT 'queued', source TEXT DEFAULT 'soulseek',
            priority INTEGER DEFAULT 5,
            track_number TEXT, disc_number INTEGER,
            year TEXT, release_id TEXT, release_mbid TEXT, recording_mbid TEXT,
            duration INTEGER, import_group TEXT, import_type TEXT,
            file_path TEXT, found_filename TEXT, matched_file_path TEXT,
            created_at TEXT, updated_at TEXT
        )
    """))
    db_session.commit()
    yield db_session
    db_session.execute(text("DELETE FROM download_queue"))
    db_session.commit()


# ---------------------------------------------------------------------------
# 1. The shared decision
# ---------------------------------------------------------------------------
class TestTheSharedDecision:
    def test_nothing_queued_returns_none(self, db_session):
        assert find_blocking_queue_item(artist=ARTIST, title=TITLE) is None

    def test_a_blocking_row_is_found(self, db_session):
        added = insert_queue_item(artist=ARTIST, title=TITLE, source="soulseek")
        found = find_blocking_queue_item(artist=ARTIST, title=TITLE, source="soulseek")

        assert found, "the insert's own dedupe must answer the same question"
        assert found["id"] == added["id"]

    def test_a_terminal_status_does_not_block(self, db_session):
        """``completed`` must not stop a fresh add — that is the reported bug.

        (``insert_queue_item`` writes the status you pass for a non-local
        source, so this row really is stored as ``completed``.)
        """
        insert_queue_item(artist=ARTIST, title=TITLE, source="soulseek", status="completed")

        assert find_blocking_queue_item(artist=ARTIST, title=TITLE, source="soulseek") is None

    def test_the_locality_half_of_the_rule_holds(self, db_session):
        """A local/discovered caller matches local rows, not soulseek ones."""
        insert_queue_item(artist=ARTIST, title=TITLE, source="soulseek")

        assert find_blocking_queue_item(artist=ARTIST, title=TITLE, source="discovered") is None, (
            "a discovered file must not be blocked by a search-queued row of a "
            "different copy — the two halves of the rule exist for that reason"
        )

    def test_case_is_insensitive(self, db_session):
        insert_queue_item(artist=ARTIST, title=TITLE, source="soulseek")

        assert find_blocking_queue_item(
            artist="professor green", title="READ ALL ABOUT IT", source="soulseek",
        )


# ---------------------------------------------------------------------------
# 2. The scan asks BEFORE it inserts
# ---------------------------------------------------------------------------
class _File:
    def __init__(self, full_path, filename, rel_path):
        self.full_path = full_path
        self.filename = filename
        self.rel_path = rel_path


class TestTheScanAsksBeforeItInserts:
    def _run(self, monkeypatch, *, blocking: bool):
        inserted: list[dict] = []

        monkeypatch.setattr(
            dss, "find_existing_discovered_file", lambda **kw: None,
        )
        monkeypatch.setattr(dss, "_matches_quality_filter", lambda p: (True, ""))
        monkeypatch.setattr(
            dss, "_extract_discovered_metadata",
            lambda p, n: {"artist": ARTIST, "title": TITLE, "album": "Alive"},
        )
        monkeypatch.setattr(dss, "_queue_has_active_match", lambda m: False)
        monkeypatch.setattr(
            dss, "insert_discovered_file",
            lambda **kw: inserted.append(kw),
        )
        # The scan imports the decision inside the loop, so patch the source.
        from db.repositories import queue as queue_repo
        monkeypatch.setattr(
            queue_repo, "find_blocking_queue_item",
            lambda **kw: {"id": 5732} if blocking else None,
        )

        stats = dss.enqueue_discovered_files(
            [_File("/downloads/Alive/01 - Read All About It.flac",
                   "01 - Read All About It.flac", "Alive/01 - Read All About It.flac")]
        )
        return stats, inserted

    def test_a_known_track_is_counted_not_reinserted(self, monkeypatch):
        stats, inserted = self._run(monkeypatch, blocking=True)

        assert inserted == [], (
            "reaching the insert is what produced the every-few-minutes "
            "'Duplicate skipped' line — the scan must answer first"
        )
        assert stats["already_in_queue"] == 1, (
            "and it must still be REPORTED, just as a count rather than a log"
        )
        assert stats["queued"] == 0

    def test_an_unknown_track_still_queues(self, monkeypatch):
        """The pre-check must not swallow genuinely new files."""
        stats, inserted = self._run(monkeypatch, blocking=False)

        assert len(inserted) == 1
        assert inserted[0]["artist"] == ARTIST
        assert inserted[0]["title"] == TITLE
        assert stats["queued"] == 1
        assert stats["already_in_queue"] == 0

    def test_the_identity_values_match_the_inserts(self, monkeypatch):
        """One derivation of artist/title — the two can never disagree."""
        _, inserted = self._run(monkeypatch, blocking=False)
        call = inserted[0]

        assert call["artist"] == ARTIST and call["title"] == TITLE
        assert call["album"] == "Alive"
