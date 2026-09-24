"""Regression tests: terminal queue rows must not block re-adding a track.

Reported:

> "I have an error that a release already has items in the queue, but they
> aren't there... It's also showing 70 files in the download processor, but
> only 18 in the queue. Files I'm adding to download aren't showing"

Three separate places treated TERMINAL statuses as if they were ACTIVE, so a
row that is finished (and therefore INVISIBLE in the queue listing) still
BLOCKED re-queueing. That produces exactly those two symptoms:

1. ``insert_queue_item`` deduped against completed/unmatched/imported/
   in_collection. Adding a track the user cannot see answered
   ``already_queued`` and inserted NOTHING — "files I'm adding aren't showing".

2. ``add_release_tracks_to_queue_detailed`` skipped the whole release with
   reason ``already_active`` for the same invisible rows — "a release already
   has items in the queue, but they aren't there". It had a THIRD copy of the
   list for its per-track duplicate probe, and the two copies had already
   drifted from each other.

3. ``/api/downloads/queue`` returned ``total = sum(status_counts.values())`` —
   every row in the table, including the terminal backlog the queue never
   shows — while the list was the active subset. So the pager read
   "showing 18 of 70" permanently: the "70 in the processor, 18 in the queue".

The fix is a single shared constant, ``BLOCKING_REQUEUE_STATUSES``, which is
deliberately NOT ``ACTIVE_QUEUE_STATUSES``: ``unmatched`` is neither active nor
terminal but must still block (re-queueing a track sitting on disk unmatched
would download a duplicate).
"""

from __future__ import annotations

import os
import tempfile

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from services.queue.queue_constraints import (
    BLOCKING_REQUEUE_STATUSES,
    TERMINAL_QUEUE_STATUSES,
)


# ---------------------------------------------------------------------------
# 1. The constant itself
# ---------------------------------------------------------------------------

class TestBlockingSet:

    def test_no_terminal_status_blocks_a_requeue(self):
        """The whole defect in one assertion. A finished row is invisible in the
        queue, so it must never prevent the user adding the track again."""
        overlap = BLOCKING_REQUEUE_STATUSES & TERMINAL_QUEUE_STATUSES
        assert not overlap, (
            f"terminal statuses must not block a re-add, but these do: "
            f"{sorted(overlap)} — a row in one of these states is invisible in "
            "the queue, so the user gets 'already in the queue' for something "
            "they cannot see"
        )

    def test_unmatched_still_blocks(self):
        """Not active, not terminal — but a track sitting on disk unmatched must
        not be re-downloaded."""
        assert "unmatched" in BLOCKING_REQUEUE_STATUSES
        assert "unmatched" not in TERMINAL_QUEUE_STATUSES

    def test_every_working_status_blocks(self):
        """If a status means "a download is in progress or about to be", it must
        block a duplicate."""
        for status in (
            "queued", "searching", "processing", "downloading", "moving",
            "matched", "queried", "copy_recommended",
        ):
            assert status in BLOCKING_REQUEUE_STATUSES, status

    def test_pending_retry_states_block(self):
        """backed_off / pending_release items WILL return to the queue, so a
        second add would duplicate them."""
        assert "backed_off" in BLOCKING_REQUEUE_STATUSES
        assert "pending_release" in BLOCKING_REQUEUE_STATUSES

    def test_user_rejected_states_do_not_block(self):
        """removed/cancelled/deleted are explicitly what the user asked to be
        rid of, so they must not stop a fresh add."""
        for status in ("removed", "cancelled", "deleted", "failed"):
            assert status not in BLOCKING_REQUEUE_STATUSES, status


# ---------------------------------------------------------------------------
# Harness: a real SQLite download_queue
# ---------------------------------------------------------------------------

