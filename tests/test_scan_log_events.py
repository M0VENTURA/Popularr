"""Unified scan-log presentation helpers.

Pins the event/context format, the quiet-period heartbeat, warning aggregation,
playlist outcome semantics, final results/genres and the wiring points. These
tests never change a scan decision — presentation only.
"""

from __future__ import annotations

import time
from pathlib import Path

from helpers import config_helpers as ch
from helpers import scan_log


def _capture(monkeypatch) -> list[str]:
    lines: list[str] = []

    def _emit(message, **kwargs):
        lines.append(str(message))

    from helpers import logging_config as lc

    monkeypatch.setattr(lc, "log_unified", _emit)
    monkeypatch.setattr(lc, "log_unified_level", lambda message, level="info", **k: _emit(message))
    monkeypatch.setattr(lc, "log_scan_detail", lambda message, **k: lines.append("DEBUG " + str(message)))
    return lines


class TestEventFormat:
    def test_an_info_event_carries_event_name_and_context(self, monkeypatch):
        lines = _capture(monkeypatch)
        scan_log.log_event(
            "track.checked",
            "Checked 1/2: Song",
            artist="A",
            track_id="t1",
        )
        assert len(lines) == 1
        assert lines[0].startswith("[SCAN] Checked 1/2: Song")
        assert "event=track.checked" in lines[0]
        assert "artist='A'" in lines[0]
        assert "track_id='t1'" in lines[0]

    def test_debug_evidence_goes_to_the_debug_channel(self, monkeypatch):
        lines = _capture(monkeypatch)
        scan_log.log_evidence("provider.candidate_rejected", "Rejected candidate", reason="live")
        assert lines and lines[0].startswith("DEBUG ")
        assert "event=provider.candidate_rejected" in lines[0]

    def test_sensitive_context_is_redacted(self, monkeypatch):
        lines = _capture(monkeypatch)
        scan_log.log_event(
            "provider.request", "Calling provider", api_key="abc123", password="hunter2"
        )
        assert "abc123" not in lines[0]
        assert "hunter2" not in lines[0]
        assert "api_key='***'" in lines[0]

    def test_warn_has_a_severity_prefix(self, monkeypatch):
        lines = _capture(monkeypatch)
        scan_log.log_event("saving.failed", "Could not save", level="warn")
        assert lines[0].startswith("[SCAN] [WARN]")

    def test_emit_never_raises(self, monkeypatch):
        from helpers import logging_config as lc

        def _boom(*_a, **_k):
            raise RuntimeError("log down")

        monkeypatch.setattr(lc, "log_unified", _boom)
        monkeypatch.setattr(lc, "log_unified_level", _boom)
        monkeypatch.setattr(lc, "log_scan_detail", _boom)
        scan_log.log_event("test", "message")


class TestHeartbeat:
    def test_it_emits_after_a_quiet_period_and_stops(self, monkeypatch):
        lines = _capture(monkeypatch)
        hb = scan_log.ScanHeartbeat(interval=1.0, activity="checking tracks")
        hb.start()
        time.sleep(1.3)
        assert any("Still" in line and "elapsed" in line for line in lines)
        hb.stop()
        count = len(lines)
        time.sleep(1.1)
        assert len(lines) == count, "heartbeat emitted after stop"

    def test_touch_resets_the_quiet_timer(self, monkeypatch):
        lines = _capture(monkeypatch)
        hb = scan_log.ScanHeartbeat(interval=1.0, activity="checking tracks")
        hb.start()
        try:
            for _ in range(5):
                time.sleep(0.2)
                hb.touch()
            assert not any("Still" in line for line in lines)
        finally:
            hb.stop()

    def test_the_interval_comes_from_config(self, monkeypatch):
        cfg = ch.get_scan_logging_config(
            {"logging": {"progress": {"heartbeat_seconds": 42}}}
        )
        assert cfg["progress"]["heartbeat_seconds"] == 42
        hb = scan_log.ScanHeartbeat(config=cfg)
        assert hb._interval == 42.0


