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
    def test_the_suffixed_title_is_replaced_in_the_persisted_row(self):
        """The reported symptom: the flag cleared but the title did not."""
        result = _persist(_false_cover())
        assert result.get("title") == CLEAN_TITLE, (
            "the cleaned title was dropped from the persist payload; "
            "`title` is in _STALE_PROTECTED_COLUMNS and must be claimed by "
            "update_payload or the DB keeps the '(Artist Cover)' suffix"
        )

    def test_the_flag_is_cleared(self):
        result = _persist(_false_cover())
        assert result["is_cover"] in (False, 0)
        assert result["is_cover_reason"] == "cover attribution removed from title"

    def test_the_cover_genre_is_gone_from_both_columns(self):
        """The second reported symptom: the Cover genre stayed behind."""
        result = _persist(_false_cover())
        assert "cover" not in str(result.get("musicbrainz_genres") or "").lower()
        assert "cover" not in str(result.get("genres") or "").lower()
        # The legitimate genre survives.
        assert "rock" in str(result.get("genres") or "").lower()

    def test_cover_as_the_only_genre_does_not_come_back(self):
        """With no other source, "Cover" must not be re-derived from the title.

        ``_append_extra_genres`` re-adds the intercepted "Cover" filter tag
        whenever the TITLE contains the word — so this case fails unless the
        TITLE is claimed too.
        """
        result = _persist(
            _false_cover(
                musicbrainz_genres=json.dumps(["Cover"]),
                genres="Cover",
            )
        )
        assert result.get("title") == CLEAN_TITLE
        assert "cover" not in str(result.get("genres") or "").lower()
        assert "cover" not in str(result.get("musicbrainz_genres") or "").lower()


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


class TestTheClaimIsScoped:
    """Guard the two mechanisms so a later refactor cannot silently undo them."""

    @staticmethod
    def _clear_branch_source() -> str:
        import inspect

        source = inspect.getsource(track_stage.process_track)
        start = source.index("cover attribution removed from title")
        # The branch ends at the next `except` that closes the cover block.
        end = source.index("Cover detection failed", start)
        return source[start:end]

    def test_the_clear_branch_claims_the_title(self):
        window = self._clear_branch_source()
        assert 'update_payload["title"]' in window, (
            "the clear branch must claim `title` in update_payload, or "
            "_strip_album_type_columns drops the cleaned value"
        )

    def test_the_clear_branch_seeds_the_genres_csv(self):
        window = self._clear_branch_source()
        assert 'update_payload["genres"]' in window, (
            "the clear branch must also seed the `genres` CSV; filtering only "
            "`musicbrainz_genres` left 'cover' to be re-voted from the stale CSV"
        )

    def test_the_stale_protection_is_not_weakened(self):
        """The fix claims the title; it must NOT remove the column protection."""
        from services.popularity.stages.track_stage import _STALE_PROTECTED_COLUMNS

        assert "title" in _STALE_PROTECTED_COLUMNS, (
            "_STALE_PROTECTED_COLUMNS must keep protecting `title` — it stops a "
            "stale loaded title clobbering the album stage's cover rename. Claim "
            "the title in update_payload instead of relaxing this guard."
        )
