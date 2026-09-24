"""A cover verdict is cleared ONLY after the deep detection pass has run.

REPORT: the popularity scan permanently deleted legitimate cover tags.

    [TRACK] false cover flag cleared track_id='…'
        title='Ruby, Don't Take Your Love to Town' detector_verdict='no_match'
    [TRACK] false cover flag cleared track_id='…'
        title='Mahna, Mahna'                        detector_verdict='no_match'
    [TRACK] false cover flag cleared track_id='…'
        title='Multiply the Heartaches'             detector_verdict='no_match'

All three ARE genuine covers. Because their titles carried "(X Cover)", the
identity pass stripped the wording and set ``title_had_cover_wording``, which
made ``track_stage`` force a re-check. It called ``detect_cover_song`` — a
SHALLOW check — whose only real evidence path needs ``work_mbid``, unpopulated
that early, so it answered ``no_match``. The old ``elif`` branch read that as
"disproven" and:

* set ``is_cover = False``,
* deleted "Cover" from ``musicbrainz_genres`` AND the ``genres`` CSV,
* claimed the cleaned title.

The deep verification — MusicBrainz work relations, ISRC, recording relations,
writer coverage — runs at the END of the album scan
(``CoverDetectorImpl.detect_covers_for_album``), so it never got a look.

⚠️ AND clearing both artifacts defeated the very check meant to protect a
confirmed cover: ``CoverDetectorImpl._is_already_confirmed_cover`` requires a
truthy ``is_cover`` AND a "cover" genre.

WHERE CLEARING BELONGS
----------------------
At the end of the deep pass, where "nothing confirmed it" is a MEANINGFUL
negative because every technique has actually been tried against real data.
The track stage now strips the wording ONLY.

These tests pin both halves: the stage must not clear, and the deep pass must.
"""

from __future__ import annotations

import inspect
import json

import pytest

from services.popularity import scan_hooks
from services.popularity.stages import track_stage

SUFFIXED = "Ruby, Don't Take Your Love to Town (Kenny Rogers Cover)"
CLEAN = "Ruby, Don't Take Your Love to Town"


class _Sink:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, row) -> None:
        self.rows.append(row)


def _run_track_stage(track_overrides: dict) -> dict:
    """Drive the REAL path: prepare_track_context -> process_track."""
    base = {
        "id": "t1",
        "artist": "Kenny Rogers",
        "album": "B-Sides and Rarities",
        "album_artist": "Kenny Rogers",
        "recording_mbid": "rec-1",
        "file_path": "Kenny Rogers/B-Sides and Rarities/01.mp3",
        "duration": 200,
    }
    track = {**base, **track_overrides}
    context = scan_hooks.prepare_track_context(
        track=dict(track),
        album_context={
            "album": "B-Sides and Rarities",
            "artist": "Kenny Rogers",
            "album_artist": "Kenny Rogers",
            "tracks": [track],
        },
    )
    raw = context["track"]
    sink = _Sink()
    track_stage.process_track(
        track=raw,
        track_context={"track": raw, "artist": "Kenny Rogers", "title": raw.get("title")},
        album_context={
            "album": "B-Sides and Rarities",
            "artist": "Kenny Rogers",
            "tracks": [raw],
        },
        album_result={},
        options={"metadata_only": True, "_deferred_persist": sink},
    )
    assert sink.rows, "the track stage persisted nothing"
    return sink.rows[0]


