"""A compilation's cover verdict must be measured against the TRACK artist.

Reported: *"Cover detection doesn't seem to be happening for various artists
compilations based on track artist."*

A cover verdict is always a statement about a **performer**: "this recording is
not by whoever released it first". On a Various Artists compilation the ALBUM
artist is the placeholder ``"Various Artists"``, which matches no real credit —
so every comparison made against it is vacuous:

* ``_same_artist(candidate, "Various Artists")`` is never true, so the
  performer's own recording is never excluded and the "the performer released it
  first" conclusive negative can never fire;
* ``_performer_wrote_work(work, "Various Artists")`` returns False by its own
  placeholder guard, so a track artist who WROTE the song is never recognised as
  the originator.

Measured on *The Best 80's Car Songs In The World… Ever!*: six of the nine
detections were correct (Tainted Love → Gloria Jones, Everything I Own → Bread,
…), but **Crowded House's own "Don't Dream It's Over" — which they wrote and
released in 1986 — was attributed to "(Denise Jannah Cover)"**, because nothing
about Crowded House was ever consulted.

Two of the six detection steps already resolved the track artist
(work-first, ISRC, writer matching). Steps 3 (recording relations), 5 (title
hints) and 6 (work fallback) passed the album artist instead.
"""

from __future__ import annotations

import pytest

from services.enrichment import cover_detector_impl as impl
from services.enrichment.cover_detector_impl import CoverDetector, comparison_artist

ALBUM_ARTIST = "Various Artists"
ALBUM = "The Best 80's Car Songs In The World... Ever!"


# ---------------------------------------------------------------------------
# 1. The resolver — pure
# ---------------------------------------------------------------------------


class TestComparisonArtist:
    def test_the_track_artist_wins_on_a_compilation(self) -> None:
        """⭐ THE REPORTED CASE."""
        track = {"artist": "Crowded House", "album_artist": ALBUM_ARTIST}
        assert comparison_artist(track, ALBUM_ARTIST) == "Crowded House"

    def test_a_placeholder_track_artist_falls_back_to_the_album(self) -> None:
        """A row whose ARTIST column carries the album placeholder is not a
        performer, so a single-artist release can still be assessed."""
        track = {"artist": "Various Artists"}
        assert comparison_artist(track, "Bonnie Tyler") == "Bonnie Tyler"

    def test_two_placeholders_resolve_to_nothing(self) -> None:
        """⭐ ``""`` means "no performer to compare against" — NEVER a match.

        Returning the placeholder here is what made the comparisons vacuous: an
        empty string is falsy, so the callers' ``if performer`` guards skip the
        comparison instead of running it against a name that can never match.
        """
        assert comparison_artist({"artist": ""}, ALBUM_ARTIST) == ""
        assert comparison_artist({"artist": ALBUM_ARTIST}, ALBUM_ARTIST) == ""
        assert comparison_artist({}, "") == ""

    def test_a_single_artist_album_is_unchanged(self) -> None:
        """CONTROL: where album artist and track artist agree, nothing moves —
        which is why this defect only ever showed up on compilations."""
        track = {"artist": "Bonnie Tyler", "album_artist": "Bonnie Tyler"}
        assert comparison_artist(track, "Bonnie Tyler") == "Bonnie Tyler"

    @pytest.mark.parametrize(
        "placeholder",
        ["Various Artists", "various artists", "VA", "v/a", "V.A.", "Soundtrack", "Unknown Artist"],
    )
    def test_every_placeholder_spelling_is_recognised(self, placeholder: str) -> None:
        """The list lives in ``is_track_artist_placeholder``; drift here is how
        this module ended up with its own copy."""
        assert comparison_artist({"artist": placeholder}, ALBUM_ARTIST) == ""

    def test_a_real_artist_that_merely_contains_various_is_kept(self) -> None:
        """CONTROL: the test is membership, not a substring scan."""
        assert comparison_artist({"artist": "Various Hands"}, ALBUM_ARTIST) == "Various Hands"

    def test_surrounding_whitespace_is_trimmed(self) -> None:
        assert comparison_artist({"artist": "  Crowded House  "}, ALBUM_ARTIST) == "Crowded House"


# ---------------------------------------------------------------------------
# 2. The mechanism — why a placeholder performer produces a false positive
# ---------------------------------------------------------------------------


