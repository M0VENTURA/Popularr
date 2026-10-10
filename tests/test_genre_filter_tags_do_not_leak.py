"""Genre filter tags must not leak onto tracks that do not carry them.

REPORT (2026-10-10): "Something is still saving track genres to album and
attaching genres such as acoustic or cover to non cover songs."

Three confirmed leaks, all in the genre aggregation layer:

1. The ACUTS/Tribute context heuristic. ``_append_extra_genres`` matched
   ``\\b(cover|tribute)\\b`` across title+album, so an ordinary song whose
   title merely contains the word ("Cover Me Now") — and every track of an
   album named "Covers: A Tribute …" — was tagged "Cover". Only the strict
   forms the cover detector itself uses may justify the tag: a trailing
   "(X Cover)" suffix, "cover of", or "originally by", on the TITLE only.

2. A corrupted regex. \u201cde12b3cf\u201d mangled
   ``(live|acoustic|unplugged)`` into ``(live\\vert{}acoustic\\vert{}unplugged)``
   (``\\v`` is a vertical-tab escape in Python), so the live/acoustic
   parenthetical detection never matched and "(Acoustic)" / "(Live)" titles
   silently lost their filter tag.

3. The album blend spreading per-track affiliation tags. ``aggregate_genres``
   appends every ``_FILTER_TAGS`` token found in ANY track's sources, and the
   album-wide ``UPDATE tracks SET genres`` in ``sync_album_file_tags`` wrote
   that blend to every track — one genuine cover's "cover", or one track's
   Last.fm "acoustic", stamped the whole album. The album blend
   (``get_track_recommendations``) and the album-genres inheritance helper
   (``track_stage._album_top_genres``) now strip the per-track affiliation
   tags; the Christmas family remains (a Christmas album is an album property
   and the genre-playlist builder relies on it surviving its 3-genre window).
"""

from __future__ import annotations

from contextlib import contextmanager

from services.enrichment.genre_aggregation_service import (
    aggregate_genres,
    get_track_recommendations,
)


def _album_recommendations_from_row(columns: list[list[str]]) -> list[str]:
    """Drive get_track_recommendations with a fake session.

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
            return [_Row(list(c)) for c in columns]

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
        return get_track_recommendations("Artist", "Album").get("genres") or []
    finally:
        gas.db_session = original


class TestCoverIsStrict:
    def test_the_word_in_a_title_is_not_a_cover(self):
        out = aggregate_genres(
            {"musicbrainz": ["rock"], "lastfm": ["rock"]},
            max_genres=2,
            context_title="Cover Me Now",
            context_album="Greatest Hits",
        )
        assert "Cover" not in out, "the word 'cover' in a title is not evidence"

    def test_an_album_named_covers_does_not_tag_its_tracks(self):
        out = aggregate_genres(
            {"musicbrainz": ["rock"]},
            max_genres=2,
            context_title="Nothing",
            context_album="Covers: A Tribute Album",
        )
        assert "Cover" not in out, (
            "an album whose NAME says covers must not tag every track; "
            "the cover affiliation belongs to the track, not the album"
        )

    def test_a_real_suffix_is_still_tagged(self):
        out = aggregate_genres(
            {"musicbrainz": ["rock"]},
            max_genres=2,
            context_title="Ruby (Kenny Rogers Cover)",
            context_album="B-Sides",
        )
        assert "Cover" in out, "a genuine '(X Cover)' title must keep its tag"

    def test_cover_of_is_still_tagged(self):
        out = aggregate_genres(
            {"musicbrainz": ["rock"]},
            max_genres=2,
            context_title="Song (cover of Original)",
            context_album="Album",
        )
        assert "Cover" in out


class TestLiveAcousticRegexRestored:
    def test_acoustic_title_gets_the_live_filter(self):
        out = aggregate_genres(
            {"musicbrainz": ["rock"]},
            max_genres=2,
            context_title="Blackbird (Acoustic)",
            context_album="Album",
        )
        assert "Live" in out, (
            "the parenthetical alternation was corrupted by de12b3cf "
            "((live\\vert{}acoustic\\vert{}unplugged) never matches); "
            "(Acoustic) titles must be tagged again"
        )

    def test_unplugged_title_gets_the_live_filter(self):
        out = aggregate_genres(
            {"musicbrainz": ["rock"]},
            max_genres=2,
            context_title="Song (Unplugged)",
            context_album="Album",
        )
        assert "Live" in out

    def test_plain_track_is_untouched(self):
        out = aggregate_genres(
            {"musicbrainz": ["rock"]},
            max_genres=2,
            context_title="Plain Song",
            context_album="Album",
        )
        assert "Live" not in out


class TestAlbumBlendNeverSpreadsTrackAffiliation:
    def test_one_tracks_cover_source_does_not_stamp_the_album(self):
        # musicbrainz column carries "cover" on ONE row of the album.
        out = _album_recommendations_from_row(
            [["[]", '["rock", "cover"]', "[]", "[]", "[]", "[]", "[]", "[]", "[]", "[]"]]
        )
        assert "Cover" not in out, (
            "the album-wide genre write must not spread one track's cover "
            "affiliation to every track"
        )
        assert "rock" in out

    def test_one_tracks_acoustic_source_does_not_stamp_the_album(self):
        out = _album_recommendations_from_row(
            [["[]", '["rock", "acoustic"]', "[]", "[]", "[]", "[]", "[]", "[]", "[]", "[]"]]
        )
        assert "Acoustic" not in out
        assert "Live" not in out
        assert "rock" in out

    def test_christmas_is_still_album_valid(self):
        out = _album_recommendations_from_row(
            [["[]", '["rock", "christmas"]', "[]", "[]", "[]", "[]", "[]", "[]", "[]", "[]"]]
        )
        assert "Christmas" in out, (
            "a Christmas album IS an album property; the genre-playlist "
            "builder detects it from the stored value"
        )
        assert "rock" in out


class TestAlbumTopGenresInheritanceStripsAffiliation:
    def _helper(self, monkeypatch):
        from services.popularity.stages import track_stage

        # A nonzero-but-tiny min weight: ``min_weight`` 0.0 is swallowed by
        # the ``or 0.25`` default in ``_genre_min_weight`` (pre-existing,
        # unrelated to this report), so 0.01 lets the voted genre through.
        def _cfg():
            return {"genres": {"min_weight": 0.01}}

        monkeypatch.setattr(
            "helpers.config_helpers.get_config", _cfg, raising=False,
        )
        return track_stage._album_top_genres

    def test_one_tracks_cover_does_not_inherit_to_siblings(self, monkeypatch):
        helper = self._helper(monkeypatch)
        out = helper(
            [{"title": "A", "musicbrainz_genres": '["cover", "nu metal"]'}],
            max_genres=3,
        )
        assert "cover" not in {g.lower() for g in out}, (
            "the album-genres inheritance list must not hand a cover "
            "affiliation to every sparse track"
        )
        assert "nu metal" in out

    def test_admin_siblings_are_filtered(self, monkeypatch):
        """Pinned contract: 'cover'/'live' never appear in album top genres."""
        helper = self._helper(monkeypatch)
        out = helper(
            [{"title": "A", "musicbrainz_genres": '["cover", "live", "nu metal"]'}],
            max_genres=3,
        )
        assert "nu metal" in out
        assert "cover" not in {g.lower() for g in out}
        assert "live" not in {g.lower() for g in out}