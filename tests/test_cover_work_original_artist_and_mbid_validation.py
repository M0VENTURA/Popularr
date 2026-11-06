"""Work-based cover attribution and stored-MBID validation.

Three reported defects:

1. **"Swerve" (Papa Roach) was treated as a cover of Dub Sex's same-titled
   1988 song.** The track carried a Rise Against recording MBID
   (``3bcdd7e1-…`` = "Historia calamitatum"); the detector trusted it, and the
   leftover writer/title heuristics matched the shared title instead of the
   recording's own work.
2. **"My Heart Will Go On" was attributed to James Horner.** The work's writers
   are James Horner and Will Jennings, but the original ARTIST is Céline Dion.
   The composer's own score recording was earlier, so "earliest" alone is not
   enough.
3. The track page had no way to CLEAR a wrong cover verdict and lock it.
"""

from __future__ import annotations

from services.enrichment import cover_detection_service as svc
from services.enrichment.cover_detector_impl import CoverDetector


class _StubMB:
    def __init__(
        self,
        recordings=None,
        work_recordings=None,
        work=None,
        release=None,
        search_results=None,
    ):
        self._recordings = recordings or {}
        self._work_recordings = work_recordings or []
        self._work = work or {}
        self._release = release or {}
        self._search_results = search_results or []
        self.recording_calls: list[str] = []
        self.release_calls: list[str] = []

    def get_recording(self, mbid, inc=""):
        self.recording_calls.append(str(mbid))
        return self._recordings.get(str(mbid), {})

    def get_release(self, mbid, inc=None, timeout=30.0):
        self.release_calls.append(str(mbid))
        return self._release

    def browse_work_recordings(self, work_id, inc="", limit=100):
        return list(self._work_recordings)

    def get_work(self, work_id, inc=None):
        return self._work

    def get_artist(self, artist_id, inc=None):
        return {"id": artist_id, "artist-relation-list": []}

    def search_recordings(self, query, limit=15):
        return list(self._search_results)

    def lookup_by_isrc(self, isrc, inc=None):
        return []


def _detector(mb) -> CoverDetector:
    det = CoverDetector(db_connection=None)
    det.mb = mb
    return det


def _recording(rec_id, title, artist, date):
    return {
        "id": rec_id,
        "title": title,
        "artist-credit": [{"artist": {"id": "a-" + rec_id, "name": artist}}],
        "first-release-date": date,
    }


WRONG_MBID = "3bcdd7e1-0d28-440e-a6fb-fd1b13be4d3e"
SWERVE_MBID = "ada3b4ec-4022-4cce-a480-8bccca11b5e0"


class TestAStoredMbidIsValidatedBeforeItIsTrusted:
    def test_a_mismatched_stored_id_is_re_resolved(self) -> None:
        mb = _StubMB(
            recordings={
                WRONG_MBID: _recording(
                    WRONG_MBID, "Historia calamitatum", "Rise Against", "2008-10-02"
                ),
            },
            release={
                "id": "rel-ego",
                "media": [
                    {
                        "tracks": [
                            {
                                "title": "Swerve (Rockzilla remix)",
                                "recording": _recording(
                                    SWERVE_MBID,
                                    "Swerve (Rockzilla remix)",
                                    "Papa Roach",
                                    "2023-01-06",
                                ),
                            }
                        ]
                    }
                ],
            },
        )
        det = _detector(mb)

        got = det._resolve_recording_mbid(
            {
                "id": "t1",
                "title": "Swerve (Rockzilla remix)",
                "artist": "Papa Roach",
                "mbid": WRONG_MBID,
            },
            "Papa Roach",
            "Ego Trip",
            default_release_mbid="rel-ego",
        )

        assert got == SWERVE_MBID
        assert WRONG_MBID in mb.recording_calls, "the stored id must be checked"

    def test_a_matching_stored_id_is_returned_without_a_search(self) -> None:
        mb = _StubMB(
            recordings={
                SWERVE_MBID: _recording(
                    SWERVE_MBID, "Swerve (Rockzilla remix)", "Papa Roach", "2023-01-06"
                )
            },
        )
        det = _detector(mb)

        got = det._resolve_recording_mbid(
            {
                "id": "t1",
                "title": "Swerve (Rockzilla remix)",
                "artist": "Papa Roach",
                "mbid": SWERVE_MBID,
            },
            "Papa Roach",
            "Ego Trip",
            default_release_mbid="rel-ego",
        )

        assert got == SWERVE_MBID
        assert mb.release_calls == []

    def test_a_failed_recording_fetch_does_not_discard_the_id(self) -> None:
        mb = _StubMB()  # get_recording returns {} — a failure/unknown
        det = _detector(mb)

        got = det._resolve_recording_mbid(
            {"id": "t1", "title": "Song", "artist": "A", "mbid": "rec-stored"},
            "A",
            "Album",
        )

        assert got == "rec-stored"