class _WriterStub:
    """MusicBrainz stub: one work, two recordings, writers as given."""

    def __init__(self, recordings, writers):
        self._recordings = recordings
        self._writers = writers

    def get_work(self, work_id, inc=None):
        return {
            "id": work_id,
            "title": "Don't Dream It's Over",
            "artist-relation-list": [
                {"type": "composer", "artist": {"id": f"w{i}", "name": name}}
                for i, name in enumerate(self._writers)
            ],
        }

    def get_artist(self, artist_id, inc=None):
        return {"id": artist_id, "artist-relation-list": []}

    def search_artists(self, query, limit=5):
        return []

    def browse_work_recordings(self, work_id, inc=None, limit=100):
        return list(self._recordings)

    def search_recordings(self, query, limit=15):
        return list(self._recordings)

    def get_recording(self, mbid, inc=None):
        for rec in self._recordings:
            if rec.get("id") == mbid:
                return dict(rec)
        return {}

    def lookup_by_isrc(self, isrc, inc=None):
        return []

    def get_release(self, mbid, inc=None):
        return {}


def _detector(mb) -> CoverDetector:
    det = CoverDetector(db_connection=None)
    det.mb = mb
    det._load_cover_checked_map = lambda ids: {}
    det._clear_unconfirmed_verdicts = lambda *a, **k: None
    det._persist_checked = lambda *a, **k: None
    det._update_file_metadata = lambda *a, **k: True
    return det


CROWDED_HOUSE_REC = {
    "id": "rec-crowded",
    "title": "Don't Dream It's Over",
    "artist-credit": [{"artist": {"id": "a1", "name": "Crowded House"}}],
    "first-release-date": "1986-08-01",
}
DENISE_REC = {
    "id": "rec-denise",
    "title": "Don't Dream It's Over",
    "artist-credit": [{"artist": {"id": "a2", "name": "Denise Jannah"}}],
    "first-release-date": "1995-03-01",
}


class TestTheWriterCheckCanFireOnlyForARealPerformer:
    def test_the_writer_performer_is_recognised_as_the_originator(self) -> None:
        """⭐ Neil Finn wrote it and Crowded House released it first, so the
        conclusive negative (``{}``) is the right answer."""
        # The writer's artist id IS the performer's, which is what makes Crowded
        # House a writer-linked candidate.
        det = _detector(_WriterStub(
            [CROWDED_HOUSE_REC, DENISE_REC], [("a1", "Crowded House")],
        ))
        got = det._original_by_writer(
            {"work-1"}, performer="Crowded House", title_hint="Don't Dream It's Over",
        )
        assert got == {}, (
            "the writer-performer must be recognised as a writer-linked "
            "candidate, or their own song is attributed to a later cover"
        )

    def test_the_album_placeholder_gets_no_conclusive_negative(self) -> None:
        """⭐⭐ THE MECHANISM OF THE REPORTED BUG, pinned as a control.

        With the album artist as the performer, ``performer_is_writer_candidate``
        is False and ``_same_artist(..., "Various Artists")`` is never true — so
        the conclusive negative CANNOT fire, whatever else the method returns.
        That is what turns a self-written song into a cover of a later version.
        """
        det = _detector(_WriterStub(
            [CROWDED_HOUSE_REC, DENISE_REC], [("a1", "Crowded House")],
        ))
        got = det._original_by_writer(
            {"work-1"}, performer=ALBUM_ARTIST, title_hint="Don't Dream It's Over",
        )
        assert got != {}, (
            "the placeholder must never be able to answer 'the performer "
            "originated this' — reaching that verdict is the fixed behaviour"
        )

    def test_the_placeholder_guard_blocks_the_wrote_work_check(self) -> None:
        """``_performer_wrote_work`` refuses a placeholder, so a self-written
        track could never be recognised as the originator."""
        det = _detector(_WriterStub([CROWDED_HOUSE_REC], [("a1", "Crowded House")]))
        assert det._performer_wrote_work("work-1", ALBUM_ARTIST) is False
        assert det._performer_wrote_work("work-1", "Crowded House") is True


# ---------------------------------------------------------------------------
# 3. Step 5 end-to-end — the title-hint step must skip the performer's own song
# ---------------------------------------------------------------------------


