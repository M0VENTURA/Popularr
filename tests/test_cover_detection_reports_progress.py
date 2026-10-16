"""Cover detection must report progress instead of going silent.

REPORT: *"Can we add more output for the Cover Detection in the logs so we can see
what's being checked rather than a static wait while it's happening."*

WHY IT WAS SILENT

``CoverDetector.detect_covers_for_album`` is the longest single operation in an
album — six detection techniques, each capable of a MusicBrainz round trip, per
track — and it wrote NOTHING to ``unified_scan.log``. Its structlog lines went to
``info.log``/``debug.log``, which the Scanner tab does not tail, and the caller
logged a single count AFTER the whole pass finished. So a healthy run and a hung
one looked identical for minutes.

WHAT IS PINNED HERE

The operator-visible contract, driven against the real detector with a stubbed
MusicBrainz client:

* a START line that reports how many tracks actually need work (that number is
  what makes the wait predictable — an album whose verdicts are all already
  settled finishes immediately);
* one progress line per TECHNIQUE, carrying the step index, so movement is
  visible even when the settled count does not change;
* a line naming each CONFIRMED cover, with the original artist;
* a SUMMARY carrying the pass breakdown rather than only the cover count;
* and the reverse: an album with nothing to check says so instead of emitting
  six useless step lines.
"""

from __future__ import annotations

import pytest

from services.enrichment.cover_detector_impl import CoverDetector


# ---------------------------------------------------------------------------
# Capture harness
# ---------------------------------------------------------------------------


@pytest.fixture()
def logged(monkeypatch):
    """Collect what would reach ``unified_scan.log`` and ``debug.log``.

    ⚠️ Patches the MODULE ATTRIBUTES that ``detect_covers_for_album`` imports
    inside the function body, so the real code path runs and only the sink is
    swapped. Nothing about the detector is stubbed except MusicBrainz.
    """
    unified: list[str] = []
    detail: list[str] = []

    from helpers import logging_config

    monkeypatch.setattr(logging_config, "log_unified", lambda m, **k: unified.append(m))
    monkeypatch.setattr(logging_config, "log_scan_detail", lambda m, **k: detail.append(m))
    return unified, detail


class _FakeMB:
    """Minimal MusicBrainz stub: every call answers with a fixed candidate set."""

    def __init__(self, work_recordings=None):
        self.work_cfg = work_recordings or []

    def browse_work_recordings(self, work_mbid, inc="", limit=100):
        return self.work_cfg

    def lookup_by_isrc(self, isrc, inc=""):
        return []

    def lookup_recording_metadata(self, *a, **k):
        return {}

    def get_recording_relations(self, *a, **k):
        return []

    def search_recordings(self, *a, **k):
        return []


def _recording(mbid: str, title: str, artist: str, year: int) -> dict:
    return {
        "id": mbid,
        "title": title,
        "artist-credit": [{"artist": {"name": artist, "id": "a-" + artist}}],
        "first-release-date": f"{year}-01-01",
    }


def _run(monkeypatch, tracks, work_recordings=None, album="No Grave but the Sea"):
    """Drive the real detector with the DB writes stubbed out.

    ``apply_cover_metadata_batch`` / ``_persist_checked`` /
    ``_clear_unconfirmed_verdicts`` are replaced by no-ops because they need a
    real database; everything the logging decisions depend on runs unchanged.
    """
    import services.enrichment.cover_detector_impl as impl

    det = CoverDetector(db_connection=None)
    det.mb = _FakeMB(work_recordings)
    det._persist_checked = lambda *a, **k: None
    det._clear_unconfirmed_verdicts = lambda *a, **k: None
    monkeypatch.setattr(
        impl, "apply_cover_metadata_batch",
        lambda conn, updates: [u["track_id"] for u in updates],
    )
    det.detect_covers_for_album(album, "Alestorm", tracks)
    return det


def _plain_tracks(n: int) -> list[dict]:
    return [
        {"id": f"t{i}", "title": f"Song {i}", "artist": "Alestorm"}
        for i in range(1, n + 1)
    ]


