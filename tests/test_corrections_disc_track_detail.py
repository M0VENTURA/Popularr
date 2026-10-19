"""The disc-number section must name the offending TRACKS, not just count them.

REPORTED:

> "Under disk number inconsistency, can we add the details of the tracks that
> are showing the wrong details in the corrections page?"

The section previously rendered per-album COUNTS only — "2 with disc #, 5
without" — which tells the operator an album is inconsistent but not WHICH rows
disagree. Finding the offenders meant opening the album and eyeballing every
track, which is the manual work the corrections page exists to remove.

So each flagged album now carries its tracks split into the two groups that need
OPPOSITE treatment:

* ``tracks_with`` — rows that HAVE a disc number. On a single-disc album these
  are the odd ones out: they are what "Clear Disc #" removes.
* ``tracks_without`` — rows that already match the majority case. Clearing is a
  no-op for them, so showing them as "offenders" would be misleading.

⚠️ WHY THESE ARE BEHAVIOURAL. A source assertion cannot tell a working split
from a neutered one — ``if False:`` leaves the text intact and still "reads"
correct. These EXECUTE ``get_artist_corrections`` against a real (in-memory)
database and assert on the returned payload, which is the same discipline the
sibling ``test_corrections_disc_and_featured_artists`` module documents.

⚠️ The "absent" test here is deliberately the SAME predicate the SQL uses
(``disc_number`` NULL / blank / '0'). If the Python split disagreed with
``DISC_NUMBER_ABSENT_SQL``, the detail pane and the counts printed beside it
could disagree — the panel would total 7 tracks while the badge says 6.
"""

from __future__ import annotations

import contextlib

import pytest
from sqlalchemy import text


# Distinct prefix so this module's rows never collide with the sibling suite's.
PREFIX = "ddet-"

#: The artist under test, and its fixture fleet.
ARTIST = "Detail Artist"
FLEET = [
    # (id suffix, album, title, disc_number, track_number)
    ("a1", "Messy Singles", "Alpha", "1", "1"),
    ("a2", "Messy Singles", "Bravo", "1", "2"),
    ("a3", "Messy Singles", "Charlie", None, "3"),
    ("a4", "Messy Singles", "Delta", "", "4"),
    ("a5", "Messy Singles", "Echo", "0", "5"),
    ("b1", "Messy One Disc", "Foxtrot", "0", "1"),
    ("b2", "Messy One Disc", "Golf", "1", "2"),
    ("c1", "Clean", "Hotel", None, "1"),
    ("c2", "Clean", "India", "", "2"),
    ("d1", "Real Two Disc", "Juliett", "1", "1"),
    ("d2", "Real Two Disc", "Kilo", "2", "1"),
]


@pytest.fixture
def disc_detail_rows(tmp_path, monkeypatch):
    """Two flagged albums plus clean albums, in a FILE-backed database.

    ``Detail Artist`` owns:
      * ``Messy Singles``  — 2 tracks with disc '1', 3 absent (NULL/''/'0') → FLAGGED
      * ``Messy One Disc`` — '0' (absent) plus '1'                          → FLAGGED
      * ``Clean``          — every track absent                            → CLEAN
      * ``Real Two Disc``  — disc 1 and disc 2, nothing absent              → CLEAN

    ⚠️ WHY A FILE AND NOT THE SHARED IN-MEMORY ENGINE.
    ``get_artist_corrections`` opens its OWN ``db.engine.db_session()``, and that
    context manager DISPOSES the engine on a transient error — which wipes an
    in-memory database (``tests/conftest.py::db_session`` documents exactly
    this). The branch under test also issues a SECOND query inside that same
    session, and the shared in-memory engine is a single connection. A file lets
    the SHIPPED function run unmodified against rows this test seeded; rewriting
    the query for the test would stop testing production.
    """
    from contextlib import contextmanager

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from db.models import Track
    import services.metadata.artist_service as svc

    engine = create_engine(
        f"sqlite:///{tmp_path / 'disc_detail.db'}",
        connect_args={"check_same_thread": False},
    )
    Track.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with engine.begin() as conn:
        for suffix, album, title, disc, track_no in FLEET:
            conn.execute(
                text("""
                    INSERT INTO tracks
                        (id, artist, album_artist, album, title, disc_number, track_number)
                    VALUES
                        (:id, :artist, :artist, :album, :title, :disc, :track)
                """),
                {
                    "id": PREFIX + suffix,
                    "artist": ARTIST,
                    "album": album,
                    "title": title,
                    "disc": disc,
                    "track": track_no,
                },
            )

    @contextmanager
    def _session():
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(svc, "db_session", _session)
    try:
        # Expose the factory so a test can install its OWN wrapper over the same
        # file database, instead of rebuilding a second one and racing the
        # fixture's monkeypatch.
        yield {"factory": factory, "session": _session, "path": tmp_path}
    finally:
        # ⚠️ NOT ``factory.close_all()`` — ``sessionmaker`` has no such method.
        # The sibling corrections suite calls it and its four endpoint tests
        # error in teardown because of it; there is no reason to copy that here.
        # ``engine.dispose()`` closes the pool, which is all that is needed once
        # every session the fixture handed out has been closed.
        engine.dispose()