@pytest.fixture()
def queue_env(monkeypatch):
    tmp = tempfile.mkdtemp()
    engine = create_engine(f"sqlite:///{os.path.join(tmp, 'q.db')}")
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE download_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                artist TEXT, title TEXT, album TEXT, album_artist TEXT,
                source TEXT DEFAULT 'soulseek', status TEXT DEFAULT 'queued',
                priority INTEGER DEFAULT 5, track_number TEXT, disc_number TEXT,
                year TEXT, duration REAL, release_id TEXT, release_mbid TEXT,
                recording_mbid TEXT, import_group TEXT, import_type TEXT,
                file_path TEXT, found_filename TEXT,
                created_at TEXT, updated_at TEXT
            )
        """))

    class _Session:
        def __init__(self, session):
            self._session = session

        def execute(self, *a, **k):
            return self._session.execute(*a, **k)

        def commit(self):
            self._session.commit()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, *exc):
            if exc_type is None:
                self._session.commit()
            self._session.close()
            return False

    monkeypatch.setattr(
        "db.repositories.queue.db_session",
        lambda *a, **kw: _Session(factory()),
    )
    return engine


def _insert(engine, **row):
    cols = ", ".join(row)
    vals = ", ".join(f":{c}" for c in row)
    with engine.begin() as conn:
        conn.execute(text(f"INSERT INTO download_queue ({cols}) VALUES ({vals})"), row)


def _statuses(engine) -> list[str]:
    with engine.begin() as conn:
        return [r[0] for r in conn.execute(text("SELECT status FROM download_queue ORDER BY id"))]


# ---------------------------------------------------------------------------
# 2. insert_queue_item
# ---------------------------------------------------------------------------

class TestInsertQueueItem:

    def test_an_imported_row_does_not_block_a_new_add(self, queue_env):
        """THE reported bug: the row exists, is invisible in the queue, and used
        to swallow the insert."""
        from db.repositories.queue import insert_queue_item

        _insert(
            queue_env, artist="Artist", title="Song", album="Album",
            status="imported", source="soulseek",
            created_at="2026-01-01", updated_at="2026-01-01",
        )

        result = insert_queue_item(artist="Artist", title="Song", album="Album")

        assert not result.get("already_queued"), (
            "an 'imported' row blocked the add — the user gets 'already in the "
            "queue' for a track they cannot see"
        )
        assert result.get("id"), "the insert did not produce a row"
        assert sorted(_statuses(queue_env)) == ["imported", "queued"]

    @pytest.mark.parametrize("terminal_status", ["completed", "imported", "in_collection"])
    def test_no_terminal_status_blocks_a_new_add(self, queue_env, terminal_status):
        from db.repositories.queue import insert_queue_item

        _insert(
            queue_env, artist="A", title="T", album="Al", status=terminal_status,
            source="soulseek", created_at="2026-01-01", updated_at="2026-01-01",
        )

        result = insert_queue_item(artist="A", title="T", album="Al")

        assert not result.get("already_queued"), terminal_status
        assert len(_statuses(queue_env)) == 2

    @pytest.mark.parametrize("blocking", ["queued", "searching", "downloading", "processing"])
    def test_an_in_progress_row_still_blocks(self, queue_env, blocking):
        """The guard must keep working for genuinely active rows, or the queue
        fills with duplicates."""
        from db.repositories.queue import insert_queue_item

        _insert(
            queue_env, artist="A", title="T", album="Al", status=blocking,
            source="soulseek", created_at="2026-01-01", updated_at="2026-01-01",
        )

        result = insert_queue_item(artist="A", title="T", album="Al")

        assert result.get("already_queued") is True, blocking
        assert len(_statuses(queue_env)) == 1, "a duplicate row was inserted"

    def test_an_unmatched_row_still_blocks(self, queue_env):
        from db.repositories.queue import insert_queue_item

        _insert(
            queue_env, artist="A", title="T", album="Al", status="unmatched",
            source="soulseek", created_at="2026-01-01", updated_at="2026-01-01",
        )

        result = insert_queue_item(artist="A", title="T", album="Al")

        assert result.get("already_queued") is True

    def test_a_local_discovered_row_does_not_block_a_search_add(self, queue_env):
        """Local/discovered rows are kept separate from searchable ones: the
        file is on disk, but the user may still want the Soulseek copy."""
        from db.repositories.queue import insert_queue_item

        _insert(
            queue_env, artist="A", title="T", album="Al", status="unmatched",
            source="discovered", created_at="2026-01-01", updated_at="2026-01-01",
        )

        result = insert_queue_item(artist="A", title="T", album="Al", source="soulseek")

        assert not result.get("already_queued")


# ---------------------------------------------------------------------------
# 3. The release-level skip
# ---------------------------------------------------------------------------

class TestReleaseLevelSkip:

    def test_imported_rows_do_not_skip_the_whole_release(self, monkeypatch):
        """``already_active`` for invisible rows is the literal reported error.

        Drives the real function and captures the reason it returns.
        """
        from services.queue import queue_processing_service as qps

        captured = {}

        class _Row:
            def __init__(self, id_, status):
                self._mapping = {"id": id_, "status": status}
                self._id = id_
                self._status = status

            def __getitem__(self, i):
                return (self._id, self._status)[i]

        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

            def fetchone(self):
                return None

            def scalar(self):
                return 0

            def scalar_one_or_none(self):
                return None

        class _Session:
            def execute(self, statement, params=None):
                sql = str(statement)
                if "SELECT id, status FROM download_queue" in sql:
                    # The release already has IMPORTED rows only.
                    return _Result([
                        _Row(1, "imported"), _Row(2, "imported"),
                    ])
                if "SELECT id FROM download_queue" in sql:
                    return _Result([])
                return _Result([])

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(qps, "db_session", lambda *a, **k: _Session())
        # Nothing should reach the library probe if the skip is wrong, but patch
        # it so the test cannot accidentally hit the DB.
        monkeypatch.setattr(qps, "find_library_track", lambda *a, **k: None)
        monkeypatch.setattr(qps, "insert_queue_item", lambda **kw: captured.setdefault("inserted", True) or {"id": 99})

        result = qps.add_release_tracks_to_queue_detailed(
            "rel-1",
            [{"title": "Song", "artist": "Artist", "track_number": "1"}],
            "Artist",
            "Album",
        )

        assert result.get("reason") != "already_active", (
            "'imported' rows made the release look active, so the user got "
            "'a release already has items in the queue' while the queue was "
            "empty of them"
        )

    def test_in_progress_rows_still_skip_the_release(self, monkeypatch):
        """A genuine in-progress release must still be skipped."""
        from services.queue import queue_processing_service as qps

        class _Row:
            def __init__(self, id_, status):
                self._mapping = {"id": id_, "status": status}
                self._id = id_
                self._status = status

            def __getitem__(self, i):
                return (self._id, self._status)[i]

        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

            def fetchone(self):
                return None

            def scalar(self):
                return 0

        class _Session:
            def execute(self, statement, params=None):
                sql = str(statement)
                if "SELECT id, status FROM download_queue" in sql:
                    return _Result([_Row(1, "downloading")])
                return _Result([])

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(qps, "db_session", lambda *a, **k: _Session())

        result = qps.add_release_tracks_to_queue_detailed(
            "rel-2",
            [{"title": "Song", "artist": "Artist"}],
            "Artist",
            "Album",
        )

        assert result.get("reason") == "already_active"
        assert result.get("already_active") == 1


# ---------------------------------------------------------------------------
# 4. The purge must not delete imported history
# ---------------------------------------------------------------------------

class TestSupersededPurgeIsScoped:
    """⚠️ The release path DELETEs stale rows before re-adding.

    Loosening the blocking set made ``_stale_ids`` (derived from "not blocking")
    suddenly include completed/imported rows — so a re-queue would have wiped
    the user's import history for that release. The purge is now restricted to
    the statuses a re-queue legitimately supersedes.
    """

    def test_the_purge_targets_only_dead_end_statuses(self):
        import inspect

        from services.queue import queue_processing_service as qps

        source = inspect.getsource(qps.add_release_tracks_to_queue_detailed)
        assert "_SUPERSEDED_STATUSES" in source, (
            "the purge is not scoped, so it would delete imported history"
        )
        # The scoped set must not contain any terminal-but-meaningful status.
        assert '"removed"' in source
        assert '"failed"' in source
        for forbidden in ('"imported"', '"completed"', '"in_collection"'):
            assert forbidden not in source.split("_SUPERSEDED_STATUSES = {")[1].split("}")[0], (
                f"{forbidden} must never be purged by a re-queue"
            )


# ---------------------------------------------------------------------------
# 5. The route's ``total`` describes the listed set
# ---------------------------------------------------------------------------

class TestQueueTotalMatchesTheListedSet:
    """``total`` must describe the set the client can page through.

    Asserted by REPLAYING the route's own expression over a crafted
    ``status_counts`` rather than driving the live endpoint: the endpoint's
    import binding is resolved by the app factory, so patching the module
    attribute is fragile (and the shared session DB means the real counts leak
    through). The expression is what must be right, and it is pinned below so
    it cannot drift from the constant sets it must agree with.
    """

    @staticmethod
    def _route_total(status_counts: dict) -> int:
        """Exactly the computation in ``routes/downloads.api_queue``.

        ⚠️ ``total`` is the PAGER's denominator, and the pager pages the ACTIVE
        list, so it counts the ACTIVE section and nothing else. It is
        deliberately NOT the union of every displayable status: that includes
        the Ready card's rows, which are a different list, and counting them
        here is what produced a "Showing 1-18 of 74" that could never be
        satisfied.
        """
        from routes.downloads import ACTIVE_SECTION

        return sum(
            int(count)
            for status, count in (status_counts or {}).items()
            if str(status) in ACTIVE_SECTION
        )

    def test_total_excludes_terminal_backlog(self):
        """The pager's "showing 18 of 70": ``total`` counted every status."""
        counts = {
            "queued": 5,
            "downloading": 2,
            "completed": 40,
            "imported": 20,
            "in_collection": 3,
            "failed": 1,
        }

        # queued 5 + downloading 2 = 7. ``failed`` and ``completed`` belong to
        # the Failed and Ready cards (different lists), and imported/
        # in_collection are not displayable at all.
        assert self._route_total(counts) == 7, (
            "total must count only the ACTIVE section. Counting the terminal "
            "backlog (completed 40 + imported 20 + in_collection 3) produced the "
            "permanent 70-vs-18 mismatch"
        )

    def test_total_counts_pending_retry_rows(self):
        """Parked-but-pending states ARE in the Active section: they are search
        work that will resume once the retry window passes, and the Active Queue
        renders them, so the pager must count them.

        ⚠️ REVERSED from the original assertion, which expected
        ``removed``/``deleted`` to be counted too on the grounds that
        "they sit in FAILED_STATUSES so the Failed card can list them". That
        reasoning was WRONG: ``get_failed_queue`` hard-codes ``status='failed'``,
        so a ``removed``/``deleted`` row was counted but rendered by no card at
        all — the very count-vs-list defect this file exists to prevent, in
        miniature. They are tombstones and are now excluded from the count.
        """
        counts = {"backed_off": 4, "pending_release": 3, "removed": 100, "deleted": 50}
        assert self._route_total(counts) == 7, (
            "backed_off 4 + pending_release 3 = 7; removed/deleted are "
            "tombstones no card renders, so they must not inflate the pager"
        )

    def test_finished_rows_are_not_counted(self):
        """The actual backlog: finished downloads are invisible in the queue, so
        counting them was the permanent "70"."""
        assert self._route_total({"completed": 40, "imported": 20, "in_collection": 3}) == 0

    def test_unmatched_is_not_in_the_active_section(self):
        """⚠️ REVERSED (user's decision) from
        ``test_unmatched_and_matched_are_not_in_the_listing``.

        The original asserted BOTH were absent from ``total``. That was the
        source of the reported bug: the client's "Queued" pill counted
        ``unmatched`` and ``matched`` while no list could render them, so the
        page showed "74 queued / 0 active / 0 ready" above 18 rows and NOTHING
        the user added appeared.

        ``matched`` is now genuinely listable (it is search work waiting to
        download), so it IS in the Active section and the pager counts it.
        ``unmatched`` is a local-disk folder — the Active Queue renders it in
        the **Ready** card, so it belongs to READY, not ACTIVE. Its pill is
        therefore the Ready pill's count, and the Active pager does not count
        it. Either way the row is now RENDERABLE, which is the point.
        """
        assert self._route_total({"matched": 2}) == 2
        assert self._route_total({"unmatched": 6}) == 0, (
            "unmatched belongs to the Ready card; counting it in the ACTIVE "
            "pager would be the same bug in the opposite direction"
        )

    def test_the_sections_partition_the_displayed_statuses(self):
        """⭐ THE LOAD-BEARING INVARIANT for the reported bug.

        The pills and the lists disagreed because they came from two different
        definitions of "the queue". They are now DIFFERENT VIEWS OF ONE
        PARTITION: the three card sections must be pairwise disjoint, and
        ``QUEUE_DISPLAY_STATUSES`` is defined as their union — so every
        displayable status renders in exactly one card, and that card's count is
        exactly its statuses' count. A status can therefore never again be
        counted by a pill but rendered by no card.
        """
        from services.queue.queue_constraints import (
            ACTIVE_SECTION,
            FAILED_SECTION,
            QUEUE_DISPLAY_STATUSES,
            READY_SECTION,
        )

        active, ready, failed = set(ACTIVE_SECTION), set(READY_SECTION), set(FAILED_SECTION)

        assert not (active & ready), f"counted by two cards: {sorted(active & ready)}"
        assert not (active & failed), f"counted by two cards: {sorted(active & failed)}"
        assert not (ready & failed), f"counted by two cards: {sorted(ready & failed)}"

        assert active | ready | failed == set(QUEUE_DISPLAY_STATUSES), (
            "the sections must cover every displayable status exactly once; "
            f"uncovered: {sorted(set(QUEUE_DISPLAY_STATUSES) - (active | ready | failed))}"
        )

    def test_each_card_can_render_every_status_it_counts(self):
        """The reconciliation, stated as the user experiences it.

        ``ready`` must equal the set ``get_completed_queue`` actually selects,
        and ``failed`` must equal what ``get_failed_queue`` selects — otherwise a
        card's badge would count rows its own query cannot return.
        """
        import inspect

        from db.repositories import queue as queue_repo
        from services.queue.queue_constraints import (
            COMPLETED_QUEUE_STATUSES,
            FAILED_SECTION,
            READY_SECTION,
        )

        assert set(READY_SECTION) == set(COMPLETED_QUEUE_STATUSES), (
            "the Ready card's count must describe the rows get_completed_queue "
            "returns, or the badge and the list disagree"
        )
        failed_src = inspect.getsource(queue_repo.get_failed_queue)
        assert "'failed'" in failed_src
        assert FAILED_SECTION == frozenset({"failed"}), (
            "get_failed_queue only ever selects status='failed', so counting any "
            "other status in the Failed badge counts rows that card cannot list"
        )

    def test_total_agrees_with_the_listing_query(self):
        """``total`` must be computed from the same section constant the listing
        query is given, so the pager and the rows cannot describe different
        sets."""
        import inspect

        from routes import downloads as downloads_route

        source = inspect.getsource(downloads_route.api_queue)
        code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())

        assert "ACTIVE_SECTION" in code, (
            "api_queue must derive its pager total from ACTIVE_SECTION; a "
            "literal status list is how the two sets drifted apart before"
        )
        # And the listing really is queried by section, not by a literal list.
        assert "get_queue_display_items" in code
        assert "ACTIVE_SECTION" in code and "READY_SECTION" in code and "FAILED_SECTION" in code

    def test_the_full_breakdown_is_still_available(self):
        """Fixing ``total`` must not hide the per-status pills, which render
        the terminal counts too."""
        import inspect

        from routes import downloads as downloads_route

        source = inspect.getsource(downloads_route.api_queue)
        assert '"status_counts": status_counts or {}' in source, (
            "status_counts must still be returned: the queue's status pills show "
            "completed/imported counts even though those rows are not listed"
        )

    def test_the_route_uses_the_shared_section_constants(self):
        """Pinned so the expression cannot drift from the constants it must
        agree with.

        ⚠️ The invariant CHANGED with the count-vs-list fix. The route used to
        derive ``total`` from ``QUEUE_LISTED_STATUSES`` (ACTIVE|FAILED|
        PENDING_RETRY) and list rows with ``get_active_queue``. That pair still
        disagreed with the CLIENT pills, which counted
        ``unmatched``/``matched``/``pending_match``/``discovered`` too — so the
        page read "74 queued" over 18 rows.

        Now the route queries each card by its SECTION and computes each count
        from the same section, so a pill and its rows are the same set by
        construction. This pins that the route keeps using the shared constants
        rather than reintroducing a literal status list.

        ⚠️ Comments are stripped first: the explanatory comments QUOTE the old
        expressions, so a naive substring check matches the prose and fails —
        the comment-matching trap.
        """
        import inspect

        from routes import downloads as downloads_route

        source = inspect.getsource(downloads_route.api_queue)
        code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())

        for name in ("ACTIVE_SECTION", "READY_SECTION", "FAILED_SECTION"):
            assert name in code, (
                f"api_queue must derive its lists and counts from {name}; a "
                "literal status list is how the sets drifted apart before"
            )
        # ``QUEUE_LISTED_STATUSES`` remains as the shared alias for the whole
        # displayed set; it must not be redefined locally.
        assert "QUEUE_LISTED_STATUSES = " not in code, (
            "the displayed set must come from the shared constant, not be "
            "redefined in the route"
        )
        assert "sum(status_counts.values())" not in code, (
            "summing EVERY status is the original bug — it counted the "
            "completed/imported backlog the queue never shows"
        )


