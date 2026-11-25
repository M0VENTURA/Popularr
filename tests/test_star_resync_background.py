"""The Star Rating Resync runs as a BACKGROUND job, not a blocking request.

WHY
---
The apply used to be ``await asyncio.to_thread(resync_star_ratings, ...)`` — the
HTTP request stayed open until the whole library was re-rated. On a real library
that outlives the reverse proxy's ~100s origin timeout, which answers with its
OWN html error page; the card's ``resp.json()`` then dies on
``Unexpected token '<'``. The work completed, only the response was lost, and
there was no progress to show meanwhile.

These tests pin the contract the fix relies on:
  * the service exposes a start-now / poll-later pair;
  * progress is published to a ``scan_states`` row the dashboard actually lists;
  * the scoring maths is NOT batched (velocity needs the whole catalogue);
  * the Config card POSTs to the background endpoint and polls status.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest

from services.popularity import star_resync_service as svc


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. The visibility contract (dashboard + card)
# ---------------------------------------------------------------------------

class TestTheJobIsVisible:
    def test_the_scan_type_is_in_the_progress_allow_list(self) -> None:
        from services.scanning.pipelines.progress_service import SCAN_TYPES

        assert svc.SCAN_TYPE in SCAN_TYPES, (
            "the dashboard poller only returns listed scan types, so the job "
            "would never appear on the dashboard while it runs"
        )

    def test_it_has_a_display_name(self) -> None:
        src = (REPO_ROOT / "static/js/activity-feed.js").read_text(encoding="utf-8")
        assert f"{svc.SCAN_TYPE}:" in src, (
            "without a display name the dashboard shows the raw scan_type"
        )

    def test_the_progress_file_matches_the_scan_type(self) -> None:
        assert svc.PROGRESS_FILE == f"{svc.SCAN_TYPE}_progress.json"


# ---------------------------------------------------------------------------
# 2. The start/poll service contract
# ---------------------------------------------------------------------------

class TestStartAndPoll:
    def test_the_service_exposes_start_and_status_helpers(self) -> None:
        for name in ("start_star_resync", "is_resync_running"):
            assert callable(getattr(svc, name, None)), f"{name} is missing"
            assert name in svc.__all__

    def test_start_returns_immediately_without_rating(self, monkeypatch) -> None:
        """``start`` must NOT run the rating pass on the calling thread.

        The whole point is that the request returns at once; if it called
        ``resync_star_ratings`` synchronously the proxy timeout would be back.
        """
        ran: list[str] = []

        def _fake_resync(**kwargs):
            ran.append("resync")
            return {"ok": True, "albums": 0, "tracks": 0, "changed": 0}

        monkeypatch.setattr(svc, "resync_star_ratings", _fake_resync)
        monkeypatch.setattr(
            "helpers.config_helpers.get_star_resync_config",
            lambda config=None: {"enabled": 1, "albums_per_batch": 5},
        )

        # Keep the spawned thread from actually doing work during the assert.
        started = {"threads": []}

        class _NoStart:
            def __init__(self, *a, **k):
                started["threads"].append(k.get("name"))

            def start(self):
                # Deliberately do NOT call target: we only assert it was armed.
                pass

        monkeypatch.setattr("threading.Thread", _NoStart)

        result = svc.start_star_resync()
        assert result == {"ok": True, "started": True}
        assert ran == [], "start_star_resync ran the rating pass inline"
        assert started["threads"] == ["star-resync"]

    def test_start_refuses_when_disabled(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "helpers.config_helpers.get_star_resync_config",
            lambda config=None: {"enabled": 0},
        )
        svc._release_apply()
        result = svc.start_star_resync()
        assert result["ok"] is False
        assert "disabled" in result["error"].lower()
        svc._release_apply()

    def test_start_is_single_flight(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "helpers.config_helpers.get_star_resync_config",
            lambda config=None: {"enabled": 1, "albums_per_batch": 5},
        )
        ran = {"n": 0}

        class _NoStart:
            def __init__(self, *a, **k):
                pass

            def start(self):
                ran["n"] += 1

        monkeypatch.setattr("threading.Thread", _NoStart)
        svc._release_apply()
        first = svc.start_star_resync()
        second = svc.start_star_resync()
        assert first["started"] is True
        assert second == {"ok": True, "started": False, "already_running": True}
        assert ran["n"] == 1, "a second apply was armed while one was claimed"
        svc._release_apply()

    def test_a_preview_is_backgrounded_and_keeps_its_rows(self, monkeypatch) -> None:
        """Preview uses the SAME background job and carries its changed rows.

        The inline preview returned the reverse proxy's HTML page on a large
        library (the reported ``Unexpected token '<'``); the rows now travel in
        the progress result so the poller can render them.
        """
        captured: dict = {}

        def _fake_resync(**kwargs):
            assert kwargs.get("dry_run") is True
            return {
                "ok": True,
                "albums": 1,
                "tracks": 2,
                "changed": 2,
                "changed_tracks": [{"title": "X", "before": 3, "after": 4}],
                "star_histogram": {"before": {}, "after": {}, "total": 0},
                "five_star_albums": [],
                "failed": [],
                "seconds": 0.1,
            }

        monkeypatch.setattr(svc, "resync_star_ratings", _fake_resync)
        monkeypatch.setattr(
            "helpers.config_helpers.get_star_resync_config",
            lambda config=None: {"enabled": 1, "albums_per_batch": 5},
        )
        monkeypatch.setattr(svc, "_write_progress", lambda **kw: captured.update(kw))

        class _RunNow:
            def __init__(self, *a, **k):
                self.target = k.get("target")

            def start(self):
                self.target()

        monkeypatch.setattr("threading.Thread", _RunNow)
        svc._release_apply()
        result = svc.start_star_resync(dry_run=True)
        svc._release_apply()

        assert result["started"] is True
        assert captured["result"]["dry_run"] is True
        assert captured["result"]["changed_tracks"] == [
            {"title": "X", "before": 3, "after": 4}
        ]

    def test_a_stale_progress_row_is_not_running(self) -> None:
        """A crashed job must not block every future attempt with 409."""
        from datetime import datetime, timedelta

        old = (datetime.now() - timedelta(hours=1)).isoformat()
        state = {"is_running": True, "last_updated": old}
        assert svc._progress_row_is_stale(state) is True

        fresh = {"is_running": True, "last_updated": datetime.now().isoformat()}
        assert svc._progress_row_is_stale(fresh) is False


# ---------------------------------------------------------------------------
# 3. The maths must NOT be batched (velocity needs the whole catalogue)
# ---------------------------------------------------------------------------

class TestVelocityIsNotBrokenByBatching:
    def test_the_load_is_not_chunked_by_albums_per_batch(self) -> None:
        """``albums_per_batch`` may report progress, but must not chunk the LOAD.

        ``_apply_velocity_adjustments`` normalises each track against its
        artist's FASTEST track and finds ListenBrainz collisions across the
        catalogue — both need every album in memory at once. Splitting the load
        would silently change ratings.
        """
        src = inspect.getsource(svc.resync_star_ratings)
        assert "_load_album_groups" in src
        # The loader is called exactly once, with no batch/offset argument.
        assert "albums_per_batch" not in src, (
            "resync_star_ratings must not consult albums_per_batch — batching "
            "the load would break velocity normalisation"
        )

    def test_velocity_runs_before_rating(self) -> None:
        src = inspect.getsource(svc.resync_star_ratings)
        assert src.index("_apply_velocity_adjustments") < src.index(
            "_compute_stars_for_album"
        ), "velocity must be applied to the loaded rows before they are rated"


# ---------------------------------------------------------------------------
# 4. The Config card uses the background endpoint
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("js_rel", (
    "static/js/config.js",
    "test_site/static/js/pages/config.js",
))
class TestTheCardPolls:
    def test_apply_posts_to_the_background_endpoint(self, js_rel: str) -> None:
        js = (REPO_ROOT / js_rel).read_text(encoding="utf-8")
        assert "/api/ratings/resync/apply" in js, (
            "the Apply button must use the background endpoint, or the request "
            "is held open past the proxy timeout"
        )
        assert "/api/ratings/resync/status" in js, "the card must poll status"

    def test_there_is_a_poller(self, js_rel: str) -> None:
        js = (REPO_ROOT / js_rel).read_text(encoding="utf-8")
        assert "async function pollStarResync(" in js

    def test_a_running_job_resumes_polling_on_load(self, js_rel: str) -> None:
        js = (REPO_ROOT / js_rel).read_text(encoding="utf-8")
        assert "pollStarResync(true)" in js, (
            "a job started from another tab (or before a reload) must resume "
            "its progress rather than leaving an idle card"
        )


# ---------------------------------------------------------------------------
# 5. The routes exist
# ---------------------------------------------------------------------------

class TestTheRoutesExist:
    def test_apply_and_status_routes_are_registered(self) -> None:
        src = (REPO_ROOT / "routes" / "misc_routes.py").read_text(encoding="utf-8")
        assert '@misc_api_bp.route("/ratings/resync/apply", methods=["POST"])' in src
        assert '@misc_api_bp.route("/ratings/resync/status", methods=["GET"])' in src
        assert "async def api_apply_star_ratings_resync" in src
        assert "def api_star_ratings_resync_status" in src

    def test_the_old_synchronous_route_is_kept_for_preview(self) -> None:
        """The inline route stays for the read-only preview (and back-compat)."""
        src = (REPO_ROOT / "routes" / "misc_routes.py").read_text(encoding="utf-8")
        assert '@misc_api_bp.route("/ratings/resync", methods=["POST"])' in src
