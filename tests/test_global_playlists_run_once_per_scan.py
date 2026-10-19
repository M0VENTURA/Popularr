"""The global playlist rebuild must run ONCE per scan, not once per artist.

⚠️ THE BUG.

``finalise_stage._run_global_playlist_rebuild`` is guarded by
``options["_global_playlists_done"]``, and its own comment promised:

    "It also runs ONCE per scan (this function is marker-guarded), not once
     per artist"

That was **false**. A full scan calls ``run_artist_scan_pipeline(artist)`` once
per artist, and ``scan_stage_runner.run_scan`` builds a **fresh ``options``
dict** for each of those calls — so the marker was re-created every time and
could never suppress anything. A full scan therefore rebuilt, after EVERY
artist:

* the genre top-tracks playlists,
* the New Music playlist,
* the per-user favourite playlists, and
* the complete playlist index (``sync_playlist_index`` — every playlist the
  server reports, ~615 of them, ~75–82k entries).

Measured on a two-artist window of a real scan that was ~5–8 minutes of pure
waste per artist; across 982 artists it is the single largest avoidable cost in
the pipeline.

It is also semantically wrong, not merely slow: genre top-tracks, New Music and
the index all aggregate across the WHOLE library, so they belong at the END of
the scan — not after artist #1.

The fix makes scan scope explicit (``services/popularity/scan_scope``): the
full-scan orchestrator defers the rebuild for the duration of the artist loop and
performs it once when the loop finishes. A targeted (artist/album/dashboard-mode)
scan never defers, so it keeps its existing single end-of-run rebuild.
"""

from __future__ import annotations

import re

import pytest


# ---------------------------------------------------------------------------
# The scan-scoped flag itself
# ---------------------------------------------------------------------------


class TestScanScopeFlag:
    def test_defaults_to_not_deferred(self):
        from services.popularity import scan_scope

        scan_scope.reset()
        assert scan_scope.global_playlists_deferred() is False

    def test_defer_then_resume(self):
        from services.popularity import scan_scope

        scan_scope.reset()
        try:
            scan_scope.defer_global_playlists()
            assert scan_scope.global_playlists_deferred() is True

            scan_scope.resume_global_playlists()
            assert scan_scope.global_playlists_deferred() is False
        finally:
            scan_scope.reset()

    def test_deferral_is_idempotent(self):
        """Nested/repeat deferrals must not need matching resumes to clear."""
        from services.popularity import scan_scope

        scan_scope.reset()
        try:
            scan_scope.defer_global_playlists()
            scan_scope.defer_global_playlists()
            scan_scope.defer_global_playlists()
            assert scan_scope.global_playlists_deferred() is True

            scan_scope.resume_global_playlists()
            assert scan_scope.global_playlists_deferred() is False, (
                "one resume must clear it regardless of how many deferrals "
                "were requested — otherwise a stale True would suppress every "
                "later targeted scan's playlist rebuild"
            )
        finally:
            scan_scope.reset()


# ---------------------------------------------------------------------------
# _run_global_playlist_rebuild honours the deferral
# ---------------------------------------------------------------------------


def _spy_rebuild(monkeypatch):
    """Patch the three global builders + the index, returning a call recorder.

    Returns ``(calls, restore)``; ``calls`` is a list of the names that ran.
    """
    import services.popularity.stages.finalise_stage as fs

    calls: list[str] = []

    monkeypatch.setattr(fs, "_genre_playlists_active", lambda: True)
    monkeypatch.setattr(
        fs, "_create_genre_top_track_playlists", lambda: calls.append("genre") or 1
    )
    monkeypatch.setattr(
        fs, "_new_music_playlist_enabled", lambda: True
    )
    monkeypatch.setattr(fs, "_create_new_music_playlist", lambda: calls.append("newmusic"))

    # The index sync imports lazily inside the function.
    import services.playlists.playlist_index_service as idx

    monkeypatch.setattr(idx, "sync_playlist_index", lambda *a, **k: calls.append("index") or {})

    # Per-user favourites also run inside the rebuild; neutralise config.
    import helpers.config_helpers as ch

    monkeypatch.setattr(
        ch, "get_favourite_playlists_config", lambda: {"favourite_playlists_enabled": False}
    )

    return fs, calls