class TestNoWorkMeansNotACover:
    def test_a_recording_without_a_work_relationship_is_a_negative(self) -> None:
        mb = _StubMB(
            recordings={
                "rec-swerve": {
                    "id": "rec-swerve",
                    "title": "Swerve (Rockzilla remix)",
                    "artist-credit": [{"artist": {"id": "a-papa", "name": "Papa Roach"}}],
                }
            }
        )
        det = _detector(mb)

        # Title-only/writer heuristics must not get a chance to turn the
        # same-title Dub Sex song into the original.
        assert det._detect_via_recording_relation(
            "rec-swerve", "Swerve (Rockzilla remix)", "Papa Roach"
        ) == {}


MY_HEART_WORK = "work-my-heart"
HORNER_SCORE = _recording(
    "rec-horner", "My Heart Will Go On", "James Horner", "1997-11-01"
)
CELINE_ORIGINAL = _recording(
    "rec-celine", "My Heart Will Go On", "Céline Dion", "1997-12-01"
)


def _my_heart_mb() -> _StubMB:
    return _StubMB(
        work_recordings=[HORNER_SCORE, CELINE_ORIGINAL],
        work={
            "id": MY_HEART_WORK,
            "title": "My Heart Will Go On",
            "artist-relation-list": [
                {"type": "composer", "artist": {"id": "horner", "name": "James Horner"}},
                {"type": "writer", "artist": {"id": "jennings", "name": "Will Jennings"}},
            ],
        },
    )


class TestTheOriginalArtistIsNotTheComposer:
    def test_the_earliest_performance_beats_the_writers_score(self) -> None:
        det = _detector(_my_heart_mb())

        result = det._detect_via_work_id(
            MY_HEART_WORK, performer="Some Cover Artist", track_year=2020
        )

        assert result is not None
        assert result["artist"] == "Céline Dion"
        assert result["source"] == "musicbrainz_work"
        assert result["work"] == "My Heart Will Go On"

    def test_the_last_resort_writer_path_uses_the_performance(self) -> None:
        det = _detector(_my_heart_mb())

        result = det._writer_from_cover_work(
            {MY_HEART_WORK}, performer="Some Cover Artist"
        )

        assert result is not None
        assert result["artist"] == "Céline Dion"


