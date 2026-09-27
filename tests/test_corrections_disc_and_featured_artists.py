"""The two corrections defects reported from the artist pages.

REPORTED:
  1. "On the corrections section (on the artists page) it's picking up missing
     disk number as an issue, but this isn't a problem when its a single disk
     release."
  2. "It's also showing a duplicate artist when tracks have a featured artists
     on them."

WHY THESE ARE BEHAVIOURAL, NOT SOURCE-TEXT, TESTS. A source assertion cannot
detect a neutered branch — ``if False:`` leaves the text of the check intact and
still "reads" as correct. Six such mutations survived an earlier round of static
tests in this codebase. So the rules are EXECUTED here (against SQLite, running
the shipped SQL verbatim) rather than pattern-matched.

The disc rule is deliberately evaluated at ALBUM level even though one caller
aggregates per ARTIST; ``TestArtistLevelDiscRollUp`` exists because applying the
album rule directly to an artist-level group would have introduced a NEW false
positive for any artist owning both a multi-disc and a single-disc release.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from services.metadata.artist_service import (
    DISC_NUMBER_INCONSISTENT_SQL,
    DISC_NUMBER_ABSENT_SQL,
    DISC_NUMBER_PRESENT_SQL,
)


# The IDs are prefixed so a cleanup fixture can remove exactly what we inserted
# (the test schema is a shared in-memory database that survives between tests).
PREFIX = "cdfa-"

# (id suffix, album_artist, album, disc_number). Every album is CLEAN except
# "Messy Album"; "Mixer" owns both a clean multi-disc and a clean single-disc
# release, which is the shape that traps a naive artist-level roll-up.
FLEET = [
    ("s1", "Solo Act", "Clean Single Disc", None),
    ("s2", "Solo Act", "Clean Single Disc", None),
    ("s3", "Solo Act", "Clean Single Disc", None),
    ("t1", "Tagged Act", "Tagged Single Disc", "1"),
    ("t2", "Tagged Act", "Tagged Single Disc", "1"),
    ("m1", "Mixer", "Two Disc Release", "1"),
    ("m2", "Mixer", "Two Disc Release", "2"),
    ("m3", "Mixer", "Single Disc Release", None),
    ("m4", "Mixer", "Single Disc Release", None),
    ("x1", "Broken Act", "Messy Album", "1"),
    ("x2", "Broken Act", "Messy Album", None),
]


def _insert_fleet(session) -> None:
    """Insert the fixture rows using the given session."""
    for suffix, artist, album, disc in FLEET:
        session.execute(
            text("""
                INSERT INTO tracks (id, artist, album_artist, album, title, disc_number)
                VALUES (:id, :artist, :artist, :album, :title, :disc)
            """),
            {"id": PREFIX + suffix, "artist": artist, "album": album,
             "title": f"Track {suffix}", "disc": disc},
        )
    session.commit()


@pytest.fixture
def badge_session():
    """A dedicated SQLite DB on which ``REGEXP_REPLACE`` is registered.

    The artist-list badge SQL calls ``REGEXP_REPLACE``, which is Postgres-only.
    It is registered here (the same helper ``tests/test_upcoming_musicbrainz_discovery.py``
    uses) so the SHIPPED query runs unmodified instead of being rewritten for the
    test, which would stop testing what production runs.
    """
    from conftest import register_sqlite_regexp_replace
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from db.models import Track

    engine = create_engine("sqlite:///:memory:")
    register_sqlite_regexp_replace(engine)
    Track.__table__.create(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _disc_case_sql(album_level: bool) -> str:
    """The rule as a per-album flag, and rolled up per artist."""
    inner = f"""
        SELECT
            album_artist AS artist,
            album,
            CASE WHEN {DISC_NUMBER_INCONSISTENT_SQL} THEN 1 ELSE 0 END AS bad
        FROM tracks
        WHERE id LIKE '{PREFIX}%'
        GROUP BY album_artist, album
    """
    if album_level:
        return f"SELECT artist, album, bad FROM ({inner}) t ORDER BY album"
    return f"SELECT artist, SUM(bad) FROM ({inner}) t GROUP BY artist ORDER BY artist"


@pytest.fixture
def rows(db_session):
    """Insert the fixture fleet used by the disc tests.

    Every album is CLEAN except ``Messy Album``; ``Mixer`` owns both a clean
    multi-disc and a clean single-disc release, which is the shape that traps a
    naive artist-level roll-up.
    """
    db_session.execute(text(f"DELETE FROM tracks WHERE id LIKE '{PREFIX}%'"))
    data = [
        # (id, album_artist, album, disc_number)
        ("s1", "Solo Act", "Clean Single Disc", None),
        ("s2", "Solo Act", "Clean Single Disc", None),
        ("s3", "Solo Act", "Clean Single Disc", None),
        ("t1", "Tagged Act", "Tagged Single Disc", "1"),
        ("t2", "Tagged Act", "Tagged Single Disc", "1"),
        ("m1", "Mixer", "Two Disc Release", "1"),
        ("m2", "Mixer", "Two Disc Release", "2"),
        ("m3", "Mixer", "Single Disc Release", None),
        ("m4", "Mixer", "Single Disc Release", None),
        ("x1", "Broken Act", "Messy Album", "1"),
        ("x2", "Broken Act", "Messy Album", None),
    ]
    for tid, artist, album, disc in data:
        db_session.execute(
            text("""
                INSERT INTO tracks (id, artist, album_artist, album, title, disc_number)
                VALUES (:id, :artist, :artist, :album, :title, :disc)
            """),
            {"id": PREFIX + tid, "artist": artist, "album": album,
             "title": f"Track {tid}", "disc": disc},
        )
    db_session.commit()
    yield data
    try:
        db_session.execute(text(f"DELETE FROM tracks WHERE id LIKE '{PREFIX}%'"))
        db_session.commit()
    except Exception:
        pass


class TestTheDiscRuleFlagsOnlyGenuineInconsistency:
    """DEFECT 1 — a clean single-disc album must not be reported.

    The app CLEARS disc_number on single-disc albums on purpose
    (routes/ui_routes.py: disctotal <= 1 strips it "so Navidrome/file tags don't
    carry a bogus 1/x or 0/x disc position"). So an absent disc number is the
    app's own correct output, not a problem to report.
    """

    def test_a_clean_single_disc_album_is_not_flagged(self, db_session, rows):
        flagged = {album for _a, album, bad in db_session.execute(
            text(_disc_case_sql(album_level=True))).fetchall() if bad}
        assert "Clean Single Disc" not in flagged, (
            "a single-disc album has no disc numbers BY DESIGN, so it must not "
            "be reported as a disc-number problem"
        )

    def test_a_single_disc_album_tagged_disc_1_is_not_flagged(self, db_session, rows):
        flagged = {album for _a, album, bad in db_session.execute(
            text(_disc_case_sql(album_level=True))).fetchall() if bad}
        assert "Tagged Single Disc" not in flagged

    def test_a_clean_multi_disc_album_is_not_flagged(self, db_session, rows):
        flagged = {album for _a, album, bad in db_session.execute(
            text(_disc_case_sql(album_level=True))).fetchall() if bad}
        assert "Two Disc Release" not in flagged, (
            "a fully-tagged multi-disc release is exactly right"
        )

    def test_a_genuinely_inconsistent_album_IS_flagged(self, db_session, rows):
        flagged = {album for _a, album, bad in db_session.execute(
            text(_disc_case_sql(album_level=True))).fetchall() if bad}
        assert "Messy Album" in flagged, (
            "some tracks with a disc number and some without is the real defect "
            "this section exists to report"
        )

    def test_the_old_rule_would_have_flagged_the_clean_album(self, db_session, rows):
        """Pins WHY the rule changed, by executing the old predicate.

        If this ever stops being true the old rule was equivalent and the change
        was pointless — so it is a real assertion, not decoration.
        """
        old = db_session.execute(text("""
            SELECT album,
                   COUNT(*) FILTER (WHERE disc_number IS NULL OR disc_number = '')
            FROM tracks WHERE id LIKE :p GROUP BY album
        """), {"p": f"{PREFIX}%"}).fetchall()
        old_flagged = {album for album, n in old if n > 0}
        assert "Clean Single Disc" in old_flagged, (
            "the previous predicate counted 'missing one' per TRACK, which is "
            "true on every single-disc album"
        )

    def test_a_stored_zero_disc_is_treated_as_absent(self, db_session):
        """A stored '0' is a bogus value from a bad rip, not a real disc number.

        The old rule compared against the STRING '0' being non-empty, so it
        missed an album mixing '1' and '0'.
        """
        db_session.execute(text(f"DELETE FROM tracks WHERE id LIKE '{PREFIX}%'"))
        for tid, disc in ((PREFIX + "z1", "1"), (PREFIX + "z2", "0")):
            db_session.execute(text("""
                INSERT INTO tracks (id, artist, album_artist, album, title, disc_number)
                VALUES (:id, 'Zero Act', 'Zero Act', 'Zero Mix', 'T', :d)
            """), {"id": tid, "d": disc})
        db_session.commit()
        try:
            flagged = {album for _a, album, bad in db_session.execute(
                text(_disc_case_sql(album_level=True))).fetchall() if bad}
            assert "Zero Mix" in flagged, (
                "'0' means 'no disc number'; mixing it with '1' is the same "
                "fault as mixing '1' with NULL"
            )
        finally:
            db_session.execute(text(f"DELETE FROM tracks WHERE id LIKE '{PREFIX}%'"))
            db_session.commit()

    def test_the_present_and_absent_predicates_are_exhaustive_and_exclusive(
        self, db_session, rows
    ):
        """Every track is exactly one of present/absent — no gaps, no overlap."""
        total, present, absent = db_session.execute(text(f"""
            SELECT COUNT(*),
                   COUNT(*) FILTER (WHERE {DISC_NUMBER_PRESENT_SQL}),
                   COUNT(*) FILTER (WHERE {DISC_NUMBER_ABSENT_SQL})
            FROM tracks WHERE id LIKE '{PREFIX}%'
        """)).fetchone()
        assert int(present) + int(absent) == int(total), (
            "a track that is neither present nor absent would be invisible to "
            "the rule and could hide a real inconsistency"
        )


class TestArtistLevelDiscRollUp:
    """The roll-up must not invent a false positive.

    ``api_artists_corrections`` aggregates per ARTIST. Evaluating the ALBUM rule
    directly on that group compares "has any track with a disc value" against
    "has any track without" across the artist's whole catalogue — which is true
    for anyone owning both a multi-disc and a single-disc release. The rule is
    therefore applied per album and summed.
    """

    def test_an_artist_with_multi_and_single_disc_albums_is_not_flagged(
        self, db_session, rows
    ):
        per_artist = dict(db_session.execute(
            text(_disc_case_sql(album_level=False))).fetchall())
        assert int(per_artist.get("Mixer") or 0) == 0, (
            "'Mixer' owns a clean multi-disc AND a clean single-disc release; "
            "neither album is broken, so the artist must not be flagged"
        )

    def test_the_roll_up_still_flags_the_genuinely_broken_artist(
        self, db_session, rows
    ):
        per_artist = dict(db_session.execute(
            text(_disc_case_sql(album_level=False))).fetchall())
        assert int(per_artist.get("Broken Act") or 0) == 1, (
            "the roll-up must report the ONE bad album, by album count"
        )

    def test_the_naive_artist_level_rule_would_have_flagged_the_mixer(
        self, db_session, rows
    ):
        """Executes the WRONG form on purpose, to keep the trap documented.

        If the naive form ever stops flagging 'Mixer', the trap is gone and the
        two-level roll-up could be simplified — so this is a real assertion, not
        decoration.
        """
        naive = db_session.execute(text(f"""
            SELECT album_artist, CASE WHEN {DISC_NUMBER_INCONSISTENT_SQL}
                                      THEN 1 ELSE 0 END
            FROM tracks WHERE id LIKE '{PREFIX}%'
            GROUP BY album_artist
        """)).fetchall()
        naive_flagged = {a for a, bad in naive if bad}
        assert "Mixer" in naive_flagged, (
            "the artist-level form compares the whole catalogue and cannot tell "
            "a mixed catalogue from a mixed album"
        )

        shipped = dict(db_session.execute(
            text(_disc_case_sql(album_level=False))).fetchall())
        assert int(shipped.get("Mixer") or 0) == 0, (
            "and the shipped per-album roll-up must not flag it"
        )


class TestTheCorrectionAlbumsService:
    """``get_correction_albums`` runs its OWN query (a third copy of the rule).

    Testing the shared constant alone would not notice this site being changed
    back to the per-track predicate, because nothing would execute the SQL it
    actually runs. So the function is called with its session redirected at the
    test database.
    """

    @staticmethod
    def _redirect(monkeypatch, session):
        import contextlib
        import services.metadata.artist_service as artist_service

        @contextlib.contextmanager
        def _fake_session():
            yield session

        monkeypatch.setattr(artist_service, "db_session", _fake_session)

    def test_a_clean_single_disc_album_is_not_a_disc_issue(
        self, badge_session, monkeypatch
    ):
        from services.metadata.artist_service import get_correction_albums

        _insert_fleet(badge_session)
        self._redirect(monkeypatch, badge_session)

        payload, code = get_correction_albums("Solo Act")
        assert code == 200
        by_album = {a["album"]: a for a in payload["albums"]}
        assert "Clean Single Disc" in by_album, "the album must still be listed"
        assert by_album["Clean Single Disc"]["disc_issues"] is False, (
            "a single-disc album has no disc numbers by design; reporting it as "
            "a disc issue is the defect the user reported"
        )
        assert by_album["Clean Single Disc"]["disc_issue_count"] == 0

    def test_a_genuinely_inconsistent_album_is_a_disc_issue(
        self, badge_session, monkeypatch
    ):
        from services.metadata.artist_service import get_correction_albums

        _insert_fleet(badge_session)
        self._redirect(monkeypatch, badge_session)

        payload, _code = get_correction_albums("Broken Act")
        by_album = {a["album"]: a for a in payload["albums"]}
        assert by_album["Messy Album"]["disc_issues"] is True, (
            "the real defect must still be reported"
        )
        assert by_album["Messy Album"]["disc_issue_count"] == 1, (
            "it is a boolean flag, so the count is capped at one bad album"
        )

    def test_a_fully_tagged_multi_disc_album_is_not_a_disc_issue(
        self, badge_session, monkeypatch
    ):
        from services.metadata.artist_service import get_correction_albums

        _insert_fleet(badge_session)
        self._redirect(monkeypatch, badge_session)

        payload, _code = get_correction_albums("Mixer")
        by_album = {a["album"]: a for a in payload["albums"]}
        for album in ("Two Disc Release", "Single Disc Release"):
            assert by_album[album]["disc_issues"] is False, (
                f"{album} is clean and must not be reported"
            )


class TestTheBadgeCountsDidNotChangeBeyondTheDiscRule:
    """Only the DISC rule was meant to change.

    The endpoint also reports ``mbid_inconsistent_count`` and
    ``missing_tracks_count``. Rewriting the query to apply the album-level disc
    rule through an inner group is exactly the kind of edit that silently turns
    those other two from per-TRACK counts into per-ALBUM ones — changing numbers
    in the UI that nobody asked to change. The disc count is per-album on purpose
    (a per-track disc number is meaningless); the other two must stay per-track.
    """

    def test_the_disc_count_is_per_album_and_the_others_are_per_track(
        self, badge_session
    ):
        from routes.artist_routes import ARTIST_CORRECTIONS_SQL

        # One artist, ONE album with 3 tracks: 2 lack an MBID, 2 lack a file.
        for i in range(3):
            badge_session.execute(
                text("""
                    INSERT INTO tracks
                        (id, artist, album_artist, album, title, mbid, file_path,
                         disc_number)
                    VALUES (:id, 'Solo', 'Solo', 'One Album', 'T', :mbid, :fp, '1')
                """),
                {
                    "id": f"cdfa-cnt{i}",
                    "mbid": "mb-1" if i == 0 else None,
                    "fp": "/music/a.mp3" if i == 0 else None,
                },
            )
        badge_session.commit()

        row = badge_session.execute(text(ARTIST_CORRECTIONS_SQL)).fetchone()
        assert row is not None, "the artist must be reported"
        m = dict(row._mapping)
        assert m["mbid_inconsistent_count"] == 2, (
            "two TRACKS lack an MBID — this count has always been per-track and "
            "must not have become 1 (one affected album)"
        )
        assert m["missing_tracks_count"] == 2, (
            "two TRACKS lack a file — likewise per-track, not one album"
        )
        assert m["disc_inconsistent_count"] == 0, (
            "every track carries disc 1, so the album is consistent; the disc "
            "count is per-ALBUM inconsistency"
        )


class TestTheArtistListBadgeQuery:
    """The exact SQL the artist LIST badge endpoint runs.

    The SQL is executed here verbatim (it lives in ``ARTIST_CORRECTIONS_SQL``
    precisely so it can be) rather than through an HTTP client: the endpoint is
    a SYNC view, so Quart dispatches it in a worker thread, and an in-memory
    SQLite connection cannot be shared across threads. Running the real query
    against seeded rows tests the same logic without that test-harness artifact.
    """

    def test_a_clean_single_disc_artist_is_not_reported(self, badge_session):
        from routes.artist_routes import ARTIST_CORRECTIONS_SQL

        _insert_fleet(badge_session)
        result = {
            row._mapping["artist_name"]: dict(row._mapping)
            for row in badge_session.execute(text(ARTIST_CORRECTIONS_SQL)).fetchall()
        }
        entry = result.get("Solo Act")
        assert entry is not None, (
            "the artist is still listed (it has missing MBIDs), but that is a "
            "SEPARATE problem from disc numbers"
        )
        assert entry["disc_inconsistent_count"] == 0, (
            "a single-disc album must not contribute a disc problem"
        )

    def test_a_genuinely_inconsistent_artist_reports_one_bad_album(
        self, badge_session
    ):
        from routes.artist_routes import ARTIST_CORRECTIONS_SQL

        _insert_fleet(badge_session)
        result = {
            row._mapping["artist_name"]: dict(row._mapping)
            for row in badge_session.execute(text(ARTIST_CORRECTIONS_SQL)).fetchall()
        }
        entry = result.get("Broken Act")
        assert entry is not None, "a broken artist must still be reported"
        assert entry["disc_inconsistent_count"] == 1, (
            "exactly one album is inconsistent"
        )

    def test_an_artist_with_both_disc_shapes_is_not_reported_for_discs(
        self, badge_session
    ):
        from routes.artist_routes import ARTIST_CORRECTIONS_SQL

        _insert_fleet(badge_session)
        result = {
            row._mapping["artist_name"]: dict(row._mapping)
            for row in badge_session.execute(text(ARTIST_CORRECTIONS_SQL)).fetchall()
        }
        entry = result.get("Mixer")
        assert entry is not None, (
            "Mixer still has missing MBIDs, so it is listed"
        )
        assert entry["disc_inconsistent_count"] == 0, (
            "owning a multi-disc and a single-disc album is normal and must "
            "not be reported as a disc inconsistency"
        )

    def test_the_query_produces_the_keys_the_consumer_reads(self, badge_session):
        """The list page reads these exact keys; a rename would blank the badge
        rather than fail loudly."""
        from routes.artist_routes import ARTIST_CORRECTIONS_SQL

        _insert_fleet(badge_session)
        row = badge_session.execute(text(ARTIST_CORRECTIONS_SQL)).fetchone()
        assert row is not None, "the fixture artists must be reported at all"
        keys = set(row._mapping.keys())
        for required in (
            "artist_name", "disc_inconsistent_count",
            "mbid_inconsistent_count", "missing_tracks_count",
        ):
            assert required in keys, f"{required} is read by the artist list page"


class TestTheDuplicateArtistReportIgnoresGuestCredits:
    """DEFECT 2 — a featured track must not look like a second artist.

    One MBID belongs to one act, so a second spelling is normally a real
    duplicate. But a featured track carries the PRIMARY artist's MBID with a
    credit-bearing name, which is the same artist, differently credited.
    """

    def test_guest_credit_spellings_all_collapse(self):
        from helpers.normalization_service import strip_guest_credit

        for spelling in (
            "Powerwolf feat. Unleash The Archers",
            "Powerwolf ft. Unleash The Archers",
            "Powerwolf featuring Unleash The Archers",
            "Powerwolf (feat. Unleash The Archers)",
            "Powerwolf [feat. X]",
            "Powerwolf  Feat.  Someone",
        ):
            assert strip_guest_credit(spelling) == "Powerwolf", spelling

    def test_a_plain_name_is_unchanged(self):
        from helpers.normalization_service import strip_guest_credit

        assert strip_guest_credit("Powerwolf") == "Powerwolf"
        assert strip_guest_credit("POWERWOLF") == "POWERWOLF"

    def test_a_co_credited_artist_is_NOT_collapsed(self):
        """⚠️ The safety property that keeps the merge honest.

        "A & B" really is two artists. If this helper folded them, the merge
        action (which rewrites the variant to the canonical name) would offer
        the user a button that DELETES the second artist.
        """
        from helpers.normalization_service import strip_guest_credit

        for pair in (
            "Simon & Garfunkel",
            "Hall & Oates",
            "Florence and the Machine",
            "Peter Gabriel with Kate Bush",
            "AC/DC",
        ):
            assert strip_guest_credit(pair) == pair, (
                f"{pair!r} credits two artists; collapsing it would make a "
                "merge delete one of them"
            )

    def test_a_name_that_is_only_a_credit_is_never_emptied(self):
        from helpers.normalization_service import strip_guest_credit

        assert strip_guest_credit("feat. Someone") == "feat. Someone", (
            "stripping must never return an empty artist name"
        )

    def test_the_collapse_merges_a_featured_track_into_its_primary(self):
        from routes.misc_routes import _collapse_duplicate_artist_rows

        out = _collapse_duplicate_artist_rows([
            {"artist": "Powerwolf", "track_count": 3},
            {"artist": "Powerwolf feat. Unleash The Archers", "track_count": 1},
        ])
        assert out == [{"artist": "Powerwolf", "track_count": 4}], (
            "a featured track is the SAME artist, and the counts must be summed "
            "so the reported total stays accurate"
        )

    def test_the_collapse_keeps_the_shortest_spelling_as_the_display_name(self):
        from routes.misc_routes import _collapse_duplicate_artist_rows

        out = _collapse_duplicate_artist_rows([
            {"artist": "Powerwolf feat. A", "track_count": 1},
            {"artist": "Powerwolf", "track_count": 1},
        ])
        assert out == [{"artist": "Powerwolf", "track_count": 2}], (
            "the bare act is the right thing to show, not the credit-bearing form"
        )

    def test_the_collapse_still_exposes_a_real_casing_variant(self):
        from routes.misc_routes import _collapse_duplicate_artist_rows

        out = _collapse_duplicate_artist_rows([
            {"artist": "Powerwolf", "track_count": 3},
            {"artist": "POWERWOLF", "track_count": 2},
        ])
        assert len(out) == 2, (
            "the fix must not silence a genuine duplicate — that is what this "
            "endpoint is for"
        )
        assert {r["artist"] for r in out} == {"Powerwolf", "POWERWOLF"}

    def test_the_collapse_does_NOT_fold_a_co_credited_artist(self):
        """⚠️ The safety property behind the merge offer.

        ``normalize_existing_artist_rows`` (the merge) rewrites the tracks of any
        variant whose normalized key matches the canonical name. "Simon &
        Garfunkel" is two artists, so folding it would put a button in the UI
        that deletes one of them.
        """
        from routes.misc_routes import _collapse_duplicate_artist_rows

        out = _collapse_duplicate_artist_rows([
            {"artist": "Simon & Garfunkel", "track_count": 3},
            {"artist": "Simon & Garfunkel", "track_count": 1},
        ])
        assert out == [{"artist": "Simon & Garfunkel", "track_count": 4}], (
            "'&' must survive; only feat./ft./featuring are guest credits"
        )
        # And a genuinely different act is still two rows.
        out2 = _collapse_duplicate_artist_rows([
            {"artist": "Simon & Garfunkel", "track_count": 3},
            {"artist": "Simon", "track_count": 1},
        ])
        assert len(out2) == 2, "'Simon' is not 'Simon & Garfunkel'"

    def test_the_collapse_ignores_blank_names(self):
        from routes.misc_routes import _collapse_duplicate_artist_rows

        out = _collapse_duplicate_artist_rows([
            {"artist": "", "track_count": 5},
            {"artist": None, "track_count": 2},
            {"artist": "Powerwolf", "track_count": 1},
        ])
        assert out == [{"artist": "Powerwolf", "track_count": 1}], (
            "a blank artist name cannot be merged and must not enter the list"
        )

    def test_a_single_survivor_is_not_a_duplicate(self):
        """The condition that decides whether the endpoint reports anything."""
        from routes.misc_routes import _collapse_duplicate_artist_rows

        variations = _collapse_duplicate_artist_rows([
            {"artist": "Powerwolf", "track_count": 3},
            {"artist": "Powerwolf feat. Unleash The Archers", "track_count": 1},
        ])
        assert len(variations) == 1, (
            "one surviving spelling means the only difference was a guest "
            "credit, so nothing may be offered as a duplicate"
        )

    def test_the_decision_helper_gates_on_more_than_one_variant(self):
        """The ``> 1`` condition itself, which used to be unreachable inline.

        Inverting this (``>= 1``) or neutering it would make the endpoint report
        EVERY artist as a duplicate of itself, so it is asserted directly.
        """
        from routes.misc_routes import _has_duplicate_artist_variants

        assert _has_duplicate_artist_variants([]) is False
        assert _has_duplicate_artist_variants(
            [{"artist": "A", "track_count": 1}]) is False, (
            "a single variant is not a duplicate"
        )
        assert _has_duplicate_artist_variants(
            [{"artist": "A", "track_count": 1},
             {"artist": "B", "track_count": 1}]) is True

    def test_the_endpoint_uses_the_collapse(self):
        """Wiring: the helper must actually be called by the route.

        Extracting it would be pointless if the route still grouped the raw
        column, which is exactly the defect.
        """
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "routes" / "misc_routes.py").read_text(encoding="utf-8")
        route_body = src.split("def api_get_duplicate_artists")[1]
        route_body = route_body.split("def api_merge_duplicate_artists")[0]
        assert "_collapse_duplicate_artist_rows(raw_rows)" in route_body, (
            "the duplicate-artist route must collapse guest credits before "
            "deciding whether there is a duplicate"
        )
        assert "if _has_duplicate_artist_variants(variations_data):" in route_body, (
            "and the collapsed set must be the thing the decision is made on"
        )


class TestTheDuplicateArtistEndpointEndToEnd:
    """The reported symptom, exercised through the real HTTP route.

    ⚠️ Uses a FILE-backed SQLite database. The endpoint is a SYNC view, so Quart
    dispatches it in a worker thread, and an in-memory database cannot be shared
    across threads — the request would fail with a ProgrammingError instead of
    testing anything. A file (with ``check_same_thread`` relaxed) lets the worker
    thread read the rows the test seeded.
    """

    @pytest.fixture
    def dup_endpoint(self, tmp_path, monkeypatch):
        from contextlib import contextmanager

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        import routes.misc_routes as misc
        from db.models import Track

        url = f"sqlite:///{tmp_path / 'dup.db'}"
        seed_engine = create_engine(
            url, connect_args={"check_same_thread": False}
        )
        Track.__table__.create(seed_engine)

        def _seed(rows) -> None:
            with seed_engine.begin() as conn:
                conn.execute(text("DELETE FROM tracks"))
                for i, (artist, mbid) in enumerate(rows):
                    conn.execute(
                        text("""
                            INSERT INTO tracks
                                (id, artist, album_artist, album, title,
                                 musicbrainz_artistid)
                            VALUES (:id, :artist, :artist, 'The Album', 'T', :mbid)
                        """),
                        {"id": f"e2e-{i}", "artist": artist, "mbid": mbid},
                    )

        request_engine = create_engine(
            url, connect_args={"check_same_thread": False}
        )
        factory = sessionmaker(bind=request_engine, expire_on_commit=False)

        @contextmanager
        def _fake_db_session():
            session = factory()
            try:
                yield session
                session.commit()
            finally:
                session.close()

        class _StubMb:
            def get_artist(self, _mbid):
                return {"name": "Powerwolf"}

        monkeypatch.setattr(misc, "db_session", _fake_db_session)
        monkeypatch.setattr(misc, "get_shared_mb_client", lambda: _StubMb())
        try:
            yield _seed
        finally:
            factory.close_all()
            request_engine.dispose()
            seed_engine.dispose()

    async def test_a_featured_track_alone_is_not_a_duplicate(
        self, client, dup_endpoint
    ):
        """The reported defect, end to end."""
        dup_endpoint([
            ("Powerwolf", "pw-1"),
            ("Powerwolf", "pw-1"),
            ("Powerwolf", "pw-1"),
            ("Powerwolf feat. Unleash The Archers", "pw-1"),
        ])
        resp = await client.get("/api/duplicate-artists/Powerwolf")
        assert resp.status_code == 200
        payload = await resp.get_json()
        assert payload["success"] is True
        assert payload["duplicates"] == [], (
            "a track featuring a guest is the SAME artist, differently credited; "
            "reporting it as a duplicate is the defect the user saw"
        )

    async def test_a_genuine_casing_variant_is_still_reported(
        self, client, dup_endpoint
    ):
        """The fix must not silence real duplicates — that is the endpoint's job."""
        dup_endpoint([
            ("Powerwolf", "pw-2"),
            ("Powerwolf", "pw-2"),
            ("POWERWOLF", "pw-2"),
        ])
        resp = await client.get("/api/duplicate-artists/Powerwolf")
        payload = await resp.get_json()
        assert len(payload["duplicates"]) == 1, (
            "two different spellings of one MBID IS a duplicate"
        )
        dup = payload["duplicates"][0]
        assert set(dup["variations"]) == {"Powerwolf", "POWERWOLF"}
        assert dup["mbid"] == "pw-2"

    async def test_a_featured_variant_plus_a_genuine_variant_still_reports(
        self, client, dup_endpoint
    ):
        """Both at once: the guest credit is folded away and the real variant
        survives, so the repair is still offered — correctly scoped."""
        dup_endpoint([
            ("Powerwolf", "pw-3"),
            ("Powerwolf feat. Someone", "pw-3"),
            ("powerwolf", "pw-3"),
        ])
        resp = await client.get("/api/duplicate-artists/Powerwolf")
        payload = await resp.get_json()
        assert len(payload["duplicates"]) == 1
        dup = payload["duplicates"][0]
        assert set(dup["variations"]) == {"Powerwolf", "powerwolf"}, (
            "the guest-credited spelling must be folded into 'Powerwolf', and "
            "the case variant kept"
        )
        assert "Powerwolf feat. Someone" not in dup["variations"], (
            "offering the credit-bearing spelling as a merge source is what let "
            "the UI suggest merging an artist with itself"
        )

    async def test_an_artist_with_no_mbid_reports_nothing(self, client, dup_endpoint):
        dup_endpoint([("Powerwolf", "")])
        resp = await client.get("/api/duplicate-artists/Powerwolf")
        payload = await resp.get_json()
        assert payload["duplicates"] == []
        assert payload["artist_info"]["mbid"] is None


class TestBothFlaggingSitesShareOneRule:
    """Wiring: the two flagging sites must use the shared constant.

    Behaviour is covered above; this pins that they were not re-implemented with
    a private copy, which is how the two rules drifted apart originally.
    """

    @staticmethod
    def _code_only(src: str) -> str:
        """Strip comments AND string bodies so a check cannot match prose.

        ⚠️ Without this, an assertion for the old predicate matches the comment
        that explains why the predicate was removed — the check passes for
        exactly the wrong reason. Triple quotes are removed before single quotes
        (see the note at the call site).
        """
        import re
        src = re.sub(r'"""[\s\S]*?"""', "", src)
        src = re.sub(r"'''[\s\S]*?'''", "", src)
        src = re.sub(r'"[^"\n]*"', '""', src)
        src = re.sub(r"'[^'\n]*'", "''", src)
        src = re.sub(r"#[^\n]*", "", src)
        return src

    def test_the_service_uses_the_shared_constant(self):
        from pathlib import Path
        svc = (Path(__file__).resolve().parent.parent
               / "services" / "metadata" / "artist_service.py")
        src = svc.read_text(encoding="utf-8")
        assert src.count("DISC_NUMBER_INCONSISTENT_SQL") >= 3, (
            "the definition plus both query sites"
        )

    def test_the_route_imports_the_shared_constant(self):
        from pathlib import Path
        route = (Path(__file__).resolve().parent.parent
                 / "routes" / "artist_routes.py")
        src = route.read_text(encoding="utf-8")
        assert "from services.metadata.artist_service import DISC_NUMBER_INCONSISTENT_SQL" in src, (
            "the shared predicate must be imported, not re-typed"
        )
        # The constant's VALUE is interpolated into the module-level SQL, so the
        # name itself must appear in that SQL string (checked on the raw source,
        # because it lives inside a string literal).
        assert "DISC_NUMBER_INCONSISTENT_SQL" in src.split("ARTIST_CORRECTIONS_SQL = ")[1], (
            "the artist-list query must actually use the shared predicate"
        )

    def test_no_site_still_asks_the_old_per_track_question(self):
        """The old predicate must not survive in executable code.

        Checked on comment- and string-stripped source, so the comments that
        explain the removal cannot satisfy the assertion.
        """
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        offenders = []
        for rel in ("routes/artist_routes.py", "services/metadata/artist_service.py"):
            code = self._code_only((root / rel).read_text(encoding="utf-8"))
            if "disc_number IS NULL OR disc_number = ''" in code:
                offenders.append(rel)
        assert not offenders, (
            "the per-track 'is it missing' predicate IS the defect; it must not "
            f"survive in executable code: {offenders}"
        )

    def test_the_shared_predicate_is_not_vacuous(self):
        """Guard against the rule being emptied (e.g. always-false)."""
        from services.metadata.artist_service import DISC_NUMBER_INCONSISTENT_SQL

        assert "> 0" in DISC_NUMBER_INCONSISTENT_SQL
        assert DISC_NUMBER_PRESENT_SQL in DISC_NUMBER_INCONSISTENT_SQL
        assert DISC_NUMBER_ABSENT_SQL in DISC_NUMBER_INCONSISTENT_SQL
        assert "!= '0'" in DISC_NUMBER_PRESENT_SQL, (
            "a stored '0' is not a real disc number"
        )


class TestTheArtistListLabelMatchesTheNewMeaning:
    """The badge's tooltip text must describe what the number now means.

    The old label was 'missing disc number', which was a faithful description of
    the old (wrong) rule and is now actively misleading: it told the user to
    "fix" clean single-disc albums. A count without a correct label is still a
    wrong report to the person reading it.
    """

    def test_the_label_no_longer_claims_a_disc_number_is_missing(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "test_site" / "static" / "js" / "pages" / "artists.js")
        code = src.read_text(encoding="utf-8")
        assert "disc_inconsistent_count: 'missing disc number'" not in code, (
            "that label described the per-track rule; the count is now per-album "
            "inconsistency, so keeping it would mislabel a clean single-disc album"
        )
        assert "disc_inconsistent_count: 'disc number inconsistency'" in code, (
            "the label must say what is actually counted"
        )
