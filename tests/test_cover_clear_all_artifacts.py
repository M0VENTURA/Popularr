"""A cleared cover verdict must clear ALL THREE of its artifacts.

REPORT: "When tracks are incorrectly marked as covers, it's removing the flag
from them being a cover, but the track name is still showing
TrackName (Artist Cover) and it's keeping the genre of cover assigned to the
track."

The cover verdict is stored as three separate things:

    1. ``is_cover``            the flag
    2. ``title``               "TrackName (Artist Cover)"
    3. ``genres`` / ``musicbrainz_genres``   the "Cover" genre

``track_stage`` cleared only #1 (and filtered #3's ``musicbrainz_genres`` copy),
so a falsely-flagged track kept the suffixed title and the Cover genre — the
reported half-fix.

WHY THE TITLE SURVIVED, precisely
---------------------------------
``prepare_track_context`` DOES strip the wording in place on the loaded row, so
the in-memory title is already clean. But ``title`` lives in
``_STALE_PROTECTED_COLUMNS`` (``frozenset({"title", "album_artist"}) | …``), and
``_strip_album_type_columns`` DROPS every protected column that
``update_payload`` does not claim — a guard that exists so a stale loaded title
cannot clobber the album stage's rename of a GENUINE cover. The clear branch
never claimed it, so the cleaned title was thrown away and the DB kept the
suffixed one.

WHY THE GENRE CAME BACK, precisely
----------------------------------
Two independent paths re-added it:

  a. step 5's genre aggregation re-votes from ``effective_track``
     (= raw track + update_payload), which still carried the OLD ``genres`` CSV
     ("Cover, Rock") — filtering ``musicbrainz_genres`` alone did not touch it;
  b. ``genre_aggregation_service._append_extra_genres`` re-adds an intercepted
     "Cover" filter tag whenever the TITLE contains the word "cover", and the
     stale title was still feeding it.

Claiming the cleaned title fixes (b) as well, because step 5 reads its
``context_title`` from ``effective_track``.

These tests drive the REAL production path — ``prepare_track_context`` then
``process_track`` — so they pin the interaction, not just the branch.
"""

from __future__ import annotations

import json

import pytest

from services.popularity import scan_hooks
from services.popularity.stages import track_stage

SUFFIXED_TITLE = "TrackName (Artist Cover)"
CLEAN_TITLE = "TrackName"


class _Sink:
    """Mirrors ``DeferredPersistSink``'s real ``.add(row)`` contract."""

    def __init__(self):
        self.rows: list[dict] = []

    def add(self, row) -> None:
        self.rows.append(row)


def _persist(track_overrides: dict) -> dict:
    """Run one track through prepare_track_context -> process_track.

    Returns the row the scan actually writes to the DB.
    """
    base = {
        "id": "t1",
        "artist": "Artist",
        "album": "Album",
        "album_artist": "Artist",
        "recording_mbid": "rec-1",
        "file_path": "Artist/Album/1.mp3",
        "duration": 200,
    }
    track = {**base, **track_overrides}

    context = scan_hooks.prepare_track_context(
        track=dict(track),
        album_context={
            "album": "Album",
            "artist": "Artist",
            "album_artist": "Artist",
            "tracks": [track],
        },
    )
    raw = context["track"]

    sink = _Sink()
    track_stage.process_track(
        track=raw,
        track_context={"track": raw, "artist": "Artist", "title": raw.get("title")},
        album_context={"album": "Album", "artist": "Artist", "tracks": [raw]},
        album_result={},
        options={"metadata_only": True, "_deferred_persist": sink},
    )
    assert sink.rows, "the track stage persisted nothing"
    return sink.rows[0]


def _false_cover(**overrides) -> dict:
    """The reported row: false cover title + verdict + genre already stored."""
    row = {
        "title": SUFFIXED_TITLE,
        "is_cover": 1,
        "original_cover_artist": "Artist",
        "musicbrainz_genres": json.dumps(["Cover", "Rock"]),
        "genres": "Cover, Rock",
    }
    row.update(overrides)
    return row


class TestAllThreeArtifactsAreCleared:
    """⚠️ UPDATED 2026-09-24 — WHERE each artifact is now cleared.

    This suite was written when ``track_stage`` cleared all three. That turned
    out to be destructive: the stage runs a SHALLOW check, so it cleared
    legitimate covers before the deep pass could look at them. See
    ``test_cover_verdict_cleared_only_after_deep_detection.py``.

    Split by artifact:

    * the TITLE is still normalised here — that was always the legitimate half;
    * the FLAG and the GENRE are now cleared by the deep detection pass, at the
      end of the album scan, where every technique has actually run.
    """

    def test_the_suffixed_title_is_replaced_in_the_persisted_row(self):
        """The reported symptom: the flag cleared but the title did not."""
        result = _persist(_false_cover())
        assert result.get("title") == CLEAN_TITLE, (
            "the cleaned title was dropped from the persist payload; "
            "`title` is in _STALE_PROTECTED_COLUMNS and must be claimed by "
            "update_payload or the DB keeps the '(Artist Cover)' suffix"
        )

    def test_the_stage_does_NOT_clear_the_flag(self):
        """⚠️ Inverted from the original assertion, deliberately.

        The stage must leave the verdict for the deep pass. Clearing here is
        what deleted genuine covers' flags.
        """
        result = _persist(_false_cover())
        assert result.get("is_cover") in (1, True), (
            "the track stage cleared the cover flag; only the deep detection "
            "pass may clear a verdict, because its negative result is "
            "meaningful and the shallow check's is not"
        )

    def test_the_cover_genre_is_left_for_the_deep_pass(self):
        """The genre is evidence ``_is_already_confirmed_cover`` needs."""
        result = _persist(_false_cover())
        assert "cover" in str(result.get("musicbrainz_genres") or "").lower(), (
            "the Cover genre was purged by the stage; the deep pass needs it to "
            "recognise an already-confirmed cover"
        )
        assert "rock" in str(result.get("genres") or "").lower(), (
            "the legitimate genre must survive"
        )

    def test_cover_as_the_only_genre_survives_the_stage(self):
        result = _persist(
            _false_cover(
                musicbrainz_genres=json.dumps(["Cover"]),
                genres="Cover",
            )
        )
        assert result.get("title") == CLEAN_TITLE
        assert "cover" in str(result.get("genres") or "").lower()


