"""Duplicate copies: recommend which to keep, and stop dupes faking missing tracks.

TWO REPORTED PROBLEMS
---------------------
1. **No guidance.** Two rows for one title both carry "Duplicate ×2"; nothing
   says which to keep, so the user compares durations and tags by eye.
2. **Dupes create phantom missing tracks.** A double download inflates every
   COUNT-based comparison, and the album then appears to be missing tracks (or
   loses its MusicBrainz identity).

THE RECOMMENDATION
------------------
Quality first (lossless > bitrate > sample rate, via the download pipeline's own
scorer), metadata only to break a quality tie. It NEVER deletes anything — it is
advice, and a genuine tie reports "tie" rather than picking arbitrarily.
"""

from __future__ import annotations

import pytest

from services.metadata import duplicate_version_service as dvs


def _row(track_id: str, path: str, **overrides) -> dict:
    row = {
        "id": track_id,
        "file_path": path,
        "stars": 0,
        "duration": 204,
        "recording_mbid": None,
        "isrc": None,
        "musicbrainz_albumid": None,
        "year": None,
        "composer": None,
        "writer": None,
        "genres": None,
        "recordlabel": None,
    }
    row.update(overrides)
    return row


@pytest.fixture
def quality(monkeypatch):
    """Override the on-disk quality scorer with fixed per-path scores."""
    scores: dict[str, int] = {}

    monkeypatch.setattr(dvs, "_quality_score", lambda path: scores.get(path, 0))
    return scores


# ---------------------------------------------------------------------------
# 1. Quality decides
# ---------------------------------------------------------------------------

class TestQualityDecides:
    def test_the_lossless_copy_is_recommended(self, quality) -> None:
        quality.update({"a.flac": 100_000, "a.mp3": 320})
        result = dvs.recommend_duplicate_keeper([
            _row("t1", "a.flac"), _row("t2", "a.mp3"),
        ])
        assert result["keep_track_id"] == "t1"
        assert result["remove_track_ids"] == ["t2"]
        assert result["confidence"] == "high"
        assert "lossless" in result["reason"].lower()

    def test_the_higher_bitrate_wins_between_two_lossy_copies(self, quality) -> None:
        quality.update({"a.mp3": 320, "b.mp3": 192})
        result = dvs.recommend_duplicate_keeper([
            _row("t1", "a.mp3"), _row("t2", "b.mp3"),
        ])
        assert result["keep_track_id"] == "t1"
        assert result["confidence"] == "high"

    def test_better_metadata_does_not_beat_better_audio(self, quality) -> None:
        """The best-tagged copy can be the worse rip — quality still wins.

        This is the whole point of ranking quality first: metadata can be
        re-fetched, a lossy file cannot be un-compressed.
        """
        quality.update({"good.flac": 100_000, "bad.mp3": 192})
        result = dvs.recommend_duplicate_keeper([
            _row("t1", "good.flac"),
            _row("t2", "bad.mp3", recording_mbid="x", isrc="y", composer="z",
                 writer="z", genres="Rock", year="2019", recordlabel="L"),
        ])
        assert result["keep_track_id"] == "t1", (
            "the well-tagged lossy copy must not beat the untagged lossless one"
        )


# ---------------------------------------------------------------------------
# 2. Metadata breaks a quality tie
# ---------------------------------------------------------------------------

class TestMetadataBreaksATie:
    def test_the_better_tagged_copy_wins_when_quality_is_equal(self, quality) -> None:
        quality.update({"a.flac": 100_000, "b.flac": 100_000})
        result = dvs.recommend_duplicate_keeper([
            _row("t1", "a.flac"),
            _row("t2", "b.flac", recording_mbid="mb", isrc="isrc",
                 genres="Rock", year="2019"),
        ])
        assert result["keep_track_id"] == "t2"
        assert result["confidence"] == "metadata"
        assert "metadata" in result["reason"].lower()

    def test_a_tie_reports_a_tie_and_picks_nothing(self, quality) -> None:
        """Identical quality AND identical tags is a judgment call.

        Stars are popularity, not file quality — they must never silently decide
        which file is deleted.
        """
        quality.update({"a.mp3": 320, "b.mp3": 320})
        result = dvs.recommend_duplicate_keeper([
            _row("t1", "a.mp3", stars=5),
            _row("t2", "b.mp3", stars=1),
        ])
        assert result["keep_track_id"] == ""
        assert result["remove_track_ids"] == []
        assert result["confidence"] == "tie"

    def test_nothing_is_ever_deleted(self, quality) -> None:
        quality.update({"a.flac": 100_000, "b.mp3": 192})
        result = dvs.recommend_duplicate_keeper([
            _row("t1", "a.flac"), _row("t2", "b.mp3"),
        ])
        # The result only NAMES the loser; no file or row operation is implied.
        assert "remove_path" not in result
        assert set(result) == {
            "keep_track_id", "remove_track_ids", "confidence", "reason", "copies",
        }


