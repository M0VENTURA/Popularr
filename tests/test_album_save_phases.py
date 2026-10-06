"""The album save's per-track writes are three explicit phases.

Step 1 of splitting the save into separately-confirmed phases (so a modal can
report progress per phase and retry only the one that failed). The extraction
is deliberately **behaviour-preserving**: the route's loop still calls the
three writes in the same order, for the same tracks, with the same counters.

## The invariant that must survive any split

``routes/ui_routes.py`` (the loop):

    # album-level values first …
    for field, value in release_values.items():
        if value: payload[field] = value
    # … then the user's per-track review on top
    _staged_for_track = _staged_updates.get(str(track_id))

A per-track value the user reviewed and kept must WIN over the album-wide
default. Nothing pinned that ordering before this change — a refactor could
reverse it and every existing suite would stay green — so it is asserted here
by position.

## Why genres stay one phase

The genre write touches **both** stores (the JSONB columns and the file's
genre tag) and both live inside one block. Splitting them across a DB pass and
a file pass would silently drop the file half — the kind of defect that only
shows up as "my genre edit didn't stick to the files".
"""

from __future__ import annotations

from pathlib import Path

import pytest

import routes.ui_routes as ui

REPO_ROOT = Path(__file__).resolve().parent.parent
UI_SOURCE = (REPO_ROOT / "routes" / "ui_routes.py").read_text(encoding="utf-8")
#: The save handler only — the module has a second writer of ``payload`` and
#: helpers whose names repeat these patterns, so whole-file searches lied.
ROUTE_SOURCE = UI_SOURCE[UI_SOURCE.index("async def album_detail("):]


# ===========================================================================
# 1. Structure — the ordering that a split must not break
# ===========================================================================
class TestThePhasesRunInAnOrderThatPreservesTheReview:
    def test_album_wide_values_are_written_before_the_staged_review(self):
        """Pin the field that ACTUALLY collides — ``writer``.

        ``writer`` is in ``_STAGED_WRITABLE`` and is set album-wide by
        ``payload["writer"] = track_composer``; if the review ran first, saving
        a composer would overwrite the writer the user just kept. (The
        ``release_values`` loop sits after the review, but it writes a
        disjoint set — the release/extended fields — so it is not the
        invariant.)
        """
        album_first = ROUTE_SOURCE.index('payload["writer"] = track_composer')
        staged_next = ROUTE_SOURCE.index("_staged_for_track = _staged_updates.get(")

        assert album_first < staged_next, (
            "the staged per-track review must be applied AFTER the album-wide "
            "values it can override — otherwise an album-level value would "
            "overwrite a value the user explicitly kept during the review"
        )

    def test_the_three_phases_are_called_in_one_pass(self):
        genres = ROUTE_SOURCE.index("_apply_album_track_genres(")
        persist = ROUTE_SOURCE.index("_persist_album_track_payload(track_id, payload)")
        # The FILE phase no longer sits inside the per-track loop: it became
        # ONE concurrent phase after it (Cloudflare 524 — N sequential disk
        # passes outlived the origin timeout), so the route's own call site is
        # the gather and the helper below is what touches the file. What must
        # not change is the ORDER of the phases relative to the database.
        file_phase = ROUTE_SOURCE.index("_write_album_track_files_concurrently(")

        assert genres < persist < file_phase, (
            "the loop's write order changed (genres → persist → file tags)"
        )

    def test_the_file_phase_still_writes_through_the_phase_helper(self):
        """Concurrency must not invent a second writer.

        The gather runs the SAME phase the loop used to call, off the event
        loop in a worker thread — if it ever opened the file itself, the
        tag-write policy gates (``write_tags_to_file``, ``ratings_only``,
        ``fill_missing_only``) would be bypassed for album saves.
        """
        # Compared whitespace-insensitively: the call wraps across lines, and a
        # reformat must not turn a passing check into a false failure.
        flat = " ".join(UI_SOURCE.split())
        assert "asyncio.to_thread( _write_album_track_file_tags," in flat, (
            "the concurrent file phase does not go through "
            "_write_album_track_file_tags — the album save would write tags "
            "outside the tagging policy"
        )

    def test_the_route_does_no_writing_inline(self):
        """The writes must live in the phases, not be duplicated in the route."""
        route = ROUTE_SOURCE

        assert "insert_or_update_track(track_id, payload)" not in route, (
            "the DB write is inline in the route again — the phase is bypassed"
        )
        assert "build_tag_updates(payload)" not in route, (
            "the file-tag write is inline in the route again"
        )
        assert "update_track_genres(" not in route, (
            "the genre write is inline in the route again"
        )