class TestRebuildHonoursDeferral:
    def test_deferred_rebuild_does_no_work(self, monkeypatch):
        """THE REGRESSION: during a full scan's artist loop this must be a no-op."""
        from services.popularity import scan_scope

        fs, calls = _spy_rebuild(monkeypatch)
        scan_scope.reset()
        scan_scope.defer_global_playlists()
        try:
            fs._run_global_playlist_rebuild({})
        finally:
            scan_scope.reset()

        assert calls == [], (
            "a deferred rebuild ran global playlist work — this is the per-artist "
            "waste (genre playlists + New Music + a full index over every playlist)"
        )

    def test_forced_rebuild_runs_while_deferred(self, monkeypatch):
        """The orchestrator's end-of-scan call must get through the deferral."""
        from services.popularity import scan_scope

        fs, calls = _spy_rebuild(monkeypatch)
        scan_scope.reset()
        scan_scope.defer_global_playlists()
        try:
            fs._run_global_playlist_rebuild({}, force=True)
        finally:
            scan_scope.reset()

        assert "genre" in calls and "newmusic" in calls and "index" in calls, (
            "the deferred rebuild must still be runnable via force=True, which is "
            "how the scan end performs it"
        )

    def test_undeferred_rebuild_runs_normally(self, monkeypatch):
        """Control: a targeted scan (no deferral) is unchanged."""
        from services.popularity import scan_scope

        fs, calls = _spy_rebuild(monkeypatch)
        scan_scope.reset()
        fs._run_global_playlist_rebuild({})

        assert "genre" in calls and "index" in calls

    def test_the_options_marker_still_dedupes_within_one_run(self, monkeypatch):
        """The per-``options`` marker is kept as a second guard.

        It cannot express scan scope, but it still stops the two call sites in
        ``finalise_scan`` from rebuilding twice within a single artist's run.
        """
        from services.popularity import scan_scope

        fs, calls = _spy_rebuild(monkeypatch)
        scan_scope.reset()

        options: dict = {}
        fs._run_global_playlist_rebuild(options)
        first = len(calls)
        fs._run_global_playlist_rebuild(options)

        assert first > 0
        assert len(calls) == first, "the marker must still suppress a repeat call"
        assert options.get("_global_playlists_done") is True

    def test_a_broken_scan_scope_module_does_not_disable_playlists(self, monkeypatch):
        """Fail OPEN: a flag-module error must never disable playlist generation."""
        import services.popularity.scan_scope as scope

        fs, calls = _spy_rebuild(monkeypatch)
        scan_scope_reset = scope.reset
        scan_scope_reset()

        def _boom(*_a, **_k):
            raise RuntimeError("scan_scope is broken")

        monkeypatch.setattr(scope, "global_playlists_deferred", _boom)

        fs._run_global_playlist_rebuild({})

        assert "genre" in calls and "index" in calls, (
            "an exception from the deferral check must fall through to running the "
            "rebuild, not silently skip it"
        )


# ---------------------------------------------------------------------------
# The full-scan orchestrator: defer during the loop, once at the end
# ---------------------------------------------------------------------------


class _ProgressRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, path, scan_type, is_running, current_artist=None, extra=None):
        self.calls.append({"extra": dict(extra or {})})