class TestWarningAggregation:
    def test_first_repeat_and_recovery(self, monkeypatch):
        lines = _capture(monkeypatch)
        agg = scan_log.WarningAggregator(every=3)
        agg.warn("db.unavailable", "Database unavailable")
        agg.warn("db.unavailable", "Database unavailable")
        assert len([l for l in lines if "Database unavailable" in l]) == 1
        agg.warn("db.unavailable", "Database unavailable")
        assert len([l for l in lines if "repeated 3 times" in l]) == 1
        agg.recovered("db.unavailable", "Database connection restored")
        assert any("recovered after 3 warning" in l for l in lines)
        assert agg.summary() == {}

    def test_unresolved_summary_reports_the_total(self, monkeypatch):
        lines = _capture(monkeypatch)
        agg = scan_log.WarningAggregator(every=100)
        agg.warn("db.unavailable", "Database unavailable")
        agg.warn("db.unavailable", "Database unavailable")
        agg.emit_summary()
        assert any("2 occurrence(s) unresolved" in l for l in lines)


class TestPlaylistOutcomes:
    def test_outcomes_map_and_names(self, monkeypatch):
        lines = _capture(monkeypatch)
        scan_log.reset_playlist_outcomes()
        assert scan_log.playlist_outcome("A", {"created": 1}, track_count=24) == "created"
        assert scan_log.playlist_outcome("B", {"updated": 1}, track_count=300) == "updated"
        assert scan_log.playlist_outcome("C", {"unchanged": 1}) == "unchanged"
        assert scan_log.playlist_outcome(
            "D", {"failed": 1}
        ) == "failed"
        assert scan_log.playlist_outcome("E", {"skipped": 1}) == "skipped"
        # A playlist that matches its resolved subset but has an ID awaiting a
        # Navidrome import is PARTIAL — not a server rejection / failure.
        assert scan_log.playlist_outcome(
            "P", {"unchanged": 1, "partial": 1, "unresolved_ids": ["x"]}
        ) == "partial"
        text = "\n".join(lines)
        assert 'Created playlist: "A"' in text
        assert "24 tracks" in text
        assert 'Could not update playlist: "D"' in text
        assert 'Playlist partially synced: "P"' in text
        assert "awaiting Navidrome import" in text
        assert 'Could not update playlist: "P"' not in text
        counts = scan_log.playlist_outcome_summary()
        assert counts == {
            "created": 1, "updated": 1, "unchanged": 1, "skipped": 1,
            "partial": 1, "failed": 1,
        }

    def test_summary_reports_all_buckets(self, monkeypatch):
        lines = _capture(monkeypatch)
        scan_log.reset_playlist_outcomes()
        scan_log.playlist_outcome("A", {"created": 1})
        scan_log.playlist_outcome("B", {"updated": 1})
        scan_log.emit_playlist_summary()
        assert any("1 created · 1 updated" in l for l in lines)

    def test_updated_is_named_outside_a_batch_and_quiet_inside_one(self, monkeypatch):
        lines = _capture(monkeypatch)
        scan_log.set_playlist_batch(False)
        scan_log.reset_playlist_outcomes()
        scan_log.playlist_outcome("Single", {"updated": 1}, track_count=30)
        assert any(
            line.startswith("[SCAN] Updated playlist: \"Single\"")
            for line in lines
        )

        lines.clear()
        scan_log.set_playlist_batch(True)
        scan_log.reset_playlist_outcomes()
        scan_log.playlist_outcome("Batch", {"updated": 1}, track_count=30)
        assert not any(
            line.startswith("[SCAN] Updated playlist:")
            for line in lines
        ), "batch updates must not name every playlist at INFO"
        assert any("DEBUG [SCAN] [DEBUG] Updated playlist" in line for line in lines)
        scan_log.set_playlist_batch(False)


