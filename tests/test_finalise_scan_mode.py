"""A dashboard "Finalise" scan: finalise everything, compute only what is missing.

Requested:

> Can we also add a Finalise scan option to the dashboard? This will just run
> the finalise scan for all files using the existing database information from
> the files, only running the popularity and singles detection for files that
> are missing the information.

The mode is built from machinery that already exists, wired together so the
three promises hold:

* **every file is finalised** — the runner's album skip gates (freshness,
  skip-unchanged, completeness) are bypassed for ``finalise_only``, so each
  album reaches the per-album file-tag sync and the end-of-run
  ``finalise_scan``;
* **popularity only where missing** — the mode rides the singles machinery,
  whose ``_has_stored_popularity`` branch reuses the stored score, and the
  runner forces ``refresh_popularity_if_due`` off so a staleness window never
  refreshes a score that already exists (a track with no score computes
  regardless);
* **DB-only otherwise** — full enrichment, album-type resolution/persist,
  annotation repair, cover detection, the metadata-recommend stash and the
  missing-track recompute are all gated off for the pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

RUNNER_SOURCE = (REPO_ROOT / "services" / "popularity" / "scan_stage_runner.py").read_text(encoding="utf-8")
ALBUM_STAGE_SOURCE = (REPO_ROOT / "services" / "popularity" / "stages" / "album_stage.py").read_text(encoding="utf-8")
PIPELINE_SOURCE = (REPO_ROOT / "services" / "scanning" / "pipelines" / "popularity_pipeline.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. The request validates and dispatches
# ---------------------------------------------------------------------------

class TestTheModeIsAcceptedAndDispatched:
    def test_scan_request_accepts_finalise(self):
        from routes.schemas import ScanRequest

        assert ScanRequest(mode="finalise").mode == "finalise"

    @pytest.mark.parametrize("mode", ["popularity", "singles", "metadata", "all"])
    def test_the_other_modes_still_validate(self, mode: str):
        from routes.schemas import ScanRequest

        assert ScanRequest(mode=mode).mode == mode

    def test_an_unknown_mode_is_still_rejected(self):
        from routes.schemas import ScanRequest

        with pytest.raises(ValidationError):
            ScanRequest(mode="wipe-everything")

    def test_dispatch_passes_the_finalise_flags(self, monkeypatch):
        """mode=finalise must ride the singles machinery (stored-score reuse)
        AND carry the finalise flag the runner's gates read."""
        from services.scanning.pipelines import popularity_pipeline as pp

        seen: dict = {}

        def _fake_run(**kwargs):
            seen.update(kwargs)
            return True

        monkeypatch.setattr(pp, "run_popularity_scan", _fake_run)
        monkeypatch.setattr(pp, "write_progress_with_current_artist", lambda *a, **k: None)
        monkeypatch.setattr(pp, "get_scan_progress_path", lambda *a, **k: "/tmp/fake-progress.json")
        monkeypatch.setattr(pp, "is_stop_requested", lambda *a, **k: False)

        pp.run_popularity_mode(mode="finalise")

        assert seen.get("finalise_only") is True
        assert seen.get("singles_only") is True
        assert "popularity_only" not in seen, "finalise must not flip the popularity_only gates"
        assert "metadata_only" not in seen

    def test_dispatch_of_popularity_mode_is_unchanged(self, monkeypatch):
        """CONTROL — the existing modes must not inherit finalise behaviour."""
        from services.scanning.pipelines import popularity_pipeline as pp

        seen: dict = {}
        monkeypatch.setattr(pp, "run_popularity_scan", lambda **kw: seen.update(kw) or True)
        monkeypatch.setattr(pp, "write_progress_with_current_artist", lambda *a, **k: None)
        monkeypatch.setattr(pp, "get_scan_progress_path", lambda *a, **k: "/tmp/fake-progress.json")
        monkeypatch.setattr(pp, "is_stop_requested", lambda *a, **k: False)

        pp.run_popularity_mode(mode="popularity")

        assert seen.get("popularity_only") is True
        assert "finalise_only" not in seen


# ---------------------------------------------------------------------------
# 2. The runner's gates: every file, missing-only, DB-only
# ---------------------------------------------------------------------------

