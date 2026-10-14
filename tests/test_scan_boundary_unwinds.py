"""The artist boundary must run on EVERY exit from the album loop.

Reported follow-up
------------------
> There's still no try/finally around the album loop, so an exception escaping
> an album skips the boundary flush — phase 1 + the heal already recover that
> on the next scan (and deferred album renames still need the boundary).

Verified before changing anything: the loop is wrapped in per-album ``try``
islands (a prep island that ``continue``s, and a main island that logs and
continues), so a normal album failure is absorbed. What was NOT covered is an
exception from a statement at LOOP LEVEL, outside both islands. There are
several, and they are realistic rather than theoretical:

* ``update(stage="album", ...)`` — a progress write, i.e. a DB round-trip;
* ``log_unified(...)`` — structlog has raised ``UnicodeEncodeError`` in this
  file's history (emoji in scan logs on a cp1252 console);
* ``_load_mb_single_titles`` / ``_load_discogs_*`` — DB-backed, called
  unguarded at loop level for every album whose artist is not already cached;
* ``was_album_scanned(...)`` — the skip gate's freshness query;
* a ``KeyboardInterrupt``, which no ``except Exception`` can see.

Only the two ``return False`` paths (stop, and the abandoned-artist cancel)
closed the boundary for themselves. Everything else fell through to the
post-loop ``_close_artist_section(...)`` — which an escaping exception never
reached.

What is actually lost when the boundary is skipped
--------------------------------------------------
* **Deferred album renames — permanently.** They live in ``run_scan``'s
  in-memory ``_deferred_album_renames`` and are ``.pop``ed at the boundary, so
  a skipped boundary dropped them until the album was resolved again by hand or
  by a forced scan.
* **Star ratings — recovered, but worse.** Phase 1 already persisted them per
  album, and ``_album_needs_star_reconcile`` force-runs a provisional album on
  the next scan, so the reconcile eventually happens. The album's closing
  SUMMARY (the SCAN RESULTS table) is lost for that run either way.
* **Essential playlists — recovered.** The ``done`` set is per run, so the
  next scan re-derives them.

So the fix is a ``finally``, not a redesign.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE = REPO_ROOT / "services" / "popularity" / "scan_stage_runner.py"


# ---------------------------------------------------------------------------
# A real run_scan, stopped at a loop-level call site
# ---------------------------------------------------------------------------


class _Escape(BaseException):
    """Escapes the loop exactly as a real fault would.

    Subclasses ``BaseException``, not ``Exception``, deliberately: the loop's
    per-album islands and its inner guards all catch ``Exception``, so an
    ``Exception`` sentinel could be absorbed before it ever proved anything.
    A ``BaseException`` sentinel also keeps the test honest against a future
    ``except Exception`` added around the same line.
    """


class _UpdateRecorder:
    """``progress_tracker.update`` — real, because it is the escape point.

    ``update(stage="album", ...)`` is called at loop level for every album,
    before the album's own try islands, so raising there is a faithful stand-in
    for the fault class this change guards against.
    """

    def __init__(self, escape_stage: str | None):
        self._escape_stage = escape_stage
        self.stages: list[str] = []

    def __call__(self, *args, **kwargs):
        stage = kwargs.get("stage")
        if stage:
            self.stages.append(str(stage))
        if self._escape_stage and stage == self._escape_stage:
            raise _Escape(f"progress write failed at stage={stage}")
        return None


def _drive(monkeypatch, *, escape_stage: str | None = "album"):
    """Patch the scan down to one album and record the boundary's work.

    The oracle is the essential-playlist sync: it is the LAST thing
    ``_close_artist_section`` does, and it runs only from there — so a record
    is proof the boundary ran, and no record is proof it did not.
    """
    from helpers import scan_report
    from services.popularity import scan_cancellation
    from services.popularity import scan_stage_runner as mod

    synced: list[str] = []

    def _sync(artist_name, featured_rows=None, **kwargs):
        synced.append(str(artist_name))
        return {"ok": True, "updated": 0}

    monkeypatch.setattr(
        mod,
        "load_candidates",
        lambda options=None: [
            {"artist": "Somebody", "album": "A Record", "tracks": []}
        ],
    )
    monkeypatch.setattr(mod, "update", _UpdateRecorder(escape_stage))
    monkeypatch.setattr(mod, "start", lambda *a, **k: None)
    monkeypatch.setattr(mod, "finish", lambda *a, **k: None)
    monkeypatch.setattr(mod, "log_unified", lambda *a, **k: None)
    monkeypatch.setattr(mod, "record_scan", lambda *a, **k: None)
    monkeypatch.setattr(mod, "is_stop_requested", lambda *a, **k: False)
    monkeypatch.setattr(mod, "get_feature", lambda name, default=None, **k: default)

    # The three singles caches are DB-backed and unguarded at loop level; a
    # real one would either be slow or (worse) raise the very exception this
    # suite is about, before the sentinel ever fires.
    monkeypatch.setattr(mod, "_load_mb_single_titles", lambda *a, **k: set())
    monkeypatch.setattr(mod, "_load_discogs_single_titles", lambda *a, **k: set())
    monkeypatch.setattr(mod, "_load_discogs_promo_titles", lambda *a, **k: set())
    monkeypatch.setattr(mod, "_pass_runs_singles", lambda *a, **k: False)
    monkeypatch.setattr(scan_cancellation, "is_cancelled", lambda *a, **k: False)
    monkeypatch.setattr(scan_report, "scan_started", lambda *a, **k: None)

    # The boundary's observable work.
    monkeypatch.setattr(mod, "_essential_playlists_enabled", lambda *a, **k: True)
    monkeypatch.setattr(mod, "_fetch_essential_featured_rows", lambda *a, **k: [])
    monkeypatch.setattr(mod, "_sync_essential_playlist", _sync)

    # Belt and braces: if an album ever DOES get past the escape point (a
    # control that raises earlier, say), enrichment must not go to MusicBrainz
    # and Navidrome for a test artist. A real run here took ~30s and produced
    # certificate-verification failures, which is a poor neighbour for a suite
    # that shares a process.
    monkeypatch.setattr(mod, "enrich_album", lambda *a, **k: ({}, []))

    return mod, synced


class TestTheBoundarySurvivesAnEscapingAlbum:
    def test_the_boundary_still_unwinds(self, monkeypatch):
        """THE FIX.

        The escape happens at ``update(stage="album")`` for the FIRST album —
        after ``_section_artist`` has been bound to that artist, and before the
        album's own try islands. Nothing is pending to flush and nothing is
        queued to rename, so the observable evidence is the essential-playlist
        sync, which only ever runs from the boundary.
        """
        mod, synced = _drive(monkeypatch)

        with pytest.raises(_Escape):
            # force=True: the skip gate reads scan_history, and this test is
            # about the boundary, not the freshness window.
            mod.run_scan(artist_filter="Somebody", force=True)

        assert synced == ["Somebody"], (
            "the artist boundary did not run when the album loop escaped — "
            "deferred album renames queued for this artist would have been "
            "silently discarded"
        )

    def test_the_exception_still_propagates(self, monkeypatch):
        """CONTROL — a ``finally`` must not swallow the fault. The caller
        reports the artist as failed; unwinding is a side effect, not a
        recovery."""
        mod, _ = _drive(monkeypatch)

        with pytest.raises(_Escape, match="stage=album"):
            mod.run_scan(artist_filter="Somebody", force=True)

    def test_a_normal_boundary_is_still_reached(self, monkeypatch):
        """CONTROL — the recorder really is the boundary's, not something that
        happens to run on the failing path. Here the escape is raised at the
        ``loading`` stage, i.e. BEFORE the album loop, so no album is ever
        processed: nothing to unwind, so nothing may be recorded.

        (This also pins that the fix did not turn the boundary into an
        unconditional sync for an artist that never started.)
        """
        mod, synced = _drive(monkeypatch, escape_stage="loading")

        with pytest.raises(_Escape, match="stage=loading"):
            mod.run_scan(artist_filter="Somebody", force=True)

        assert synced == [], (
            "the boundary synced for an artist whose scan never reached the "
            "album loop"
        )


# ---------------------------------------------------------------------------
# The structure itself
# ---------------------------------------------------------------------------


def _run_scan_tree() -> tuple[ast.FunctionDef, ast.Module]:
    source = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "run_scan"
    )
    return node, tree


def _called_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        func = node.func
        return func.id if isinstance(func, ast.Name) else None
    return None


class TestTheLoopIsWrappedNotJustGuarded:
    def test_the_album_loop_is_inside_a_try_with_a_finally(self):
        """Pins the SHAPE, so a refactor cannot quietly drop the unwind.

        An ``ast`` walk rather than a source substring: the loop is 1400 lines
        long, and "there is a try somewhere" is not the claim — the claim is
        that THIS loop is inside THAT try.
        """
        node, _ = _run_scan_tree()

        loops = [
            n for n in ast.walk(node)
            if isinstance(n, ast.For) and "album_index" in ast.dump(n.target)
        ]
        assert loops, "run_scan no longer has the album loop"

        wrapped = [
            n for n in ast.walk(node)
            if isinstance(n, ast.Try)
            and any(loop in list(ast.walk(n)) for loop in loops)
            and n.finalbody
        ]
        assert wrapped, "the album loop is not inside a try/finally"

    def test_the_finally_calls_the_boundary(self):
        node, _ = _run_scan_tree()

        for try_node in ast.walk(node):
            if not isinstance(try_node, ast.Try) or not try_node.finalbody:
                continue
            calls = [
                _called_name(stmt)
                for stmt in try_node.finalbody
                for stmt in ast.walk(stmt)
            ]
            if "_close_artist_section" in calls:
                return
        raise AssertionError(
            "no finally block calls _close_artist_section — the boundary was "
            "detached from the loop"
        )

    def test_the_boundary_call_cannot_mask_the_fault(self):
        """A ``finally`` that raises REPLACES the in-flight exception, turning
        a diagnosable crash into a confusing one. The close is therefore
        wrapped in its own handler."""
        node, _ = _run_scan_tree()

        for try_node in ast.walk(node):
            if not isinstance(try_node, ast.Try) or not try_node.finalbody:
                continue
            guarded = False
            for stmt in try_node.finalbody:
                if not isinstance(stmt, ast.Try):
                    continue
                names = [
                    _called_name(inner)
                    for inner in ast.walk(stmt)
                ]
                if "_close_artist_section" in names:
                    guarded = True
            if guarded:
                assert any(
                    handler.type is not None
                    for stmt in try_node.finalbody
                    if isinstance(stmt, ast.Try)
                    for handler in stmt.handlers
                ), "the boundary call in the finally has no exception handler"
                return
        raise AssertionError("no guarded boundary call found in any finally block")

    def test_the_loop_no_longer_has_its_own_close_after_it(self):
        """The post-loop call became the ``finally``; leaving a second one
        after the try would be a third call site for no reason."""
        node, _ = _run_scan_tree()

        # Indent 4 == function body level, which is where the old call lived.
        level_calls = [
            n for n in node.body
            if isinstance(n, ast.Expr)
            and _called_name(n.value) == "_close_artist_section"
        ]
        assert level_calls == [], (
            "a boundary close still sits at function level, outside the try — "
            "that is the pre-fix shape"
        )

    def test_the_two_exit_paths_still_close_themselves(self):
        """The stop and cancel paths close BEFORE ``finish()`` reports the scan
        as stopped; the finally's call is the second (no-op) one for them."""
        source = MODULE.read_text(encoding="utf-8")

        assert '"Scan stopped by user request"' in source
        assert '"— abandoned after exceeding its time budget; moving on"' in source

        # Both are the `log_unified` line each exit path prints. The stop one is
        # a whole string literal, so its leading quote is a real character; the
        # cancel one is the TAIL of a split f-string, so only the closing quote
        # is on that line.
        for marker in ('"Scan stopped by user request"', 'time budget; moving on"'):
            index = source.index(marker)
            window = source[max(0, index - 400):index]
            assert "_close_artist_section(" in window, (
                f"the exit path near {marker} no longer unwinds before finish()"
            )


class TestTheSecondCallIsDeliberatelySafe:
    """The in-loop paths close and the ``finally`` closes again for them, so
    every third of the boundary must be re-entrant.
    """

    @staticmethod
    def _source() -> str:
        return MODULE.read_text(encoding="utf-8")

    def test_the_flush_pops_its_bucket(self):
        source = self._source()
        body = source[source.index("def _flush_artist_star_ratings"):]

        assert "pop(artist" in body or "pop(artist," in body, (
            "the flush no longer consumes its pending albums, so a second call "
            "would re-rate them"
        )

    def test_the_renames_are_popped_once(self):
        source = self._source()
        body = source[source.index("def _close_artist_section"):]

        assert "_deferred_album_renames.pop(" in body, (
            "the rename batch is not consumed on read, so a second boundary "
            "would re-apply (and re-move files for) a rename"
        )

    def test_the_essential_section_is_keyed_by_a_done_set(self):
        source = self._source()
        body = source[
            source.index("def _close_artist_essential_section"):
            source.index("def _run_album_cover_detection")
        ]

        assert "key in done" in body, (
            "the essential collection is not guarded by its done set, so a "
            "second boundary would re-push the artist's playlist"
        )