# ---------------------------------------------------------------------------
# 3. Metadata scoring
# ---------------------------------------------------------------------------

class TestMetadataScoring:
    def test_identity_fields_score_highest(self) -> None:
        bare = _row("t1", "a.flac")
        tagged = _row("t2", "b.flac", recording_mbid="mb", isrc="i")
        assert dvs.metadata_score(tagged) > dvs.metadata_score(bare)

    def test_empty_placeholders_do_not_score(self) -> None:
        for empty in (None, "", "[]", "{}", "null", "none", "—"):
            row = _row("t1", "a.flac", composer=empty, genres=empty)
            assert dvs.metadata_score(row) == 0, empty

    def test_the_reasons_name_the_fields(self) -> None:
        row = _row("t1", "a.flac", recording_mbid="mb", year="2019")
        assert set(dvs.metadata_reasons(row)) == {"recording_mbid", "year"}


# ---------------------------------------------------------------------------
# 4. Duplicates must not fabricate missing tracks
# ---------------------------------------------------------------------------

class TestDuplicatesDoNotFakeMissingTracks:
    def test_the_local_track_count_ignores_duplicate_rows(self, monkeypatch) -> None:
        """REPORTED: a duplicate made the album look like it had extra tracks.

        ``_get_local_track_count`` feeds the release guard, which rejects a
        release when local and MusicBrainz counts differ by >= 4 — so one
        duplicate could cost the album its identity.
        """
        from services.enrichment import musicbrainz_service as mbs

        sql_seen: list[str] = []

        class _Result:
            def fetchone(self):
                return (10,)

        class _Session:
            def execute(self, stmt, params=None):
                sql_seen.append(str(stmt))
                return _Result()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        import contextlib

        @contextlib.contextmanager
        def _session():
            yield _Session()

        monkeypatch.setattr("db.engine.db_session", _session)
        count = mbs._get_local_track_count("Flaw", "Vol IV")

        assert count == 10
        joined = " ".join(sql_seen).upper()
        assert "DISTINCT" in joined, (
            "the local track count still counts ROWS, so a duplicate inflates it"
        )

    def test_the_missing_count_sql_is_distinct(self) -> None:
        """Static check: the count query must be DISTINCT-keyed.

        This is a SOURCE check rather than a runtime one on purpose: the real
        query needs the ``missing_album_tracks`` table, so a runtime test would
        be asserting against an environment rather than the code. Reading the
        source still fails if the query is reverted.
        """
        import inspect

        from services.metadata import album_missing_service as ams

        src = inspect.getsource(ams.get_missing_tracks_from_db)
        count_section = src.split("library_count = session.execute", 1)[1][:900]
        assert "DISTINCT" in count_section.upper(), (
            "the persisted-missing library count is a plain COUNT(*), so a "
            "duplicate inflates it"
        )

    def test_a_perfect_tracklist_with_one_duplicate_still_matches(self) -> None:
        """MBIDs are only written on a perfect match; a dupe used to break it."""
        from services.metadata.album_tag_sync_service import _is_perfect_match

        mb_index = {(1, 1): {}, (1, 2): {}, (1, 3): {}}
        tracks_with_dupe = [
            {"disc_number": "1", "track_number": "1"},
            {"disc_number": "1", "track_number": "2"},
            {"disc_number": "1", "track_number": "2"},   # the duplicate row
            {"disc_number": "1", "track_number": "3"},
        ]
        assert _is_perfect_match(tracks_with_dupe, mb_index, 3) is True, (
            "a duplicate row made the tracklist fail the perfect-match test, so "
            "the MusicBrainz identity tags were never written"
        )

    def test_a_genuinely_extra_position_still_fails(self) -> None:
        from services.metadata.album_tag_sync_service import _is_perfect_match

        mb_index = {(1, 1): {}, (1, 2): {}}
        tracks = [
            {"disc_number": "1", "track_number": "1"},
            {"disc_number": "1", "track_number": "2"},
            {"disc_number": "1", "track_number": "3"},   # not on the release
        ]
        assert _is_perfect_match(tracks, mb_index, 2) is False
