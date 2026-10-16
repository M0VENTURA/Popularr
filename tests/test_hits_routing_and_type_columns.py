"""The Hits compilation routing, and the one release-type list behind it.

Reported
--------
Two compilations by the same shape rated differently:

* ``Ash - '94-'04: The Singles`` routed to ``[COMPILATION: per-track-artist
  rating]``;
* ``Billy Talent - Hits`` did not — it was scored as a studio album against its
  own tracklist, which tanked the album-relative z-scores and stripped eleven
  GLOBAL 5★ locks down to 4★.

Root cause (two of them, at the same decision point)
----------------------------------------------------
1. ``detect_greatest_hits_album`` matched ``"greatest hits"`` as a full phrase
   plus ``"singles"``, so Ash matched on the word ``singles`` while the BARE word
   ``"Hits"`` matched nothing at all. The test above is titled exactly "Hits".
2. ``prepare_album_context`` (the LIVE rating path) and
   ``_resolve_compilation_flags`` (the end-of-run fallback) read the album's
   release TYPE differently — the live path took ``spotify_album_type`` then
   MusicBrainz with an ``or``-chain, the fallback JOINED five columns. An album
   whose sources disagreed therefore reached opposite verdicts, which is how the
   page could read "Album (Compilation)" while the rating scored a studio
   release.

The fix
-------
One :data:`ALBUM_TYPE_KEYS` list, one :func:`joined_album_type` (a JOIN, never a
chain), and one anchored pattern for the bare ``Hits`` title. Both readers now
consume the same evidence, and the title rule is shared.

The word ``hits`` is anchored to the WHOLE TITLE on purpose: ``release_categories``
also calls this function with a TRACK title, where "Hits Different" is an
ordinary song, and a substring match would re-file the album it sits on.
"""

from __future__ import annotations

import pytest


def _tracks(album, *, count=6, artist="Feuerschwanz", **extra):
    return [
        {"title": f"Track {i}", "artist": artist, "album": album, **extra}
        for i in range(count)
    ]


def _album_context(
    album,
    *,
    artist="Feuerschwanz",
    album_artist=None,
    mb_type=None,
    spotify_type=None,
    releasetype=None,
    tracks=None,
):
    """The LIVE rating path, with the type evidence a scan actually holds."""
    from services.popularity.scan_hooks import prepare_album_context

    if tracks is None:
        tracks = (
            _tracks(album, releasetype=releasetype) if releasetype else _tracks(album)
        )

    return prepare_album_context(
        artist=artist,
        album=album,
        tracks=tracks,
        album_artist=album_artist,
        spotify_album_type=spotify_type,
        musicbrainz_album_type=mb_type,
    )


def _fallback_flags(album, *, album_artist="Feuerschwanz", **row_extra):
    """The end-of-run fallback, given a track row's own columns."""
    from services.popularity.stages.finalise_stage import _resolve_compilation_flags

    row = {
        "title": "Hit",
        "artist": "Feuerschwanz",
        "album": album,
        "album_artist": album_artist,
        **row_extra,
    }
    return _resolve_compilation_flags(
        album_results=[row], artist="Feuerschwanz", album=album
    )


# ===========================================================================
# 1. The bare word "Hits" is a best-of; the word INSIDE a title is not
# ===========================================================================