class TestStepFiveUsesTheTrackArtist:
    @staticmethod
    def _run(monkeypatch, asked: list[tuple[str, str]]) -> list[dict]:
        det = _detector(_WriterStub([CROWDED_HOUSE_REC, DENISE_REC], []))
        det._collect_track_writers = lambda *a, **k: {}
        monkeypatch.setattr(impl, "apply_cover_metadata_batch", lambda conn, ups: [])

        def _has_original(conn, artist, title):
            asked.append((artist, title))
            return artist == "Crowded House"

        monkeypatch.setattr(impl, "track_has_original_by_artist", _has_original)
        tracks = [{
            "id": "t1",
            "artist": "Crowded House",
            "album_artist": ALBUM_ARTIST,
            "album": ALBUM,
            "title": "Don't Dream It's Over (Denise Jannah Cover)",
        }]
        return det.detect_covers_for_album(ALBUM, ALBUM_ARTIST, tracks, force=True)

    def test_the_performer_own_song_is_asked_about_under_the_track_artist(
        self, monkeypatch
    ) -> None:
        """⭐ THE REPORTED CASE, end to end. The look-up asks about the PERFORMER,
        so the existing "the album already holds the original" guard can fire."""
        asked: list[tuple[str, str]] = []
        self._run(monkeypatch, asked)
        assert asked, "the guard was never consulted"
        assert asked[0][0] == "Crowded House", (
            f"asked about {asked[0][0]!r} instead of the track artist — on a "
            "compilation that is the 'Various Artists' placeholder"
        )

    def test_no_cover_is_recorded_for_the_performers_own_song(self, monkeypatch) -> None:
        asked: list[tuple[str, str]] = []
        results = self._run(monkeypatch, asked)
        assert results == [], (
            "Crowded House's own 'Don't Dream It's Over' was recorded as a cover"
        )

    def test_a_placeholder_row_is_discarded_not_mis_attributed(self, monkeypatch) -> None:
        """CONTROL: a row whose ARTIST column is the album placeholder has no
        performer to measure a verdict against, so the ``_record`` funnel drops
        it rather than attributing the song to whoever the title names."""
        det = _detector(_WriterStub([], []))
        det._collect_track_writers = lambda *a, **k: {}
        monkeypatch.setattr(impl, "apply_cover_metadata_batch", lambda conn, ups: [])
        monkeypatch.setattr(impl, "track_has_original_by_artist", lambda *a, **k: False)
        tracks = [{
            "id": "t1",
            "artist": ALBUM_ARTIST,
            "album_artist": ALBUM_ARTIST,
            "album": ALBUM,
            "title": "Tainted Love (Gloria Jones Cover)",
        }]
        assert det.detect_covers_for_album(ALBUM, ALBUM_ARTIST, tracks, force=True) == [], (
            "a placeholder performer produced a cover verdict"
        )


# ---------------------------------------------------------------------------
# 4. A VA compilation that is not an official release → match artist + title
# ---------------------------------------------------------------------------


COVER_REC = {
    "id": "rec-cover",
    "title": "Tainted Love",
    "artist-credit": [{"artist": {"id": "a-soft", "name": "Soft Cell"}}],
    "first-release-date": "1981-07-01",
    "recording-relation-list": [
        {
            "type": "cover",
            "direction": "forward",
            "recording": {
                "id": "rec-orig",
                "title": "Tainted Love",
                "artist-credit": [
                    {"artist": {"id": "a-gloria", "name": "Gloria Jones"}}
                ],
                "first-release-date": "1965-01-01",
            },
        }
    ],
}


