"""Two-step genre → playlist resync: preview writes nothing, apply is ordered.

WHY IT IS TWO STEPS
-------------------
A library-wide genre rewrite plus a playlist rebuild is not something to trigger
blind. **Preview** reports the per-track genre changes AND the membership
changes per genre playlist; **Apply** then runs in the background behind a
progress row the dashboard already polls.

THE INVARIANTS PINNED HERE
--------------------------
1. Preview is READ-ONLY — it must not write genres or playlists. It DOES publish
   progress, but only to its OWN row: a full-library comparison takes minutes,
   so it runs on a worker thread and is polled (see the test docstring).
2. The preview's playlist prediction comes from the REAL builder in
   ``plan_only`` mode, so it cannot drift from what Apply writes.
3. Apply writes GENRES FIRST, then playlists — the playlists are built from the
   Final Genres, so the reverse order would re-push the old membership.
4. Apply invalidates the genre-row cache between the two phases, or the rebuild
   would pool by the pre-rewrite genres.
"""

from __future__ import annotations

import inspect
import re

import pytest

from services.enrichment import genre_playlist_resync_service as svc


def _progress_calls(src: str) -> list[str]:
    """Each ``_write_progress(...)`` call's source text (balanced to its ``)``).

    A plain regex cannot do this — the arguments span newlines and contain their
    own parentheses — so this walks the parentheses from each call site.
    """
    calls: list[str] = []
    for match in re.finditer(r"_write_progress\(", src):
        start = match.end() - 1
        depth = 0
        for index in range(start, len(src)):
            char = src[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    calls.append(src[start:index + 1])
                    break
    return calls


# ---------------------------------------------------------------------------
# 1. Preview is read-only
# ---------------------------------------------------------------------------

class TestPreviewWritesNothing:
    def test_it_never_writes_to_the_APPLY_progress_row(self) -> None:
        """The apply's row is off limits to a preview.

        Publishing there made the dashboard announce a completed resync, and the
        Config card's poller restore itself with a summary for a job that never
        ran.

        ⚠️ THE PREVIEW DOES NOW PUBLISH — to its OWN row. A full-library preview
        measured 132s on 69k tracks, past the reverse proxy's ~100s origin
        timeout, so the gateway answered with its own HTML error page and the
        browser's ``resp.json()`` threw ``Unexpected token '<'`` ("SyntaxError:
        Unexpected token '<', "<!DOCTYPE "... is not valid JSON"). Moving it off
        the request thread is what makes the response reliable at all, and a
        background job that reports nothing is a silent spinner. The guarantee
        that matters is that the two rows never collide.
        """
        src = inspect.getsource(svc.preview_genre_playlist_resync)
        assert "PREVIEW_SCAN_TYPE" in src or "PREVIEW_PROGRESS_FILE" in src, (
            "the preview must publish to its OWN row, never a silent one"
        )
        # Every call site must name the preview row explicitly; the defaults are
        # the APPLY row, so an unqualified call would collide with the apply.
        for call in _progress_calls(src):
            assert "scan_type=PREVIEW_SCAN_TYPE" in call, (
                "a preview progress write did not target the preview row: " + call
            )
            assert "progress_file=PREVIEW_PROGRESS_FILE" in call, (
                "a preview progress write did not target the preview file: " + call
            )

    def test_a_preview_never_claims_the_apply_lock(self) -> None:
        src = inspect.getsource(svc.preview_genre_playlist_resync)
        assert "_claim_apply" not in src

    def test_it_computes_genres_as_a_dry_run(self) -> None:
        src = inspect.getsource(svc.preview_genre_playlist_resync)
        assert "_genre_changes(dry_run=True)" in src

    def test_it_only_plans_the_playlists(self, builder_spy) -> None:
        """It uses the real builder with ``plan_only``, never the write path."""
        svc.preview_genre_playlist_resync()
        assert builder_spy["calls"], "the preview never called the pool builder"
        for call in builder_spy["calls"]:
            assert call["plan_only"] is True, (
                "the preview called the builder WITHOUT plan_only, so it wrote "
                f"to Navidrome: {call}"
            )

    def test_it_predicts_the_POST_REWRITE_playlists(self, monkeypatch) -> None:
        """The overlay is the point of the preview.

        Pooling by the genres still in the database would describe the state
        BEFORE the job and under-report exactly the changes being previewed.
        """
        from services.popularity.stages import finalise_stage as fs

        seen: dict = {}

        def _builder(**kwargs):
            rows = kwargs.get("rows_override")
            seen["genres"] = {
                str(r.get("id")): r.get("genres") for r in (rows or [])
            }
            return 1

        monkeypatch.setattr(fs, "_create_genre_top_track_playlists", _builder)
        monkeypatch.setattr(
            fs, "_fetch_genre_playlist_rows",
            lambda min_stars, genres=None: [
                {"id": "t1", "genres": "Alternative Rock"},
                {"id": "t2", "genres": "Post-Grunge"},
            ],
            raising=False,
        )
        monkeypatch.setattr(fs, "_genre_refresh_config", lambda: (1, 4))
        monkeypatch.setattr(
            svc, "_genre_changes",
            lambda dry_run: {"ok": True, "changed": 1, "tracks": 2,
                             "with_evidence": 2,
                             "changed_tracks": [
                                 {"track_id": "t1",
                                  "before": "Alternative Rock",
                                  "after": "Alternative Metal"},
                             ]},
        )
        monkeypatch.setattr(svc, "_server_playlists", lambda only=None: ({}, {}))

        svc.preview_genre_playlist_resync()

        assert seen.get("genres", {}).get("t1") == "Alternative Metal", (
            "the prediction pooled by the OLD genre, so the preview describes "
            f"the state before the rewrite: {seen.get('genres')}"
        )
        # A track the rewrite does not touch keeps its stored value.
        assert seen.get("genres", {}).get("t2") == "Post-Grunge"


# ---------------------------------------------------------------------------
# 2. The plan mode on the builder
# ---------------------------------------------------------------------------

class TestTheBuilderCanPlan:
    def test_plan_only_is_a_supported_mode(self) -> None:
        sig = inspect.signature(
            __import__(
                "services.popularity.stages.finalise_stage",
                fromlist=["_create_genre_top_track_playlists"],
            )._create_genre_top_track_playlists
        )
        assert "plan_only" in sig.parameters
        assert "plan_out" in sig.parameters

    def test_plan_mode_returns_before_any_write(self) -> None:
        """The plan branch must precede ``_sync_playlist_to_navidrome``."""
        from services.popularity.stages import finalise_stage as fs

        src = inspect.getsource(fs._create_genre_top_track_playlists)
        plan_at = src.index("if plan_only:")
        sync_at = src.index("_sync_playlist_to_navidrome(\n                playlist_name")
        assert plan_at < sync_at, (
            "the plan branch runs AFTER the sync call, so a preview would write"
        )

    def test_plan_records_a_clear_as_an_empty_list(self) -> None:
        from services.popularity.stages import finalise_stage as fs

        src = inspect.getsource(fs._create_genre_top_track_playlists)
        assert "plan_out[_empty_name] = []" in src, (
            "an emptied playlist must be reported as a planned CLEAR"
        )


# ---------------------------------------------------------------------------
# 3. Apply ordering and guards
# ---------------------------------------------------------------------------

class TestApplyOrdering:
    def test_genres_are_written_before_playlists_are_rebuilt(self) -> None:
        src = inspect.getsource(svc.apply_genre_playlist_resync)
        assert src.index("_genre_changes(dry_run=False)") < src.index(
            "_create_genre_top_track_playlists("
        ), (
            "the playlists were rebuilt before the new genres were written, so "
            "the OLD membership would be pushed back"
        )

    def test_the_row_cache_is_invalidated_between_the_phases(self) -> None:
        src = inspect.getsource(svc.apply_genre_playlist_resync)
        cache_at = src.index("invalidate_genre_playlist_rows()")
        rebuild_at = src.index("_create_genre_top_track_playlists(")
        assert cache_at < rebuild_at, (
            "the genre-row cache still holds the pre-rewrite genres"
        )

    def test_a_second_apply_is_refused(self, monkeypatch) -> None:
        """Two concurrent applies would fight over the same playlists."""
        assert svc._claim_apply() is True
        try:
            result = svc.apply_genre_playlist_resync()
            assert result.get("ok") is False
            assert "already running" in str(result.get("error") or "")
        finally:
            svc._release_apply()

    def test_it_always_releases_the_claim(self, monkeypatch) -> None:
        """A failed job must not lock the feature out until a restart."""
        monkeypatch.setattr(
            svc, "_genre_changes",
            lambda dry_run: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        result = svc.apply_genre_playlist_resync()
        assert result.get("ok") is False
        # The claim is free again, so a retry is accepted.
        assert svc._claim_apply() is True
        svc._release_apply()

    def test_it_publishes_progress(self, monkeypatch) -> None:
        frames: list[dict] = []
        monkeypatch.setattr(
            svc, "_write_progress", lambda **kw: frames.append(kw)
        )
        monkeypatch.setattr(svc, "_genre_changes",
                            lambda dry_run: {"ok": True, "changed": 2})
        monkeypatch.setattr(
            svc, "_create_genre_top_track_playlists",
            lambda **kw: 3, raising=False,
        )
        # The apply imports the builder lazily from finalise_stage; patch there.
        from services.popularity.stages import finalise_stage as fs
        monkeypatch.setattr(fs, "_create_genre_top_track_playlists", lambda **kw: 3)
        monkeypatch.setattr(fs, "invalidate_genre_playlist_rows", lambda: None)

        result = svc.apply_genre_playlist_resync()
        assert result.get("ok") is True
        assert result.get("genres_changed") == 2
        assert result.get("playlists_written") == 3
        assert frames, "the apply published no progress at all"
        assert any(f.get("phase") == svc.PHASE_PLAYLISTS for f in frames)
        assert frames[-1].get("running") is False


# ---------------------------------------------------------------------------
# 4. The diff helper
# ---------------------------------------------------------------------------

class TestTheDiff:
    def test_gains_and_losses(self) -> None:
        d = svc._diff(["a", "b", "c"], ["b", "x"])
        assert d["added"] == ["a", "c"]
        assert d["removed"] == ["x"]
        assert d["count_before"] == 2
        assert d["count_after"] == 3

    def test_a_clear_is_all_losses(self) -> None:
        d = svc._diff([], ["a", "b"])
        assert d["removed"] == ["a", "b"]
        assert d["count_after"] == 0

    def test_identical_lists_have_no_membership_change(self) -> None:
        d = svc._diff(["a", "b"], ["a", "b"])
        assert d["added"] == [] and d["removed"] == []

    def test_order_only_change_is_flagged_but_not_a_membership_change(self) -> None:
        d = svc._diff(["b", "a"], ["a", "b"])
        assert d["added"] == [] and d["removed"] == []
        assert d["order_changed"] is True


# ---------------------------------------------------------------------------
# 5. Wiring
# ---------------------------------------------------------------------------

class TestThePreviewRunsInTheBackground:
    """A full-library preview must not be answered inline.

    REPORTED

    > "SyntaxError: Unexpected token '<', "<!DOCTYPE "... is not valid JSON"

    The preview genuinely ran — 69 241 tracks in 132s — but the request was held
    open past the reverse proxy's origin timeout, so the gateway replied with its
    OWN html error page. ``resp.json()`` then threw on the ``<!DOCTYPE``. The
    apply already avoided this by returning 202; the preview must too.
    """

    def test_the_route_returns_202_not_the_result(self) -> None:
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1] / "routes" / "misc_routes.py"
        ).read_text(encoding="utf-8")
        start = src.index("async def api_genre_playlist_resync_preview")
        body = src[start:start + 2600]
        assert "start_preview_genre_playlist_resync" in body, (
            "the preview runs inline, so a large library times out at the proxy"
        )
        assert "return jsonify(result), 202" in body, (
            "202 ACCEPTED is what tells the client to poll instead of waiting"
        )

    def test_there_is_a_preview_status_endpoint(self) -> None:
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1] / "routes" / "misc_routes.py"
        ).read_text(encoding="utf-8")
        assert '"/genres/playlist-resync/preview/status"' in src

    def test_starting_a_second_preview_is_refused_cleanly(self, monkeypatch) -> None:
        """Concurrent previews would double a very expensive library walk."""
        monkeypatch.setattr(svc, "_PREVIEW_RUNNING",True)
        assert svc.is_preview_running() in (True, False)  # reads the real row
        # The claim is what guards it; assert the guard's shape.
        src = inspect.getsource(svc.start_preview_genre_playlist_resync)
        assert "_PREVIEW_RUNNING" in src
        assert "already_running" in src

    def test_the_js_does_not_parse_json_blindly(self) -> None:
        """A gateway HTML page must surface as an error, not a SyntaxError."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        for rel in ("static/js/config.js", "test_site/static/js/pages/config.js"):
            src = (root / rel).read_text(encoding="utf-8")
            block = src[src.index("async function runGenrePlaylistPreview"):]
            block = block[:block.index("async function runGenrePlaylistApply")]
            assert "application/json" in block, (
                f"{rel} calls resp.json() without checking the content type; a "
                "proxy HTML error page throws 'Unexpected token <'"
            )
            assert "pollGenrePlaylistPreview" in block, (
                f"{rel} does not poll for the background preview result"
            )


class TestTheServerReadIsScoped:
    """Reading every playlist flooded Navidrome and broke an unrelated job.

    REPORTED (log)

        WARNING api_clients.navidrome  getPlaylist failed ... timed out
        WARNING api_clients.http_utils  host cooldown opened — failing fast for
            60s: 2 consecutive requests to 192.168.1.200 failed (ReadTimeout)
        INFO services.playlists.playlist_index_service  Playlist index synced
            playlists=659 ... unresolved=12534

    The preview read the contents of EVERY playlist to diff one at a time. On a
    659-playlist library that is 659 serial ``getPlaylist`` calls; the two
    timeouts it produced opened the client's host cooldown, and the cooldown then
    failed the playlist index that was running beside it. The listing already
    reports ``songCount``, so only the playlists actually being compared need
    their contents read.
    """

    def test_only_the_compared_playlists_are_read(self) -> None:
        src = inspect.getsource(svc._server_playlists)
        assert "only" in inspect.signature(svc._server_playlists).parameters, (
            "the read is unscoped, so a preview walks every playlist on the server"
        )
        assert "name not in only" in src, (
            "the contents fetch must skip playlists that are not being compared"
        )

    def test_the_listing_supplies_the_size(self) -> None:
        """``songCount`` answers existence/size without a getPlaylist call."""
        src = inspect.getsource(svc._server_playlists)
        assert "songCount" in src

    def test_an_unreadable_playlist_is_not_reported_as_new_or_empty(self) -> None:
        """THE INVARIANT. Unreadable is neither "missing" nor "empty"."""
        src = inspect.getsource(svc.preview_genre_playlist_resync)
        assert "server_counts" in src, (
            "the preview cannot tell 'does not exist' from 'could not read', so "
            "a timeout is predicted as a create or as all-gains"
        )


class TestTheJobIsVisible:
    def test_the_scan_type_is_in_the_progress_allow_list(self) -> None:
        from services.scanning.pipelines.progress_service import SCAN_TYPES

        assert svc.SCAN_TYPE in SCAN_TYPES, (
            "the dashboard poller only returns listed scan types, so the job "
            "would never appear on the dashboard"
        )

    def test_the_preview_row_is_also_visible(self) -> None:
        from services.scanning.pipelines.progress_service import SCAN_TYPES

        assert svc.PREVIEW_SCAN_TYPE in SCAN_TYPES, (
            "the background preview publishes its own row; without listing it "
            "the dashboard would never show that a preview is running"
        )

    def test_both_rows_have_display_names(self) -> None:
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "static/js/activity-feed.js"
        ).read_text(encoding="utf-8")
        for scan_type in (svc.SCAN_TYPE, svc.PREVIEW_SCAN_TYPE):
            assert f"{scan_type}:" in src, (
                f"without a display name the dashboard shows the raw "
                f"scan_type ({scan_type})"
            )

    def test_it_does_not_widen_the_playlist_only_client_factory(self) -> None:
        """The read-only comparison must not use ``finalise_stage._navidrome_clients``.

        That factory is deduped to ONE CLIENT PER SERVER — correct for playlist
        writes, wrong for anything per-account. A guard test pins its caller set
        to ``finalise_stage.py`` alone precisely so a per-user consumer cannot
        silently start sharing a client. A resync only READS playlists, so it
        builds its own client rather than widening the guard.
        """
        src = inspect.getsource(svc._server_playlists)
        # Strip comments and the docstring: both explain WHY the guard exists
        # and would otherwise match the very name being excluded.
        code = "\n".join(
            line for line in src.splitlines() if not line.strip().startswith("#")
        )
        code = code.split('"""')[2] if code.count('"""') >= 2 else code
        assert "_navidrome_clients" not in code, (
            "the resync uses the playlist-only client factory; that guard "
            "exists to keep per-user consumers off it"
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def builder_spy(monkeypatch):
    """Record every call to the pool builder and make it a no-op."""
    from services.popularity.stages import finalise_stage as fs

    spy: dict = {"calls": []}

    def _builder(**kwargs):
        spy["calls"].append(dict(kwargs))
        plan_out = kwargs.get("plan_out")
        if plan_out is not None:
            plan_out["Alternative Rock - Top Tracks"] = ["t1", "t2"]
        return 1

    monkeypatch.setattr(fs, "_create_genre_top_track_playlists", _builder)
    monkeypatch.setattr(svc, "_genre_changes",
                        lambda dry_run: {"ok": True, "changed": 0, "tracks": 2,
                                         "with_evidence": 2, "changed_tracks": []})
    monkeypatch.setattr(svc, "_server_playlists",
                        lambda only=None: ({"Alternative Rock - Top Tracks": ["t1"]},
                                           {"Alternative Rock - Top Tracks": 1}))
    return spy