class TestTheBareHitsTitle:
    @pytest.mark.parametrize(
        "title",
        ["Hits", "The Hits", "HITS", "hits", "Hits (Deluxe)", "Hits [Deluxe]"],
    )
    def test_the_word_alone_names_the_album(self, title):
        from services.catalog.album_classification_service import (
            detect_greatest_hits_album,
        )

        assert detect_greatest_hits_album(title, "Billy Talent") is True

    def test_it_promotes_the_album_in_the_live_rating_path(self):
        """THE REPORTED ALBUM. ``is_single_artist_compilation`` is what routes
        every track into the compilation branch of ``_assign_stars`` — the one
        that can reach 5★ on the artist's catalogue."""
        ctx = _album_context("Hits")

        assert ctx["is_greatest_hits"] is True
        assert ctx["is_compilation"] is True
        assert ctx["is_single_artist_compilation"] is True
        assert ctx["is_va_compilation"] is False

    def test_the_siblings_still_match(self):
        """CONTROL — the five original tokens are untouched."""
        from services.catalog.album_classification_service import (
            detect_greatest_hits_album,
        )

        for title in ("Greatest Hits", "Best of Feuerschwanz", "The Collection",
                      "Anthology", "Singles"):
            assert detect_greatest_hits_album(title, "Artist") is True, title

    @pytest.mark.parametrize(
        "title",
        [
            # ``release_categories`` / ``release_cache_service`` call this with a
            # TRACK title. A substring match for "hits" would re-file the album
            # such a song sits on.
            "Hits Different",
            "Hitsville UK",
            "Hits and Misses",
            # A qualifier BEFORE the word is deliberately NOT matched: the
            # anchored form cannot tell these from the track-title cases above,
            # and a false positive re-files an ordinary song's album. Missing a
            # compilation leaves the album rated exactly as it was.
            "Big Hits of the 80s",
            "Dance Hits",
            "",
        ],
    )
    def test_the_word_inside_other_shapes_is_not(self, title):
        from services.catalog.album_classification_service import (
            detect_greatest_hits_album,
        )

        assert detect_greatest_hits_album(title, "Artist") is False


# ===========================================================================
# 2. Both readers of the release type use the same evidence
# ===========================================================================


class TestBothReadersUseTheSameTypeColumns:
    """⭐ The album page said "Album (Compilation)" while the rating scored a
    studio release, because the two readers disagreed about the same album."""

    def test_a_marking_behind_a_disagreeing_source_survives_the_live_path(self):
        """spotify reports plain "album"; MusicBrainz reports
        "album+compilation". ``spotify or musicbrainz`` stopped at spotify and
        dropped the compilation marking."""
        ctx = _album_context(
            "Memento Mori", spotify_type="album", mb_type="album+compilation",
        )

        assert ctx["is_compilation"] is True
        assert ctx["is_single_artist_compilation"] is True

    def test_the_fallback_reaches_the_same_verdict(self):
        """The same album, through the end-of-run fallback."""
        assert _fallback_flags(
            "Memento Mori",
            spotify_album_type="album",
            musicbrainz_album_type="album+compilation",
        ) == (True, False)

    def test_a_releasetype_column_reaches_the_live_path(self):
        """``releasetype`` is a real TRACK column — the one a Navidrome import
        fills in on a row whose type is still blank. The live path read only the
        two keys the scan passed on, so a compilation marked ONLY in
        ``releasetype`` was invisible to it while the fallback (which reads the
        track rows) saw it."""
        assert _album_context("Memento Mori", releasetype="compilation")[
            "is_compilation"
        ] is True

    def test_a_plain_releasetype_keeps_a_hits_title_plain(self):
        """The same evidence from the other direction: ``releasetype`` that
        positively says "album" IS a type tag, and a tag stays authoritative
        over the title — exactly as ``_resolve_compilation_flags`` decides."""
        ctx = _album_context("Hits", releasetype="album")

        assert ctx["is_compilation"] is False
        assert ctx["is_greatest_hits"] is True, (
            "the display flag still reports the title — only the RATING is "
            "governed by the type tag"
        )

    def test_the_fallback_agrees_the_tag_silences_the_title(self):
        assert _fallback_flags("Hits", releasetype="album") == (False, False)

    def test_both_readers_agree_on_the_two_reported_albums(self):
        """The regression this whole change exists for: the SAME verdict from
        the live path and the end-of-run fallback, for both albums."""
        for album in ("Hits", "'94 - '04: The Singles"):
            live = _album_context(album)
            fallback = _fallback_flags(album)
            assert live["is_single_artist_compilation"] is True, album
            assert fallback == (True, False), album

    def test_an_explicit_type_text_wins(self):
        """A caller that already holds the joined value must not be re-joined —
        the parameter REPLACES the two single-column arguments."""
        from services.popularity.scan_hooks import prepare_album_context

        ctx = prepare_album_context(
            artist="Feuerschwanz",
            album="Memento Mori",
            tracks=_tracks("Memento Mori"),
            musicbrainz_album_type="album",
            type_text="album+compilation",
        )

        assert ctx["is_compilation"] is True


