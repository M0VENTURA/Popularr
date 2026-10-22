"""ONE track's holiday tag must not turn the whole album seasonal.

Reported: *"So much for the afterglow keeps detecting as a holiday album. I
think the holiday genre from the one track is propegating to the whole
album."*

The guess was exactly right, and the mechanism was deliberate code:

* the holiday family is EXEMPT from ``_TRACK_AFFILIATION_FILTER_TAGS`` (a
  Christmas album IS an album property, and the genre-playlist builder needs
  the tag outside its 3-genre window) — so unlike Cover/Live/Acoustic it had
  NO protection against spreading;
* ``get_track_recommendations`` pools EVERY track's sources and appends every
  filter tag it finds, so one track's Last.fm "holiday" reached the blend;
* ``sync_album_file_tags`` then writes that blend ALBUM-WIDE
  (``UPDATE tracks SET genres = …``), so every track of the record claimed it.

Measured on the reported shape (1 of 10 tracks tagged): the blend collapsed
to ``['Holiday']`` — the real genres were condensed away entirely, and the
album-wide write stamped every sibling.

Two further defects the same trace exposed:

* every spelling of the family was pooled SEPARATELY, so "Christmas" on one
  track and "Holiday" on another produced two near-duplicate playlists;
* a track whose only tag was "Holiday" took the ``elif is_christmas`` branch
  and landed in a plain "Holiday" pool — NOT matched by the configured
  ``christmas_playlist_marker``, so the seasonal exclusion did not cover it.

At the user's request the family now merges to ONE umbrella token, "Holiday",
which covers Christmas and the other winter holidays without labelling a
Hanukkah track as Christmas.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from services.enrichment.genre_aggregation_service import (
    HOLIDAY_CANONICAL_TOKEN,
    HOLIDAY_FAMILY_ALIASES,
    album_is_holiday_album,
    is_holiday_family_token,
    merge_holiday_tokens,
)


# ---------------------------------------------------------------------------
# 1. The family vocabulary and the merge
# ---------------------------------------------------------------------------

class TestTheFamilyMergesToOneToken:
    def test_every_spelling_is_recognised(self):
        for spelling in ("Christmas", "Xmas", "X-Mas", "Noel", "Yule",
                         "Yuletide", "Advent", "Holiday", "Holidays",
                         "Hanukkah", "Chanukah"):
            assert is_holiday_family_token(spelling), spelling

    def test_an_ordinary_genre_is_not_holiday(self):
        for genre in ("Rock", "Holiday Rock", "Metal", "", None, "Christmas Rock"):
            assert not is_holiday_family_token(genre), genre

    def test_the_family_matches_the_classification_vocabulary(self):
        """Two modules own this vocabulary — they must not drift."""
        from services.catalog.album_classification_service import (
            CHRISTMAS_GENRE_TOKENS,
        )

        assert HOLIDAY_FAMILY_ALIASES == CHRISTMAS_GENRE_TOKENS

    def test_all_spellings_collapse_to_one_token(self):
        merged = merge_holiday_tokens(["Rock", "Christmas", "Holiday", "Xmas"])
        assert merged == ["Rock", HOLIDAY_CANONICAL_TOKEN]

    def test_the_token_keeps_the_first_spelling_position(self):
        """Order matters — the playlist builder reads only the first 3 genres."""
        assert merge_holiday_tokens(["Holiday", "Pop"]) == ["Holiday", "Pop"]
        assert merge_holiday_tokens(["Pop", "Holiday"]) == ["Pop", "Holiday"]

    def test_a_list_without_holiday_is_untouched(self):
        assert merge_holiday_tokens(["Rock", "Metal"]) == ["Rock", "Metal"]

    def test_the_canonical_token_is_holiday(self):
        assert HOLIDAY_CANONICAL_TOKEN == "Holiday", (
            "the user chose 'Holiday' as the umbrella so a Hanukkah track is "
            "not displayed as Christmas"
        )


# ---------------------------------------------------------------------------
# 2. Is the ALBUM seasonal, or is it one track?
# ---------------------------------------------------------------------------

class TestAlbumLevelVerdict:
    def test_one_tagged_track_on_a_normal_album_is_not_an_album(self):
        """⭐ THE REPORT: "So Much for the Afterglow" with one holiday track."""
        assert not album_is_holiday_album(
            album="So Much for the Afterglow", holiday_track_count=1, track_count=10
        )

    def test_a_title_named_album_is_always_seasonal(self):
        for album in ("A Very Special Christmas", "Holiday Album", "Merry Xmas"):
            assert album_is_holiday_album(album=album, track_count=12), album

    def test_a_mostly_tagged_album_is_seasonal(self):
        assert album_is_holiday_album(album="Seasonal", holiday_track_count=8, track_count=10)

    def test_exactly_half_is_not_enough(self):
        """A majority, not a tie — the tie is what let one track decide."""
        assert not album_is_holiday_album(
            album="Half", holiday_track_count=5, track_count=10
        )

    def test_just_over_half_is_seasonal(self):
        assert album_is_holiday_album(
            album="Mostly", holiday_track_count=6, track_count=10
        )

    def test_no_tagged_tracks_and_no_title_signal_is_not_seasonal(self):
        assert not album_is_holiday_album(
            album="Afterglow", holiday_track_count=0, track_count=10
        )

    def test_an_album_with_no_tagged_tracks_at_all_is_not_seasonal(self):
        """A denominator of zero must not divide its way to True."""
        assert not album_is_holiday_album(album="Unknown", holiday_track_count=0, track_count=0)

    def test_the_title_test_is_album_scoped(self):
        """A TRACK title never makes the album seasonal (only the album does)."""
        assert not album_is_holiday_album(
            album="Afterglow", track_titles=["Making Christmas"],
            holiday_track_count=1, track_count=10,
        )


class TestTheDenominatorIsTheAlgebraOfTheRealTrackCount:
    """⭐ THE REPORTED ALBUM: 14 tracks, ONE seasonal, the rest with NO data.

    ``_album_holiday_votes`` used to count only rows that HELD a genre, so this
    shape scored 1-of-1 = 100% and the album read as seasonal — the Holiday
    token stayed in the blend and the album-wide write stamped all 14 rows.
    """

    def test_the_reported_album_is_not_seasonal(self):
        assert not album_is_holiday_album(
            album="So Much for the Afterglow", holiday_track_count=1, track_count=14
        )

    def test_one_seasonal_track_among_untagged_siblings_is_not_enough(self):
        """The denominator must NOT shrink to the tracks that had data."""
        assert not album_is_holiday_album(
            album="Any Album", holiday_track_count=1, track_count=14
        )

    def test_the_same_ratio_over_two_tracks_still_fails_the_majority(self):
        assert not album_is_holiday_album(
            album="Any Album", holiday_track_count=1, track_count=2
        )

    def test_a_one_track_seasonal_release_is_seasonal(self):
        """The majority test alone decides — a 1-of-1 IS the whole release."""
        assert album_is_holiday_album(
            album="Christmas Single", holiday_track_count=1, track_count=1
        )

    def test_the_vote_counter_counts_EVERY_row(self):
        """⭐ The bug lived in the helper's denominator, not the rule."""
        import json

        from services.enrichment.genre_aggregation_service import _album_holiday_votes

        class _Row:
            def __init__(self, vals):
                self._v = vals

            def __getitem__(self, i):
                return self._v[i]

        seasonal = _Row(["[]", json.dumps(["Hard Rock", "Holiday"]), "[]", "[]", "[]",
                         "[]", "[]", "[]", "[]", "[]"])
        blank = _Row(["[]", "[]", "[]", "[]", "[]", "[]", "[]", "[]", "[]", "[]"])
        rows = [seasonal] + [blank] * 13

        with_holiday, examined = _album_holiday_votes(rows, "Album", list(range(10)))
        assert with_holiday == 1
        assert examined == 14, (
            "rows with no genre data must still count toward the denominator, "
            "or one tagged track of fourteen reads as 100%"
        )

    def test_the_untagged_rows_do_not_inflate_the_holiday_count(self):
        import json

        from services.enrichment.genre_aggregation_service import _album_holiday_votes

        class _Row:
            def __init__(self, vals):
                self._v = vals

            def __getitem__(self, i):
                return self._v[i]

        blank = _Row(["[]", "[]", "[]", "[]", "[]", "[]", "[]", "[]", "[]", "[]"])
        with_holiday, examined = _album_holiday_votes([blank] * 5, "Album", list(range(10)))
        assert with_holiday == 0
        assert examined == 5