class TestAnUnflaggedCompilationCoverDoesNotWin:
    """Reported: "My Heart Will Go On (100 gecs Cover)" corrected to Deja Vu.

    MusicBrainz does NOT flag Deja Vu's 1998-03-31 recordings as covers — the
    ``cover`` attribute is user-contributed and frequently missing (verified
    against the live API). The "earliest non-cover" probe therefore treated a
    Euro-house cover that only appears on a Various Artists compilation as
    authoritative and vetoed the title search that finds Céline Dion. A
    candidate is only authoritative when it is corroborated by a commercial
    release of its own; otherwise the canonical/popular ranking must win.
    """

    def test_the_canonical_original_beats_a_compilation_only_cover(
        self, monkeypatch
    ) -> None:
        import services.popularity.popularity_sources as popularity

        monkeypatch.setattr(
            popularity,
            "get_lastfm_artist_max_listeners",
            lambda name, *a, **k: 0,
        )

        def _visual(rec_id, artist, date, releases):
            return {
                "id": rec_id,
                "title": "My Heart Will Go On",
                "artist-credit": [
                    {"artist": {"id": "a-" + rec_id, "name": artist}}
                ],
                "first-release-date": date,
                "work-relation-list": [
                    {"type": "performance", "work": {"id": MY_HEART_WORK}}
                ],
                "releases": releases,
            }

        va_only = [{"artist-credit": [{"artist": {"name": "Various Artists"}}]}]
        dejavu = _visual("rec-dejavu", "Deja Vu", "1998-03-31", va_only)
        # Céline's early recording is compilation-only in this payload, so only
        # the CANONICAL-release signal (she is on many releases) can pick her.
        celine = _visual(
            "rec-celine",
            "Céline Dion",
            "1998",
            [
                {"artist-credit": [{"artist": {"name": "Various Artists"}}]}
                for _ in range(6)
            ],
        )
        horner = _visual("rec-horner", "James Horner", "2004-01-01", va_only)
        filler = [
            _visual(f"f{i}", f"Filler {i}", "2010-01-01", va_only)
            for i in range(97)
        ]
        page_one = [dejavu, celine, horner] + filler
        assert len(page_one) == 100

        class _MB:
            def browse_work_recordings(
                self, work_id, inc="", limit=100, offset=0
            ):
                return list(page_one) if offset == 0 else []

            def get_work(self, work_id, inc=None):
                return {
                    "id": work_id,
                    "title": "My Heart Will Go On",
                    "artist-relation-list": [
                        {
                            "type": "composer",
                            "artist": {"id": "a-h", "name": "James Horner"},
                        },
                        {
                            "type": "writer",
                            "artist": {"id": "a-j", "name": "Will Jennings"},
                        },
                    ],
                }

            def get_artist(self, artist_id, inc=None):
                return {"id": artist_id, "artist-relation-list": []}

            def get_recording(self, mbid, inc=""):
                return {
                    "rec-dejavu": dejavu,
                    "rec-celine": celine,
                    "rec-horner": horner,
                }.get(mbid, {})

            def search_recordings(self, query, limit=15):
                return [{"id": "rec-celine"}]

        det = _detector(_MB())
        result = det._detect_via_work_id(
            MY_HEART_WORK, performer="DragonForce", track_year=2019
        )

        assert result is not None
        assert result["artist"] == "Céline Dion", (
            "a compilation-only 1998 cover must not veto the canonical original"
        )

    def test_a_title_search_soundtrack_candidate_does_not_block_popularity(
        self, monkeypatch
    ) -> None:
        """MusicBrainz's top-10 title search does not surface Céline.

        It returns Deja Vu, Davy Spillane (Titanic soundtrack, artist-owned
        album) and other covers. The title search may therefore pick Davy, but
        his artist-owned release must NOT veto the work-wide popularity
        fallback — the work's own recording list DOES contain Céline, and she
        is the hit's original performer.
        """
        import services.popularity.popularity_sources as popularity

        monkeypatch.setattr(
            popularity,
            "get_lastfm_artist_max_listeners",
            lambda name, *a, **k: (
                5_000_000 if "Céline" in str(name) else 1000
            ),
        )

        def _visual(rec_id, artist, date):
            return {
                "id": rec_id,
                "title": "My Heart Will Go On",
                "artist-credit": [
                    {"artist": {"id": "a-" + rec_id, "name": artist}}
                ],
                "first-release-date": date,
                "work-relation-list": [
                    {"type": "performance", "work": {"id": MY_HEART_WORK}}
                ],
                "releases": [
                    {"artist-credit": [{"artist": {"name": "Various Artists"}}]}
                ],
            }

        dejavu = _visual("rec-dejavu", "Deja Vu", "1998-03-31")
        celine = _visual("rec-celine", "Céline Dion", "1999")
        filler = [
            _visual(f"f{i}", f"Filler {i}", "2010-01-01") for i in range(98)
        ]
        page_one = [dejavu, celine] + filler

        class _MB:
            def browse_work_recordings(
                self, work_id, inc="", limit=100, offset=0
            ):
                return list(page_one) if offset == 0 else []

            def get_work(self, work_id, inc=None):
                return {
                    "id": work_id,
                    "title": "My Heart Will Go On",
                    "artist-relation-list": [
                        {
                            "type": "composer",
                            "artist": {"id": "a-h", "name": "James Horner"},
                        },
                        {
                            "type": "writer",
                            "artist": {"id": "a-j", "name": "Will Jennings"},
                        },
                    ],
                }

            def get_artist(self, artist_id, inc=None):
                return {"id": artist_id, "artist-relation-list": []}

            def get_recording(self, mbid, inc=""):
                return {
                    "rec-dejavu": dejavu,
                    "rec-celine": celine,
                }.get(mbid, {})

            def search_recordings(self, query, limit=15):
                return []

        det = _detector(_MB())
        # The real title search picks an artist-owned soundtrack performer
        # because Céline is absent from MusicBrainz's top results.
        det._find_original_recording = lambda *a, **k: {
            "artist": "Davy Spillane",
            "year": 1998,
            "confidence": "medium",
            "own_release": True,
        }

        result = det._detect_via_work_id(
            MY_HEART_WORK, performer="DragonForce", track_year=2019
        )

        assert result is not None
        assert result["artist"] == "Céline Dion", (
            "a title-search soundtrack candidate must not suppress the "
            "work-wide popularity fallback"
        )