# ===========================================================================
# 3. The shared helpers themselves
# ===========================================================================


class TestJoinedAlbumType:
    def test_it_joins_every_column_in_a_stable_order(self):
        from services.catalog.album_classification_service import (
            ALBUM_TYPE_KEYS,
            joined_album_type,
        )

        row = {
            "musicbrainz_album_type": "album",
            "spotify_album_type": "compilation",
            "releasetype": "album",
        }

        joined = joined_album_type(row)

        assert joined.split() == ["album", "compilation", "album"]
        assert [k for k in joined.split()] == [
            str(row[k]) for k in ALBUM_TYPE_KEYS if row.get(k)
        ]

    def test_both_musicbrainz_spellings_are_read(self):
        """The DB column is ``musicbrainz_albumtype``; the in-memory album row
        built by ``load_stage`` uses ``musicbrainz_album_type``. A reader that
        knows only one spelling is the bug this list exists to prevent."""
        from services.catalog.album_classification_service import joined_album_type

        assert joined_album_type({"musicbrainz_albumtype": "album+compilation"}) == (
            "album+compilation"
        )
        assert joined_album_type({"musicbrainz_album_type": "album+compilation"}) == (
            "album+compilation"
        )

    def test_a_row_with_no_type_is_empty_not_falsy_noise(self):
        """``""`` is what tells a caller "nothing declared a type" — the state
        the greatest-hits title rescue is allowed to fill."""
        from services.catalog.album_classification_service import joined_album_type

        assert joined_album_type({}) == ""
        assert joined_album_type({"album": "Greatest Hits"}) == ""
        assert joined_album_type(None) == ""
        assert joined_album_type("not a mapping") == ""

    def test_the_rows_helper_dedupes_and_unions(self):
        """A scan holds TYPE on the track ROWS, and every row repeats the
        album's type — the union is deduped so the value stays short and
        stable."""
        from services.catalog.album_classification_service import (
            joined_album_type_from_rows,
        )

        rows = [
            {"releasetype": "album"},
            {"releasetype": "album", "musicbrainz_albumtype": "album+compilation"},
            {"releasetype": "album"},
            {"artist": "no type here"},
        ]

        assert joined_album_type_from_rows(rows) == "album album+compilation"

    def test_the_rows_helper_tolerates_junk(self):
        from services.catalog.album_classification_service import (
            joined_album_type_from_rows,
        )

        assert joined_album_type_from_rows(None) == ""
        assert joined_album_type_from_rows([None, "x", 7, {}]) == ""


class TestClassifyAcceptsAPreJoinedTypeText:
    def test_type_text_replaces_the_single_column_arguments(self):
        """The contract the docstring states: when ``type_text`` is given it
        REPLACES spotify/MusicBrainz, so a caller cannot pass both and produce a
        doubled join."""
        from services.catalog.album_classification_service import (
            classify_compilation_category,
        )

        assert classify_compilation_category(
            "A", "B", [], spotify_album_type="compilation", type_text="album",
        ) == ""

    def test_type_text_alone_classifies(self):
        from services.catalog.album_classification_service import (
            classify_compilation_category,
        )

        assert classify_compilation_category(
            "A", "B", [], type_text="album+compilation",
        ) == "single_artist"

    def test_omitting_type_text_keeps_the_old_two_key_join(self):
        """CONTROL — every existing caller that passes only spotify/MusicBrainz
        (``album_tag_sync_service``, ``album_stage``, ``detect_compilation_album``)
        must behave exactly as before."""
        from services.catalog.album_classification_service import (
            classify_compilation_category,
        )

        assert classify_compilation_category(
            "A", "B", [], spotify_album_type="album",
            musicbrainz_album_type="album+compilation",
        ) == "single_artist"
        assert classify_compilation_category(
            "A", "B", [], spotify_album_type="album", musicbrainz_album_type="album",
        ) == ""
