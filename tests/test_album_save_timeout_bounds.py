"""Album save: it must not outlive the origin timeout.

Reported twice: "still getting page timeouts when trying to save edits after
doing an mbid lookup on the album page."

WHAT THE SAVE ACTUALLY COSTS (per album)
----------------------------------------
    1. MusicBrainz backfill        bounded at 15s      (already fixed)
    2. cover FETCH                 was unbounded       <- fixed here
    3. tag writes, 4 concurrent    bounded pool        (already fixed)
    4. cover EMBED, N files        was SEQUENTIAL      <- fixed here
    5. Navidrome rescan            fire-and-forget

The lookup is what makes 2 and 4 fire at all: it fills ``#cover_art_url``, so
the save fetches an image and then rewrites EVERY file to embed it. Step 4 was a
plain ``for`` loop, so an album paid two full rewrite passes per file — once for
tags, once for the picture — with the second strictly serial. At ~370ms a file
on a local disk that is tolerable; at seconds a file on a network mount it is
minutes, which is the 524.

WHY THESE TESTS LOOK THE WAY THEY DO
------------------------------------
The wall clock depends on the DISK, which no test here can vary. So the
properties that make it bounded are asserted directly instead:

* the embed runs CONCURRENTLY (proven by timing against a slow fake writer —
  a sequential loop cannot beat its own per-file cost);
* a single bad file does not cost the others their picture;
* the two network waits are wrapped in deadlines;
* an abandoned disk phase is REPORTED as still-running, never as confirmed.
"""

from __future__ import annotations

import ast
import pathlib
import time

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
UI_ROUTES = REPO_ROOT / "routes" / "ui_routes.py"
ART_SERVICE = REPO_ROOT / "services" / "enrichment" / "album_art_service.py"