def _patch_orchestrator(monkeypatch, artists, *, run_artist=None):
    """Patch the full-scan orchestrator's collaborators; return (pp, recorder)."""
    import services.scanning.pipelines.popularity_pipeline as pp
    import db.repositories.library as library_mod
    import services.scanning.pipeline as pipeline_mod
    import services.popularity.stages.load_stage as load_stage_mod

    monkeypatch.setattr(library_mod, "get_all_artists", lambda: list(artists))
    monkeypatch.setattr(
        pipeline_mod,
        "run_artist_scan_pipeline",
        run_artist or (lambda artist, force=False, progress_callback=None: None),
    )
    monkeypatch.setattr(
        load_stage_mod, "_artist_key",
        lambda v: re.sub(r"[^a-z0-9]+", "", (v or "").lower()),
    )
    monkeypatch.setattr(pp, "get_scan_progress_path", lambda t: t)
    monkeypatch.setattr(pp, "is_stop_requested", lambda path: False)
    monkeypatch.setattr(pp, "record_scan", lambda *a, **k: None)
    monkeypatch.setattr(pp, "log_unified", lambda *a, **k: None)
    monkeypatch.setattr(pp, "_drain_abandoned_workers", lambda *a, **k: None)

    recorder = _ProgressRecorder()
    monkeypatch.setattr(pp, "write_progress_with_current_artist", recorder)
    return pp, recorder


class TestOrchestratorOwnsTheRebuild:
    def test_the_rebuild_is_deferred_for_the_whole_artist_loop_and_run_once(
        self, monkeypatch
    ):
        """The core behaviour: N artists ⇒ ONE global rebuild, at the END."""
        import services.popularity.stages.finalise_stage as fs
        import services.popularity.scan_scope as scope

        scope.reset()
        seen: list[tuple[str, bool]] = []

        # Observe the deferral as each artist's pipeline runs (the real
        # per-artist path calls _run_global_playlist_rebuild; here we only need
        # to know what the flag was during the loop).
        def _run_artist(artist, force=False, progress_callback=None):
            seen.append((artist, scope.global_playlists_deferred()))

        rebuilds: list[bool] = []
        monkeypatch.setattr(
            fs,
            "_run_global_playlist_rebuild",
            lambda options, force=False: rebuilds.append(bool(force)),
        )

        pp, _rec = _patch_orchestrator(monkeypatch, ["A", "B", "C"], run_artist=_run_artist)
        try:
            pp._run_full_scan_as_artist_pipeline()
        finally:
            scope.reset()

        assert seen == [("A", True), ("B", True), ("C", True)], (
            "the deferral must be set for EVERY artist in the loop, so no artist "
            "pays for the global playlist rebuild"
        )
        assert rebuilds == [True], (
            f"expected exactly one end-of-scan rebuild (forced); got {rebuilds}"
        )

    def test_the_rebuild_runs_after_the_last_artist(self, monkeypatch):
        """Order matters: the rebuild must follow the loop, not precede it."""
        import services.popularity.stages.finalise_stage as fs
        import services.popularity.scan_scope as scope

        scope.reset()
        order: list[str] = []

        def _run_artist(artist, force=False, progress_callback=None):
            order.append(f"artist:{artist}")

        monkeypatch.setattr(
            fs,
            "_run_global_playlist_rebuild",
            lambda options, force=False: order.append("rebuild"),
        )

        pp, _rec = _patch_orchestrator(monkeypatch, ["A", "B"], run_artist=_run_artist)
        try:
            pp._run_full_scan_as_artist_pipeline()
        finally:
            scope.reset()

        assert order == ["artist:A", "artist:B", "rebuild"], (
            "the global rebuild aggregates over the whole library, so it must run "
            "after every artist"
        )

    def test_the_deferral_is_cleared_even_when_the_artist_loop_raises(
        self, monkeypatch
    ):
        """⚠️ A left-set flag would silently suppress LATER targeted scans.

        That failure is worse than a missing rebuild and is invisible without
        asserting it: the next artist-page scan would quietly skip its playlist
        rebuild.

        ⚠️ An artist pipeline that RAISES is not enough to test this: the loop
        wraps each artist in ``_bounded_call_report``, which catches the
        exception, records the artist as failed and carries on (correctly — one
        bad artist must not kill the whole scan). So the raise has to come from
        the loop itself, which is where ``is_stop_requested`` sits: it is called
        after ``defer_global_playlists()`` and inside the ``try``.
        """
        import services.popularity.stages.finalise_stage as fs
        import services.popularity.scan_scope as scope

        scope.reset()

        monkeypatch.setattr(
            fs, "_run_global_playlist_rebuild", lambda options, force=False: None
        )

        pp, _rec = _patch_orchestrator(monkeypatch, ["A", "B"])

        def _boom(_path):
            raise RuntimeError("the loop itself exploded")

        monkeypatch.setattr(pp, "is_stop_requested", _boom)

        try:
            with pytest.raises(RuntimeError):
                pp._run_full_scan_as_artist_pipeline()

            assert scope.global_playlists_deferred() is False, (
                "the deferral must be cleared in a finally — a stale True would "
                "suppress the playlist rebuild of every later targeted scan"
            )
        finally:
            scope.reset()

    def test_a_stopped_scan_does_not_run_the_end_of_scan_rebuild(self, monkeypatch):
        """Stop must stop — not then rebuild genre playlists for minutes."""
        import services.popularity.stages.finalise_stage as fs
        import services.popularity.scan_scope as scope

        scope.reset()
        rebuilds: list[bool] = []
        monkeypatch.setattr(
            fs,
            "_run_global_playlist_rebuild",
            lambda options, force=False: rebuilds.append(bool(force)),
        )

        pp, _rec = _patch_orchestrator(monkeypatch, ["A", "B", "C"])

        # Stop after the first artist.
        calls = {"n": 0}

        def _stop(path):
            calls["n"] += 1
            return calls["n"] > 1

        monkeypatch.setattr(pp, "is_stop_requested", _stop)

        try:
            pp._run_full_scan_as_artist_pipeline()
        finally:
            scope.reset()

        assert rebuilds == [], (
            "a stopped scan must not spend minutes on the end-of-scan playlist "
            "rebuild; the next run catches the playlists up"
        )
        assert scope.global_playlists_deferred() is False, (
            "the deferral must still be cleared on a stop"
        )