# ---------------------------------------------------------------------------
# The START line
# ---------------------------------------------------------------------------


class TestTheStartLine:
    def test_it_reports_how_many_tracks_need_work(self, monkeypatch, logged):
        unified, _ = logged
        _run(monkeypatch, _plain_tracks(8))

        assert any(
            "Checking 8 track(s)" in line and "Alestorm - No Grave but the Sea" in line
            for line in unified
        ), f"no START line naming the work to do: {unified}"

    def test_it_reports_the_already_settled_count(self, monkeypatch, logged):
        """⭐ The number that makes the wait predictable.

        A re-scanned album usually settles every verdict immediately; a first
        pass pays a MusicBrainz round trip per track. Both were previously
        invisible, so an operator could not tell which one they were waiting on.
        """
        unified, _ = logged
        _run(
            monkeypatch,
            [
                {"id": "t1", "title": "A", "artist": "Alestorm",
                 "cover_last_checked": "2099-01-01 00:00:00"},
                {"id": "t2", "title": "B", "artist": "Alestorm"},
            ],
        )

        assert any("Checking 1 track(s)" in line for line in unified), unified
        assert any("1 already settled" in line for line in unified), (
            f"the settled count must be reported so the wait is predictable: {unified}"
        )

    def test_an_album_with_nothing_to_do_says_so(self, monkeypatch, logged):
        unified, _ = logged
        _run(
            monkeypatch,
            [{"id": "t1", "title": "A", "artist": "Alestorm",
              "cover_last_checked": "2099-01-01 00:00:00"}],
        )

        assert any("already settled" in line and "nothing to check" in line for line in unified), (
            f"an instantly-finished pass must not look like a silent one: {unified}"
        )


# ---------------------------------------------------------------------------
# The per-technique progress
# ---------------------------------------------------------------------------


class TestTheStepProgress:
    def test_every_technique_reports(self, monkeypatch, logged):
        unified, _ = logged
        _run(monkeypatch, _plain_tracks(4))

        steps = [line for line in unified if "step " in line]
        assert len(steps) == 6, f"expected 6 step lines, got {len(steps)}: {steps}"

    def test_the_step_index_advances(self, monkeypatch, logged):
        """⭐ The part that shows movement when nothing else does.

        The settled count is NOT a progress signal on its own: a track with no
        work id, no ISRC and no logged identity is skipped by all six techniques
        in turn, so the count sits at its starting value while the pass flies
        through. The step index advances regardless.
        """
        unified, _ = logged
        _run(monkeypatch, _plain_tracks(4))

        steps = [line for line in unified if "step " in line]
        for index in range(1, 7):
            assert any(f"step {index}/6" in line for line in steps), (
                f"step {index}/6 never reported: {steps}"
            )

    def test_each_step_names_the_technique(self, monkeypatch, logged):
        unified, _ = logged
        _run(monkeypatch, _plain_tracks(4))
        joined = " ".join(unified)

        for technique in (
            "work lookup", "ISRC lookup", "recording relations",
            "writer matching", "title hints", "work fallback",
        ):
            assert technique in joined, f"{technique!r} is not reported: {joined}"

    def test_a_fully_settled_album_does_not_emit_step_lines(self, monkeypatch, logged):
        """CONTROL: the step lines must not become noise when there is no work."""
        unified, _ = logged
        _run(
            monkeypatch,
            [{"id": "t1", "title": "A", "artist": "Alestorm",
              "cover_last_checked": "2099-01-01 00:00:00"}],
        )

        assert not [line for line in unified if "step " in line], (
            "an album where every verdict was already settled must not narrate "
            "six techniques that all did nothing"
        )


# ---------------------------------------------------------------------------
# The findings and the summary
# ---------------------------------------------------------------------------


