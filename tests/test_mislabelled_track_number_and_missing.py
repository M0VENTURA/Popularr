"""A mislabelled track number must not hide a different missing track.

Reported: *"Missing tracks doesn't pick up a track if another track has the
wrong track number. A track is track 4, but labelled as one, so it didn't
properly load 1 or 4."*

The mechanism: `lib_by_position` maps ``(disc, track_number)`` to whatever file
sits there. Track 4's audio tagged as track 1 therefore occupies position 1, and
the release's track 1 looked PRESENT. The pairing rule could not tell, because it
needs either an agreeing title or a DISAGREEING length to reject a position, and
a freshly-imported row usually has no duration yet — and an unknown length is
deliberately permissive.

The occupant's own title says whose slot it is, so the check now resolves the
position FIRST: if the row at this number names a DIFFERENT track of this
release, the number belongs to that track and cannot prove this one present.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

MB_WORDS = ["One", "Two", "Three", "Four", "Five"]


def _mb_tracks() -> list[dict]:
    return [
        {
            "title": word,
            "track_number": str(index),
            "disc_number": 1,
            "duration": 200000 + index * 1000,
            "recording_mbid": f"rec-{index}",
            "artist": "NumArtist",
        }
        for index, word in enumerate(MB_WORDS, start=1)
    ]


def _ensure_table(session) -> None:
    session.execute(text(
        "CREATE TABLE IF NOT EXISTS missing_album_tracks ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  artist_name TEXT NOT NULL, album_name TEXT NOT NULL,"
        "  title TEXT NOT NULL, track_number TEXT, disc_number INTEGER DEFAULT 1,"
        "  track_artist TEXT, year TEXT, release_id TEXT, recording_mbid TEXT,"
        "  duration INTEGER, ignored BOOLEAN NOT NULL DEFAULT 0,"
        "  is_bonus BOOLEAN NOT NULL DEFAULT 0,"
        "  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    ))


def _seed(session, album: str, rows: list[tuple[int, str]], *, durations: bool) -> None:
    _ensure_table(session)
    session.execute(text("DELETE FROM tracks WHERE album = :a"), {"a": album})
    session.execute(
        text("DELETE FROM missing_album_tracks WHERE album_name = :a"), {"a": album}
    )
    for number, word in rows:
        session.execute(
            text(
                "INSERT INTO tracks (id, artist, album_artist, album, title, "
                "file_path, track_number, disc_number, duration) "
                "VALUES (:id, 'NumArtist', 'NumArtist', :al, :ti, :fp, :tn, '1', :dur)"
            ),
            {
                "id": f"{album}-{number}-{word}", "al": album, "ti": word,
                "fp": f"/nonexistent/{album}/{number}.mp3", "tn": str(number),
                "dur": (200 + number) if durations else None,
            },
        )
    session.commit()


def _missing(album: str) -> list[str]:
    from services.metadata import album_missing_service as svc

    original = svc.fetch_musicbrainz_release_metadata
    svc.fetch_musicbrainz_release_metadata = lambda *a, **k: {
        "tracks": _mb_tracks(), "release_year": 2020,
    }
    try:
        result = svc.get_missing_tracks("NumArtist", album, release_mbid="rel-num")
    finally:
        svc.fetch_musicbrainz_release_metadata = original
    return [r["title"] for r in result.get("missing_tracks") or []]


# ---------------------------------------------------------------------------
# The reported case
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("durations", [True, False])
def test_a_mislabelled_track_no_longer_hides_the_track_it_displaced(db_session, durations):
    """Track 4's audio tagged as track 1: 'One' is still genuinely missing."""
    _seed(
        db_session, f"Mislabelled {durations}",
        # positions 1,2,3,5 exist; the file at 1 is actually track Four.
        [(1, "Four"), (2, "Two"), (3, "Three"), (5, "Five")],
        durations=durations,
    )
    missing = _missing(f"Mislabelled {durations}")
    assert "One" in missing, (
        "position 1 is occupied by track Four's audio, so it cannot prove the "
        "release's track One present — this is the reported 'didn't properly "
        "load 1'"
    )
    assert "Four" not in missing, (
        "track Four's audio IS in the library, just mislabelled, so it must not "
        "be offered for download"
    )


def test_the_correct_album_is_unaffected(db_session):
    """A properly numbered album still reports exactly its real gap."""
    _seed(
        db_session, "Correctly Numbered",
        [(1, "One"), (2, "Two"), (4, "Four"), (5, "Five")],
        durations=True,
    )
    assert _missing("Correctly Numbered") == ["Three"]


def test_an_agreeing_title_at_its_own_number_is_present(db_session):
    _seed(
        db_session, "All Present",
        [(i, w) for i, w in enumerate(MB_WORDS, start=1)],
        durations=True,
    )
    assert _missing("All Present") == []


def test_a_differently_worded_title_shares_its_own_position(db_session):
    """A title variant at ITS OWN number must still pair (the existing rule).

    "Heaven Can Wait (live)" at the number the release gives that song is the
    same track; the mislabelling check must not break that.
    """
    from services.metadata import album_missing_service as svc

    _ensure_table(db_session)
    db_session.execute(text("DELETE FROM tracks WHERE album = 'Variant Album'"))
    db_session.execute(
        text("DELETE FROM missing_album_tracks WHERE album_name = 'Variant Album'")
    )
    db_session.execute(
        text(
            "INSERT INTO tracks (id, artist, album_artist, album, title, "
            "file_path, track_number, disc_number, duration) "
            "VALUES ('v1', 'A', 'A', 'Variant Album', 'Song One (live)', "
            "'/nonexistent/v1.mp3', '1', '1', 201)"
        )
    )
    db_session.commit()

    original = svc.fetch_musicbrainz_release_metadata
    svc.fetch_musicbrainz_release_metadata = lambda *a, **k: {
        "tracks": [
            {"title": "Song One", "track_number": "1", "disc_number": 1,
             "duration": 200000, "recording_mbid": "r1", "artist": "A"},
            {"title": "Song Two", "track_number": "2", "disc_number": 1,
             "duration": 210000, "recording_mbid": "r2", "artist": "A"},
        ],
        "release_year": 2020,
    }
    try:
        result = svc.get_missing_tracks("A", "Variant Album", release_mbid="rel-v")
    finally:
        svc.fetch_musicbrainz_release_metadata = original

    titles = [r["title"] for r in result.get("missing_tracks") or []]
    assert "Song One" not in titles, (
        "a title variant at its own number is the same track"
    )
    assert "Song Two" in titles


def test_the_reason_is_counted_for_diagnostics(db_session):
    """The dropped-position gate reports itself rather than vanishing."""
    _seed(
        db_session, "Counted Mislabelled",
        [(1, "Four"), (2, "Two"), (3, "Three"), (5, "Five")],
        durations=True,
    )
    from services.metadata import album_missing_service as svc

    original = svc.fetch_musicbrainz_release_metadata
    svc.fetch_musicbrainz_release_metadata = lambda *a, **k: {
        "tracks": _mb_tracks(), "release_year": 2020,
    }
    try:
        result = svc.get_missing_tracks(
            "NumArtist", "Counted Mislabelled", release_mbid="rel-num")
    finally:
        svc.fetch_musicbrainz_release_metadata = original
    assert (result.get("excluded") or {}).get("mislabelled_position", 0) >= 1