def _genuine_cover(**overrides) -> dict:
    """A real cover whose title carries the scanner's own "(X Cover)" suffix."""
    row = {
        "title": SUFFIXED,
        "is_cover": 1,
        "original_cover_artist": "Mel Tillis",
        "musicbrainz_genres": json.dumps(["Cover", "Country"]),
        "genres": "Cover, Country",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# 1. The track stage must NOT clear the verdict
# ---------------------------------------------------------------------------

class TestTrackStageDoesNotClearTheVerdict:
    def test_the_cover_flag_survives_a_shallow_no_match(self, monkeypatch):
        """THE reported bug: a shallow ``no_match`` must not delete the flag."""
        monkeypatch.setattr(
            track_stage, "detect_cover_song", lambda *a, **k: (False, "no_match")
        )
        result = _run_track_stage(_genuine_cover())
        assert result["is_cover"] in (1, True), (
            "the shallow check's no_match cleared a genuine cover's flag; only "
            "the deep detection pass may clear a verdict"
        )

    def test_the_cover_genre_survives(self, monkeypatch):
        monkeypatch.setattr(
            track_stage, "detect_cover_song", lambda *a, **k: (False, "no_match")
        )
        result = _run_track_stage(_genuine_cover())
        assert "cover" in str(result.get("musicbrainz_genres") or "").lower(), (
            "the Cover genre was purged; _is_already_confirmed_cover needs it to "
            "recognise an already-confirmed cover"
        )
        assert "country" in str(result.get("genres") or "").lower(), (
            "the legitimate genre must survive too"
        )

    def test_the_reason_is_not_overwritten_with_a_false_verdict(self, monkeypatch):
        monkeypatch.setattr(
            track_stage, "detect_cover_song", lambda *a, **k: (False, "no_match")
        )
        result = _run_track_stage(_genuine_cover())
        reason = str(result.get("is_cover_reason") or "")
        assert "removed from title" not in reason, (
            "the reason still claims the attribution was removed, which is the "
            "false verdict this report is about"
        )

    def test_the_wording_is_STILL_stripped_from_the_title(self, monkeypatch):
        """The strip was always the legitimate half and must keep working."""
        monkeypatch.setattr(
            track_stage, "detect_cover_song", lambda *a, **k: (False, "no_match")
        )
        result = _run_track_stage(_genuine_cover())
        assert result.get("title") == CLEAN, (
            "the '(Kenny Rogers Cover)' suffix must still be removed — that part "
            "was the point of the branch"
        )

    def test_a_genuine_confirmation_still_wins(self, monkeypatch):
        monkeypatch.setattr(
            track_stage, "detect_cover_song",
            lambda *a, **k: (True, "musicbrainz_work_relation"),
        )
        result = _run_track_stage(_genuine_cover())
        assert result["is_cover"] in (1, True)
        assert "cover" in str(result.get("musicbrainz_genres") or "").lower()

    def test_a_manual_override_is_untouched(self):
        result = _run_track_stage(_genuine_cover(cover_manual_override=1))
        assert result["is_cover"] in (1, True)


class TestTheStageNoLongerContainsTheDestructiveCode:
    """Source-level guards, so a later refactor cannot quietly reinstate it."""

    @staticmethod
    def _stage_source() -> str:
        return inspect.getsource(track_stage.process_track)

    def test_it_never_assigns_a_false_cover_flag(self):
        source = self._stage_source()
        assert 'update_payload["is_cover"] = False' not in source, (
            "the track stage must never clear is_cover; that is the deep pass's "
            "job, where a negative result is meaningful"
        )

    def test_it_never_writes_the_false_verdict_reason(self):
        source = self._stage_source()
        assert "cover attribution removed from title" not in source, (
            "the reason string that recorded the false verdict must be gone"
        )

    def test_it_claims_the_cleaned_title_still(self):
        """The one thing the branch must keep doing."""
        source = self._stage_source()
        assert 'update_payload["title"]' in source


# ---------------------------------------------------------------------------
# 2. The deep pass must clear an unconfirmed verdict
# ---------------------------------------------------------------------------

class _FakeRow(dict):
    def __getitem__(self, key):  # pragma: no cover - dict is enough
        return super().__getitem__(key)


class TestDeepPassClearsUnconfirmedVerdicts:
    @staticmethod
    def _detector():
        from services.enrichment.cover_detector_impl import CoverDetector

        return CoverDetector(db_connection=None)

    def test_it_clears_a_flag_it_could_not_confirm(self, monkeypatch):
        import services.enrichment.cover_detector_impl as impl

        detector = self._detector()
        track = {
            "id": "t1",
            "title": CLEAN,
            "artist": "Kenny Rogers",
            "is_cover": 1,
            "original_cover_artist": "Mel Tillis",
            "musicbrainz_genres": json.dumps(["Cover", "Country"]),
        }

        cleared: list[str] = []

        class _Result:
            rowcount = 1

        class _Session:
            def execute(self, statement, params):
                cleared.append(str(params.get("id")))
                return _Result()

        from contextlib import contextmanager

        @contextmanager
        def _fake_session(*_a, **_k):
            yield _Session()

        # Import locally, exactly as the production code does.
        import db.engine as engine

        monkeypatch.setattr(engine, "db_session", _fake_session)
        monkeypatch.setattr(impl, "_fresh_skipped_never_used", None, raising=False)

        detector._clear_unconfirmed_verdicts(
            [track],
            confirmed_ids=set(),
            fresh_skipped=set(),
            confirmed_skipped=set(),
            force=False,
        )
        assert cleared == ["t1"], (
            "an assessed track whose verdict was not confirmed must be cleared"
        )

    def test_it_never_clears_a_confirmed_verdict(self):
        detector = self._detector()
        track = {"id": "t1", "is_cover": 1, "original_cover_artist": "Mel Tillis"}

        cleared: list[str] = []

        class _Result:
            rowcount = 1

        class _Session:
            def execute(self, statement, params):
                cleared.append(str(params.get("id")))
                return _Result()

        from contextlib import contextmanager

        @contextmanager
        def _fake_session(*_a, **_k):
            yield _Session()

        import db.engine as engine

        original = engine.db_session
        engine.db_session = _fake_session
        try:
            detector._clear_unconfirmed_verdicts(
                [track],
                confirmed_ids={"t1"},
                fresh_skipped=set(),
                confirmed_skipped=set(),
                force=False,
            )
        finally:
            engine.db_session = original
        assert cleared == [], "a confirmed cover must never be cleared"

    def test_it_never_clears_a_manual_override(self):
        detector = self._detector()
        track = {
            "id": "t1",
            "is_cover": 1,
            "original_cover_artist": "Mel Tillis",
            "cover_manual_override": 1,
        }

        cleared: list[str] = []

        class _Result:
            rowcount = 1

        class _Session:
            def execute(self, statement, params):
                cleared.append(str(params.get("id")))
                return _Result()

        from contextlib import contextmanager

        @contextmanager
        def _fake_session(*_a, **_k):
            yield _Session()

        import db.engine as engine

        original = engine.db_session
        engine.db_session = _fake_session
        try:
            detector._clear_unconfirmed_verdicts(
                [track],
                confirmed_ids=set(),
                fresh_skipped=set(),
                confirmed_skipped=set(),
                force=False,
            )
        finally:
            engine.db_session = original
        assert cleared == [], "a user-locked verdict outranks every heuristic"

    def test_a_fresh_track_is_not_cleared(self):
        """Assessed within the recheck window — silence is not evidence.

        Without this the daily scan would clear every unflagged track on its
        second pass.
        """
        detector = self._detector()
        track = {"id": "t1", "is_cover": 1, "original_cover_artist": "Mel Tillis"}

        cleared: list[str] = []

        class _Result:
            rowcount = 1

        class _Session:
            def execute(self, statement, params):
                cleared.append(str(params.get("id")))
                return _Result()

        from contextlib import contextmanager

        @contextmanager
        def _fake_session(*_a, **_k):
            yield _Session()

        import db.engine as engine

        original = engine.db_session
        engine.db_session = _fake_session
        try:
            detector._clear_unconfirmed_verdicts(
                [track],
                confirmed_ids=set(),
                fresh_skipped={"t1"},
                confirmed_skipped=set(),
                force=False,
            )
        finally:
            engine.db_session = original
        assert cleared == [], (
            "a track skipped as fresh was never assessed, so it must not be "
            "cleared on that silence"
        )


class TestSelfReferentialVerdictsAreIgnored:
    """\"Track X (Track X Cover)\" is provably wrong and must not self-confirm."""

    @staticmethod
    def _is_self_referential(track: dict) -> bool:
        from services.enrichment.cover_detector_impl import CoverDetector

        return CoverDetector._stored_verdict_is_self_referential(track)

    def test_an_original_matching_the_performer_is_flagged(self):
        assert self._is_self_referential({
            "is_cover": 1,
            "artist": "P.O.D.",
            "original_cover_artist": "P.O.D.",
        }) is True

    def test_it_matches_when_only_whitespace_differs(self):
        """``names_match`` tolerates whitespace/case, which is enough here.

        ⚠️ Verified rather than assumed: ``names_match`` is NOT
        accent- or punctuation-insensitive. It returns False for
        ``"Ünloco"`` vs ``"Unloco"``, ``"P.O.D."`` vs ``"POD"`` and
        ``"AC/DC"`` vs ``"ACDC"``. An earlier version of this test asserted the
        opposite and failed — which is the correct outcome for a test that
        encodes an assumption about a shared helper instead of its behaviour.

        This matters for the check itself: it can only catch a self-referential
        verdict when the two names normalise identically, so it is a
        conservative guard, not a complete one. That is the right failure
        direction — a missed detection leaves the verdict alone, whereas a
        false positive would clear a real cover.
        """
        assert self._is_self_referential({
            "is_cover": 1,
            "artist": "Kenny  Rogers",
            "original_cover_artist": "Kenny Rogers",
        }) is True

    def test_a_real_cover_is_not_flagged(self):
        assert self._is_self_referential({
            "is_cover": 1,
            "artist": "Kenny Rogers",
            "original_cover_artist": "Mel Tillis",
        }) is False

    def test_a_placeholder_performer_cannot_conclude(self):
        assert self._is_self_referential({
            "is_cover": 1,
            "artist": "Various Artists",
            "original_cover_artist": "Various Artists",
        }) is False

    def test_a_manual_override_is_never_flagged(self):
        assert self._is_self_referential({
            "is_cover": 1,
            "artist": "P.O.D.",
            "original_cover_artist": "P.O.D.",
            "cover_manual_override": 1,
        }) is False


class TestClearingHappensAfterDetection:
    """Ordering guard: the clear call must come AFTER the detection pipeline."""

    def test_the_clear_call_follows_the_detection_steps(self):
        import services.enrichment.cover_detector_impl as impl

        source = inspect.getsource(impl.CoverDetector.detect_covers_for_album)
        clear_at = source.index("_clear_unconfirmed_verdicts")
        # Every deep technique must be attempted before the clear runs.
        for technique in (
            "_detect_via_isrc",
            "_detect_via_recording_relation",
            "track_writers",
        ):
            assert source.index(technique) < clear_at, (
                f"{technique} must run BEFORE the clear, or a verdict is cleared "
                "on incomplete evidence"
            )