class TestAPlaylistCompilationMatchesByArtistAndTitle:
    """⭐ Reported: a VA compilation that was never an official release must
    still be matched, using the track ARTIST and TITLE.

    Such albums have no release MBID, so the metadata pass logged no
    recording/work identity for any track and step 3's duplicate-search skip
    silenced the whole album. The artist+title pair resolves the recording.
    """

    ALBUM = "Spotify - New Nu Metal"

    def _run(self, monkeypatch, track: dict) -> list[dict]:
        det = _detector(_WriterStub([COVER_REC], []))
        det._collect_track_writers = lambda *a, **k: {}
        monkeypatch.setattr(impl, "apply_cover_metadata_batch", lambda conn, ups: [])
        return det.detect_covers_for_album(
            self.ALBUM, ALBUM_ARTIST, [track], force=True,
        )

    def test_a_va_album_without_a_release_is_recognised(self) -> None:
        det = _detector(_WriterStub([], []))
        assert det._va_compilation_without_release(
            ALBUM_ARTIST,
            [{"id": "t1", "artist": "Soft Cell", "title": "Tainted Love"}],
        ) is True

    def test_a_known_release_keeps_the_duplicate_search_skip(self) -> None:
        det = _detector(_WriterStub([], []))
        assert det._va_compilation_without_release(
            ALBUM_ARTIST,
            [{"id": "t1", "artist": "Soft Cell", "title": "Tainted Love",
              "musicbrainz_album_mbid": "rel-1"}],
        ) is False

    def test_a_non_va_album_keeps_the_duplicate_search_skip(self) -> None:
        det = _detector(_WriterStub([], []))
        assert det._va_compilation_without_release(
            "Bonnie Tyler",
            [{"id": "t1", "artist": "Bonnie Tyler",
              "title": "Holding Out for a Hero"}],
        ) is False

    def test_the_track_is_matched_with_artist_and_title(self, monkeypatch) -> None:
        results = self._run(monkeypatch, {
            "id": "t1", "title": "Tainted Love", "artist": "Soft Cell",
            "album_artist": ALBUM_ARTIST, "album": self.ALBUM,
        })

        assert results, "a VA playlist compilation produced no cover verdict"
        assert results[0]["original_artist"] == "Gloria Jones"

    def test_the_search_query_names_the_track_artist(self, monkeypatch) -> None:
        mb = _WriterStub([COVER_REC], [])
        det = _detector(mb)
        det._collect_track_writers = lambda *a, **k: {}
        monkeypatch.setattr(impl, "apply_cover_metadata_batch", lambda conn, ups: [])
        queries: list[str] = []
        _real_search = mb.search_recordings
        mb.search_recordings = (
            lambda query, limit=15: queries.append(query)
            or _real_search(query, limit=limit)
        )

        det.detect_covers_for_album(
            self.ALBUM, ALBUM_ARTIST,
            [{"id": "t1", "title": "Tainted Love", "artist": "Soft Cell",
              "album_artist": ALBUM_ARTIST, "album": self.ALBUM}],
            force=True,
        )

        assert any(
            'recording:"Tainted Love"' in query and 'artist:"Soft Cell"' in query
            for query in queries
        ), queries


# ---------------------------------------------------------------------------
# 5. Wiring — every step names the performer, none names the album artist
# ---------------------------------------------------------------------------


class TestEveryDetectionStepIsGivenThePerformer:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        import inspect

        return inspect.getsource(impl.CoverDetector.detect_covers_for_album)

    def test_recording_relations_take_the_performer(self, source: str) -> None:
        call = source[source.index("_detect_via_recording_relation("):]
        assert "performer=_performer" in call[:400]
        assert "album_artist=artist" not in call[:400]

    def test_the_title_hint_compares_against_the_performer(self, source: str) -> None:
        step5 = source[source.index("Step 5: heuristic title-based"):]
        step5 = step5[:step5.index("_step_done(\"title hints\"")]
        assert "_performer = comparison_artist(track, artist)" in step5
        assert "track_has_original_by_artist(self.db_conn, artist," not in step5
        assert "_same_artist(mb_orig.get(\"artist\", \"\"), _performer)" in step5

    def test_the_work_fallback_takes_the_performer(self, source: str) -> None:
        step6 = source[source.index("Step 6: work-based fallback"):]
        step6 = step6[:step6.index("_step_done(\"work fallback\"")]
        assert "_performer = comparison_artist(track, artist)" in step6
        assert "album_artist=_performer" in step6
        assert "album_artist=artist" not in step6

    def test_the_writer_step_does_not_fall_back_to_the_album_artist(
        self, source: str
    ) -> None:
        step4 = source[source.index("Step 4: writer-based detection"):]
        step4 = step4[:step4.index("_step_done(\"writer matching\"")]
        assert 'performer = str(info.get("track_artist") or artist or "").strip()' not in step4
        assert "comparison_artist(source, artist)" in step4

    def test_the_relation_pipeline_never_receives_an_album_artist(self) -> None:
        """The parameter was NAMED ``album_artist``, which is how the bug read as
        correct. Its name is the invariant."""
        import inspect

        sig = inspect.signature(impl.CoverDetector._detect_via_recording_relation)
        assert "performer" in sig.parameters
        assert "album_artist" not in sig.parameters
        sig2 = inspect.signature(impl.CoverDetector._check_release_for_cover)
        assert "performer" in sig2.parameters
        assert "album_artist" not in sig2.parameters