class TestTheFindingsAndSummary:
    def test_a_confirmed_cover_is_named_with_its_original(self, monkeypatch, logged):
        unified, detail = logged
        _run(
            monkeypatch,
            [{"id": "t1", "title": "Song One", "artist": "Alestorm",
              "work_mbid": "work-1", "isrc": "GB-1XA-01"}],
            work_recordings=[
                _recording("rec-orig", "Song One", "The Original", 1969),
                _recording("rec-self", "Song One", "Alestorm", 2017),
            ],
        )

        assert any(
            "Song One" in line and "The Original" in line and "->" in line
            for line in unified
        ), f"the confirmed cover was not announced: {unified}"
        # The per-track detail also lands at debug level, per the house
        # convention track_stage records.
        assert any("Song One" in line for line in detail), (
            f"no debug detail for the finding: {detail}"
        )

    def test_the_summary_carries_the_breakdown(self, monkeypatch, logged):
        """⭐ Not just the cover count.

        The old line reported only how many covers were found, so an album where
        NOTHING was checked read exactly like one where everything was checked
        and came back negative — two very different amounts of work.
        """
        unified, _ = logged
        _run(monkeypatch, _plain_tracks(3))

        summary = [line for line in unified if "cover(s)" in line]
        assert summary, f"no summary emitted: {unified}"
        line = summary[-1]
        assert "checked=" in line, f"the summary does not say what was checked: {line}"
        assert "settled-in-window=" in line, line
        assert "already-confirmed=" in line, line

    def test_it_announces_the_file_tag_write(self, monkeypatch, logged):
        """The remaining slow part after detection: one mutagen write per cover."""
        unified, _ = logged
        _run(
            monkeypatch,
            [{"id": "t1", "title": "Song One", "artist": "Alestorm",
              "work_mbid": "work-1", "isrc": "GB-1XA-01"}],
            work_recordings=[
                _recording("rec-orig", "Song One", "The Original", 1969),
                _recording("rec-self", "Song One", "Alestorm", 2017),
            ],
        )

        assert any("writing tags" in line for line in unified), (
            f"the tag-write phase is silent: {unified}"
        )


# ---------------------------------------------------------------------------
# The caller
# ---------------------------------------------------------------------------


class TestTheCallerDoesNotDuplicateOrSwallow:
    @staticmethod
    def _runner_source() -> str:
        """The runner's CODE, comments removed.

        ⚠️ LOAD-BEARING. The fix's own comment explains what the old line said
        and quotes it verbatim, so a naive substring check is satisfied by the
        PROSE documenting the change — it would keep passing after the line was
        reinstated. Only whole-line ``#`` comments are removed, and a ``#`` is
        ignored inside a quoted string, so ``"#tag"`` literals survive.
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        raw = (root / "services" / "popularity" / "scan_stage_runner.py").read_text(
            encoding="utf-8"
        )
        kept: list[str] = []
        for line in raw.splitlines():
            quote: str | None = None
            cut = len(line)
            for index, char in enumerate(line):
                if quote:
                    if char == "\\":
                        continue
                    if char == quote:
                        quote = None
                elif char in "\"'":
                    quote = char
                elif char == "#":
                    cut = index
                    break
            kept.append(line[:cut])
        return "\n".join(kept)

    def test_the_runner_no_longer_logs_a_bare_count(self):
        """The detector reports a breakdown; a second count line would disagree.

        The caller knows only the album and a track list — it cannot know how
        many tracks were checked, skipped or already confirmed. Logging its own
        count alongside the detector's breakdown is how the two would drift.
        """
        source = self._runner_source()
        assert "cover(s) found" not in source, (
            "the caller still logs its own cover count — the detector owns the "
            "breakdown and the two can disagree"
        )

    def test_a_failed_pass_is_visible(self):
        """⚠️ A failure reached only ``debug.log``.

        So a pass that silently did nothing looked exactly like a pass that found
        no covers — the same class of invisibility as the static wait.
        """
        source = self._runner_source()
        assert "failed (" in source, (
            "a failed cover pass must surface on the scan log, not only debug.log"
        )
