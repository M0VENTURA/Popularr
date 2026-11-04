"""A corrected cover original must reach the FILE, not just the DB.

REPORTED
--------
The deep pass resolved ``result=Michael Jackson`` but a stale ``"Santana"``
attribution kept coming back, and the album re-ran every two minutes:

    [COVER_DETECT] Alien Ant Farm - 20th Century Masters...: 1 cover(s) (new=0)
        | checked=1, settled-in-window=0, already-confirmed=1, titles-repaired=1
    [COVER_DETECT] original lookup: writers=Michael Jackson ... result=Michael Jackson
    [COVER_DETECT] writing tags for 1 cover(s)…

WHY
---
``_update_file_metadata`` wrote ONLY ``title``. The DB row was corrected to
``original_cover_artist = Michael Jackson`` while the audio file still carried
``ORIGINAL_COVER_ARTIST = Santana``. The Navidrome import reads that tag back
(Subsonic never echoes these fields), so Santana returned to the DB on the next
scan — and the repair rewrote a "(Santana Cover)" title again. A file mtime
change also makes Navidrome rescan, so the cycle repeated.

THE RULE
--------
The verdict is written as one unit: ``title`` (with its suffix), ``is_cover``
and ``original_cover_artist``. The file and the DB can never disagree, and a
stale tag can never be re-imported over a fresh verdict.

The positive-verdict re-check itself is intentional and unchanged: see
``tests/test_cover_flag_cleared_on_rescan.py``. Once the file is consistent,
no update is produced, so the file is not touched and the rescan loop stops.
"""
from __future__ import annotations

import pytest

import services.enrichment.cover_detector_impl as impl
import services.metadata.tag_file_service as tag_service
from services.enrichment.cover_detector_impl import CoverDetector

WORK = "work-1"


def _detector(state: dict | None = None) -> CoverDetector:
    det = CoverDetector(db_connection=None)
    det._load_cover_checked_map = lambda ids: (state or {})
    det._collect_track_writers = lambda *a, **k: {}
    det._persist_checked = lambda *a, **k: None
    det._clear_unconfirmed_verdicts = lambda *a, **k: None
    # These tests mock ``_detect_via_work_id`` and never stub the MB client;
    # the stored-work title check would otherwise make a real request.
    det._work_title_matches_track = lambda *a, **k: True
    return det


def _track(
    title: str,
    artist: str = "Alien Ant Farm",
    *,
    original: str = "Michael Jackson",
) -> dict:
    return {
        "id": "t1",
        "title": title,
        "artist": artist,
        "album": "20th Century Masters",
        "album_artist": artist,
        "file_path": "/music/Alien Ant Farm/20th Century Masters/01 - Smooth Criminal.mp3",
        "work_mbid": WORK,
        # Direct calls to the repair helper need the stored verdict on the row;
        # the album flow overlays it from ``_load_cover_checked_map``.
        "is_cover": 1,
        "original_cover_artist": original,
    }


def _stored(original: str) -> dict:
    return {
        "t1": {
            "cover_last_checked": None,
            "is_cover": 1,
            "original_cover_artist": original,
            "cover_manual_override": False,
        }
    }


def _capture_writes(monkeypatch) -> dict:
    """Record the DB updates and the audio-file tag payloads."""
    captured: dict = {"updates": [], "files": []}

    def _apply(conn, updates):
        captured["updates"].extend(dict(u) for u in updates)
        return [str(u.get("track_id")) for u in updates]

    def _write(file_path, tags):
        captured["files"].append({"file_path": file_path, "tags": dict(tags)})
        return True

    monkeypatch.setattr(impl, "apply_cover_metadata_batch", _apply)
    monkeypatch.setattr(tag_service, "write_tags_to_file", _write)
    return captured


# ===========================================================================
# 1. The stale original is replaced by the resolved one
# ===========================================================================
class TestStaleOriginalIsCorrected:
    def test_a_new_original_overwrites_the_stored_one(self, monkeypatch):
        det = _detector(_stored("Santana"))
        det._detect_via_work_id = lambda **kwargs: {
            "artist": "Michael Jackson", "year": 1987, "confidence": "high",
        }
        captured = _capture_writes(monkeypatch)

        det.detect_covers_for_album(
            "20th Century Masters", "Alien Ant Farm",
            [_track("Smooth Criminal (Santana Cover)")],
        )

        assert len(captured["updates"]) == 1, (
            "a stored cover from a DIFFERENT original was treated as settled; "
            "the newly resolved original must be persisted"
        )
        update = captured["updates"][0]
        assert update["original_artist"] == "Michael Jackson"
        assert update["title"] == "Smooth Criminal", (
            "the stale '(Santana Cover)' suffix must be rebuilt from the new "
            "original"
        )

    def test_the_corrected_verdict_reaches_the_audio_file(self, monkeypatch):
        det = _detector(_stored("Santana"))
        det._detect_via_work_id = lambda **kwargs: {
            "artist": "Michael Jackson", "year": 1987, "confidence": "high",
        }
        captured = _capture_writes(monkeypatch)

        det.detect_covers_for_album(
            "20th Century Masters", "Alien Ant Farm",
            [_track("Smooth Criminal (Santana Cover)")],
        )

        assert captured["files"], "the verdict was written to the DB only"
        tags = captured["files"][0]["tags"]
        assert tags["original_cover_artist"] == "Michael Jackson", (
            "ORIGINAL_COVER_ARTIST stayed 'Santana' on disk; the next Navidrome "
            "import reads it back into the DB and restarts the loop"
        )
        assert tags["is_cover"] == "1"
        assert tags["title"] == "Smooth Criminal (Michael Jackson Cover)"


