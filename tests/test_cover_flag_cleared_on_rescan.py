"""A false cover verdict is re-verified on every scan and cleared if unconfirmed.

REPORT: the "Great Covers" playlist kept containing tracks that are not covers.

The deep pass's ``_clear_unconfirmed_verdicts`` was never the whole story: a
stored verdict (``is_cover`` + ``original_cover_artist``) was trusted FOREVER.
Every scan re-stamped its ``cover_last_checked``, so the 90-day window never
expired, and the row's own evidence kept it out of the clear step. A verdict
written by an older buggy detection could therefore survive every scan.

TWO RULES, pinned here:

1. A POSITIVE verdict is always re-assessed. The freshness window still protects
   negative verdicts (an unflagged track checked recently is not re-checked),
   but "this IS a cover" is a claim the detector must stand behind on the
   current pass — positive verdicts are a small fraction of a library.
2. Clearing a verdict clears the AUDIO FILE's ``IS_COVER`` /
   ``ORIGINAL_COVER_ARTIST`` tags too. Leaving them on disk let the next
   Navidrome import read them back and restore the flag the scan had cleared.
"""
from __future__ import annotations

from contextlib import contextmanager

import services.enrichment.cover_detector_impl as impl
from services.enrichment.cover_detector_impl import CoverDetector

FUTURE = "2099-01-01 00:00:00"


def _detector(state: dict | None = None) -> CoverDetector:
    det = CoverDetector(db_connection=None)
    det._load_cover_checked_map = lambda ids: (state or {})
    det._collect_track_writers = lambda *a, **k: {}
    det._persist_checked = lambda *a, **k: None
    return det


def _capture_clear(det: CoverDetector) -> dict:
    """Replace the clear step with a recorder of its arguments."""
    captured: dict = {}

    def _record(tracks, *, confirmed_ids, fresh_skipped, confirmed_skipped, force):
        captured["tracks"] = tracks
        captured["confirmed_ids"] = set(confirmed_ids)
        captured["fresh_skipped"] = set(fresh_skipped)
        captured["confirmed_skipped"] = set(confirmed_skipped)

    det._clear_unconfirmed_verdicts = _record
    return captured


# ---------------------------------------------------------------------------
# 1. Positive verdicts are re-assessed, not trusted forever
# ---------------------------------------------------------------------------


class TestPositiveVerdictsAreReassessed:
    def test_a_fresh_stored_verdict_is_still_assessed(self):
        """THE reported leak: fresh timestamp + original artist shielded it."""
        stale = {
            "t1": {
                "cover_last_checked": FUTURE,
                "is_cover": 1,
                "original_cover_artist": "Wrong Artist",
                "cover_manual_override": False,
            }
        }
        det = _detector(stale)
        captured = _capture_clear(det)

        det.detect_covers_for_album(
            "Album", "Real Artist",
            [{"id": "t1", "title": "Own Song", "artist": "Real Artist"}],
        )

        assert "t1" not in captured["fresh_skipped"], (
            "a positive verdict must not be skipped just because it was checked "
            "recently — nothing here re-verified it"
        )
        assert "t1" not in captured["confirmed_skipped"], (
            "the tracked verdict was treated as settled instead of re-assessed"
        )
        assert "t1" not in captured["confirmed_ids"], (
            "no detection confirmed it, so the clear step must be allowed to "
            "remove the flag"
        )

    def test_the_stored_verdict_is_overlaid_onto_the_caller_rows(self):
        """The track stage may have flagged the track EARLIER in this same scan.

        The caller's rows are the load-time snapshot, so the current DB state
        must be overlaid or the clear step sees "nothing stored" and leaves a
        flag the track stage just wrote.
        """
        state = {
            "t1": {
                "cover_last_checked": None,
                "is_cover": 1,
                "original_cover_artist": "Wrong Artist",
                "cover_manual_override": False,
            }
        }
        det = _detector(state)
        captured = _capture_clear(det)

        det.detect_covers_for_album(
            "Album", "Real Artist",
            [{"id": "t1", "title": "Own Song", "artist": "Real Artist"}],
        )

        overlaid = captured["tracks"][0]
        assert overlaid["is_cover"] in (1, True)
        assert overlaid["original_cover_artist"] == "Wrong Artist"
        assert "t1" not in captured["confirmed_ids"]

    def test_an_unflagged_fresh_track_is_still_skipped(self):
        """CONTROL: the 90-day cache still protects NEGATIVE verdicts."""
        state = {
            "t1": {
                "cover_last_checked": FUTURE,
                "is_cover": 0,
                "original_cover_artist": None,
                "cover_manual_override": False,
            }
        }
        det = _detector(state)
        captured = _capture_clear(det)

        det.detect_covers_for_album(
            "Album", "Real Artist",
            [{"id": "t1", "title": "Own Song", "artist": "Real Artist"}],
        )

        assert "t1" in captured["fresh_skipped"], (
            "re-checking every unflagged track on every scan is exactly the cost "
            "the freshness window exists to avoid"
        )

    def test_a_manual_override_is_never_reassessed(self):
        state = {
            "t1": {
                "cover_last_checked": None,
                "is_cover": 1,
                "original_cover_artist": "Wrong Artist",
                "cover_manual_override": True,
            }
        }
        det = _detector(state)
        captured = _capture_clear(det)

        det.detect_covers_for_album(
            "Album", "Real Artist",
            [{"id": "t1", "title": "Own Song", "artist": "Real Artist"}],
        )

        assert "t1" in captured["confirmed_skipped"]
        assert "t1" not in captured["confirmed_ids"]


