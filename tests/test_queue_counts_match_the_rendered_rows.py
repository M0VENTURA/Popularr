"""Regression tests for "74 queued / 0 active / 0 ready" above a list of 18.

REPORTED: the queue page's count pills said 74 queued, 0 active, 0 ready, while
the Active Queue list underneath showed 18 items — and files the user had just
added never appeared in that list at all.

ROOT CAUSE (two independent halves of the same defect):

1. **The counts and the lists came from DIFFERENT definitions of "the queue".**
   The "Queued" pill was a hand-written status list inside the CLIENT
   (``static/js/downloads.js`` / ``test_site/.../download-queue.js``) that
   included ``unmatched`` / ``matched`` / ``pending_match`` / ``discovered``.
   The list came from the server, whose query excludes all four AND drops every
   ``source IN ('local','discovered')`` row. So the pill counted a strict
   SUPERSET of what could ever be rendered — paging could never reconcile them,
   because no amount of paging produces rows that are not selected.

2. **The list used the wrong query.** ``get_active_queue`` is the WORK query:
   the slskd reaper cancels stalled transfers from it and the folder matcher
   resolves album tracks from it, so it deliberately ignores local/discovered
   sources. Reusing it for DISPLAY is what left rows counted-but-unrenderable.

THE FIX: the page's three cards are now defined as a PARTITION of every
displayable status (``ACTIVE_SECTION`` / ``READY_SECTION`` / ``FAILED_SECTION``),
and BOTH the count and the list are derived from the same section — server-side.
A pill reading "N" therefore has exactly N rows beneath it, by construction.

These tests pin the partition and the reconciliation, not the implementation.
"""

from __future__ import annotations

import os
import tempfile

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


# ---------------------------------------------------------------------------
# 1. The partition — the invariant that makes the two halves unable to disagree
# ---------------------------------------------------------------------------

class TestTheSectionsPartitionEveryDisplayedStatus:
    """A status counted by a pill MUST be rendered by exactly one card."""

    def test_the_three_sections_are_pairwise_disjoint(self):
        from services.queue.queue_constraints import (
            ACTIVE_SECTION, FAILED_SECTION, READY_SECTION,
        )

        active, ready, failed = set(ACTIVE_SECTION), set(READY_SECTION), set(FAILED_SECTION)
        assert not (active & ready), f"counted by two cards: {sorted(active & ready)}"
        assert not (active & failed), f"counted by two cards: {sorted(active & failed)}"
        assert not (ready & failed), f"counted by two cards: {sorted(ready & failed)}"

    def test_the_sections_cover_every_displayed_status(self):
        """``QUEUE_DISPLAY_STATUSES`` is DEFINED as the union, so no status can
        be counted without a card to render it."""
        from services.queue.queue_constraints import (
            ACTIVE_SECTION, FAILED_SECTION, QUEUE_DISPLAY_STATUSES, READY_SECTION,
        )

        union = set(ACTIVE_SECTION) | set(READY_SECTION) | set(FAILED_SECTION)
        assert union == set(QUEUE_DISPLAY_STATUSES)
        assert union.issubset(set(QUEUE_DISPLAY_STATUSES))

    def test_unmatched_is_displayable_and_lands_in_the_ready_card(self):
        """``unmatched`` was the headline casualty: counted by the "Queued" pill
        and rendered by no list. It must now be displayable, and it belongs to
        the Ready card (where the UI has always shown un-matched disk folders
        with a warning badge) — NOT to Active, or the same bug reappears with
        the cards swapped."""
        from services.queue.queue_constraints import (
            ACTIVE_SECTION, QUEUE_DISPLAY_STATUSES, READY_SECTION,
        )

        assert "unmatched" in QUEUE_DISPLAY_STATUSES
        assert "unmatched" in READY_SECTION
        assert "unmatched" not in ACTIVE_SECTION

    def test_matched_and_pending_match_are_displayable(self):
        """These were also counted but unrenderable. ``matched`` is search work
        waiting to download, so it belongs to the Active card."""
        from services.queue.queue_constraints import (
            ACTIVE_SECTION, QUEUE_DISPLAY_STATUSES,
        )

        for status in ("matched", "pending_match", "discovered"):
            assert status in QUEUE_DISPLAY_STATUSES, f"{status} must be renderable"
            assert status in ACTIVE_SECTION, f"{status} belongs to the Active card"

    def test_terminal_imported_statuses_are_not_displayable(self):
        """The reverse error must not creep in: a finished download is NOT queue
        work. Counting ``completed``/``imported``/``in_collection`` in the queue
        pills was the ORIGINAL "70" (the permanent terminal backlog)."""
        from services.queue.queue_constraints import QUEUE_DISPLAY_STATUSES

        for status in ("imported", "in_collection", "awaiting_selection"):
            assert status not in QUEUE_DISPLAY_STATUSES, (
                f"{status} is finished work, not queue work — counting it in the "
                "queue pills recreates the permanent-backlog mismatch"
            )

    def test_tombstones_are_not_displayable(self):
        """``removed``/``cancelled``/``deleted`` are rows the user deleted. No
        card shows them, so counting them would inflate the Failed badge with
        rows that card cannot list — the same defect in miniature.

        ``get_failed_queue`` only ever selects ``status='failed'``.
        """
        from services.queue.queue_constraints import FAILED_SECTION, QUEUE_DISPLAY_STATUSES

        for status in ("removed", "cancelled", "deleted"):
            assert status not in QUEUE_DISPLAY_STATUSES
        assert FAILED_SECTION == frozenset({"failed"})

    def test_the_ready_section_matches_the_query_that_fills_that_card(self):
        """The Ready badge must describe the rows ``get_completed_queue``
        returns, or the badge and the card disagree."""
        import inspect

        from db.repositories import queue as queue_repo
        from services.queue.queue_constraints import (
            COMPLETED_QUEUE_STATUSES, READY_SECTION,
        )

        assert set(READY_SECTION) == set(COMPLETED_QUEUE_STATUSES), (
            "the Ready card's count must be the set its own query selects"
        )
        source = inspect.getsource(queue_repo.get_completed_queue)
        assert "COMPLETED_QUEUE_STATUSES" in source, (
            "get_completed_queue must build its filter from the shared constant, "
            "not a literal list that can drift from READY_SECTION"
        )