class TestGenuineCoversAreUntouched:
    """The clear branch must only run for wording-derived verdicts."""

    def test_a_confirmed_cover_keeps_its_title_and_genre(self, monkeypatch):
        """When detection still says COVER, nothing is cleared.

        A genuine cover's DB title is "Title (X Cover)" — written by
        ``_build_cover_title``. On the next scan the identity pass strips that
        wording (setting ``title_had_cover_wording``) and the verdict is
        FORCE-re-detected; detection finding real cover evidence must win, so
        the ``if is_cover`` branch runs and the stored title/genre stand.
        """
        monkeypatch.setattr(
            track_stage, "detect_cover_song", lambda *a, **k: (True, "musicbrainz_work_relation")
        )
        result = _persist(
            {
                "title": SUFFIXED_TITLE,
                "is_cover": 1,
                "original_cover_artist": "Disturbed",
                "musicbrainz_genres": json.dumps(["Cover", "Rock"]),
                "genres": "Cover, Rock",
            }
        )
        assert result["is_cover"] in (1, True)
        assert result["is_cover_reason"] == "musicbrainz_work_relation"
        assert "cover" in str(result.get("musicbrainz_genres") or "").lower()

    def test_a_manual_override_is_never_cleared(self):
        """A user-locked verdict outranks the wording heuristic."""
        result = _persist(_false_cover(cover_manual_override=1))
        assert result["is_cover"] in (1, True)

    def test_a_title_merely_containing_the_word_is_untouched(self):
        """"Cover Me" is not an attribution and must survive verbatim."""
        result = _persist(
            {
                "title": "Cover Me",
                "is_cover": 0,
                "musicbrainz_genres": json.dumps(["Rock"]),
                "genres": "Rock",
            }
        )
        assert result.get("title") in (None, "Cover Me")
        assert "rock" in str(result.get("genres") or "").lower()


class TestTheTitleClaimIsScoped:
    """Guard the mechanism so a later refactor cannot silently undo it.

    ⚠️ UPDATED 2026-09-24. The original version of this class anchored on the
    string ``"cover attribution removed from title"``, which was the reason
    string the destructive branch wrote. That branch is gone (it cleared
    genuine covers from a shallow check), so the anchor is now the surviving
    part — the title claim — and the assertions about the genre purge are
    replaced by ones asserting the genre is NOT purged here.
    """

    @staticmethod
    def _cover_branch_source() -> str:
        """The branch that strips the wording without clearing the verdict.

        ⚠️ The anchor is the branch COMMENT, not the log message. Anchoring on
        ``"cover wording stripped from title"`` lands on the ``logger.debug``
        call at the END of the branch, so the window is the tail only and the
        title claim before it is never inspected — the assertion then fails for
        a reason that has nothing to do with the behaviour. (This codebase has
        hit the same class of mistake with ``source.index`` on a doc comment
        before.)
        """
        import inspect

        source = inspect.getsource(track_stage.process_track)
        start = source.index("STRIP THE WORDING ONLY")
        end = source.index("Cover detection failed", start)
        return source[start:end]

    def test_the_branch_claims_the_title(self):
        window = self._cover_branch_source()
        assert 'update_payload["title"]' in window, (
            "the branch must claim `title` in update_payload, or "
            "_strip_album_type_columns drops the cleaned value"
        )

    def test_the_branch_does_not_clear_the_verdict(self):
        window = self._cover_branch_source()
        assert 'update_payload["is_cover"] = False' not in window, (
            "the branch must NOT clear is_cover — that is the deep pass's job"
        )

    def test_the_branch_does_not_purge_the_genre(self):
        window = self._cover_branch_source()
        assert 'update_payload["musicbrainz_genres"]' not in window, (
            "the branch must NOT rewrite musicbrainz_genres; the deep pass "
            "needs the Cover genre to recognise a confirmed cover"
        )
        assert 'update_payload["genres"]' not in window, (
            "the branch must NOT rewrite the genres CSV either"
        )

    def test_the_stale_protection_is_not_weakened(self):
        """The fix claims the title; it must NOT remove the column protection."""
        from services.popularity.stages.track_stage import _STALE_PROTECTED_COLUMNS

        assert "title" in _STALE_PROTECTED_COLUMNS, (
            "_STALE_PROTECTED_COLUMNS must keep protecting `title` — it stops a "
            "stale loaded title clobbering the album stage's cover rename. Claim "
            "the title in update_payload instead of relaxing this guard."
        )