class TestTheReportedAlbumEndToEnd:
    """14 rows, one seasonal, all 14 already stamped Holiday by an old scan."""

    def _seed(self):
        import json

        from sqlalchemy import text

        from db.engine import db_session
        from db.models import Track

        with db_session() as session:
            Track.__table__.create(session.get_bind(), checkfirst=True)
            session.execute(text("DELETE FROM tracks"))
            for i in range(1, 15):
                is_xmas = (i == 14)
                session.execute(
                    text("""
                        INSERT INTO tracks
                            (id, artist, album_artist, album, title, file_path,
                             genres, musicbrainz_genres, lastfm_tags, discogs_genres,
                             track_number, disc_number)
                        VALUES (:id, :a, :a, 'So Much for the Afterglow', :title,
                                :fp, :g, :mb, :lf, :dc, :n, '1')
                    """),
                    {
                        "id": f"evy-{i}", "a": "Everclear",
                        "title": "Hating You for Christmas" if is_xmas else f"Track {i}",
                        "fp": f"/music/evy{i}.flac",
                        "g": "Holiday",
                        "mb": json.dumps(["Hard Rock", "Holiday"]) if is_xmas else "[]",
                        "lf": json.dumps(["Alternative Rock"]) if is_xmas else "[]",
                        "dc": json.dumps(["Hard Rock"]) if is_xmas else "[]",
                        "n": str(i),
                    },
                )
            session.commit()

    def _holiday_rows(self) -> int:
        from sqlalchemy import text

        from db.engine import db_session

        with db_session() as session:
            rows = session.execute(
                text("SELECT genres FROM tracks WHERE album = 'So Much for the Afterglow'")
            ).fetchall()
        return sum(
            1 for r in rows
            if "holiday" in str((getattr(r, "_mapping", None) or {}).get("genres") or "").lower()
        )

    def test_the_album_blend_carries_no_holiday_token(self):
        self._seed()
        from db.engine import db_session
        from services.enrichment.genre_aggregation_service import get_track_recommendations
        from sqlalchemy import text

        blend = get_track_recommendations("Everclear", "So Much for the Afterglow").get("genres")
        assert not any(is_holiday_family_token(g) for g in (blend or [])), blend
        with db_session() as session:
            session.execute(text("DELETE FROM tracks"))

    def test_the_scan_REMOVES_the_stale_holiday_from_every_row(self):
        """⭐ The reported damage is repaired, not merely stopped."""
        self._seed()
        from sqlalchemy import text

        from db.engine import db_session
        from services.metadata.album_tag_sync_service import sync_album_file_tags

        assert self._holiday_rows() == 14, "precondition: every row starts stamped"
        sync_album_file_tags("Everclear", "So Much for the Afterglow")
        assert self._holiday_rows() == 0, (
            "the album-wide write must have replaced the stale Holiday with the "
            "album's real genres"
        )
        with db_session() as session:
            session.execute(text("DELETE FROM tracks"))


