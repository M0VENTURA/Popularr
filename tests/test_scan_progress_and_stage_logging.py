"""Scan progress must not read "0%", and Singles Detection must narrate itself.

Two reports from a live full scan:

1. *\"full scan — 0% · Singles Detection · Afi - The Art of Drowning —
   Initiation\"* — the status bar claimed 0% while the stage was demonstrably
   working. One artist's share of a full scan is ``100 / total_artists``, and
   ``_cb`` ran the total through ``int()``, so on a large library the first
   artists never reached 1% and every bit of real progress was thrown away.
   ``progress_service`` truncated to ``int`` as well, so even a float payload
   would not have survived to the browser.
2. *\"there is nothing in the scanner system logs, but the info log is showing
   it correctly\"* — the album loop's stage bands and the per-album singles
   result were both silent: the singles result was only logged when the album
   actually yielded a single, so an album with none left no trace at all.
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

FINALISE_STAGE = REPO_ROOT / "services" / "popularity" / "stages" / "finalise_stage.py"
SCAN_LOG_SOURCES = (
    REPO_ROOT / "static" / "js" / "dashboard.js",
    REPO_ROOT / "static" / "js" / "main.js",
    REPO_ROOT / "test_site" / "static" / "js" / "pages" / "dashboard.js",
    REPO_ROOT / "test_site" / "static" / "js" / "main.js",
)


# ===========================================================================
# Stage bands and their announcement
# ===========================================================================
class TestStageBands:
    def test_the_first_quarter_is_metadata_and_the_last_is_singles(self):
        from services.scanning.pipeline import stage_for_album_index

        # band = (12 + 3) // 4 = 3
        assert [stage_for_album_index(i, 12) for i in range(12)] == (
            ["metadata"] * 3 + ["popularity"] * 6 + ["singles"] * 3
        )

    def test_a_four_album_artist_gets_one_album_per_outer_band(self):
        from services.scanning.pipeline import stage_for_album_index

        assert [stage_for_album_index(i, 4) for i in range(4)] == [
            "metadata", "popularity", "popularity", "singles",
        ]

    def test_the_same_band_is_reported_for_every_size(self):
        """The guard against the bands silently drifting back to 'album'/'final'."""
        from services.scanning.pipeline import stage_for_album_index

        for total in (1, 2, 5, 9, 40, 500):
            bands = [stage_for_album_index(i, total) for i in range(total)]
            assert set(bands) <= {"metadata", "popularity", "singles"}
            assert bands[0] == "metadata"


class TestStageChangeIsLoggedOnce:
    def _patch(self, monkeypatch) -> list[str]:
        import services.scanning.pipeline as pipeline_mod

        logged: list[str] = []
        monkeypatch.setattr(
            pipeline_mod, "log_unified", lambda message, **kw: logged.append(message)
        )
        return logged

    def test_each_band_is_announced_exactly_once(self, monkeypatch):
        from services.scanning.pipeline import announce_stage_change

        logged = self._patch(monkeypatch)
        state: dict = {}

        # The real callback fires per track as well as per album.
        for album in ("Album 1", "Album 2", "Album 3"):
            announce_stage_change("metadata", state, "Afi", album)
            announce_stage_change("metadata", state, "Afi", album)
        announce_stage_change("singles", state, "Afi", "Album 4")
        announce_stage_change("singles", state, "Afi", "Album 4")

        assert len(logged) == 2, logged
        assert "Metadata" in logged[0]
        assert "Singles Detection" in logged[1]

    def test_the_line_carries_the_artist_and_the_stage(self, monkeypatch):
        from services.scanning.pipeline import announce_stage_change

        logged = self._patch(monkeypatch)
        announce_stage_change("singles", {}, "Afi", "The Art of Drowning")

        assert len(logged) == 1
        assert "[SCAN_PIPELINE]" in logged[0]
        assert "Singles Detection" in logged[0]
        assert "Afi" in logged[0]
        assert "The Art of Drowning" in logged[0]

    def test_a_new_state_dict_restarts_the_announcement(self, monkeypatch):
        """Each artist run gets its own state — a stage must not be swallowed."""
        from services.scanning.pipeline import announce_stage_change

        logged = self._patch(monkeypatch)
        announce_stage_change("metadata", {}, "Artist One", "A")
        announce_stage_change("metadata", {}, "Artist Two", "B")

        assert len(logged) == 2, logged

    def test_the_announcement_survives_the_scanner_tab_filter(self):
        from services.log_service import _scan_activity_filter, _scheduler_noise_filter

        line = (
            "2026-10-05 09:15:00 [INFO] [services.scanning.pipeline] "
            "[SCAN_PIPELINE] Stage: Singles Detection — Afi / The Art of Drowning"
        )
        assert _scan_activity_filter().search(line), "the Scanner tab would drop it"
        assert not _scheduler_noise_filter().search(line)

    def test_the_singles_result_line_also_survives_the_filter(self):
        from services.log_service import _scan_activity_filter

        line = (
            "2026-10-05 09:15:00 [INFO] [popularr.unified] "
            "Singles Detection - Detected 0 single(s) in 'The Art of Drowning'"
        )
        assert _scan_activity_filter().search(line)


class TestSinglesResultIsReportedForEveryAlbum:
    def test_the_log_call_is_not_gated_on_detecting_a_single(self):
        """An album with no singles used to leave no trace in the Scanner tab."""
        source = FINALISE_STAGE.read_text(encoding="utf-8")
        tree = ast.parse(source)

        call_lines = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and "Singles Detection - Detected" in ast.unparse(node)
        ]
        assert call_lines, "the per-album singles result must still be logged"

        still_gated = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            if "singles_detected" not in ast.unparse(node.test):
                continue
            end = node.end_lineno or node.lineno
            still_gated.extend(
                (node.lineno, line) for line in call_lines if node.lineno <= line <= end
            )

        assert not still_gated, (
            "the singles result is still conditional on finding a single — an "
            f"album with none would log nothing at all: {still_gated}"
        )

    def test_the_popularity_only_mode_says_so_instead_of_reporting_zero(self):
        source = FINALISE_STAGE.read_text(encoding="utf-8")
        assert "skipped (popularity only)" in source


# ===========================================================================
# Progress percentage
# ===========================================================================
class TestProgressKeepsSubIntegerPrecision:
    def test_the_api_payload_does_not_truncate(self):
        from services.scanning.pipelines.progress_service import _as_percent

        # The whole point: a large library's first artists sit well under 1%.
        assert _as_percent(0.42) == 0.42
        assert _as_percent(0.016) == 0.016   # 400 artists, first album boundary
        assert _as_percent(0.005) == 0.005   # 5 000 artists — must not become 0
        assert _as_percent(0) == 0.0

    def test_it_still_clamps_and_survives_junk(self):
        from services.scanning.pipelines.progress_service import _as_percent

        assert _as_percent(None) == 0.0
        assert _as_percent("not a number") == 0.0
        assert _as_percent(-10) == 0.0
        assert _as_percent(150) == 100.0

    def test_whole_percentages_stay_integers_for_existing_consumers(self):
        from services.scanning.pipelines.progress_service import _as_percent

        # tests/test_full_scan_progress_counters.py asserts `== 17`.
        assert _as_percent(17) == 17
        assert _as_percent(100) == 100


# ===========================================================================
# The label the status bar prints
# ===========================================================================
@pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the JS"
)
class TestTheStatusLabelShowsSubOnePercent:
    def _probe(self, source: Path, values: list[float]) -> dict:
        node = shutil.which("node")
        program = f"""
        const fs = require('fs');
        const src = fs.readFileSync({json.dumps(str(source))}, 'utf8');
        const lines = src.split(/\\r?\\n/);
        const start = lines.findIndex((l) => l.includes('function formatScanPercent'));
        if (start < 0) {{ throw new Error('formatScanPercent missing from {source.name}'); }}
        let end = -1;
        for (let i = start + 1; i < lines.length; i++) {{
          if (lines[i].trim() === '}}') {{ end = i; break; }}
        }}
        if (end < 0) {{ throw new Error('could not isolate formatScanPercent'); }}
        eval(lines.slice(start, end + 1).join('\\n'));
        const values = {json.dumps(values)};
        const out = {{}};
        for (const v of values) out[String(v)] = formatScanPercent(v);
        console.log('RESULT ' + JSON.stringify(out));
        """
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "probe.cjs"
            script.write_text(program, encoding="utf-8")
            proc = subprocess.run(
                [node, str(script)],
                capture_output=True,
                text=True,
                timeout=60,
                encoding="utf-8",
                errors="replace",
            )
        lines = [
            ln for ln in (proc.stdout or "").splitlines() if ln.startswith("RESULT ")
        ]
        assert lines, f"node failed:\n{proc.stdout}\n{proc.stderr}"
        return json.loads(lines[-1][len("RESULT "):])

    @pytest.mark.parametrize("source", SCAN_LOG_SOURCES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
    def test_sub_one_percent_is_visible(self, source: Path):
        result = self._probe(source, [0.4, 0.99])
        assert result["0.4"] == "<1%"
        assert result["0.99"] == "<1%"

    @pytest.mark.parametrize("source", SCAN_LOG_SOURCES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
    def test_real_values_still_round_to_whole_percent(self, source: Path):
        result = self._probe(source, [0, 1, 33.33, 66.67, 99.6, 100])
        assert result["0"] == "0%"
        assert result["1"] == "1%"
        assert result["33.33"] == "33%"
        assert result["66.67"] == "67%"
        assert result["99.6"] == "100%"
        assert result["100"] == "100%"

    @pytest.mark.parametrize("source", SCAN_LOG_SOURCES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
    def test_every_ui_copy_defines_the_helper(self, source: Path):
        text = source.read_text(encoding="utf-8")
        assert "function formatScanPercent" in text, source