class TestTheRunnerProcessesEveryAlbumForFinalise:
    def test_the_mode_is_plumbed_into_options(self):
        assert "finalise_only: bool = False" in RUNNER_SOURCE
        assert '"finalise_only": finalise_only,' in RUNNER_SOURCE

    def test_the_skip_gates_are_bypassed(self):
        """Freshness / skip-unchanged / completeness must not stop the album
        before the file-tag sync — that sync IS the finalise pass."""
        assert "if not force and not album_filter and not _mode_finalise:" in RUNNER_SOURCE

    def test_the_progress_label_names_the_pass(self):
        assert '"Finalise Pass" if options.get("finalise_only")' in RUNNER_SOURCE

    def test_a_stored_score_is_never_window_refreshed(self):
        """"…only running the popularity … for files that are missing the
        information": the staleness window must not re-score what exists."""
        assert "if _mode_finalise:" in RUNNER_SOURCE
        idx = RUNNER_SOURCE.index("if _mode_finalise:")
        assert "_pop_due = False" in RUNNER_SOURCE[idx: idx + 700]

    def test_network_metadata_work_is_gated_off(self):
        """The recommend-stash and the missing-track recompute both reach
        MusicBrainz; a finalise pass is DB-only."""
        assert (
            'if _stash_only and not options.get("popularity_only") '
            'and not options.get("singles_detection_only") and not _mode_finalise:'
        ) in RUNNER_SOURCE
        assert (
            'if not options.get("popularity_only") '
            'and not options.get("singles_detection_only") and not _mode_finalise:\n'
            "                try:\n"
            "                    _missing = get_missing_tracks(artist=artist, album=album)"
        ) in RUNNER_SOURCE

    def test_the_file_tag_sync_is_NOT_gated_away(self):
        """CONTROL — the whole point of the pass is this sync; its gate must
        keep letting a singles-riding finalise mode through."""
        idx = RUNNER_SOURCE.index("--- FILE TAG SYNC ---")
        window = RUNNER_SOURCE[idx: idx + 400]
        assert 'if not options.get("popularity_only") and not options.get("singles_detection_only"):' in window
        assert "sync_album_file_tags(" in window
        assert "_mode_finalise" not in window.split("sync_album_file_tags(")[0], (
            "the finalise mode was excluded from its own sync"
        )


class TestEnrichmentStaysOutOfTheFinalisePass:
    def test_the_album_stage_uses_the_light_type_path(self):
        """The DB-only pass takes the stored-value/heuristic branch — never
        `_resolve_album_type` + `_persist_album_type_to_tracks`."""
        assert "finalise_pass = bool(options.get(\"finalise_only\"))" in ALBUM_STAGE_SOURCE
        assert "if popularity_pass or finalise_pass:" in ALBUM_STAGE_SOURCE

    def test_full_enrichment_stays_deferred(self):
        assert "_full_pass = not (_mode_meta or _mode_pop or _mode_singles or options.get(\"singles_detection_only\"))" in RUNNER_SOURCE, (
            "finalise rides singles_only, so _full_pass must stay False and "
            "set defer_full_enrichment"
        )


# ---------------------------------------------------------------------------
# 3. The dashboard option
# ---------------------------------------------------------------------------

class TestTheDashboardOffersTheMode:
    @pytest.mark.parametrize(
        "rel",
        ["templates/pages/dashboard.html", "test_site/templates/Pages/dashboard.html"],
    )
    def test_the_selector_has_the_option(self, rel: str):
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert '<option value="finalise">Finalise</option>' in source, rel

    @pytest.mark.parametrize(
        "rel",
        [
            "static/js/scan-preflight.js",
            "test_site/static/js/services/scan-preflight.js",
            "static/js/dashboard.js",
            "test_site/static/js/pages/dashboard.js",
        ],
    )
    def test_the_label_maps_name_it(self, rel: str):
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "finalise: " in source or '"finalise": ' in source, (
            f"{rel}: no Finalise label — the preflight confirm would say a bare mode key"
        )

    def test_the_pipeline_dispatch_has_the_documented_branch(self):
        assert 'elif mode == "finalise":' in PIPELINE_SOURCE
        assert 'kwargs["finalise_only"] = True' in PIPELINE_SOURCE