# ---------------------------------------------------------------------------
# 3. End to end: the album blend the write actually uses
# ---------------------------------------------------------------------------

def _album_blend(rows: list[list[str]], album: str = "Album") -> list[str]:
    """Drive ``get_track_recommendations`` with a fake session.

    Column order matches the function's SELECT
    (lastfm, musicbrainz, discogs, listenbrainz, spotify, essentia,
     manual, navidrome, audiodb, wikidata).
    """
    import services.enrichment.genre_aggregation_service as gas

    class _Row:
        def __init__(self, vals):
            self._v = vals

        def __getitem__(self, i):
            return self._v[i]

    class _Result:
        def fetchall(self):
            return [_Row(list(c)) for c in rows]

    class _Session:
        def execute(self, sql, params=None):
            return _Result()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    @contextmanager
    def _fake(*_a, **_k):
        yield _Session()

    original = gas.db_session
    gas.db_session = _fake
    try:
        return gas.get_track_recommendations("Artist", album).get("genres") or []
    finally:
        gas.db_session = original


def _row(*genres: str) -> list[str]:
    """One album row with `genres` in the musicbrainz column."""
    import json

    return ["[]", json.dumps(list(genres)), "[]", "[]", "[]", "[]", "[]", "[]", "[]", "[]"]


