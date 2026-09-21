"""The scan log must be a readable section report, with detail behind debug.

REPORTED: "Can we look at cleaning up the logs for the Scan Process. This is a
regular log layout, anything more detailed should only be attached when debug
is enabled in config"

WHAT WAS WRONG
--------------
The unified scan log was a stream of low-level instrumentation. Per album:

* ``[MB] call started`` / ``[MB] call completed`` for EVERY MusicBrainz request
  (a 13-track album made dozens, incl. ``section='single.release_group_search'``)
* ``[ENRICH] section started`` / ``section completed`` for ~25 enrichment
  sections
* ``[SCAN] section started`` / ``section completed``
* ``[TRACK] ▶ Processing`` and ``▶/✓ Singles detection`` per track per stage
* one ``[PLAYLISTS] Updated Navidrome playlist in place`` per genre playlist
  (~45 on a finalise pass)

so the SCAN's shape — which album, which stage, what it found — could not be
seen. The readable report the user asked for did not exist at all.

THE TWO HALVES OF THE FIX (both pinned below)
---------------------------------------------
1. A section report (`helpers/scan_report.py`) that frames the scan and each
   album in stages.
2. The detailed instrumentation DEMOTED TO DEBUG, so
   ``logging.level: debug`` in config.yaml restores it inline.

The demand is therefore not "delete the detail" but "gate it" — these tests
assert the detail still EXISTS and is reachable, so a future cleanup cannot
quietly throw away the troubleshooting capability.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. The debug gate
# ---------------------------------------------------------------------------


class TestTheDebugGate:
    def test_debug_enabled_follows_the_configured_level(self, monkeypatch):
        from helpers import logging_config as lc

        monkeypatch.setattr(lc, "_resolve_log_level", lambda: "DEBUG")
        assert lc.debug_enabled() is True

        monkeypatch.setattr(lc, "_resolve_log_level", lambda: "INFO")
        assert lc.debug_enabled() is False

    def test_log_scan_detail_is_silent_at_info(self, monkeypatch):
        """THE CORE REQUIREMENT: detail must not reach the log at info level."""
        from helpers import logging_config as lc

        emitted: list[str] = []
        monkeypatch.setattr(lc, "_resolve_log_level", lambda: "INFO")
        monkeypatch.setattr(lc, "log_unified", lambda msg, **k: emitted.append(msg))

        lc.log_scan_detail("[MB] call started", section="recording.search")

        assert emitted == [], "debug detail leaked into the info-level log"

    def test_log_scan_detail_emits_at_debug(self, monkeypatch):
        """The detail must come BACK when debug is enabled."""
        from helpers import logging_config as lc

        emitted: list[str] = []
        monkeypatch.setattr(lc, "_resolve_log_level", lambda: "DEBUG")
        monkeypatch.setattr(lc, "log_unified", lambda msg, **k: emitted.append(msg))

        lc.log_scan_detail("[MB] call started", section="recording.search")

        assert len(emitted) == 1
        assert "[MB] call started" in emitted[0]

    def test_the_gate_never_raises_on_a_broken_config(self, monkeypatch):
        """A config failure must degrade to 'off', not break the scan."""
        from helpers import logging_config as lc

        def _boom():
            raise RuntimeError("config unavailable")

        monkeypatch.setattr(lc, "_resolve_log_level", _boom)

        assert lc.debug_enabled() is False


# ---------------------------------------------------------------------------
# 2. The high-volume instrumentation is debug-gated, NOT deleted
# ---------------------------------------------------------------------------


class TestInstrumentationIsGatedNotDeleted:
    """Every noisy site must still exist, reachable at debug level."""

    @pytest.mark.parametrize(
        "rel_path,marker",
        [
            ("services/enrichment/musicbrainz_service.py", "[MB] call started"),
            ("services/enrichment/musicbrainz_service.py", "[MB] call completed"),
            ("services/enrichment/musicbrainz_service.py", "[MB] section started"),
            ("services/popularity/stages/album_stage.py", "[ENRICH] section started"),
            ("services/popularity/stages/album_stage.py", "[ENRICH] call started"),
            ("services/popularity/scan_stage_runner.py", "[SCAN] section started"),
            ("services/playlists/playlist_navidrome_service.py",
             "[PLAYLISTS] Updated Navidrome playlist in place"),
        ],
    )
    def test_the_line_still_exists(self, rel_path: str, marker: str):
        src = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        assert marker in src, (
            f"{rel_path} lost the {marker!r} line entirely — the fix must GATE "
            f"detail, not delete it (debug logging could no longer trace a scan)"
        )

    @pytest.mark.parametrize(
        "rel_path,marker",
        [
            ("services/enrichment/musicbrainz_service.py", "[MB] call started"),
            ("services/enrichment/musicbrainz_service.py", "[MB] call completed"),
            ("services/enrichment/musicbrainz_service.py", "[MB] section started"),
            ("services/popularity/stages/album_stage.py", "[ENRICH] section started"),
            ("services/popularity/stages/album_stage.py", "[ENRICH] call started"),
            ("services/popularity/scan_stage_runner.py", "[SCAN] section started"),
        ],
    )
    def test_the_line_is_debug_level(self, rel_path: str, marker: str):
        """Each must go through a DEBUG call, not Logger.info."""
        src = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        # Find the statement carrying the marker and inspect its level.
        idx = src.index(marker)
        window = src[max(0, idx - 400):idx + 80]
        assert re.search(r"(Logger|logger)\.debug\(", window), (
            f"{rel_path}: {marker!r} is not emitted at DEBUG level, so it still "
            f"floods the default log"
        )
        assert not re.search(r"(Logger|logger)\.info\(\s*[\"']?\[?(MB|ENRICH|SCAN)\]", window), (
            f"{rel_path}: {marker!r} is still emitted at INFO level"
        )

    def test_the_playlist_spam_is_debug_level(self):
        src = (REPO_ROOT / "services/playlists/playlist_navidrome_service.py").read_text(
            encoding="utf-8"
        )
        idx = src.index("[PLAYLISTS] Updated Navidrome playlist in place")
        window = src[max(0, idx - 300):idx + 80]
        assert "logger.debug(" in window, (
            "the per-playlist Navidrome update line must be debug-only — ~45 of "
            "them per finalise pass buried the scan report"
        )

    def test_per_track_chatter_is_gated_but_the_score_line_survives(self):
        """The SCORE line is what the operator reads; the wrappers are chatter.

        ``[TRACK] 🎵 "<title>" | Score: … | ISRC: … | Single: …`` must stay at
        INFO (it carries the per-song verdict). The ``▶ Processing`` /
        ``▶ Singles detection`` / ``✓ Singles detection done`` wrappers are
        detail.
        """
        src = (REPO_ROOT / "services/popularity/stages/track_stage.py").read_text(
            encoding="utf-8"
        )

        # Chatter -> log_scan_detail (the singles lines import it as an alias)
        assert "log_scan_detail" in src
        for marker in ("▶ Processing:", "▶ Singles detection:", "✓ Singles detection done:"):
            idx = src.index(marker)
            window = src[max(0, idx - 500):idx]
            assert "log_scan_detail" in window or "_log_sd_detail" in window, (
                f"{marker!r} must be debug-gated"
            )

        # The scored line -> still INFO
        score_idx = src.index('f"[TRACK] 🎵 \\"')
        assert "log_unified(_consolidated)" in src[score_idx:], (
            "the per-track SCORE line must stay visible — it carries the "
            "score/LF/LB/ISRC/single verdict the operator reads"
        )


# ---------------------------------------------------------------------------
# 3. The readable section report
# ---------------------------------------------------------------------------


class TestTheSectionReport:
    def _capture(self, monkeypatch) -> list[str]:
        from helpers import scan_report as sr

        lines: list[str] = []
        monkeypatch.setattr(sr, "_emit", lambda line: lines.append(line))
        return lines

    def test_scan_started_frames_artist_albums_and_mode(self, monkeypatch):
        from helpers import scan_report as sr

        lines = self._capture(monkeypatch)
        sr.scan_started(artist="Silent Civilian", albums=2, mode="Forced Scan")
        text = "\n".join(lines)

        assert "ARTIST SCAN STARTED" in text
        assert "Artist: Silent Civilian" in text
        assert "Albums Found: 2" in text
        assert "Mode: Forced Scan" in text

    def test_a_library_scan_omits_the_artist_line(self, monkeypatch):
        """A full-library scan has no single artist; the line must not appear."""
        from helpers import scan_report as sr

        lines = self._capture(monkeypatch)
        sr.scan_started(artist="", albums=400, mode="Normal Scan")
        text = "\n".join(lines)

        assert "LIBRARY SCAN STARTED" in text
        assert "Artist:" not in text

    def test_album_started_numbers_the_block(self, monkeypatch):
        from helpers import scan_report as sr

        lines = self._capture(monkeypatch)
        sr.album_started(index=1, total=2, album="Silent Civilian — Ghost Stories")
        text = "\n".join(lines)

        assert "ALBUM 1 OF 2" in text
        assert "Ghost Stories" in text

    def test_album_summary_reports_every_rating_bucket(self, monkeypatch):
        from helpers import scan_report as sr

        lines = self._capture(monkeypatch)
        sr.album_summary(
            album="Ghost Stories",
            tracks_processed=11,
            metadata_corrections=34,
            genres_added=3,
            genres_removed=0,
            singles_detected=0,
            star_counts={5: 2, 4: 1, 3: 3, 2: 2, 1: 3},
            playlists_updated=5,
            duration_s=138,
        )
        text = "\n".join(lines)

        assert "ALBUM SUMMARY" in text
        assert "Tracks Processed: 11" in text
        assert "Metadata Corrections: 34" in text
        assert "5★: 2" in text and "1★: 3" in text
        assert "Duration: 2m 18s" in text

    def test_stage_sections_announce_and_close(self, monkeypatch):
        from helpers import scan_report as sr

        lines = self._capture(monkeypatch)
        sr.stage_started(emoji="📈", name="POPULARITY SCAN")
        sr.stage_complete(name="POPULARITY SCAN COMPLETE", stats={"Tracks Processed": 11})
        text = "\n".join(lines)

        assert "COMMENCING POPULARITY SCAN" in text
        assert "✅ POPULARITY SCAN COMPLETE" in text
        assert "Tracks Processed: 11" in text

    def test_list_section_says_none_rather_than_going_blank(self, monkeypatch):
        from helpers import scan_report as sr

        lines = self._capture(monkeypatch)
        sr.list_section(heading="Singles Detected:", items=[])
        text = "\n".join(lines)

        assert "Singles Detected:" in text
        assert "• None" in text

    @pytest.mark.parametrize(
        "seconds,expected",
        [(0, "0s"), (45, "45s"), (60, "1m 0s"), (138, "2m 18s"), (3661, "61m 1s")],
    )
    def test_duration_formatting(self, seconds, expected):
        from helpers.scan_report import format_duration

        assert format_duration(seconds) == expected

    def test_duration_formatting_survives_garbage(self):
        from helpers.scan_report import format_duration

        assert format_duration(None) == "0s"
        assert format_duration("abc") == "0s"


# ---------------------------------------------------------------------------
# 4. Reporting must never be able to break a scan
# ---------------------------------------------------------------------------


class TestReportingIsFailSafe:
    def test_emit_swallows_a_logging_failure(self, monkeypatch):
        """A logging fault must not propagate into the scan."""
        from helpers import logging_config as lc
        from helpers import scan_report as sr

        def _boom(msg, **kw):
            raise RuntimeError("log backend down")

        monkeypatch.setattr(lc, "log_unified", _boom)

        # Must not raise.
        sr.scan_started(artist="A", albums=1, mode="Normal Scan")
        sr.album_summary(album="X", tracks_processed=1)
        sr.stage_started(emoji="🎵", name="TEST")

    def test_an_empty_summary_still_renders(self, monkeypatch):
        from helpers import scan_report as sr

        lines: list[str] = []
        monkeypatch.setattr(sr, "_emit", lambda line: lines.append(line))

        sr.album_summary(album="", tracks_processed=0)
        text = "\n".join(lines)

        assert "ALBUM SUMMARY" in text
        assert "Tracks Processed: 0" in text


# ---------------------------------------------------------------------------
# 5. The report reaches the log through the existing unified channel
# ---------------------------------------------------------------------------


class TestTheReportUsesTheUnifiedChannel:
    def test_scan_report_writes_via_log_unified(self, monkeypatch):
        """The dashboard panel and log filters key off popularr.unified.

        Emitting the report on a different logger would make it invisible on
        the dashboard scanning panel, so this is load-bearing.
        """
        from helpers import scan_report as sr

        seen: list[str] = []
        import helpers.logging_config as lc

        monkeypatch.setattr(lc, "log_unified", lambda msg, **k: seen.append(msg))

        sr.scan_started(artist="A", albums=1, mode="Normal Scan")

        assert seen, "the report emitted nothing through log_unified"
        assert any("ARTIST SCAN STARTED" in line for line in seen)
