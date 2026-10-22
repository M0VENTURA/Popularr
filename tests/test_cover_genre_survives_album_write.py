"""A detected cover keeps the "Cover" genre.

Reported: *"Can you confirm that the 'Cover' genre is being added to any track
that is detected as a cover song? It doesn't seem to be applying it."*

It WAS being applied — and then destroyed, by the album-wide genre write later
in the SAME scan. The three writes on a cover track, in order:

1. ``track_stage.process_track`` — sets ``is_cover = 1`` and puts "Cover" in
   ``musicbrainz_genres``, and the genre aggregation carries "Cover" into the
   per-track ``genres`` (it is an intercepted filter tag);
2. ``cover_detector_impl`` (the deep pass) — sets ``is_cover = 1`` and adds
   "Cover" to ``musicbrainz_genres``. It never writes the ``genres`` column;
3. ``sync_album_file_tags`` — builds the ALBUM blend and writes it to EVERY
   track: ``UPDATE tracks SET genres = :g WHERE artist = :a AND album = :alb``.

The blend deliberately has the affiliation tags STRIPPED from it (Cover/Live/
Acoustic describe ONE recording, not the album), so step 3 replaced the cover
track's ``genres`` with a list that could not contain "Cover". Measured with
the real function: ``'Cover'`` became ``'Progressive Metal'``.

So the flag survived and the genre did not — visible as "the cover is detected
but the Cover genre is missing".
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from db.engine import db_session
from services.metadata import album_tag_sync_service as svc

ARTIST = "Cover Genre Probe"
ALBUM = "Cover Genre Album"
PREFIX = "covergen-"


# ---------------------------------------------------------------------------
# 1. Reading the markers off a track
# ---------------------------------------------------------------------------

class TestMarkersComeFromTheTracksOwnEvidence:
    def test_the_cover_flag_is_a_marker(self):
        assert svc._track_affiliation_markers({"id": "t", "is_cover": 1}) == ["Cover"]

    def test_the_string_and_bool_forms_count(self):
        for value in (1, True, "1", "true", "True"):
            assert svc._track_affiliation_markers({"is_cover": value}) == ["Cover"], value

    def test_a_cleared_flag_is_not_a_marker(self):
        for value in (0, False, "0", None, ""):
            assert svc._track_affiliation_markers({"is_cover": value}) == [], value

    def test_every_affiliation_flag_maps_to_its_marker(self):
        markers = svc._track_affiliation_markers(
            {"is_cover": 1, "is_live": 1, "is_acoustic": 1, "is_remix": 1}
        )
        assert markers == ["Cover", "Live", "Acoustic", "Remix"]

    def test_a_marker_in_the_tracks_own_sources_is_read(self):
        """A hand-tagged "Live" must not be lost either."""
        assert svc._track_affiliation_markers(
            {"musicbrainz_genres": '["Cover", "Rock"]'}
        ) == ["Cover"]

    def test_a_csv_genre_column_is_parsed(self):
        assert svc._track_affiliation_markers({"genres": "Live, Rock"}) == ["Live"]

    def test_an_ordinary_track_has_no_markers(self):
        assert svc._track_affiliation_markers({"genres": "Rock", "is_cover": 0}) == []

    def test_an_ordinary_genre_containing_a_marker_word_is_not_one(self):
        """Whole-token matching — "Cover Me" is a title, not a genre."""
        assert svc._track_affiliation_markers({"genres": "Cover Band Rock"}) == []

    def test_markers_are_deduplicated(self):
        assert svc._track_affiliation_markers(
            {"is_cover": 1, "musicbrainz_genres": '["Cover"]', "genres": "cover"}
        ) == ["Cover"]


# ---------------------------------------------------------------------------
# 2. The merge — the blend keeps its leading positions
# ---------------------------------------------------------------------------

class TestTheMergeAppends:
    def test_the_marker_is_appended(self):
        assert svc._merge_album_blend_with_affiliations(
            "Progressive Metal", ["Cover"]
        ) == "Progressive Metal, Cover"

    def test_a_marker_already_in_the_blend_is_not_duplicated(self):
        merged = svc._merge_album_blend_with_affiliations(
            "Progressive Metal, cover", ["Cover"]
        )
        assert merged.casefold().count("cover") == 1, merged

    def test_an_empty_blend_still_yields_the_marker(self):
        assert svc._merge_album_blend_with_affiliations("", ["Cover"]) == "Cover"

    def test_no_markers_leaves_the_blend_alone(self):
        assert svc._merge_album_blend_with_affiliations(
            "Progressive Metal", []
        ) == "Progressive Metal"

    def test_the_album_genres_keep_the_leading_positions(self):
        """The playlist builder reads only the first few genres."""
        merged = svc._merge_album_blend_with_affiliations("Rock", ["Cover", "Live"])
        assert merged.split(", ")[0] == "Rock"


# ---------------------------------------------------------------------------
# 3. End to end, through the real sync
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean(db_session):
    db_session.execute(text("DELETE FROM tracks"))
    db_session.commit()
    yield
    db_session.execute(text("DELETE FROM tracks"))
    db_session.commit()


def _insert(rows: list[dict]) -> None:
    with db_session() as session:
        for row in rows:
            session.execute(
                text("""
                    INSERT INTO tracks
                        (id, artist, album_artist, album, title, file_path, genres,
                         musicbrainz_genres, lastfm_tags, discogs_genres, is_cover,
                         original_cover_artist, track_number, disc_number)
                    VALUES (:id, :artist, :artist, :album, :title, :file_path,
                            :genres, :mb, :lf, :dc, :is_cover, :orig, :num, '1')
                """),
                row,
            )
        session.commit()


def _genres(track_id: str) -> str:
    with db_session() as session:
        row = session.execute(
            text("SELECT genres FROM tracks WHERE id = :id"), {"id": track_id}
        ).fetchone()
    return str(row[0] or "") if row else ""


def _cover_rows() -> list[dict]:
    """One detected cover + one ordinary sibling, with aggregatable sources.

    The sibling is load-bearing: without a non-cover source that clears the
    aggregation's confidence floor the album blend is ``[]``, no album-wide
    write happens, and the test would pass for the wrong reason.
    """
    return [
        {
            "id": f"{PREFIX}cover", "artist": ARTIST, "album": ALBUM,
            "title": "Covered Song", "file_path": "/music/cover.flac",
            "genres": "Cover",
            "mb": json.dumps(["Cover", "Progressive Metal"]),
            "lf": json.dumps(["Progressive Metal"]),
            "dc": json.dumps(["Progressive Metal"]),
            "is_cover": 1, "orig": "Original Band", "num": "1",
        },
        {
            "id": f"{PREFIX}plain", "artist": ARTIST, "album": ALBUM,
            "title": "Plain Song", "file_path": "/music/plain.flac",
            "genres": "Progressive Metal",
            "mb": json.dumps(["Progressive Metal"]),
            "lf": json.dumps(["Progressive Metal"]),
            "dc": json.dumps(["Progressive Metal"]),
            "is_cover": 0, "orig": None, "num": "2",
        },
    ]


class TestTheCoverGenreSurvivesTheAlbumWrite:
    def test_a_detected_cover_keeps_its_cover_genre(self):
        """⭐ THE REGRESSION. Measured before the fix: 'Cover' -> blend."""
        _insert(_cover_rows())
        svc.sync_album_file_tags(ARTIST, ALBUM)
        assert "cover" in _genres(f"{PREFIX}cover").casefold(), (
            "the album-wide genre write deleted the detected cover's "
            f"'Cover' genre; got {_genres(f'{PREFIX}cover')!r}"
        )

    def test_the_cover_track_still_gets_the_album_genres_too(self):
        """The marker is APPENDED, not a replacement for the album's list."""
        _insert(_cover_rows())
        svc.sync_album_file_tags(ARTIST, ALBUM)
        genres = _genres(f"{PREFIX}cover").casefold()
        assert "progressive metal" in genres, genres
        assert "cover" in genres, genres

    def test_an_ordinary_sibling_takes_only_the_album_blend(self):
        """CONTROL — the marker must not leak to the non-cover track."""
        _insert(_cover_rows())
        svc.sync_album_file_tags(ARTIST, ALBUM)
        assert "cover" not in _genres(f"{PREFIX}plain").casefold(), (
            "the Cover marker spread to a non-cover track"
        )

    def test_a_live_flag_keeps_its_marker(self):
        rows = _cover_rows()
        rows[0].update({"is_cover": 0, "is_live": 1, "genres": "Live"})
        _insert(rows)
        svc.sync_album_file_tags(ARTIST, ALBUM)
        assert "live" in _genres(f"{PREFIX}cover").casefold()

    def test_an_album_where_nothing_earned_a_marker_still_writes_once(self):
        """CONTROL — the common case keeps the single album-wide statement."""
        rows = _cover_rows()
        rows[0].update({"is_cover": 0, "genres": "Progressive Metal"})
        _insert(rows)
        svc.sync_album_file_tags(ARTIST, ALBUM)
        assert "progressive metal" in _genres(f"{PREFIX}cover").casefold()
        assert "progressive metal" in _genres(f"{PREFIX}plain").casefold()

    def test_the_marker_is_not_written_to_manual_genres(self):
        """``manual_genres`` is a per-track SOURCE; a marker there would stick."""
        _insert(_cover_rows())
        svc.sync_album_file_tags(ARTIST, ALBUM)
        with db_session() as session:
            row = session.execute(
                text("SELECT manual_genres FROM tracks WHERE id = :id"),
                {"id": f"{PREFIX}cover"},
            ).fetchone()
        assert not str(row[0] or "").strip(), (
            "the album sync wrote manual_genres; that fabricates per-track "
            "evidence and makes the value survive every later aggregation"
        )