class _Row:
    def __init__(self, mapping):
        self._mapping = mapping


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Session:
    def __init__(self, row):
        self._row = row
        self.executed: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=None):
        self.executed.append((str(statement), dict(params or {})))
        return _Result(self._row)

    def commit(self):
        pass


class TestClearCoverAndLock:
    def test_the_verdict_suffix_and_file_tags_are_cleared(self, monkeypatch) -> None:
        session = _Session(
            _Row(
                {
                    "title": "My Heart Will Go On (James Horner Cover)",
                    "file_path": "/music/celine.mp3",
                }
            )
        )
        monkeypatch.setattr(svc, "db_session", lambda: session)

        written: dict = {}

        def _write(path, tags):
            written["path"] = path
            written["tags"] = tags
            return True

        monkeypatch.setattr(
            "services.metadata.tag_file_service.write_tags_to_file", _write
        )

        result = svc.clear_track_cover_verdict("track-1", lock=True)

        assert result == {
            "track_id": "track-1",
            "is_cover": False,
            "locked": True,
            "title": "My Heart Will Go On",
        }
        sql, params = session.executed[-1]
        assert "is_cover = 0" in sql
        assert "original_cover_artist = NULL" in sql
        assert params["lock"] is True
        assert params["title"] == "My Heart Will Go On"
        assert written["path"] == "/music/celine.mp3"
        assert written["tags"]["is_cover"] == ""
        assert written["tags"]["original_cover_artist"] == ""
        assert written["tags"]["title"] == "My Heart Will Go On"

    def test_an_unknown_track_returns_none(self, monkeypatch) -> None:
        session = _Session(None)
        monkeypatch.setattr(svc, "db_session", lambda: session)

        assert svc.clear_track_cover_verdict("missing", lock=True) is None


# ---------------------------------------------------------------------------
# 4. A stored WORK must name the same song (the reported "Santana" loop)
# ---------------------------------------------------------------------------