# ---------------------------------------------------------------------------
# 2. The server's counts agree with the rows it returns
# ---------------------------------------------------------------------------

def _make_engine():
    tmp = tempfile.mkdtemp()
    return create_engine(f"sqlite:///{os.path.join(tmp, 'queue.db')}")


@pytest.fixture()
def seeded_queue(monkeypatch):
    """A ``download_queue`` with exactly the shapes from the report.

    Deliberately includes local/discovered rows — the ones the WORK query must
    keep ignoring, but which the page must still count and render. That
    asymmetry is the whole bug, so the fixture has to include both sides.
    """
    engine = _make_engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE download_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                artist TEXT, title TEXT, album TEXT, album_artist TEXT,
                status TEXT DEFAULT 'queued', source TEXT DEFAULT 'soulseek',
                track_number TEXT, disc_number INTEGER, file_path TEXT,
                import_group TEXT, release_id TEXT, created_at TEXT, updated_at TEXT
            )
        """))
        rows = [
            # ── genuinely active search work ─────────────────────────────
            ("A", "Queued One", "Al", "queued", "soulseek"),
            ("A", "Queued Two", "Al", "queued", "soulseek"),
            ("B", "Downloading", "Bl", "downloading", "soulseek"),
            ("C", "Searching", "Cl", "searching", "soulseek"),
            # ── parked but pending (still queue work) ────────────────────
            ("D", "Backed Off", "Dl", "backed_off", "soulseek"),
            # ── the statuses the pill counted and NO list could render ───
            ("E", "Matched Item", "El", "matched", "soulseek"),
            ("F", "Pending Match", "Fl", "pending_match", "soulseek"),
            # ── local disk: the WORK query must ignore these ─────────────
            ("G", "Local Folder", "Gl", "unmatched", "local"),
            ("H", "Discovered Folder", "Hl", "unmatched", "discovered"),
            # ── ready + failed cards ─────────────────────────────────────
            ("I", "Completed One", "Il", "completed", "soulseek"),
            ("J", "Failed One", "Jl", "failed", "soulseek"),
            # ── not displayable at all ───────────────────────────────────
            ("K", "Imported One", "Kl", "imported", "soulseek"),
            ("L", "Removed One", "Ll", "removed", "soulseek"),
        ]
        for artist, title, album, status, source in rows:
            conn.execute(text("""
                INSERT INTO download_queue
                    (artist, title, album, status, source, created_at, updated_at)
                VALUES (:a, :t, :al, :s, :src, '2026-09-01 00:00:00', '2026-09-01 00:00:00')
            """), {"a": artist, "t": title, "al": album, "s": status, "src": source})

    class _Session:
        def __init__(self, session):
            self._session = session

        def execute(self, *a, **kw):
            return self._session.execute(*a, **kw)

        def commit(self):
            self._session.commit()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, *exc):
            if exc_type is None:
                self._session.commit()
            self._session.close()
            return False

    session = factory()
    monkeypatch.setattr(
        "db.repositories.queue.db_session", lambda *a, **kw: _Session(session)
    )
    monkeypatch.setattr(
        "db.repositories.queue.numeric_track_number_expr", lambda s: "track_number"
    )
    return engine


class TestTheCountsMatchTheRowsReturned:
    """The user-visible contract: each pill equals its card's row count."""

    def test_every_displayed_row_is_returned_by_exactly_one_section(self, seeded_queue):
        """Walk the actual repository calls the route makes. Summing the three
        sections must equal the count of displayable rows in the table — no row
        dropped, none double-counted."""
        from db.repositories.queue import (
            get_queue_display_items, get_queue_status_counts,
        )
        from services.queue.queue_constraints import (
            ACTIVE_SECTION, FAILED_SECTION, QUEUE_DISPLAY_STATUSES, READY_SECTION,
        )

        active = get_queue_display_items(ACTIVE_SECTION, limit=500)
        ready = get_queue_display_items(READY_SECTION, limit=500)
        failed = get_queue_display_items(FAILED_SECTION, limit=500)

        ids = [r["id"] for r in active] + [r["id"] for r in ready] + [r["id"] for r in failed]
        assert len(ids) == len(set(ids)), "a row rendered in two cards"

        counts = get_queue_status_counts()
        displayable_total = sum(
            int(c) for s, c in counts.items() if str(s) in QUEUE_DISPLAY_STATUSES
        )
        assert len(ids) == displayable_total, (
            "the sections must return exactly the displayable rows: "
            f"{len(ids)} returned vs {displayable_total} displayable"
        )

    def test_the_section_counts_equal_the_rows_each_section_returns(self, seeded_queue):
        """⭐ THE REPORTED SYMPTOM, pinned directly.

        For every card: the count computed from ``status_counts`` (what the
        server sends as ``section_counts``) must equal the number of rows the
        matching query returns (what the client renders). If these disagree the
        page shows "N" above a list that cannot contain N rows — the "74 vs 18".
        """
        from db.repositories.queue import (
            get_queue_display_items, get_queue_status_counts,
        )
        from services.queue.queue_constraints import (
            ACTIVE_SECTION, FAILED_SECTION, READY_SECTION,
        )

        counts = get_queue_status_counts()

        def _section_total(section):
            return sum(
                int(c) for s, c in counts.items() if str(s) in section
            )

        for name, section in (
            ("active", ACTIVE_SECTION),
            ("ready", READY_SECTION),
            ("failed", FAILED_SECTION),
        ):
            rows = get_queue_display_items(section, limit=500)
            assert _section_total(section) == len(rows), (
                f"the {name} count ({_section_total(section)}) disagrees with the "
                f"number of rows its query returns ({len(rows)}) — this is the "
                "count-vs-list defect the user reported"
            )

    def test_local_disk_rows_are_counted_and_rendered(self, seeded_queue):
        """The specific rows that vanished. They must appear in a list, while
        the WORK query still ignores them (pinned separately below)."""
        from db.repositories.queue import (
            get_queue_display_items, get_queue_status_counts,
        )
        from services.queue.queue_constraints import QUEUE_DISPLAY_STATUSES, READY_SECTION

        counts = get_queue_status_counts()
        assert counts.get("unmatched", 0) == 2, "fixture sanity"

        ready = get_queue_display_items(READY_SECTION, limit=500)
        titles = {r["title"] for r in ready}
        assert "Local Folder" in titles, (
            "a local/discovered row was counted but rendered nowhere — the "
            "reported 'files I'm adding aren't showing'"
        )
        assert "Discovered Folder" in titles

        # And it IS inside the displayed set, so the pill's sum includes it.
        assert "unmatched" in QUEUE_DISPLAY_STATUSES

    def test_local_disk_rows_are_still_hidden_from_the_work_query(self, seeded_queue):
        """⚠️ THE BOUNDARY THE FIX MUST NOT BREAK.

        Displaying a disk folder is legitimate; treating it as an active
        transfer is not. ``get_active_queue`` feeds the slskd reaper (which
        CANCELS stalled transfers) and the folder matcher, so it must keep
        excluding local/discovered sources. That is why the fix added a
        separate DISPLAY query instead of widening this one.
        """
        from db.repositories.queue import get_active_queue

        titles = {r["title"] for r in get_active_queue(limit=500)}
        assert "Local Folder" not in titles
        assert "Discovered Folder" not in titles
        # …while real search work is still there.
        assert "Queued One" in titles

    def test_a_queued_row_added_by_the_user_is_visible_in_the_active_section(self, seeded_queue):
        """The user's own complaint: "none of the ones I've added are showing".

        A freshly added row is ``status='queued'``, which must be in the Active
        section AND returned by the Active query.
        """
        from db.repositories.queue import get_queue_display_items
        from services.queue.queue_constraints import ACTIVE_SECTION

        active = get_queue_display_items(ACTIVE_SECTION, limit=500)
        titles = {r["title"] for r in active}
        assert "Queued One" in titles
        assert "Queued Two" in titles