# ===========================================================================
# 2. Phase behaviour
# ===========================================================================
class TestPersistPhase:
    def test_success_reports_ok(self, monkeypatch):
        monkeypatch.setattr(ui, "insert_or_update_track", lambda tid, payload: True)
        assert ui._persist_album_track_payload("t1", {"title": "x"}) == (True, "")

    def test_failure_is_reported_not_swallowed(self, monkeypatch, caplog):
        def _boom(_tid, _payload):
            raise ValueError("invalid input syntax for type json")

        monkeypatch.setattr(ui, "insert_or_update_track", _boom)

        ok, error = ui._persist_album_track_payload("t1", {"title": "x"})

        assert ok is False, "the caller counts this as db_failures"
        assert "invalid input syntax" in error, (
            "the DB's own reason must reach the caller — that is what makes "
            "the failure diagnosable instead of a silent no-op save"
        )


class TestGenresPhase:
    def test_rows_and_failures_are_reported_separately(self, monkeypatch):
        _patch_genres(monkeypatch, rows=3)

        rows, failed = ui._apply_album_track_genres("t1", "Rock, Punk")

        assert (rows, failed) == (3, 0)
        assert ui._apply_album_track_genres("t1", "  ") == (0, 0), (
            "no genres → nothing to write"
        )

    def test_a_rejected_write_is_counted(self, monkeypatch):
        _patch_genres(monkeypatch, rows=0, raise_error=True)

        rows, failed = ui._apply_album_track_genres("t1", "Rock")

        assert (rows, failed) == (0, 1), (
            "a JSONB rejection must be counted, or a genres-only save looks "
            "like a successful no-op"
        )

    def test_the_save_does_not_touch_the_file(self, monkeypatch):
        """File genres belong to the POPULARITY SCAN, not to this pass.

        ``sync_album_file_tags`` owns DB → file. When the save wrote them too
        there were two writers for one tag, and the edit only survived an
        import because the file happened to be updated alongside it.
        """
        def _fail(_path, _tags):
            raise AssertionError("the album save must not write genre tags")

        monkeypatch.setattr(ui, "update_file_tags", _fail)
        _patch_genres(monkeypatch, rows=1)

        rows, failed = ui._apply_album_track_genres("t1", "Rock; Punk")

        assert (rows, failed) == (1, 0), (
            "the database write must still happen even though the file is off-limits"
        )

    def test_the_file_helpers_are_not_even_reached(self, monkeypatch):
        """No path resolution either — this phase is database-only end to end."""
        def _fail(_path):
            raise AssertionError("the album save must not resolve a file path for genres")

        monkeypatch.setattr(ui, "resolve_music_file_path", _fail)
        _patch_genres(monkeypatch, rows=1)

        assert ui._apply_album_track_genres("t1", "Rock") == (1, 0)