# ===========================================================================
# 2. A re-confirmed cover writes the whole verdict to the file
# ===========================================================================
class TestReconfirmedVerdictWritesItsFileTags:
    def test_the_suffix_repair_carries_the_original_artist(self, monkeypatch):
        det = _detector(_stored("Michael Jackson"))
        det._detect_via_work_id = lambda **kwargs: {
            "artist": "Michael Jackson", "year": 1987, "confidence": "high",
        }
        captured = _capture_writes(monkeypatch)

        det.detect_covers_for_album(
            "20th Century Masters", "Alien Ant Farm",
            [_track("Smooth Criminal")],
        )

        assert captured["files"], "the lost suffix was not repaired on disk"
        tags = captured["files"][0]["tags"]
        assert tags["title"] == "Smooth Criminal (Michael Jackson Cover)"
        assert tags["original_cover_artist"] == "Michael Jackson"
        assert tags["is_cover"] == "1"


# ===========================================================================
# 3. Once the file is consistent, nothing is rewritten
# ===========================================================================
class TestAStableVerdictIsNotRewritten:
    def test_a_matching_suffix_produces_no_update(self, monkeypatch):
        det = _detector(_stored("Michael Jackson"))
        det._detect_via_work_id = lambda **kwargs: {
            "artist": "Michael Jackson", "year": 1987, "confidence": "high",
        }
        captured = _capture_writes(monkeypatch)

        det.detect_covers_for_album(
            "20th Century Masters", "Alien Ant Farm",
            [_track("Smooth Criminal (Michael Jackson Cover)")],
        )

        assert captured["updates"] == [], (
            "a consistent verdict must not churn the DB row on every scan"
        )
        assert captured["files"] == [], (
            "rewriting an unchanged file changes its mtime and wakes the "
            "Navidrome scanner — the reported every-two-minutes loop"
        )


# ===========================================================================
# 4. The repair prefers the freshly resolved original over the stored one
# ===========================================================================
class TestTheRepairUsesTheResolvedOriginal:
    def test_resolved_original_wins(self):
        det = _detector()
        update = det._suffix_repair_update(
            _track("Smooth Criminal (Santana Cover)"),
            "Alien Ant Farm",
            resolved_original="Michael Jackson",
        )
        assert update is not None
        assert update["original_artist"] == "Michael Jackson"

    def test_stored_original_is_the_fallback(self):
        det = _detector()
        update = det._suffix_repair_update(
            _track("Smooth Criminal"), "Alien Ant Farm",
        )
        assert update is not None
        assert update["original_artist"] == "Michael Jackson"

    @pytest.mark.parametrize("title", [
        "Smooth Criminal (Michael Jackson Cover)",
    ])
    def test_a_matching_suffix_needs_no_repair(self, title):
        det = _detector()
        assert det._suffix_repair_update(
            _track(title), "Alien Ant Farm", resolved_original="Michael Jackson",
        ) is None

    def test_a_matching_suffix_still_repairs_stale_FILE_tags(self, monkeypatch):
        """The title was right but the file still said Santana.

        Navidrome reads ``ORIGINAL_COVER_ARTIST`` back, so leaving the file
        stale re-introduces the wrong verdict on the next import.
        """
        det = _detector()
        monkeypatch.setattr(
            det, "_file_cover_tags_are_current", lambda path, orig: False
        )
        update = det._suffix_repair_update(
            _track("Smooth Criminal (Michael Jackson Cover)"),
            "Alien Ant Farm",
            resolved_original="Michael Jackson",
        )
        assert update is not None
        assert update["original_artist"] == "Michael Jackson"

    def test_current_file_tags_produce_no_update(self, monkeypatch):
        det = _detector()
        monkeypatch.setattr(
            det, "_file_cover_tags_are_current", lambda path, orig: True
        )
        assert det._suffix_repair_update(
            _track("Smooth Criminal (Michael Jackson Cover)"),
            "Alien Ant Farm",
            resolved_original="Michael Jackson",
        ) is None