# ---------------------------------------------------------------------------
# 3. The pills' contract with the cards
# ---------------------------------------------------------------------------

class TestEachRenderedPillMapsToACard:
    """A rendered count must be a card's row count — nothing in between. That
    gap (a number with no rows to explain it) IS the reported bug."""

    @staticmethod
    def _render_body() -> str:
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1] / "static" / "js" / "downloads.js"
        ).read_text(encoding="utf-8")
        start = source.index("async function renderQueuePage")
        end = source.index("function renderQueueList", start)
        return source[start:end]

    def test_every_card_pill_is_fed_from_a_section_count(self):
        """The card pills must read the server's partition, not a status list."""
        body = self._render_body()

        for pill in (
            "queueActiveCount", "queueCompletedCount", "queueFailedCount",
            "statQueuedNum", "statDownloadingNum", "statCompletedNum", "statFailedNum",
        ):
            line = next(
                (ln for ln in body.splitlines() if f"setNum('{pill}'" in ln), None
            )
            assert line is not None, f"{pill} is not filled by renderQueuePage"
            assert "section" in line.lower() or "downloadingCount" in line, (
                f"{pill} is filled from {line.strip()!r} — a card pill must read "
                "the server's section counts so its number matches its rows"
            )

    def test_the_imported_pill_is_a_documented_statistic_not_a_card(self):
        """⭐ Pin the ONE exception, so it is a decision rather than an oversight.

        ``imported`` rows are finished work that no queue card lists. The pill is
        therefore a lifetime "moved to library" STATISTIC. It must stay
        documented as such and must NOT become displayable — if it did, the pill
        would count rows no card can render, which is the defect being fixed.
        """
        from services.queue.queue_constraints import QUEUE_DISPLAY_STATUSES

        body = self._render_body()
        assert "STATISTIC" in body, (
            "the Imported pill is the one non-card count; it must be documented "
            "as a statistic in the code or it reads as unlisted queue work"
        )
        assert "imported" not in QUEUE_DISPLAY_STATUSES, (
            "making 'imported' displayable would make the Imported pill count "
            "rows no card renders — the same defect in a new place"
        )