class TestFileTagPhase:
    def test_no_file_path_is_reported_as_not_written(self, monkeypatch):
        monkeypatch.setattr(ui, "resolve_music_file_path", lambda p: "")

        ok, path = ui._write_album_track_file_tags(
            "t1", {"file_path": "x"}, {"title": "y"},
            strip_disc_numbers=False, disc_staged=False,
        )

        assert (ok, path) == (False, "")
        assert ok is False, "the caller counts this as file_sync_failures"

    def test_the_cover_genre_convention_survives_the_extraction(self, monkeypatch):
        seen: list[dict] = []
        monkeypatch.setattr(ui, "resolve_music_file_path", lambda p: "/music/a.mp3")
        monkeypatch.setattr(ui, "build_tag_updates", lambda payload: {"title": "y"})
        monkeypatch.setattr(ui, "update_file_tags", lambda path, tags: seen.append(tags) or True)

        ui._write_album_track_file_tags(
            "t1", {"file_path": "x"}, {"title": "y", "is_cover": True},
            strip_disc_numbers=False, disc_staged=False,
        )

        assert seen and seen[0].get("genre") == "Cover"

    def test_the_disc_clear_only_applies_to_an_unreviewed_single_disc(self, monkeypatch):
        seen: list[dict] = []
        monkeypatch.setattr(ui, "resolve_music_file_path", lambda p: "/music/a.mp3")
        monkeypatch.setattr(ui, "build_tag_updates", lambda payload: {"title": "y"})
        monkeypatch.setattr(ui, "update_file_tags", lambda path, tags: seen.append(tags) or True)

        ui._write_album_track_file_tags(
            "t1", {"file_path": "x"}, {"title": "y"},
            strip_disc_numbers=True, disc_staged=False,
        )
        ui._write_album_track_file_tags(
            "t1", {"file_path": "x"}, {"title": "y"},
            strip_disc_numbers=True, disc_staged=True,
        )

        assert "disc_number" in seen[0], "single-disc album → clear the frame"
        assert "disc_number" not in seen[1], (
            "a staged disc number is a confirmed correction and must not be "
            "cleared by the album-level heuristic"
        )


class TestGenresSurviveANavidromeImport:
    """The other half of "database-only": the database has to keep the value.

    The album save writes ``genres``/``manual_genres`` and no longer touches
    the file, so Navidrome's view of that tag is now *stale* by construction.
    The sync's ``UPDATE … SET genres=EXCLUDED.genres`` would then write the old
    file value straight back — the reported "my edit reverts a few hours
    later". These columns join the protected set so the write is skipped on a
    ``_navidrome_sync`` upsert.
    """

    def test_both_columns_are_protected(self):
        from db.repositories.popularity_repository import _POPULARITY_PROTECTED_COLUMNS

        for col in ("genres", "manual_genres"):
            assert col in _POPULARITY_PROTECTED_COLUMNS, (
                f"{col} must be protected from _navidrome_sync overwrites — "
                "the album save writes it to the database only, so a sync "
                "would revert it to whatever the file still holds"
            )

    def test_a_sync_does_not_write_them(self, monkeypatch):
        import db.repositories.popularity_repository as repo

        seen: dict[str, str] = {}

        class _FakeResult:
            def fetchall(self):
                return []

        class _FakeSession:
            def execute(self, statement, params=None):
                seen["sql"] = str(statement)
                seen["params"] = str(params)
                return _FakeResult()

        monkeypatch.setattr(
            repo, "get_tracks_table_columns",
            lambda session=None: {"id", "title", "genres", "manual_genres"},
        )
        monkeypatch.setattr(
            repo, "get_tracks_table_column_types",
            lambda session=None: {"id": "text", "title": "text",
                                  "genres": "text", "manual_genres": "jsonb"},
        )

        repo._execute_save(_FakeSession(), {
            "_navidrome_sync": True,
            "id": "t1",
            "title": "Song",
            "genres": "Rock",
            "manual_genres": '["Rock"]',
        })

        sql = seen["sql"]
        assert "genres=EXCLUDED.genres" not in sql, (
            f"a Navidrome sync would overwrite the album save's genres: {sql}"
        )
        assert "manual_genres=EXCLUDED.manual_genres" not in sql

    def test_a_real_save_still_writes_them(self, monkeypatch):
        """CONTROL — protection must apply to syncs, not to every writer."""
        import db.repositories.popularity_repository as repo

        seen: dict[str, str] = {}

        class _FakeResult:
            def fetchall(self):
                return []

        class _FakeSession:
            def execute(self, statement, params=None):
                seen["sql"] = str(statement)
                return _FakeResult()

        monkeypatch.setattr(
            repo, "get_tracks_table_columns",
            lambda session=None: {"id", "title", "genres"},
        )
        monkeypatch.setattr(
            repo, "get_tracks_table_column_types",
            lambda session=None: {"id": "text", "title": "text", "genres": "text"},
        )

        repo._execute_save(_FakeSession(), {"id": "t1", "title": "Song", "genres": "Rock"})

        assert "genres=EXCLUDED.genres" in seen["sql"], (
            "an ordinary save must still write genres"
        )