# ---------------------------------------------------------------------------
# Structural guards (the orchestrator cannot be exercised end-to-end cheaply)
# ---------------------------------------------------------------------------


def test_the_orchestrator_defers_before_the_artist_loop():
    """Pin the ordering that makes the whole fix work.

    ``defer_global_playlists()`` must be called BEFORE the loop, and the forced
    rebuild in the ``finally``. A future edit that reorders or drops the
    deferral would silently restore the per-artist rebuild.
    """
    src = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "services" / "scanning" / "pipelines" / "popularity_pipeline.py"
    ).read_text(encoding="utf-8")

    assert "defer_global_playlists()" in src, "the orchestrator must defer"
    assert "resume_global_playlists()" in src, "and must always clear the deferral"
    assert "_run_global_playlist_rebuild({}, force=True)" in src, (
        "the end-of-scan rebuild must be forced, or the deferral check would "
        "suppress the orchestrator's own call too"
    )

    defer_at = src.index("defer_global_playlists()")
    loop_at = src.index("def _run_one_artist")
    resume_at = src.index("resume_global_playlists()")
    assert defer_at < loop_at < resume_at, (
        "deferral must open before the artist loop and be cleared after it"
    )


def test_the_rebuild_declares_the_scan_scope_reason():
    """The guard must keep explaining WHY it cannot be a marker on ``options``."""
    src = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "services" / "popularity" / "stages" / "finalise_stage.py"
    ).read_text(encoding="utf-8")

    start = src.index("def _run_global_playlist_rebuild")
    body = src[start:start + 2000]

    assert "scan_scope" in body, (
        "the function must point at the module that owns scan scope"
    )
    assert "force: bool = False" in body, "the forced end-of-scan call needs it"