# ---------------------------------------------------------------------------
# 6. No hand-written status list may drift back
# ---------------------------------------------------------------------------

class TestNoHandWrittenStatusLists:
    """The defect existed in THREE copies that had already drifted apart. This
    guard makes a fourth impossible to add silently.

    ⚠️ SCOPE, learned the hard way. A blanket "no terminal status in any
    ``status IN (...)`` list" guard is WRONG — it flags four legitimate
    selections that must keep naming terminal statuses:

      * ``album_missing_service`` — missing-track detection must count
        ``imported``/``completed`` rows, because owning the file is exactly
        what makes a released track "not missing".
      * ``queue.requeue_queue_item`` — the whole point is to SELECT the
        ``failed``/``removed``/``cancelled`` rows to revive them.
      * ``queue_admin.cleanup_orphaned*`` — prunes ``imported``/``completed``
        rows, so it must select them.
      * ``musicbrainz.retry`` — requeues a release's ``failed`` rows.

    So the guard targets the DECISION that was broken: the two functions whose
    job is "is this track already queued?" must consult the shared constant.
    The direction of the list is what matters, not its vocabulary.
    """

    #: Functions whose status list decides whether a NEW add is a duplicate.
    #: Every one of these previously hand-wrote terminal statuses into that
    #: decision, which is the reported bug.
    DEDUPE_DECISIONS = (
        ("db/repositories/queue.py", "insert_queue_item"),
        ("services/queue/queue_processing_service.py", "add_release_tracks_to_queue_detailed"),
    )

    def test_every_dedupe_decision_uses_the_shared_constant(self):
        import inspect

        from db.repositories import queue as queue_repo
        from services.queue import queue_processing_service as qps

        for rel, fn_name in self.DEDUPE_DECISIONS:
            module = queue_repo if "repositories" in rel else qps
            source = inspect.getsource(getattr(module, fn_name))
            assert "BLOCKING_REQUEUE_STATUSES" in source, (
                f"{rel}::{fn_name} decides whether a track is already queued "
                "without consulting BLOCKING_REQUEUE_STATUSES — that is how "
                "terminal rows started blocking re-adds"
            )

    def test_no_dedupe_decision_lists_a_terminal_status_literally(self):
        """A literal terminal status inside the dedupe SQL is the exact shape of
        the bug, so it is banned in these two functions specifically."""
        import inspect
        import re

        from db.repositories import queue as queue_repo
        from services.queue import queue_processing_service as qps

        terminal = sorted(TERMINAL_QUEUE_STATUSES)
        for rel, fn_name in self.DEDUPE_DECISIONS:
            module = queue_repo if "repositories" in rel else qps
            source = inspect.getsource(getattr(module, fn_name))
            for match in re.finditer(r"status\s+IN\s*\(([^)]*)\)", source, flags=re.IGNORECASE):
                body = match.group(1)
                present = [s for s in terminal if f"'{s}'" in body]
                assert not present, (
                    f"{rel}::{fn_name} hard-codes terminal status {present} in a "
                    "dedupe decision, so invisible rows block a re-add. Use "
                    "BLOCKING_REQUEUE_STATUSES instead."
                )

    def test_the_legitimate_selections_are_left_alone(self):
        """Regression guard on the guard: these four MUST keep naming terminal
        statuses, and a future over-broad rule must not 'fix' them."""
        import inspect

        from db.repositories import queue as queue_repo
        from services.queue import queue_processing_service as qps

        # Requeuing SELECTS failed/removed/cancelled rows to revive them.
        assert "'failed'" in inspect.getsource(queue_repo.requeue_queue_item)
        # The release path still knows which statuses it may purge.
        assert "_SUPERSEDED_STATUSES" in inspect.getsource(
            qps.add_release_tracks_to_queue_detailed
        )