def _patch_genres(monkeypatch: pytest.MonkeyPatch, *, rows: int, raise_error: bool = False):
    """Replace the DB genre writer with a controllable one."""

    def _write(*, track_id, genres_str, **_kwargs):
        if raise_error:
            raise ValueError("invalid input syntax for type json")
        return rows

    import db.repositories.metadata as meta

    monkeypatch.setattr(meta, "update_track_genres", _write)


# ===========================================================================
# 4. The save must answer inside Cloudflare's origin timeout (error 524)
# ===========================================================================
class TestTheSaveCannotOutliveTheOriginTimeout:
    """A save is a request, and requests have a deadline.

    Reported: *"I keep getting a cloudflare timeout error when saving"* —
    error **524**, which Cloudflare raises when the origin has not answered
    within 100s. Two things in this handler could get there on their own:

    * the MusicBrainz backfill, whose shared throttle SLEEPS for a slot in a
      1 req/s budget a running scan is spending (30-40s calls in production);
    * the per-track file writes — one reads the file twice to honour
      ``preserve_file_timestamps`` and then rewrites it (~370ms for an 8MB MP3
      on a local disk), done once per track IN SEQUENCE inside the loop.
    """

    def test_the_backfill_deadline_sits_inside_the_origin_timeout(self):
        assert ui._MB_BACKFILL_DEADLINE_SECONDS < 100, (
            "the enrichment deadline must leave room for the rest of the save "
            "inside Cloudflare's 100s — a deadline that reaches the limit "
            "just moves the 524 somewhere else"
        )

    def test_the_fetch_is_wrapped_and_still_off_the_event_loop(self):
        window = ROUTE_SOURCE[ROUTE_SOURCE.index("_prev_mbids = {"):][:700]

        assert "album_mbid != _prev_mbid" in window, (
            "the release-change gate was lost — every save would pay for two "
            "MusicBrainz calls again"
        )
        assert "asyncio.to_thread" in window, (
            "the fetch must not run on the event loop"
        )
        assert "asyncio.wait_for" in window and (
            "timeout=_MB_BACKFILL_DEADLINE_SECONDS" in window
        ), (
            "the backfill is unbounded again: off the event loop is not the "
            "same as off the critical path, and a saturated throttle would "
            "still hold the response until Cloudflare gave up"
        )

    def test_a_timeout_saves_with_the_form_values_rather_than_failing(self):
        """The save must SURVIVE the deadline — not 500.

        The enrichment is additive (artist MBID, type, status, country, year
        and the per-recording map); everything the user actually edited is
        already in the form. Dropping it is a degraded save, raising is a
        lost one.
        """
        flat = " ".join(ROUTE_SOURCE.split())
        assert "except (asyncio.TimeoutError, TimeoutError)" in flat
        assert "_back = {}" in flat, (
            "a timed-out backfill must fall back to empty enrichment, or the "
            "field merges below would raise on a missing key"
        )

    def test_the_route_collects_file_jobs_instead_of_writing_inline(self):
        """Phase 3 is queued in the loop and run once, after it."""
        queue_at = ROUTE_SOURCE.index("_file_jobs.append(")
        persist_at = ROUTE_SOURCE.index("_persist_album_track_payload(track_id, payload)")
        run_at = ROUTE_SOURCE.index("_write_album_track_files_concurrently(")

        assert persist_at < queue_at < run_at, (
            "the file phase must stay AFTER the database writes and be "
            "started once for the album, not once per track"
        )

    def test_the_save_logs_where_it_spent_its_time(self):
        """Without numbers, the next timeout report is a guess.

        Every slow phase here has produced one at some point (event-loop MB
        calls, then the wall clock behind the 524), so the handler reports its
        own phases.
        """
        flat = " ".join(UI_SOURCE.split())
        assert '"Album save phases"' in flat
        for field in (
            "mb_backfill_ms=", "write_loop_ms=", "file_tags_ms=", "total_ms=",
        ):
            assert field in flat, f"the phase log is missing {field}"