class TestFinalResults:
    def _cfg(self, **overrides):
        results = {
            "show_final_ratings": True,
            "show_final_genres": True,
            "genre_display": "defaults_with_exceptions",
        }
        results.update(overrides)
        return ch.get_scan_logging_config({"logging": {"results": results}})

    def test_shared_genres_collapse_to_one_line(self, monkeypatch):
        lines = _capture(monkeypatch)
        rows = [
            {"title": "A", "stars": 5, "final_score": 80, "genres": "Power Metal, Progressive Metal"},
            {"title": "B", "stars": 4, "final_score": 60, "genres": "Power Metal, Progressive Metal"},
        ]
        scan_log.album_final_results("Album", rows, config=self._cfg())
        text = "\n".join(lines)
        assert "Final results: Album" in text
        assert "5★" in text and "80.0" in text
        assert "Final genres for all 2 tracks: Power Metal, Progressive Metal" in text

    def test_defaults_plus_exceptions_and_unresolved(self, monkeypatch):
        lines = _capture(monkeypatch)
        rows = [
            {"title": "A", "stars": 5, "final_score": 80, "genres": "Power Metal"},
            {"title": "B", "stars": 4, "final_score": 60, "genres": "Power Metal"},
            {"title": "C", "stars": 3, "final_score": 40, "genres": "Power Metal, Symphonic Metal"},
            {"title": "D", "stars": 2, "final_score": 20, "genres": ""},
        ]
        scan_log.album_final_results("Album", rows, config=self._cfg())
        text = "\n".join(lines)
        assert "Album genre defaults: Power Metal" in text
        assert "C: Power Metal, Symphonic Metal" in text
        assert "D: no genres resolved" in text

    def test_ratings_can_be_hidden_while_genres_remain(self, monkeypatch):
        lines = _capture(monkeypatch)
        rows = [{"title": "A", "stars": 5, "final_score": 80, "genres": "Rock"}]
        scan_log.album_final_results(
            "Album", rows, config=self._cfg(show_final_ratings=False)
        )
        text = "\n".join(lines)
        assert "Final results:" not in text
        assert "Final genres for all 1 tracks: Rock" in text


class TestConfigDefaults:
    def test_defaults(self):
        cfg = ch.get_scan_logging_config({"logging": {}})
        assert cfg["progress"] == {
            "show_track_starts": True,
            "show_track_completions": True,
            "heartbeat_seconds": 15,
        }
        assert cfg["results"]["genre_display"] == "defaults_with_exceptions"
        assert cfg["results"]["show_provisional_scores"] is False
        assert cfg["playlists"]["show_unchanged_individually"] is False
        assert cfg["diagnostics"]["provider_payloads"] is False

    def test_partial_override_and_clamps(self):
        cfg = ch.get_scan_logging_config(
            {
                "logging": {
                    "progress": {"heartbeat_seconds": 0, "show_track_starts": "false"},
                    "results": {"genre_display": "nonsense"},
                    "playlists": {"show_created": "0"},
                }
            }
        )
        assert cfg["progress"]["heartbeat_seconds"] == 1
        assert cfg["progress"]["show_track_starts"] is False
        assert cfg["results"]["genre_display"] == "defaults_with_exceptions"
        assert cfg["playlists"]["show_created"] is False


class TestTheWiring:
    def test_run_scan_starts_the_heartbeat_and_emits_completion(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "services/popularity/scan_stage_runner.py"
        ).read_text(encoding="utf-8")
        assert "scan_log.scan_started(" in source
        assert "ScanHeartbeat(" in source
        assert "_scan_heartbeat.stop()" in source
        assert "scan_log.scan_completed(" in source
        assert "album_final_results(" in source
        assert "SCAN_WARNINGS.emit_summary()" in source

    def test_track_stage_emits_track_started(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "services/popularity/stages/track_stage.py"
        ).read_text(encoding="utf-8")
        assert "track_started(" in source

    def test_playlist_sync_emits_named_outcomes(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "services/popularity/stages/finalise_stage.py"
        ).read_text(encoding="utf-8")
        assert "playlist_outcome(" in source
        assert "ratings.updated" in source

    def test_the_scan_panel_filter_keeps_the_new_prefixes(self):
        from services.log_service import _scan_activity_filter

        pattern = _scan_activity_filter()
        assert pattern.search("[SCAN] Checking: Song")
        assert pattern.search('[SCAN] [WARN] Could not update playlist: "X"')
        assert pattern.search('[PLAYLIST] Created playlist: "X"')