# ---------------------------------------------------------------------------
# 2. A re-confirmed verdict is not rewritten
# ---------------------------------------------------------------------------


class TestReconfirmedVerdictsAreNotRewritten:
    def test_the_same_original_is_confirmed_without_writing(self, monkeypatch):
        """Avoid churning the row + file for a verdict that has not changed."""
        state = {
            "t1": {
                "cover_last_checked": None,
                "is_cover": 1,
                "original_cover_artist": "The Original",
                "cover_manual_override": False,
            }
        }
        det = _detector(state)
        det._detect_via_work_id = lambda **kwargs: {
            "artist": "The Original", "year": 1969, "confidence": "high",
        }
        captured = _capture_clear(det)

        written: list[dict] = []
        monkeypatch.setattr(
            impl, "apply_cover_metadata_batch",
            lambda conn, updates: written.extend(updates) or [],
        )

        det.detect_covers_for_album(
            "Album", "Real Artist",
            [{"id": "t1", "title": "Own Song (The Original Cover)",
              "artist": "Real Artist", "work_mbid": "work-1"}],
        )

        assert written == [], (
            "an unchanged cover verdict rewrote the DB row and audio-file tags "
            "on every scan"
        )
        assert "t1" in captured["confirmed_ids"], (
            "the re-confirmed verdict must still be protected from the clear step"
        )
        assert det.last_pass_counts == {
            "cover_total": 1,
            "new_count": 0,
            "repaired_count": 0,
        }, (
            "the pass must publish the album's cover TOTAL: its returned list "
            "holds only the verdicts it wrote, so reporting callers that counted "
            "the list logged a re-scanned album as having 0 covers"
        )

    def test_a_reconfirmed_verdict_restores_a_lost_suffix(self, monkeypatch):
        """The suffix repair belongs AFTER confirmation, never before it."""
        state = {
            "t1": {
                "cover_last_checked": None,
                "is_cover": 1,
                "original_cover_artist": "The Original",
                "cover_manual_override": False,
            }
        }
        det = _detector(state)
        det._detect_via_work_id = lambda **kwargs: {
            "artist": "The Original", "year": 1969, "confidence": "high",
        }
        _capture_clear(det)

        written: list[dict] = []
        monkeypatch.setattr(
            impl, "apply_cover_metadata_batch",
            lambda conn, updates: written.extend(updates) or [],
        )

        det.detect_covers_for_album(
            "Album", "Real Artist",
            [{"id": "t1", "title": "Own Song", "artist": "Real Artist",
              "work_mbid": "work-1"}],
        )

        assert [u.get("title") for u in written] == ["Own Song"], (
            "a genuinely re-confirmed cover must get its suffix repaired"
        )


# ---------------------------------------------------------------------------
# 3. Clearing removes the verdict from the FILE too
# ---------------------------------------------------------------------------


class TestClearingRemovesFileTags:
    @staticmethod
    def _clear_with_fake_db(monkeypatch, track: dict) -> dict:
        written: dict = {}

        class _Result:
            rowcount = 1

        class _Session:
            def execute(self, statement, params):
                return _Result()

        @contextmanager
        def _fake_session(*_a, **_k):
            yield _Session()

        import db.engine as engine

        monkeypatch.setattr(engine, "db_session", _fake_session)

        def _write_tags(file_path, tags):
            written["file_path"] = file_path
            written["tags"] = dict(tags)
            return True

        import services.metadata.tag_file_service as tag_service

        monkeypatch.setattr(tag_service, "write_tags_to_file", _write_tags)

        CoverDetector(db_connection=None)._clear_unconfirmed_verdicts(
            [track],
            confirmed_ids=set(),
            fresh_skipped=set(),
            confirmed_skipped=set(),
            force=False,
        )
        return written

    def test_the_db_clear_also_clears_the_file_cover_tags(self, monkeypatch):
        track = {
            "id": "t1",
            "title": "Own Song (Wrong Artist Cover)",
            "artist": "Real Artist",
            "is_cover": 1,
            "original_cover_artist": "Wrong Artist",
            "file_path": "Real Artist/Album/01.mp3",
        }
        written = self._clear_with_fake_db(monkeypatch, track)

        assert written.get("file_path") == track["file_path"]
        assert written["tags"].get("is_cover") == "", (
            "IS_COVER was left on disk; the next import would re-flag the track"
        )
        assert written["tags"].get("original_cover_artist") == ""
        assert written["tags"].get("title") == "Own Song", (
            "the stale '(X Cover)' suffix must still be stripped from the file"
        )