class TestTheFilePhaseRunsConcurrentlyButBounded:
    """Concurrency is the fix — but only if it is real AND capped."""

    @staticmethod
    def _jobs(count: int):
        return [(f"t{i}", {"id": f"t{i}"}, {"id": f"t{i}"}, False, False)
                for i in range(count)]

    async def test_writes_overlap_and_stay_within_the_cap(self, monkeypatch):
        import threading
        import time as _time

        lock = threading.Lock()
        state = {"active": 0, "peak": 0}

        def _slow(track_id, track, payload, *, strip_disc_numbers, disc_staged):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            _time.sleep(0.2)
            with lock:
                state["active"] -= 1
            return (True, f"/music/{track_id}.mp3")

        monkeypatch.setattr(ui, "_write_album_track_file_tags", _slow)

        started = _time.monotonic()
        results = await ui._write_album_track_files_concurrently(self._jobs(8))
        elapsed = _time.monotonic() - started

        assert state["peak"] >= 2, (
            "the file writes are sequential again — that is exactly the wall "
            "clock the 524 was caused by"
        )
        assert state["peak"] <= ui._FILE_WRITE_CONCURRENCY, (
            "the semaphore is not bounding the writes: one album could put "
            "every track on the disk (or the network mount) at once"
        )
        # Sequential would be 8 × 0.2s = 1.6s; four at a time is ~0.4s.
        assert elapsed < 1.0, (
            f"8 writes took {elapsed:.2f}s — the phase is not overlapping"
        )

    async def test_results_stay_in_job_order(self, monkeypatch):
        """The caller attributes a failure to ITS file by position."""
        def _echo(track_id, track, payload, *, strip_disc_numbers, disc_staged):
            return (True, f"/music/{track_id}.mp3")

        monkeypatch.setattr(ui, "_write_album_track_file_tags", _echo)

        results = await ui._write_album_track_files_concurrently(self._jobs(5))

        assert [path for _ok, path in results] == [
            f"/music/t{i}.mp3" for i in range(5)
        ]

    async def test_one_crashing_track_does_not_lose_the_rest(self, monkeypatch):
        """A single bad file must not cost the album its other tags."""
        def _sometimes(track_id, track, payload, *, strip_disc_numbers, disc_staged):
            if track_id == "t2":
                raise RuntimeError("disk went away")
            return (True, f"/music/{track_id}.mp3")

        monkeypatch.setattr(ui, "_write_album_track_file_tags", _sometimes)

        results = await ui._write_album_track_files_concurrently(self._jobs(4))

        assert results[2] == (False, ""), (
            "the crashing track must report a failure the counter can see"
        )
        assert all(ok for ok, _ in results[:2] + results[3:]), (
            "the other tracks must still be written"
        )

    async def test_an_empty_album_writes_nothing(self):
        assert await ui._write_album_track_files_concurrently([]) == []