def _disc_payload(*_ignored):
    """Run the SHIPPED disc analysis; return its rows keyed by album.

    ⚠️ Calls ``fetch_disc_inconsistent_albums`` — the helper the corrections page
    itself calls — rather than ``get_artist_corrections``. That outer function
    also runs a PostgreSQL-only ``STRING_AGG(DISTINCT …, ', ')`` query, so it
    cannot execute against SQLite at all. Going through the helper keeps the SQL
    and the track split VERBATIM: a test that rewrote them would stop testing
    production.

    ⚠️ Takes and IGNORES its argument. Call sites pass the ``disc_detail_rows``
    fixture only to force it to run first; the helper is handed the session that
    fixture built.
    """
    from services.metadata.artist_service import fetch_disc_inconsistent_albums

    session = _ignored[0]["factory"]() if _ignored else None
    assert session is not None, "the disc_detail_rows fixture must be requested"
    try:
        rows = fetch_disc_inconsistent_albums(session, ARTIST)
    finally:
        session.close()
    return {str(r.get("album") or ""): r for r in rows}


class TestTheSectionNamesTheOffendingTracks:
    def test_a_flagged_album_carries_its_track_detail(self, disc_detail_rows):
        """THE FEATURE: a flagged album exposes the tracks behind its counts."""
        by_album = _disc_payload(disc_detail_rows)

        assert "Messy Singles" in by_album, (
            "the inconsistent album is missing from the payload entirely"
        )
        entry = by_album["Messy Singles"]

        assert "tracks_with" in entry and "tracks_without" in entry, (
            "the album carries counts but no track detail — this is the "
            "reported gap: the page cannot name which tracks are wrong"
        )
        assert entry["tracks_with"], "the tracks that HAVE a disc number are missing"
        assert entry["tracks_without"], "the tracks WITHOUT a disc number are missing"

    def test_the_two_groups_match_the_rendered_counts(self, disc_detail_rows):
        """⚠️ The detail must AGREE with the counts printed beside it.

        A split that disagrees with the badge is worse than no split: the
        operator would "fix" the wrong rows and the album would stay
        inconsistent.
        """
        entry = _disc_payload(disc_detail_rows)["Messy Singles"]

        assert len(entry["tracks_with"]) == entry["tracks_with_disc"], (
            f"detail lists {len(entry['tracks_with'])} track(s) WITH a disc "
            f"number but the count says {entry['tracks_with_disc']}"
        )
        assert len(entry["tracks_without"]) == entry["tracks_without_disc"], (
            f"detail lists {len(entry['tracks_without'])} track(s) WITHOUT one "
            f"but the count says {entry['tracks_without_disc']}"
        )
        assert (
            len(entry["tracks_with"]) + len(entry["tracks_without"])
            == entry["track_count"]
        ), "the two groups must account for every track in the album"

    def test_the_detail_names_the_right_tracks(self, disc_detail_rows):
        """Not just the right COUNT — the right rows, by title."""
        entry = _disc_payload(disc_detail_rows)["Messy Singles"]

        with_disc = {t["title"] for t in entry["tracks_with"]}
        without_disc = {t["title"] for t in entry["tracks_without"]}

        assert with_disc == {"Alpha", "Bravo"}, (
            f"tracks with a disc number should be Alpha/Bravo, got {with_disc}"
        )
        assert without_disc == {"Charlie", "Delta", "Echo"}, (
            f"tracks without one should be Charlie/Delta/Echo, got {without_disc}"
        )

    def test_absent_means_zero_and_blank_too(self, disc_detail_rows):
        """The split uses the SAME absent predicate as the SQL.

        ``'0'`` and ``''`` are "absent" by ``DISC_NUMBER_ABSENT_SQL``. A Python
        split that only treated NULL as absent would file Echo ('0') and Delta
        ('') under "has a disc number" — and offer to clear a number they do not
        have.
        """
        entry = _disc_payload(disc_detail_rows)["Messy One Disc"]

        with_disc = {t["title"] for t in entry["tracks_with"]}
        without_disc = {t["title"] for t in entry["tracks_without"]}

        assert with_disc == {"Golf"}, f"only Golf really has a disc number, got {with_disc}"
        assert without_disc == {"Foxtrot"}, (
            f"Foxtrot's disc_number is '0', which is ABSENT, got {without_disc}"
        )

    def test_the_detail_reports_the_disc_value_it_will_clear(self, disc_detail_rows):
        """Each "with" row shows the value, so the operator knows what changes."""
        entry = _disc_payload(disc_detail_rows)["Messy Singles"]

        values = {t["disc_number"] for t in entry["tracks_with"]}
        assert values == {"1"}, f"expected the literal disc value '1', got {values}"

        for t in entry["tracks_without"]:
            assert t["disc_number"] is None, (
                "an absent disc number must normalise to None, not to '' or '0', "
                "so the template cannot render a phantom value"
            )

    def test_track_identity_and_number_are_present(self, disc_detail_rows):
        """The rows are actionable: an id to link/navigate, a number to order by."""
        entry = _disc_payload(disc_detail_rows)["Messy Singles"]

        for t in entry["tracks_with"] + entry["tracks_without"]:
            assert t["id"], "a track row with no id cannot be acted on"
            assert t["title"], "a track row with no title cannot be identified"

        numbered = [t for t in entry["tracks_with"] + entry["tracks_without"] if t["track_number"]]
        assert len(numbered) == 5, (
            "every fixture track has a track_number, so all five should carry one"
        )

    def test_tracks_with_a_disc_number_are_ordered_first(self, disc_detail_rows):
        """Order matters: the ones to clear should be the ones read first."""
        entry = _disc_payload(disc_detail_rows)["Messy Singles"]

        for t in entry["tracks_with"]:
            assert t["has_disc_number"] is True
        for t in entry["tracks_without"]:
            assert t["has_disc_number"] is False