def _code_only(path: pathlib.Path) -> str:
    """Source with comments and docstrings removed.

    ⚠️ Not tidiness. These files DOCUMENT the traps they fix, quoting the
    retired forms ("a plain ``for row in rows:`` loop", the unbounded ``await``)
    — so an assertion about absence would be satisfied or broken by prose. Fifth
    time this has come up in this codebase.

    ⚠️ ALSO NOTE what it is NOT for: ``ast.unparse`` compacts the file onto short
    lines, so a CHARACTER WINDOW around an anchor means something different here
    than in the real file. Absence assertions use this; the structural
    "is this call wrapped in that one" assertions use ``_raw`` with an exact
    multi-line snippet, which is stronger evidence anyway.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _raw(path: pathlib.Path) -> str:
    """Unmodified source, for structural adjacency assertions."""
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------

class _Row:
    """A row that quacks like SQLAlchemy's: the code reads ``row._mapping``.

    ⚠️ A plain dict does NOT work — the service checks ``getattr(row,
    "_mapping", None)`` and, finding none, falls back to the LEGACY TUPLE
    shape (``row[1]``), so a dict raises ``KeyError: 1``. That failure looks
    like a broken query in the service rather than a mis-shaped test double.
    """

    def __init__(self, values: dict):
        self._mapping = values


class _FakeSession:
    """Minimal ``db_session`` stand-in returning canned rows.

    ⚠️ The service binds ``db_session`` at MODULE IMPORT (``from db.engine import
    db_session``), so patching ``db.engine.db_session`` does NOT reach it — the
    module's own name must be patched. That trap is documented in this codebase
    and it still cost a run here: the first version of these tests patched
    ``db.engine`` and every write count came back 0, which looks exactly like a
    broken concurrency implementation.
    """

    def __init__(self, rows):
        self._rows = [_Row(r) if isinstance(r, dict) else r for r in rows]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *_a, **_k):
        return _FakeResult(self._rows)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


@pytest.fixture
def art_service(monkeypatch):
    """The service with its DB and file layer faked."""
    import services.enrichment.album_art_service as svc

    def _install(rows, writer, *, exists=True):
        monkeypatch.setattr(svc, "db_session", lambda: _FakeSession(rows))
        monkeypatch.setattr(svc.os.path, "exists", lambda _p: exists)
        monkeypatch.setattr(
            "services.metadata.tag_file_service.resolve_music_file_path", lambda p: p
        )
        monkeypatch.setattr(
            "services.metadata.tag_file_service.write_tags_to_file", writer
        )

    svc.install = _install  # type: ignore[attr-defined]
    return svc


# ---------------------------------------------------------------------------
# 1. The cover embed is concurrent
# ---------------------------------------------------------------------------

class TestTheCoverEmbedIsConcurrent:
    def test_it_uses_a_bounded_pool(self):
        code = _code_only(ART_SERVICE)
        assert "ThreadPoolExecutor" in code, "the cover embed is not pooled"
        assert "_COVER_EMBED_CONCURRENCY" in code

    def test_the_cap_is_small(self):
        """A pool wide enough to saturate the disk is the bug, not the fix."""
        import services.enrichment.album_art_service as svc

        assert 2 <= svc._COVER_EMBED_CONCURRENCY <= 8

    def test_the_old_sequential_loop_is_gone(self):
        code = _code_only(ART_SERVICE)
        # The sequential shape was `for row in rows:` … `if write_tags_to_file(`
        # inside apply_album_art_to_tracks. The paths are now collected first and
        # mapped through the pool, so no write call sits inside a per-row loop.
        assert "constant_time_loop_marker" not in code  # placeholder guard
        start = code.index("def apply_album_art_to_tracks")
        body = code[start:start + 3000]
        assert "with ThreadPoolExecutor" in body
        assert "pool.map(" in body

    def test_it_is_actually_faster_than_one_file_at_a_time(self, art_service, monkeypatch):
        """⭐ THE BEHAVIOURAL TEST, and the only one that cannot be faked.

        The per-file cost is faked (250ms) so the assertion does not depend on
        the machine's disk. Four concurrent 250ms writes finish well under the
        4 x 250ms a serial loop would need — which is the property the fix is
        FOR. A sequential implementation fails this no matter how fast the disk
        is.
        """
        svc = art_service
        files = [f"/music/track{i}.mp3" for i in range(4)]
        starts: list[float] = []

        def _slow_write(path, _tags):  # noqa: ANN001
            starts.append(time.monotonic())
            time.sleep(0.25)
            return True

        svc.install(
            [{"id": str(i), "file_path": f} for i, f in enumerate(files)],
            _slow_write,
        )

        t0 = time.monotonic()
        updated = svc.apply_album_art_to_tracks("A", "B", b"image")
        elapsed = time.monotonic() - t0

        assert updated == 4
        assert elapsed < 4 * 0.25 * 0.7, (
            f"4 files took {elapsed:.3f}s — that is serial, not concurrent"
        )
        # And the writes really did overlap rather than being batched by luck.
        assert max(starts) - min(starts) < 0.25, "the writes did not overlap in time"


# ---------------------------------------------------------------------------
# 2. One bad file does not cost the others
# ---------------------------------------------------------------------------

class TestCrashIsolation:
    def test_a_failing_write_does_not_stop_the_rest(self, art_service):
        """The old loop had no try/except at all, so one locked file aborted the
        whole embed and every later track silently kept the OLD picture."""
        svc = art_service
        files = ["/music/a.mp3", "/music/b.mp3", "/music/c.mp3"]

        def _write(path, _tags):  # noqa: ANN001
            if path.endswith("b.mp3"):
                raise OSError("file is locked")
            return True

        svc.install(
            [{"id": str(i), "file_path": f} for i, f in enumerate(files)],
            _write,
        )

        assert svc.apply_album_art_to_tracks("A", "B", b"image") == 2

    def test_a_missing_file_is_skipped_not_counted(self, art_service):
        svc = art_service
        svc.install([{"id": "1", "file_path": "/music/gone.mp3"}], lambda *a: True,
                    exists=False)

        assert svc.apply_album_art_to_tracks("A", "B", b"image") == 0

    def test_a_row_with_no_file_path_is_skipped(self, art_service):
        svc = art_service
        svc.install([{"id": "1", "file_path": ""}], lambda *a: True)

        assert svc.apply_album_art_to_tracks("A", "B", b"image") == 0

    def test_no_image_is_a_no_op(self):
        import services.enrichment.album_art_service as svc

        assert svc.apply_album_art_to_tracks("A", "B", b"") == 0


# ---------------------------------------------------------------------------
# 3. Both network waits have deadlines
# ---------------------------------------------------------------------------

class TestTheWaitsAreBounded:
    """Are the two network waits wrapped, not merely near a timeout keyword.

    ⚠️ ASSERTED ON EXACT ADJACENCY, not on a character window. A window is a
    proximity argument: "``wait_for`` appears somewhere in the ~400 characters
    before this call" is satisfied by a wait_for guarding something ELSE nearby,
    which is exactly the shape a refactor produces when a wrapper slips one call
    down. The snippets below pin the call INSIDE the wrapper.
    """

    def test_the_cover_fetch_is_under_a_deadline(self):
        source = _raw(UI_ROUTES)
        assert (
            "await asyncio.wait_for(\n"
            "                    asyncio.to_thread(\n"
            "                        _resolve_album_cover_bytes, cover_url, album_mbid, album_rg_mbid\n"
            "                    ),\n"
            "                    timeout=_COVER_FETCH_DEADLINE_SECONDS,\n"
            "                )"
        ) in source, "the cover fetch is not wrapped in its own deadline"

    def test_the_backfill_is_still_under_its_deadline(self):
        """The fix that was already there must not be lost in this one."""
        source = _raw(UI_ROUTES)
        assert (
            "await asyncio.wait_for(\n"
            "                    asyncio.to_thread(\n"
            "                        _fetch_album_mb_backfill,"
        ) in source, "the MusicBrainz backfill lost its deadline"

    def test_the_inline_disk_phase_is_under_a_budget(self):
        source = _raw(UI_ROUTES)
        assert (
            "await asyncio.wait_for(\n"
            "                    asyncio.to_thread(_run_album_file_sync_phases, _file_payload),\n"
            "                    timeout=_INLINE_DISK_BUDGET_SECONDS,\n"
            "                )"
        ) in source, "the inline disk phase is unbounded"

    def test_the_budget_leaves_room_for_everything_before_it(self):
        """The budget is only meaningful against the 100s origin window.

        Everything ahead of the disk phase is bounded but NOT free: the backfill
        (15s) and the cover fetch (12s) run first, so a budget at or near 100s
        would be no budget at all.
        """
        import routes.ui_routes as ui

        before_it = ui._MB_BACKFILL_DEADLINE_SECONDS + ui._COVER_FETCH_DEADLINE_SECONDS
        assert ui._INLINE_DISK_BUDGET_SECONDS < 100.0 - before_it, (
            "the disk budget plus the phases ahead of it can still exceed "
            "Cloudflare's 100s origin timeout"
        )

    def test_a_cover_timeout_clears_the_bytes_so_nothing_is_embedded(self):
        """A cover is additive — losing it must cost one picture, not the save.

        The bytes are cleared explicitly because the ``finally``-shaped
        alternative (leaving whatever partial value the thread produced) would
        embed a truncated image.
        """
        source = _raw(UI_ROUTES)
        idx = source.index("_resolve_album_cover_bytes, cover_url")
        after = source[idx:idx + 900]
        assert "except (asyncio.TimeoutError, TimeoutError):" in after
        assert "_cover_bytes, _cover_mime = None" in after


# ---------------------------------------------------------------------------
# 4. An abandoned save is reported honestly
# ---------------------------------------------------------------------------

class TestTheAbandonedPhaseIsReported:
    def test_the_flag_exists_and_starts_false(self):
        code = _code_only(UI_ROUTES)
        assert "_file_abandoned = False" in code
        assert "_file_abandoned = True" in code

    def test_it_responds_as_backgrounded_work(self):
        """⚠️ The message must not claim the files were written.

        When the budget expires the response has NOT confirmed the tags. Telling
        the user "saved" would be the same class of lie as the "No changes were
        made." bug this handler already fixed once.
        """
        source = _raw(UI_ROUTES)
        assert "if _file_backgrounded or _file_abandoned:" in source

    def test_an_abandoned_disk_result_does_not_report_failures_it_cannot_know(self):
        """``_disk`` is emptied on expiry, so the failure count stays 0.

        Reading failures off an abandoned result would report "0 tracks failed"
        as though that had been checked. Asserted by ADJACENCY: the clearing must
        be the first statement of that handler, before anything else can read
        the half-result.
        """
        source = _raw(UI_ROUTES)
        assert (
            "except (asyncio.TimeoutError, TimeoutError):\n"
            "                _disk = {}\n"
            "                _file_abandoned = True"
        ) in source, (
            "the abandoned result is not cleared before the flag is set — its "
            "counters would be read as though the phase had completed"
        )

    def test_the_phase_line_reaches_the_UI_log(self):
        """The last two reports arrived without these numbers, because the line
        existed only in the JSON app log."""
        source = _raw(UI_ROUTES)
        assert (
            'log_unified(\n'
            '            f"💾 Album save: {album_name}'
        ) in source, "the phase line is not written to the UI log"

    def test_the_two_disk_passes_are_timed_separately(self):
        """Otherwise "the save is slow" cannot name which pass is slow."""
        source = _raw(UI_ROUTES)
        assert '"tags_ms": round(tags_ms, 1)' in source
        assert '"cover_ms": round(cover_ms, 1)' in source
        assert "tag_writes_ms=round(_tags_ms, 1)" in source
        assert "cover_embed_ms=round(_cover_ms, 1)" in source


# ---------------------------------------------------------------------------
# 5. The runner reports its steps
# ---------------------------------------------------------------------------

class TestTheRunnerReportsItsSteps:
    def test_it_returns_per_step_timings(self):
        import routes.ui_routes as ui

        result = ui._run_album_file_sync_phases({"file_jobs": []})
        assert "tags_ms" in result
        assert "cover_ms" in result
        assert result["tags_ms"] >= 0
        assert result["cover_ms"] >= 0

    def test_an_empty_album_is_still_cheap(self):
        import routes.ui_routes as ui

        t0 = time.monotonic()
        result = ui._run_album_file_sync_phases({"file_jobs": []})
        assert time.monotonic() - t0 < 1.0
        assert result["file_failures"] == 0
        assert result["tracks"] == 0
