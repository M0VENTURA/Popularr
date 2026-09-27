"""Tracks on a VARIOUS-ARTISTS album must keep their OWN genres.

REPORTED
    "On various artists albums, tracks shouldn't get the genre from the album
    only from the tracks."

DEFECT (confirmed against the real code before the fix)
    ``sync_album_file_tags`` aggregated EVERY track's genre sources for the
    album into ONE list and then ran::

        UPDATE tracks SET genres = :g
        WHERE COALESCE(NULLIF(album_artist, ''), artist) = :a AND album = :alb

    On a compilation each track is a different performer with its own genre, so
    seeding a compliant VA album with Thrash Metal / Jazz / Hip Hop left all
    three tracks holding the single value ``'thrash metal, jazz, hip-hop'``.
    The same value was also written onto each track dict, which is what the
    file-tag writer then uses.

THE RULE
    Skip the album-level genre write for a VA album ONLY. A single-artist
    compilation (a greatest-hits, say) genuinely has one artist's genre to
    share, so it must keep the existing behaviour. ``classify_compilation_category``
    returns ``"va"`` for the former and ``"single_artist"`` for the latter, and
    the guard tests for ``"va"`` specifically.

The end-to-end tests deliberately use a PRIMARY genre source (musicbrainz).
``_vote_genres`` ends with a "strict primary confirmation" guardrail that
DELETES any genre voted only by navidrome/essentia, so a fixture built solely
from ``navidrome_genres`` aggregates to ``[]`` and would assert nothing.
"""
from __future__ import annotations

import pytest

VA_ALBUM_ARTIST = "Various Artists"
NORMAL_ARTIST = "VA Isolation Artist"
NORMAL_ALBUM = "VA Isolation Studio Album"
VA_ALBUM = "VA Isolation Now That Is Music"
TAG_ONLY_ALBUM = "VA Isolation Greatest Hits"

PREFIX = "vatest-"


def _insert(session, rows: list[dict]) -> None:  # type: ignore[type-arg]
    """Insert tracks with their own primary + local genre sources.

    Only columns declared on ``db.models.Track`` are used: the unit suite
    builds its schema from the ORM (``Track.__table__.create`` in conftest),
    so a column that exists only in ``db/schema.py`` cannot be inserted here.
    """
    from sqlalchemy import text

    for row in rows:
        session.execute(
            text("""
                INSERT INTO tracks
                    (id, artist, album_artist, album, title, genres,
                     musicbrainz_genres, navidrome_genres, musicbrainz_albumtype,
                     track_number, disc_number)
                VALUES (:id, :artist, :album_artist, :album, :title, :genres,
                        :mb, :nav, :album_type, :num, '1')
            """),
            {
                "id": row["id"],
                "artist": row["artist"],
                "album_artist": row["album_artist"],
                "album": row["album"],
                "title": row["title"],
                # The DB ``genres`` value and the genre SOURCES are set
                # independently: the sources decide what the album-level
                # aggregation would write, while ``genres`` is what the track
                # holds right now. Keeping them distinct is what makes
                # "was written" distinguishable from "was left alone" —
                # seeding both with the same string lets a blanket removal
                # pass the control test.
                "genres": row["db_genre"],
                "mb": '["' + row["source_genre"] + '"]',
                "nav": '["' + row["source_genre"] + '"]',
                "album_type": row.get("album_type"),
                "num": row["num"],
            },
        )
    session.commit()


def _genres_by_id(session, ids: list[str]) -> dict[str, str | None]:
    from sqlalchemy import text

    placeholders = ", ".join(f":i{n}" for n in range(len(ids)))
    rows = session.execute(
        text(f"SELECT id, genres FROM tracks WHERE id IN ({placeholders})"),
        {f"i{n}": v for n, v in enumerate(ids)},
    ).fetchall()
    return {r[0]: r[1] for r in rows}


# ---------------------------------------------------------------------------
# The decision itself — called, so a neutered branch cannot hide
# ---------------------------------------------------------------------------

class TestTheVerdict:
    def test_generic_album_artist_is_various_artists(self):
        from services.metadata.album_tag_sync_service import (
            is_various_artists_album,
        )

        tracks = [
            {"artist": "Metallica", "album_artist": VA_ALBUM_ARTIST},
            {"artist": "Miles Davis", "album_artist": VA_ALBUM_ARTIST},
        ]
        assert is_various_artists_album(tracks, VA_ALBUM_ARTIST, VA_ALBUM) is True

    def test_many_distinct_track_artists_are_various_artists(self):
        """A VA album filed under a non-generic name is still a VA album."""
        from services.metadata.album_tag_sync_service import (
            is_various_artists_album,
        )

        tracks = [
            {"artist": name, "album_artist": "Big Compilation"}
            for name in ("A", "B", "C", "D")
        ]
        assert is_various_artists_album(
            tracks, "Big Compilation", VA_ALBUM
        ) is True

    def test_single_artist_album_is_not_various_artists(self):
        """CONTROL — the rule must not fire on an ordinary album."""
        from services.metadata.album_tag_sync_service import (
            is_various_artists_album,
        )

        tracks = [
            {"artist": "Opeth", "album_artist": "Opeth"} for _ in range(3)
        ]
        assert is_various_artists_album(
            tracks, "Opeth", NORMAL_ALBUM
        ) is False

    def test_single_artist_compilation_is_not_various_artists(self):
        """CONTROL — one artist's compilation still HAS an album genre.

        Pins ``== "va"`` rather than "any compilation": a greatest-hits by a
        single artist must keep the album-level behaviour.
        """
        from services.catalog.album_classification_service import (
            classify_compilation_category,
        )
        from services.metadata.album_tag_sync_service import (
            is_various_artists_album,
        )

        tracks = [
            {"artist": "Opeth", "album_artist": "Opeth",
             "musicbrainz_albumtype": "compilation"}
            for _ in range(3)
        ]
        # The classifier does separate the two...
        assert classify_compilation_category(
            artist="Opeth", album=TAG_ONLY_ALBUM, tracks=tracks,
            album_artist="Opeth", musicbrainz_album_type="compilation",
        ) == "single_artist"
        # ...and the guard must NOT treat it as a VA album.
        assert is_various_artists_album(tracks, "Opeth", TAG_ONLY_ALBUM) is False

    def test_missing_album_artist_and_no_tracks_is_not_various_artists(self):
        from services.metadata.album_tag_sync_service import (
            is_various_artists_album,
        )

        assert is_various_artists_album([], "Opeth", NORMAL_ALBUM) is False