class TestCleanAlbumsAreNotListed:
    def test_a_uniformly_untagged_album_is_not_flagged(self, disc_detail_rows):
        """Control: the detail work must not start flagging clean albums."""
        by_album = _disc_payload(disc_detail_rows)

        assert "Clean" not in by_album, (
            "an album where EVERY track has no disc number is consistent — it is "
            "a single-disc release, and flagging it was the original defect"
        )

    def test_a_genuine_multi_disc_is_not_flagged(self, disc_detail_rows):
        """Control: disc 1 + disc 2 with nothing absent is consistent."""
        by_album = _disc_payload(disc_detail_rows)

        assert "Real Two Disc" not in by_album, (
            "a real multi-disc release has both disc values and no absent rows"
        )

    def test_only_the_inconsistent_albums_are_returned(self, disc_detail_rows):
        by_album = _disc_payload(disc_detail_rows)

        assert set(by_album) == {"Messy Singles", "Messy One Disc"}, (
            f"expected exactly the two inconsistent albums, got {sorted(by_album)}"
        )


class TestTheDetailIsEnhancementNotAFailureMode:
    def test_the_counts_survive_when_the_detail_query_fails(
        self, disc_detail_rows, monkeypatch
    ):
        """⚠️ A failing detail query must not cost the counts or the Clear button.

        The detail is a SECOND query on a page that already worked without it. If
        it fails the page must degrade to EXACTLY the previous behaviour — counts
        and action intact, empty groups — rather than to an empty panel or a 500.
        """
        import services.metadata.artist_service as svc

        factory = disc_detail_rows["factory"]

        @contextlib.contextmanager
        def _flaky_session():
            session = factory()

            class _FlakyWrap:
                """Raises ONLY for the detail query (the one selecting disc_text)."""

                def execute(self, stmt, *a, **k):
                    if "disc_text" in str(stmt):
                        raise RuntimeError("detail query exploded")
                    return session.execute(stmt, *a, **k)

                def __getattr__(self, name):
                    return getattr(session, name)

            try:
                yield _FlakyWrap()
                session.commit()
            finally:
                session.close()

        session = None
        with _flaky_session() as flaky:
            rows = svc.fetch_disc_inconsistent_albums(flaky, ARTIST)

        albums = {str(r.get("album") or ""): r for r in rows}

        assert "Messy Singles" in albums, (
            "the album must still be listed — the detail is an ENHANCEMENT"
        )
        entry = albums["Messy Singles"]
        assert entry["tracks_with_disc"] == 2, "the counts must survive"
        assert entry["tracks_without_disc"] == 3, "the counts must survive"
        assert entry["tracks_with"] == [], (
            "a failed detail query yields empty groups, not a crash — and the "
            "template renders no disclosure for an empty group"
        )
        assert entry["tracks_without"] == []