class TestAStoredWorkMustNameTheSameSong:
    """Reported: "This was happening via the track page run, it was still
    selecting Santana."

    The track page runs ``detect_covers_for_track`` with ``force=True``, which
    skips the title-hint step — so "Santana" was coming from a REAL-evidence
    step that trusted a stored work MBID for a DIFFERENT song. ``_titles_match``
    accepts prefixes, and "Smooth" is a prefix of "Smooth Criminal", so Santana's
    "Smooth" work answered for "Smooth Criminal".
    """

    def test_a_prefix_is_not_the_same_song(self):
        from services.enrichment.cover_detector_impl import _titles_match_exact

        assert _titles_match_exact("Smooth Criminal", "Smooth") is False
        assert _titles_match_exact(
            "Smooth Criminal", "Smooth Criminal (Radio Edit)"
        ) is True

    def test_a_work_for_a_different_song_is_rejected(self):
        det = _detector(_StubMB(work={"title": "Smooth"}))
        assert det._work_title_matches_track("work-smooth", "Smooth Criminal") is False

    def test_a_matching_work_title_is_accepted(self):
        det = _detector(_StubMB(work={"title": "Smooth Criminal"}))
        assert det._work_title_matches_track("work-sc", "Smooth Criminal") is True

    def test_an_explicit_cover_relation_beats_a_later_cover_in_the_work(self):
        """The reported "Smooth Criminal (Santana Cover)".

        Michael Jackson wrote AND sang the song, so his earliest recording is
        ranked as a WRITER recording and skipped; the earliest NON-writer
        recording of the work (Santana's later cover) then won and was returned
        as the original — even though the writer lookup itself found Michael
        Jackson. An explicit forward ``cover`` recording relation (Alien Ant
        Farm → Michael Jackson) is authoritative and must win.
        """
        seed = {
            "id": "rec-aaf",
            "title": "Smooth Criminal",
            "artist-credit": [{"artist": {"id": "a-aaf", "name": "Alien Ant Farm"}}],
            "first-release-date": "2001-01-01",
            "recording-relation-list": [{
                "type": "cover",
                "direction": "forward",
                "recording": {
                    "id": "rec-mj",
                    "title": "Smooth Criminal",
                    "artist-credit": [
                        {"artist": {"id": "a-mj", "name": "Michael Jackson"}}
                    ],
                    "first-release-date": "1987-08-25",
                },
            }],
        }
        mb = _StubMB(
            recordings={"rec-aaf": seed},
            work={
                "title": "Smooth Criminal",
                "artist-relation-list": [
                    {"type": "writer", "artist": {"id": "a-mj", "name": "Michael Jackson"}},
                ],
            },
            work_recordings=[
                _recording("rec-mj", "Smooth Criminal", "Michael Jackson", "1987-08-25"),
                _recording("rec-santana", "Smooth Criminal", "Santana", "2010-09-21"),
            ],
        )
        det = _detector(mb)

        result = det._detect_via_work_id(
            "work-sc", performer="Alien Ant Farm", exclude_mbid="rec-aaf"
        )

        assert result is not None
        assert result["artist"] == "Michael Jackson", (
            "the explicit cover relation must win; without it the "
            "writer-performer original is skipped and the later Santana cover "
            "wins"
        )

    def test_a_writer_performer_original_beats_a_far_later_cover(self):
        """The same case with NO explicit cover relation in MusicBrainz.

        MJ's 1987 recording is a "writer recording" and is skipped, so the
        earliest non-writer recording (Santana, 2010) became the performed
        original. A writer-performer whose recording predates the earliest
        other recording by years IS the original; the composer case ("My Heart
        Will Go On") is contemporaneous and keeps the performer.
        """
        mb = _StubMB(
            work={
                "title": "Smooth Criminal",
                "artist-relation-list": [
                    {"type": "writer", "artist": {"id": "a-mj", "name": "Michael Jackson"}},
                ],
            },
            work_recordings=[
                _recording("mj", "Smooth Criminal", "Michael Jackson", "1987-08-25"),
                _recording("santana", "Smooth Criminal", "Santana", "2010-09-21"),
            ],
        )
        det = _detector(mb)

        result = det._detect_via_work_id(
            "work-sc", performer="Alien Ant Farm", exclude_mbid="rec-aaf"
        )

        assert result is not None
        assert result["artist"] == "Michael Jackson"

    def test_a_huge_work_scans_past_page_one_to_find_the_performed_original(self):
        """The reported "My Heart Will Go On (Deja Vu Cover)".

        MusicBrainz has 320 recordings of this work. Page 1 contains Céline
        Dion's UNDATED compilation recording (skipped) and a dated 1998
        Euro-house cover by "Deja Vu" — so Deja Vu won. Céline's dated recording
        is on a later page. A FULL first page must trigger the extra-page scan.
        """
        filler = [
            _recording(f"f{i}", "My Heart Will Go On", f"Filler {i}", "2010-01-01")
            for i in range(98)
        ]
        page_one = filler + [
            _recording("dejavu", "My Heart Will Go On", "Deja Vu", "1998-03-31"),
            _recording("horner", "My Heart Will Go On", "James Horner", "2004-01-01"),
        ]
        # An EARLIER karaoke/compilation cover on page 2 (the "WorldStar Music"
        # false positive), versus Céline's own-release recording.
        worldstar = _recording(
            "worldstar", "My Heart Will Go On", "WorldStar Music", "1995-01-01"
        )
        worldstar["releases"] = [{
            "artist-credit": [{"artist": {"id": "a-various", "name": "Various Artists"}}],
        }]
        celine = _recording("celine", "My Heart Will Go On", "Céline Dion", "1996-01-01")
        celine["releases"] = [{
            "title": "Falling into You",
            "artist-credit": [{"artist": {"id": "a-celine", "name": "Céline Dion"}}],
        }]
        page_two = [worldstar, celine]

        class _PagedMB:
            def browse_work_recordings(self, work_id, inc="", limit=100, offset=0):
                return list(page_one if offset == 0 else page_two if offset == 100 else [])

            def get_work(self, work_id, inc=None):
                return {
                    "title": "My Heart Will Go On",
                    "artist-relation-list": [
                        {"type": "writer", "artist": {"id": "a-horner", "name": "James Horner"}},
                        {"type": "writer", "artist": {"id": "a-jennings", "name": "Will Jennings"}},
                    ],
                }

            def get_artist(self, artist_id, inc=None):
                return {"id": artist_id, "artist-relation-list": []}

            def get_recording(self, mbid, inc=""):
                return {}

            def search_recordings(self, query, limit=15):
                return []

        det = _detector(_PagedMB())

        result = det._detect_via_work_id(
            "work-my-heart", performer="DragonForce", exclude_mbid=""
        )

        assert result is not None
        assert result["artist"] == "Céline Dion", (
            "page 1 yields a 1998 cover; a full first page must scan further for "
            "Céline Dion's dated original"
        )

    def test_the_title_search_prefers_the_artists_own_release(self, monkeypatch):
        """The page-2 browse falls back to `artist-credits` (no releases), so
        "own release" cannot be computed there — "WorldStar Music" won.

        The title-search path fetches each candidate WITH releases, so it can
        tell the artist's own single/album from a karaoke/compilation cover.
        The final tie-break is artist POPULARITY: the original of a hit is the
        most-listened candidate (Céline Dion), not Davy Spillane / Jeff Leyton.
        """
        import services.popularity.popularity_sources as popularity

        monkeypatch.setattr(
            popularity,
            "get_lastfm_artist_max_listeners",
            lambda name, *a, **k: 5_000_000 if "Céline" in str(name) else 1000,
        )

        def _own_release(title: str, artist: str) -> dict:
            return {
                "title": title,
                "artist-credit": [{"artist": {"name": artist}}],
            }

        details = {
            "d-jeff": {
                "id": "d-jeff",
                "title": "My Heart Will Go On",
                "artist-credit": [{"artist": {"name": "Jeff Leyton"}}],
                # EARLIER and own-release — the reported "Jeff Leyton" cover.
                "first-release-date": "1994-01-01",
                "releases": [_own_release("Songs of the Musicals", "Jeff Leyton")],
            },
            "d-celine": {
                "id": "d-celine",
                "title": "My Heart Will Go On",
                "artist-credit": [{"artist": {"name": "Céline Dion"}}],
                "first-release-date": "1996-01-01",
                # The canonical hit: on many releases.
                "releases": [
                    _own_release(f"Release {i}", "Céline Dion") for i in range(6)
                ],
                "work-relation-list": [{
                    "work": {
                        "artist-relation-list": [
                            {"type": "writer", "artist": {"name": "James Horner"}},
                        ],
                    },
                }],
            },
        }

        class _SearchMB:
            def search_recordings(self, query, limit=15):
                return [{"id": "d-jeff"}, {"id": "d-celine"}]

            def get_recording(self, mbid, inc=""):
                return details.get(mbid, {})

            def get_artist(self, artist_id, inc=None):
                return {"id": artist_id, "artist-relation-list": []}

        det = _detector(_SearchMB())

        result = det._find_original_recording(
            "My Heart Will Go On",
            "James Horner",
            album_artist="DragonForce",
            exclude_artists=["James Horner", "Will Jennings"],
        )

        assert result is not None
        assert result["artist"] == "Céline Dion", (
            "the canonical recording (on the most releases) must win over an "
            "earlier single-release cover like Jeff Leyton; dates and "
            "own-release alone kept picking those covers"
        )

    def test_a_single_release_beats_a_more_popular_soundtrack_performer(
        self, monkeypatch
    ):
        """Davy Spillane (Titanic soundtrack) has NO single release.

        MusicBrainz shows his "My Heart Will Go On" only on "The Sea of Dreams"
        (Album / Compilation). Céline Dion's is on the 1997 **Single**. "Most
        covers are based on a song that had a single release", so the artist's
        own Single must win — even if a soundtrack performer is more popular.
        """
        import services.popularity.popularity_sources as popularity

        monkeypatch.setattr(
            popularity,
            "get_lastfm_artist_max_listeners",
            lambda name, *a, **k: 2_000_000 if "Davy" in str(name) else 1000,
        )

        details = {
            "d-davy": {
                "id": "d-davy",
                "title": "My Heart Will Go On",
                "artist-credit": [{"artist": {"name": "Davy Spillane"}}],
                "first-release-date": "1998-12-01",
                "releases": [{
                    "artist-credit": [{"artist": {"name": "Davy Spillane"}}],
                    "release-group": {"primary-type": "Album"},
                }],
            },
            "d-celine": {
                "id": "d-celine",
                "title": "My Heart Will Go On",
                "artist-credit": [{"artist": {"name": "Céline Dion"}}],
                "first-release-date": "1997-11-24",
                "releases": [{
                    "artist-credit": [{"artist": {"name": "Céline Dion"}}],
                    "release-group": {"primary-type": "Single"},
                }],
                "work-relation-list": [{
                    "work": {
                        "artist-relation-list": [
                            {"type": "writer", "artist": {"name": "James Horner"}},
                        ],
                    },
                }],
            },
        }

        class _SearchMB:
            def search_recordings(self, query, limit=15):
                return [{"id": "d-davy"}, {"id": "d-celine"}]

            def get_recording(self, mbid, inc=""):
                return details.get(mbid, {})

            def get_artist(self, artist_id, inc=None):
                return {"id": artist_id, "artist-relation-list": []}

        det = _detector(_SearchMB())

        result = det._find_original_recording(
            "My Heart Will Go On",
            "James Horner",
            album_artist="DragonForce",
            exclude_artists=["James Horner", "Will Jennings"],
        )

        assert result is not None
        assert result["artist"] == "Céline Dion", (
            "the song's own Single release must beat a more-popular soundtrack "
            "performer that never had a single"
        )

    def test_the_forced_pass_does_not_query_a_work_for_a_different_song(self):
        det = _detector(_StubMB(
            work={"title": "Smooth"},
            work_recordings=[
                _recording("rec-smooth", "Smooth", "Santana", "1999-06-15")
            ],
        ))
        det._load_cover_checked_map = lambda ids: {}
        det._collect_track_writers = lambda *a, **k: {}
        det._persist_checked = lambda *a, **k: None
        det._clear_unconfirmed_verdicts = lambda *a, **k: None
        det._is_va_compilation = lambda *a, **k: False

        queried: list[dict] = []
        det._detect_via_work_id = lambda **kwargs: queried.append(kwargs) or None

        det.detect_covers_for_album(
            "20th Century Masters", "Alien Ant Farm",
            [{
                "id": "t1",
                "title": "Smooth Criminal (Santana Cover)",
                "artist": "Alien Ant Farm",
                "work_mbid": "work-smooth",
                "is_cover": 1,
                "original_cover_artist": "Santana",
            }],
            force=True,
        )

        assert queried == [], (
            "a work titled 'Smooth' must not answer for 'Smooth Criminal'"
        )