# ---------------------------------------------------------------------------
# The effect on the database
# ---------------------------------------------------------------------------

class TestVerseArtistGenreIsolation:
    """Each track on a VA album keeps its own genre."""

    def test_va_tracks_keep_their_own_genres(self, db_session):
        from services.enrichment.genre_aggregation_service import (
            get_track_recommendations,
        )
        from services.metadata.album_tag_sync_service import (
            sync_album_file_tags,
        )

        own = {
            f"{PREFIX}va-a": "Thrash Metal",
            f"{PREFIX}va-b": "Jazz",
            f"{PREFIX}va-c": "Hip Hop",
        }
        performers = ("Metallica", "Miles Davis", "Kendrick Lamar")
        _insert(db_session, [
            {
                "id": tid, "artist": artist, "album_artist": VA_ALBUM_ARTIST,
                "album": VA_ALBUM, "title": f"Song {artist}",
                "db_genre": own[tid], "source_genre": own[tid],
                "num": str(n),
            }
            for n, (tid, artist) in enumerate(zip(own, performers), 1)
        ])

        # POSITIVE PRECONDITION. The guard is only meaningful if the
        # album-level aggregation would actually have written something: if it
        # aggregated to [], every track would keep its own genre whether the
        # guard existed or not and this test would pass for the wrong reason.
        blended = get_track_recommendations(VA_ALBUM_ARTIST, VA_ALBUM).get("genres")
        assert blended and len(blended) >= 2, (
            "fixture cannot exercise the defect: the album aggregation "
            f"produced {blended!r} instead of a blend of the three genres"
        )
        assert set(blended) != set(own.values()), (
            "the aggregated blend must DIFFER from every track's own genre "
            f"for this test to discriminate; got {blended!r}"
        )

        sync_album_file_tags(VA_ALBUM_ARTIST, VA_ALBUM)

        after = _genres_by_id(db_session, list(own))
        assert after == own, (
            "each track on a various-artists album must keep its own genre, "
            f"got {after!r}"
        )
        # The defect collapsed every track onto ONE album-level value; assert
        # that shape explicitly so a partial regression is still caught.
        assert len(set(after.values())) == 3


class TestNormalAlbumsAreUnaffected:
    """CONTROL — without this, a blanket removal would look 'fixed'."""

    def test_normal_album_tracks_receive_the_album_genre(self, db_session):
        from services.enrichment.genre_aggregation_service import (
            get_track_recommendations,
        )
        from services.metadata.album_tag_sync_service import (
            sync_album_file_tags,
        )

        ids = [f"{PREFIX}norm-{n}" for n in "abc"]
        # SENTINEL: a value the aggregation can never produce, so "the tracks
        # still hold this" proves the write did NOT happen. Seeding the same
        # string the album aggregation writes would let a blanket removal of
        # the write pass this control.
        sentinel = "Sentinel Genre Never Aggregated"
        _insert(db_session, [
            {
                "id": tid, "artist": NORMAL_ARTIST,
                "album_artist": NORMAL_ARTIST, "album": NORMAL_ALBUM,
                "title": f"Track {n}", "db_genre": sentinel,
                "source_genre": "Progressive Metal", "num": str(i),
            }
            for i, (tid, n) in enumerate(zip(ids, "abc"), 1)
        ])

        expected = get_track_recommendations(NORMAL_ARTIST, NORMAL_ALBUM).get(
            "genres"
        )
        assert expected, (
            "precondition: the normal album must aggregate to a genre, "
            "otherwise this control cannot tell a write from a no-op"
        )
        expected_value = ", ".join(expected)

        sync_album_file_tags(NORMAL_ARTIST, NORMAL_ALBUM)

        after = _genres_by_id(db_session, ids)
        assert all(v == expected_value for v in after.values()), (
            "the album-level genre write must still run for a normal album; "
            f"expected {expected_value!r}, got {after!r}"
        )
        assert all(v != sentinel for v in after.values()), (
            "the tracks were left holding the sentinel, so the write did not "
            f"happen at all; got {after!r}"
        )
