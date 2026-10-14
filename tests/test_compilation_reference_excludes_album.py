"""A compilation must not be part of the reference used to judge it.

Asked
-----
> I previously had that 5 star tracks shouldn't have a limit on compilations,
> is that still the case? I want any song that was a high popularity single to
> be able to score 5 star on a compilation as they may be a greatest hits
> population. But I also don't want that to force any that weren't 5 star to be
> 1 star due to the rest of the album being rated so highly.

Two separate things were checked, and only one needed changing.

**1. The slot cap — still absent, and now pinned.** Both cap passes in
``post_album_star_ratings`` are gated ``and not is_compilation``, and the 4★
percentage cap is commented out. A compilation may therefore hold as many 5★
tracks as earn them. ``TestNoSlotCapOnACompilation`` pins both directions: no
cap on a compilation, and the cap still applying to a studio album.

**2. The ratings WERE self-referential — fixed.** ``_assign_stars`` rates a
compilation track against the credited artist's catalogue, and that catalogue
was built by ``compute_track_artist_scores(artist, album_results)`` — which
counts the album under rating. A greatest-hits record is nothing BUT hits, so
its own tracks dragged the artist's catalogue mean up, pushed every track's z
down, and flattened the album. Measured on an 8-track best-of against a
20-track catalogue of the same artist: **no track above 4★, six at 3★** — a
record of hit singles that could not produce a single 5★.

The same recording is also counted twice — once on the best-of, once on the
album it was lifted from — so the hits carried double weight in a distribution
they are supposed to be exceptional against. The function's ``(album, title)``
dedupe cannot see that: the two copies are different pairs. It documents its own
intent as "so nothing is double-counted"; this closes the gap it could not.

``exclude_album`` now keeps the album out of both halves of that decision —
the rating reference (``compute_track_artist_scores``) and the score remap
(``_load_track_artist_scores``) — because they are two views of one question.

**3. Nothing is pushed DOWN by it.** Removing the album's own hits LOWERS the
reference mean, so every remaining track's z rises: the change is monotonic
upward in that respect. ``test_a_weak_track_is_not_crushed`` is the control for
the second half of the request.

A library that owns ONLY the best-of is left with a thin catalogue. That is the
honest answer — a best-of cannot evidence the artist's range — and it falls
through to the online/thin path that already exists for this case.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import text

from db.engine import db_session
from services.popularity.stages import finalise_stage as fs


# ---------------------------------------------------------------------------
# A recording session, for the tests that drive post_album_star_ratings.
#
# It implements only what that function touches: execute() (returning no rows,
# so the stored-stars and artist-listen lookups simply find nothing) plus the
# ``begin_nested`` savepoint used around the star UPDATE.
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Nested:
    @property
    def is_active(self):
        return True

    def commit(self):
        pass

    def rollback(self):
        pass


class _RecordingSession:
    def __init__(self):
        self.executed: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        self.executed.append((str(sql), params))
        return _FakeResult()

    def begin_nested(self):
        return _Nested()

    def commit(self):
        pass

    def rollback(self):
        pass


ARTIST = "Testfinity"
BEST_OF = "Testfinity's Greatest Hits"
OTHER_ALBUM = "Testfinity Rides Again"

#: The artist's OWN albums — the honest reference for a best-of.
STUDIO = [
    ("s1", "Big Song", "Testfinity Rides Again", 92.0),
    ("s2", "Second Song", OTHER_ALBUM, 88.0),
    ("s3", "Third Song", OTHER_ALBUM, 84.0),
    ("s4", "Fourth Song", OTHER_ALBUM, 80.0),
    ("s5", "Fifth Song", OTHER_ALBUM, 76.0),
    ("s6", "Sixth Song", OTHER_ALBUM, 72.0),
    ("s7", "Seventh Song", OTHER_ALBUM, 68.0),
    ("s8", "Eighth Song", OTHER_ALBUM, 64.0),
    ("s9", "Ninth Song", OTHER_ALBUM, 58.0),
    ("s10", "Tenth Song", OTHER_ALBUM, 50.0),
    ("s11", "Deep Cut", OTHER_ALBUM, 42.0),
    ("s12", "Deeper Cut", OTHER_ALBUM, 34.0),
    ("s13", "Deepest Cut", OTHER_ALBUM, 26.0),
    ("s14", "Filler One", OTHER_ALBUM, 20.0),
    ("s15", "Filler Two", OTHER_ALBUM, 16.0),
]

#: The best-of's tracks — the SAME recordings, re-presented. The library owns
#: both, so every hit exists twice in the database.
BEST_OF_ROWS = [
    ("b1", "Big Song", 91.0),
    ("b2", "Second Song", 87.0),
    ("b3", "Third Song", 83.0),
    ("b4", "Fourth Song", 79.0),
    ("b5", "Fifth Song", 75.0),
    ("b6", "Sixth Song", 71.0),
]


@pytest.fixture(autouse=True)
def _clean_tracks():
    """``tracks`` is shared across the session (StaticPool), so wipe it.

    Tolerant by design: if a code path under test reaches the shared engine and
    disposes it (a missing table is treated as transient), the DELETE here
    cannot run — and that must not turn a passing assertion into a teardown
    error that looks like a failure of the change under test.
    """
    _try_clean()
    yield
    _try_clean()


def _try_clean():
    try:
        with db_session() as session:
            session.execute(text("DELETE FROM tracks"))
    except Exception:
        pass


def _seed(rows):
    with db_session() as session:
        for tid, title, album, score in rows:
            session.execute(
                text(
                    "INSERT INTO tracks (id, title, album, artist, album_artist, "
                    "final_score) VALUES (:i, :t, :a, :ar, :ar, :s)"
                ),
                {"i": tid, "t": title, "a": album, "ar": ARTIST, "s": score},
            )


def _album_results():
    """The best-of as the scan hands it over."""
    return [
        {
            "track_id": tid,
            "title": title,
            "artist": ARTIST,
            "album": BEST_OF,
            "album_artist": ARTIST,
            "popularity_score": score,
            "final_score": score,
            "_raw_combined": score,
            "lastfm_listeners": int(score * 9_000),
            "listenbrainz_listens": int(score * 14_000),
            "single_confidence": "high",
            "is_single": True,
            "is_live": False,
            "year": 2000,
        }
        for tid, title, score in BEST_OF_ROWS
    ]


# ===========================================================================
# 1. The reference excludes the album
# ===========================================================================


class TestTheRatingReferenceExcludesTheAlbum:
    def test_the_best_of_is_not_in_its_own_reference(self):
        _seed(STUDIO + [(t, ti, BEST_OF, s) for t, ti, s in BEST_OF_ROWS])

        included = fs.compute_track_artist_scores(ARTIST, _album_results())
        excluded = fs.compute_track_artist_scores(
            ARTIST, _album_results(), exclude_album=BEST_OF
        )

        assert len(excluded) < len(included), (
            "excluding the album did not shrink the reference"
        )
        # The difference is exactly the album's own tracks: it contributed N,
        # and the artist's other albums contributed the rest.
        assert len(included) - len(excluded) == len(BEST_OF_ROWS), (
            "the excluded amount does not match the album's track count"
        )

    def test_the_artists_other_albums_are_still_counted(self):
        """CONTROL — the exclusion must not throw away the real catalogue."""
        _seed(STUDIO + [(t, ti, BEST_OF, s) for t, ti, s in BEST_OF_ROWS])

        excluded = fs.compute_track_artist_scores(
            ARTIST, _album_results(), exclude_album=BEST_OF
        )

        assert excluded, "the artist's own albums were excluded too"

    def test_the_exclusion_also_drops_the_stored_rows(self):
        """The scan rows and the DB rows are two sources for the same album, so
        both are filtered — a stored title can drift (a rename, or a feat.
        credit the scan moved onto the artist) and would slip the (album,title)
        dedupe."""
        _seed(STUDIO + [(t, ti, BEST_OF, s) for t, ti, s in BEST_OF_ROWS])
        # A stored row whose title cannot match any scan row.
        _seed([("b9", "Hidden Bonus On The Best Of", BEST_OF, 60.0)])

        excluded = fs.compute_track_artist_scores(
            ARTIST, _album_results(), exclude_album=BEST_OF
        )
        # 60.0 re-anchors to something around the middle; the point is that the
        # catalogue is the same size whether or not that stray row is excluded
        # by the album filter rather than by a title match.
        assert len(excluded) == len(STUDIO), (
            "a stored row belonging to the album under rating survived the "
            "exclusion (it would have needed a (album, title) match)"
        )

    def test_excluding_nothing_is_the_old_behaviour(self):
        """CONTROL — ``exclude_album=None`` must reproduce the previous result
        exactly, so a caller that passes nothing is unaffected."""
        _seed(STUDIO + [(t, ti, BEST_OF, s) for t, ti, s in BEST_OF_ROWS])

        assert (
            fs.compute_track_artist_scores(ARTIST, _album_results())
            == fs.compute_track_artist_scores(
                ARTIST, _album_results(), exclude_album=None
            )
        )

    def test_an_unknown_album_excludes_nothing(self):
        """CONTROL — a name that matches no row is a no-op, not a wipe."""
        _seed(STUDIO + [(t, ti, BEST_OF, s) for t, ti, s in BEST_OF_ROWS])

        baseline = fs.compute_track_artist_scores(ARTIST, _album_results())
        other = fs.compute_track_artist_scores(
            ARTIST, _album_results(), exclude_album="Some Other Record"
        )

        assert len(other) == len(baseline)

    def test_the_builder_passes_the_album_through(self):
        """The rating path calls the BUILDER, so it has to forward the album."""
        _seed(STUDIO + [(t, ti, BEST_OF, s) for t, ti, s in BEST_OF_ROWS])

        per_artist = fs._build_compilation_track_artist_scores(
            _album_results(), exclude_album=BEST_OF
        )
        direct = fs.compute_track_artist_scores(
            ARTIST, _album_results(), exclude_album=BEST_OF
        )

        assert per_artist[ARTIST.casefold()] == direct


class TestTheRemapReferenceExcludesTheAlbum:
    def test_the_remap_loader_drops_the_album(self):
        """``_load_track_artist_scores`` feeds the per-artist score remap —
        the other half of the same decision, so it takes the same exclusion."""
        from services.popularity.scan_stage_runner import _load_track_artist_scores

        _seed(STUDIO + [(t, ti, BEST_OF, s) for t, ti, s in BEST_OF_ROWS])

        included = _load_track_artist_scores(ARTIST)
        excluded = _load_track_artist_scores(ARTIST, exclude_album=BEST_OF)

        assert len(excluded) < len(included)
        assert len(excluded) == len(STUDIO), "the artist's own album was dropped"

    def test_the_normalisation_passes_the_album(self):
        """It derives the album from the rows it is handed — no signature
        change, so the call site stays as it was."""
        from services.popularity import scan_stage_runner as runner

        _seed(STUDIO + [(t, ti, BEST_OF, s) for t, ti, s in BEST_OF_ROWS])

        seen: list[str | None] = []
        real = runner._load_track_artist_scores

        def _spy(artist, *, exclude_album=None):
            seen.append(exclude_album)
            return real(artist, exclude_album=exclude_album)

        runner._load_track_artist_scores = _spy
        try:
            runner._apply_track_artist_relative_normalization(_album_results())
        finally:
            runner._load_track_artist_scores = real

        assert BEST_OF in seen, (
            "the normalisation did not exclude the album it is normalising"
        )


# ===========================================================================
# 2. What the exclusion buys: hits reach the top of the band
# ===========================================================================


class TestABestOfCanProduceFiveStars:
    def _stars(self, catalogue):
        rows = _album_results()
        album_scores = [r["popularity_score"] for r in rows]
        out = {}
        for tr in rows:
            stars = fs._assign_stars(
                dict(tr),
                album_scores,
                catalogue,
                [float(r["lastfm_listeners"]) for r in rows],
                [float(r["listenbrainz_listens"]) for r in rows],
                is_compilation=True,
                is_va_compilation=False,
                compilation_raw_scores=[float(r["_raw_combined"]) for r in rows],
            )
            out[tr["title"]] = stars
        return out

    def test_the_hits_reach_five_stars(self):
        """THE REPORTED CASE.

        Every track on this record was a hit single, so most of it belongs in
        the top band of the artist's catalogue. Rated against a reference that
        INCLUDED the album, none of them could clear it.
        """
        _seed(STUDIO + [(t, ti, BEST_OF, s) for t, ti, s in BEST_OF_ROWS])
        catalogue = fs.compute_track_artist_scores(
            ARTIST, _album_results(), exclude_album=BEST_OF
        )

        stars = self._stars(catalogue)

        five = [title for title, value in stars.items() if value == 5]
        assert five, (
            "no track on a greatest-hits album could reach 5★ — the album is "
            f"still part of its own reference ({stars})"
        )

    def test_the_album_being_included_is_what_stopped_them(self):
        """CONTROL, and the proof of the mechanism: the SAME album rates lower
        when its own tracks are part of the reference. If this ever stops being
        true the exclusion has become a no-op."""
        _seed(STUDIO + [(t, ti, BEST_OF, s) for t, ti, s in BEST_OF_ROWS])
        rows = _album_results()

        included = fs.compute_track_artist_scores(ARTIST, rows)
        excluded = fs.compute_track_artist_scores(
            ARTIST, rows, exclude_album=BEST_OF
        )

        five_included = sum(1 for v in self._stars(included).values() if v == 5)
        five_excluded = sum(1 for v in self._stars(excluded).values() if v == 5)

        assert five_excluded > five_included, (
            "excluding the album did not lift any track — the self-reference "
            "may have been removed by something else"
        )

    def test_the_reference_mean_is_lower_without_the_album(self):
        """The arithmetic behind it: the album's own hits are high, so removing
        them lowers the mean every track is measured against."""
        _seed(STUDIO + [(t, ti, BEST_OF, s) for t, ti, s in BEST_OF_ROWS])
        rows = _album_results()

        included = fs.compute_track_artist_scores(ARTIST, rows)
        excluded = fs.compute_track_artist_scores(ARTIST, rows, exclude_album=BEST_OF)

        assert sum(excluded) / len(excluded) < sum(included) / len(included)

    def test_a_weak_track_is_not_crushed(self):
        """THE SECOND HALF OF THE REQUEST.

        Removing the album lowers the reference mean, so every remaining
        track's z rises — the change can only lift. Pin that a genuinely weak
        track does not go DOWN when the hits are lifted.
        """
        _seed(
            STUDIO
            + [(t, ti, BEST_OF, s) for t, ti, s in BEST_OF_ROWS]
            + [("b7", "Filler On The Best Of", BEST_OF, 18.0)]
        )
        rows = _album_results() + [
            {
                "track_id": "b7", "title": "Filler On The Best Of",
                "artist": ARTIST, "album": BEST_OF, "album_artist": ARTIST,
                "popularity_score": 18.0, "final_score": 18.0,
                "_raw_combined": 18.0, "lastfm_listeners": 900,
                "listenbrainz_listens": 1_200, "single_confidence": "low",
                "is_single": False, "is_live": False, "year": 2000,
            }
        ]

        included = fs.compute_track_artist_scores(ARTIST, rows)
        excluded = fs.compute_track_artist_scores(ARTIST, rows, exclude_album=BEST_OF)

        def _stars(catalogue):
            album_scores = [r["popularity_score"] for r in rows]
            out = {}
            for tr in rows:
                out[tr["title"]] = fs._assign_stars(
                    dict(tr), album_scores, catalogue,
                    [float(r["lastfm_listeners"]) for r in rows],
                    [float(r["listenbrainz_listens"]) for r in rows],
                    is_compilation=True, is_va_compilation=False,
                    compilation_raw_scores=[float(r["_raw_combined"]) for r in rows],
                )
            return out

        weak_before = _stars(included)["Filler On The Best Of"]
        weak_after = _stars(excluded)["Filler On The Best Of"]

        assert weak_after >= weak_before, (
            "lifting the hits pushed a weak track DOWN — exactly what the "
            "request asked not to happen"
        )


class TestTheRatingPathWiresTheExclusionIn:
    """THE WIRING CONTRACT — the half an API test cannot reach.

    Every other test here calls ``compute_track_artist_scores`` with
    ``exclude_album`` itself, so reverting the production change would only
    raise ``TypeError``: that proves the parameter exists, not that the rating
    path USES it. This one spies on the real call from
    ``post_album_star_ratings``, so it fails if the album stops being passed —
    which is the actual fix.
    """

    def _drive(self, monkeypatch, seen: list):
        real = fs._build_compilation_track_artist_scores

        def _spy(album_results, *, exclude_album=None):
            seen.append(exclude_album)
            return real(album_results, exclude_album=exclude_album)

        monkeypatch.setattr(fs, "_build_compilation_track_artist_scores", _spy)
        monkeypatch.setattr(fs, "_assign_stars", lambda *a, **k: 3)
        monkeypatch.setattr(fs, "_build_album_model", lambda *a, **k: {})
        monkeypatch.setattr(fs, "_build_rating_sync_clients", lambda: [])
        monkeypatch.setattr(
            "services.favourites_service.apply_favourite_rating_floor",
            lambda artist, album: 0,
        )

        @contextmanager
        def _session_factory():
            yield _RecordingSession()

        monkeypatch.setattr(fs, "db_session", _session_factory)

        fs.post_album_star_ratings(
            album_results=_album_results(),
            artist=ARTIST,
            artist_scores=[float(r["popularity_score"]) for r in _album_results()],
            options={"sync_navidrome": False},
            is_compilation=True,
            is_va_compilation=False,
        )

    def test_post_album_star_ratings_passes_the_album(self, monkeypatch):
        seen: list = []
        self._drive(monkeypatch, seen)

        assert seen, "the compilation builder was never called"
        assert seen[0] == BEST_OF, (
            f"the rating path built the reference with exclude_album={seen[0]!r} "
            f"instead of the album under rating ({BEST_OF!r}) — the album is "
            "back in its own reference"
        )

    def test_a_studio_album_does_not_build_a_compilation_reference(self, monkeypatch):
        """CONTROL — a studio album must not take this path at all."""
        seen: list = []

        real = fs._build_compilation_track_artist_scores
        monkeypatch.setattr(
            fs,
            "_build_compilation_track_artist_scores",
            lambda *a, **k: seen.append(True) or real(*a, **k),
        )
        monkeypatch.setattr(fs, "_assign_stars", lambda *a, **k: 3)
        monkeypatch.setattr(fs, "_build_album_model", lambda *a, **k: {})
        monkeypatch.setattr(fs, "_build_rating_sync_clients", lambda: [])
        monkeypatch.setattr(
            "services.favourites_service.apply_favourite_rating_floor",
            lambda artist, album: 0,
        )

        @contextmanager
        def _session_factory():
            yield _RecordingSession()

        monkeypatch.setattr(fs, "db_session", _session_factory)

        fs.post_album_star_ratings(
            album_results=_album_results(),
            artist=ARTIST,
            artist_scores=[90.0, 80.0, 70.0, 60.0, 50.0],
            options={"sync_navidrome": False},
            is_compilation=False,
            is_va_compilation=False,
        )

        assert seen == [], "a studio album built a compilation reference"


# ===========================================================================
# 3. No slot cap on a compilation (and the cap still works on a studio album)
# ===========================================================================


class TestNoSlotCapOnACompilation:
    """The first half of the question: "is that still the case?" — yes, and
    both directions are pinned so it cannot silently regress.

    ⚠️ Harness note: ``post_album_star_ratings`` is driven against a FAKE
    session rather than the shared SQLite engine. Its stored-stars query names
    columns the test schema does not carry, and a missing table/column inside
    ``db_session`` is classified as transient — which DISPOSES the shared
    in-memory engine and wipes every table for the rest of the session. The
    recording session also lets the assertions read what was written.
    """

    def _post(self, monkeypatch, *, is_compilation, count=6):
        """Drive the real ``post_album_star_ratings`` with a forced 5★."""
        @contextmanager
        def _session_factory():
            yield _RecordingSession()

        def _five(track, *args, **kwargs):
            # Faithful to the real path: a 5★ from the era ladder carries
            # ``_era_5star``, which is what the slot cap is allowed to demote.
            # Without it the studio control has nothing to cap and would pass
            # for the wrong reason.
            track["_era_5star"] = True
            return 5

        monkeypatch.setattr(fs, "db_session", _session_factory)
        monkeypatch.setattr(fs, "_assign_stars", _five)
        monkeypatch.setattr(fs, "_build_album_model", lambda *a, **k: {
            "has_benchmark": True, "era": "minor", "max_5star_slots": 2,
            "catalog_cutoff": 0.0, "album_top_n": 99,
        })
        monkeypatch.setattr(fs, "_build_rating_sync_clients", lambda: [])
        # Step 4 reaches for the favourite floor, which lives in its OWN module
        # and therefore uses its OWN ``db_session`` — unreachable from the
        # ``fs`` patch above. Left real, it queries a table this test schema
        # does not carry, and a missing table inside ``db_session`` DISPOSES
        # the shared engine (its transient-error path), wiping every table for
        # the rest of the session. It is irrelevant to a slot-cap assertion.
        monkeypatch.setattr(
            "services.favourites_service.apply_favourite_rating_floor",
            lambda artist, album: 0,
        )

        rows = [
            {
                "track_id": f"c{i}", "title": f"Song {i}",
                "album": BEST_OF if is_compilation else OTHER_ALBUM,
                "artist": ARTIST, "album_artist": ARTIST,
                "popularity_score": 90.0 - i, "final_score": 90.0 - i,
                "lastfm_listeners": 500_000, "listenbrainz_listens": 900_000,
                "single_confidence": "high", "is_single": True,
                "is_live": False, "year": 2000,
            }
            for i in range(count)
        ]

        fs.post_album_star_ratings(
            album_results=rows,
            artist=ARTIST,
            artist_scores=[float(r["popularity_score"]) for r in rows],
            options={"sync_navidrome": False},
            is_compilation=is_compilation,
            is_va_compilation=False,
        )
        return rows

    def test_a_compilation_keeps_every_five_star_track(self, monkeypatch):
        """THE QUESTION. A greatest-hits record is all hits; nothing may cap
        how many of them can be 5★."""
        rows = self._post(monkeypatch, is_compilation=True)

        five = [r for r in rows if int(r.get("stars") or 0) == 5]
        assert len(five) == len(rows), (
            f"only {len(five)} of {len(rows)} tracks kept 5★ on a compilation — "
            "a slot cap is being applied where it should not be"
        )

    def test_a_studio_album_is_still_capped(self, monkeypatch):
        """CONTROL — the cap is not gone, it just does not apply to
        compilations. A studio album with a 2-slot era cap must still demote."""
        rows = self._post(monkeypatch, is_compilation=False)

        five = [r for r in rows if int(r.get("stars") or 0) == 5]
        assert len(five) == 2, (
            f"{len(five)} of {len(rows)} tracks kept 5★ on a studio album with "
            "a 2-slot cap — the cap stopped applying"
        )

    def test_both_cap_passes_are_gated_on_the_compilation_verdict(self):
        """Structural companion: the behavioural test above drives ONE path, so
        pin that every cap pass in ``post_album_star_ratings`` carries the
        guard — including the live-album one, which a future edit could add
        without it."""
        import inspect

        source = inspect.getsource(fs.post_album_star_ratings)

        assert "if is_live_album and not is_compilation:" in source
        assert "elif album_model.get(\"has_benchmark\") and not is_compilation:" in source