# ===========================================================================
# 5. Our own annotation must not re-confirm itself (the reported Santana loop)
# ===========================================================================
class TestAStoredVerdictCannotConfirmItself:
    """A non-forced scan used to re-read its own suffix as evidence.

    Steps 1-4 are turned off here, leaving only the title-hint step — the
    scenario where the wrong "Santana" attribution used to survive every
    popularity scan (a forced pass would clear it, the popularity scan restored
    it).
    """

    @staticmethod
    def _no_other_evidence(det) -> None:
        det._detect_via_work_id = lambda **kwargs: None   # inconclusive
        det._detect_via_isrc = lambda **kwargs: None
        det._detect_via_recording_relation = lambda **kwargs: None
        det._find_original_recording = lambda *a, **k: None
        det._resolve_album_release_mbid = lambda *a, **k: ""
        det._resolve_recording_mbid = lambda *a, **k: ""
        det._is_va_compilation = lambda *a, **k: False

    def test_the_stored_verdict_is_not_reconfirmed_from_its_own_suffix(
        self, monkeypatch
    ):
        det = _detector(_stored("Santana"))
        self._no_other_evidence(det)
        captured = _capture_writes(monkeypatch)

        det.detect_covers_for_album(
            "20th Century Masters", "Alien Ant Farm",
            [_track("Smooth Criminal (Santana Cover)")],
        )

        assert captured["updates"] == [], (
            "the stored 'Santana' verdict was re-confirmed by its own "
            "'(Santana Cover)' suffix; nothing could ever correct or clear it, "
            "which is why the popularity scan kept restoring the wrong cover"
        )

    def test_an_annotation_that_is_not_our_stored_verdict_is_honoured(
        self, monkeypatch
    ):
        """CONTROL — a title annotation is still evidence when it is not ours."""
        det = _detector({})
        self._no_other_evidence(det)
        captured = _capture_writes(monkeypatch)
        track = _track("Smooth Criminal (Santana Cover)")
        track["is_cover"] = 0
        track["original_cover_artist"] = None

        det.detect_covers_for_album(
            "20th Century Masters", "Alien Ant Farm", [track]
        )

        assert captured["updates"], (
            "an annotation that is NOT our stored verdict must still be used"
        )
        assert captured["updates"][0]["original_artist"] == "Santana"


# ===========================================================================
# 6. The FILE's TITLE is part of the verdict
# ===========================================================================
class TestTheFilesTitleIsPartOfTheVerdict:
    """The logged case: the resolver returned Michael Jackson, but the pass
    reported ``new=0, already-confirmed=1`` and nothing changed.

    ``_file_cover_tags_are_current`` compared only ``IS_COVER`` and
    ``ORIGINAL_COVER_ARTIST``. A file whose tags said "Michael Jackson" but
    whose TITLE frame still said "Smooth Criminal (Santana Cover)" was declared
    current, so the repair never ran and the stale suffix lived on disk (and in
    Navidrome) forever.
    """

    @staticmethod
    def _patch_file(monkeypatch, tmp_path, *, tags, title):
        import helpers.metadata_reader as reader
        import services.metadata.tag_file_service as tfs

        path = tmp_path / "01.mp3"
        path.write_bytes(b"x")
        monkeypatch.setattr(reader, "read_release_tag_values", lambda p: dict(tags))
        monkeypatch.setattr(reader, "read_mp3_metadata", lambda p: {"title": title})
        monkeypatch.setattr(tfs, "resolve_music_file_path", lambda p: p)
        return str(path)

    def test_a_stale_title_suffix_is_not_current(self, monkeypatch, tmp_path):
        path = self._patch_file(
            monkeypatch, tmp_path,
            tags={"is_cover": "1", "original_cover_artist": "Michael Jackson"},
            title="Smooth Criminal (Santana Cover)",
        )

        assert CoverDetector._file_cover_tags_are_current(
            path, "Michael Jackson",
            expected_title="Smooth Criminal (Michael Jackson Cover)",
        ) is False, (
            "a file whose TITLE still names Santana must be rewritten even when "
            "its cover tags already name Michael Jackson"
        )

    def test_a_matching_title_is_current(self, monkeypatch, tmp_path):
        path = self._patch_file(
            monkeypatch, tmp_path,
            tags={"is_cover": "1", "original_cover_artist": "Michael Jackson"},
            title="Smooth Criminal (Michael Jackson Cover)",
        )

        assert CoverDetector._file_cover_tags_are_current(
            path, "Michael Jackson",
            expected_title="Smooth Criminal (Michael Jackson Cover)",
        ) is True

    def test_a_middle_suffix_order_still_compares_the_base_title(
        self, monkeypatch, tmp_path
    ):
        path = self._patch_file(
            monkeypatch, tmp_path,
            tags={"is_cover": "1", "original_cover_artist": "Michael Jackson"},
            title="A Different Song (Michael Jackson Cover)",
        )

        assert CoverDetector._file_cover_tags_are_current(
            path, "Michael Jackson",
            expected_title="Smooth Criminal (Michael Jackson Cover)",
        ) is False, "the file title must name the same base song"