# ---------------------------------------------------------------------------
# 6. A row that still carries the album placeholder gets its performer
# ---------------------------------------------------------------------------


class TestAPlaceholderRowIsGivenThePerformerItsFileNames:
    """REPORTED: Ghost's "It's a Sin" came back "not a cover" on a VA
    compilation, although MusicBrainz models it as a cover of the Pet Shop
    Boys' work (a ``performance`` work relation carrying the ``cover``
    attribute).

    The DB row still carried the album placeholder — the track stage only
    replaces it when the metadata scan RESOLVED the track — so
    ``comparison_artist`` was empty and every step silently bailed: the pass
    reported 0 cover(s) with all six steps settling nothing.
    """

    @staticmethod
    def _run(
        monkeypatch, track: dict, *, recording_credit: str = "",
    ) -> tuple[list[dict], dict]:
        det = _detector(_WriterStub([], []))
        det._collect_track_writers = lambda *a, **k: {}
        det._resolve_recording_mbid = lambda *a, **k: "rec-ghost"
        det._get_recording = lambda mbid, inc: {
            "id": mbid,
            "artist-credit": [
                {"artist": {"id": "a-ghost", "name": recording_credit}}
            ],
        }
        seen: dict = {}

        def _detect(**kwargs):
            seen.update(kwargs)
            return {"artist": "Pet Shop Boys", "year": 1987, "confidence": "high"}

        det._detect_via_recording_relation = _detect
        monkeypatch.setattr(
            impl, "apply_cover_metadata_batch",
            lambda conn, ups: [str(u["track_id"]) for u in ups],
        )
        results = det.detect_covers_for_album(
            "Spotify - Metal Covers", ALBUM_ARTIST, [track], force=True,
        )
        return results, seen

    def test_the_files_artist_is_used_as_the_performer(self, monkeypatch) -> None:
        import helpers.metadata_reader as metadata_reader

        monkeypatch.setattr(
            metadata_reader, "read_mp3_metadata",
            lambda path: {"artist": "Ghost", "title": "It's a Sin"},
        )
        track = {
            "id": "t1", "title": "It's a Sin", "artist": ALBUM_ARTIST,
            "album_artist": ALBUM_ARTIST, "album": "Spotify - Metal Covers",
            "file_path": "/music/Spotify - Metal Covers/It's a Sin.mp3",
        }

        results, seen = self._run(monkeypatch, track)

        assert seen.get("performer") == "Ghost", (
            "the pass compared against the placeholder, so the confirmed cover "
            "was discarded by the _record funnel"
        )
        assert results and results[0]["original_artist"] == "Pet Shop Boys"

    def test_the_recordings_credit_is_the_fallback(self, monkeypatch) -> None:
        import helpers.metadata_reader as metadata_reader

        monkeypatch.setattr(metadata_reader, "read_mp3_metadata", lambda path: {})
        track = {
            "id": "t1", "title": "It's a Sin", "artist": ALBUM_ARTIST,
            "album_artist": ALBUM_ARTIST, "album": "Spotify - Metal Covers",
            "file_path": "/music/missing.mp3",
            "recording_mbid": "rec-ghost",
        }

        results, seen = self._run(monkeypatch, track, recording_credit="Ghost")

        assert seen.get("performer") == "Ghost"
        assert results and results[0]["original_artist"] == "Pet Shop Boys"

    def test_a_real_track_artist_never_opens_the_file(self, monkeypatch) -> None:
        """CONTROL: ordinary tracks must not pay a file read."""
        import helpers.metadata_reader as metadata_reader

        opened: list[str] = []
        monkeypatch.setattr(
            metadata_reader, "read_mp3_metadata",
            lambda path: opened.append(path) or {"artist": "Ghost"},
        )
        track = {
            "id": "t1", "title": "It's a Sin", "artist": "Ghost",
            "album_artist": ALBUM_ARTIST, "album": "Spotify - Metal Covers",
            "file_path": "/music/Spotify - Metal Covers/It's a Sin.mp3",
        }

        results, seen = self._run(monkeypatch, track)

        assert opened == [], "an ordinary track paid a file read"
        assert seen.get("performer") == "Ghost"
        assert results