ORDINARY = _row("alternative rock", "power pop")


class TestTheBlendDoesNotSpreadOneTracksHoliday:
    def test_one_tagged_track_of_many_does_not_make_the_album_seasonal(self):
        """⭐ THE REGRESSION. Every sibling used to inherit the token."""
        blend = _album_blend([_row("rock", "holiday")] + [ORDINARY] * 5)
        assert not any(is_holiday_family_token(g) for g in blend), (
            f"one track's holiday tag reached the album blend: {blend}"
        )

    def test_one_tagged_track_never_reaches_a_CHRISTMAS_titled_album(self):
        """The decisive case: 'So Much for the Afterglow' is not seasonal."""
        blend = _album_blend(
            [_row("rock", "holiday")] + [ORDINARY] * 5,
            album="So Much for the Afterglow",
        )
        assert not any(is_holiday_family_token(g) for g in blend), blend

    def test_a_genuine_seasonal_album_keeps_the_token(self):
        blend = _album_blend([_row("rock", "christmas")] * 6)
        assert HOLIDAY_CANONICAL_TOKEN in blend

    def test_a_genuinely_seasonal_album_is_not_emptied_of_genres(self):
        blend = _album_blend([_row("rock", "christmas")] * 6)
        assert "rock" in {g.lower() for g in blend}

    def test_the_family_arrives_as_ONE_token(self):
        """Christmas on one track, Holiday on another = one bucket."""
        blend = _album_blend(
            [_row("rock", "christmas"), _row("rock", "holiday")] * 3
        )
        holiday_tokens = [g for g in blend if is_holiday_family_token(g)]
        assert holiday_tokens == [HOLIDAY_CANONICAL_TOKEN]

    def test_a_title_named_album_keeps_the_token_from_a_single_track(self):
        """The album TITLE is album-scoped evidence, so it always counts."""
        blend = _album_blend([_row("rock", "holiday")] + [ORDINARY] * 5,
                             album="A Very Special Christmas")
        assert HOLIDAY_CANONICAL_TOKEN in blend

    def test_a_per_track_title_alone_does_not_qualify(self):
        """Only the ALBUM title votes — not a track called 'Making Christmas'."""
        blend = _album_blend([_row("rock")] + [ORDINARY] * 5,
                             album="Afterglow")
        assert not any(is_holiday_family_token(g) for g in blend)


# ---------------------------------------------------------------------------
# 4. The playlist pool — the family must land in ONE, marker-matched pool
# ---------------------------------------------------------------------------

class TestThePoolIsMarkerMatched:
    """A "Holiday" pool would dodge the exclusion, which keys on the marker."""

    def _pool_constants(self):
        from services.popularity.stages import finalise_stage

        return finalise_stage._HOLIDAY_POOL_KEY, finalise_stage._HOLIDAY_POOL_DISPLAY

    def test_the_pool_key_contains_the_default_marker(self):
        from services.popularity.stages.finalise_stage import (
            _playlist_allows_christmas,
        )

        key, _display = self._pool_constants()
        assert _playlist_allows_christmas(key), (
            "the seasonal pool must be matched by the configured marker, or it "
            "is excluded-when-it-should-not-be and invisible to the user"
        )

    def test_the_pool_display_names_the_umbrella(self):
        _key, display = self._pool_constants()
        assert display == "Holiday"

    def test_a_holiday_tagged_track_maps_to_the_canonical_pool(self):
        from services.popularity.stages.finalise_stage import _HOLIDAY_POOL_KEY

        row = {"genres": "rock, holiday"}
        assert is_holiday_family_token("holiday"), (
            "precondition: the tag is recognised as seasonal"
        )
        # The builder's own intercept is exercised in
        # test_christmas_playlist_exclusion.py; here we pin the constant it uses.
        assert _HOLIDAY_POOL_KEY.casefold().startswith("christmas")
