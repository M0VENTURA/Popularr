"""A greatest-hits album rates on the ARTIST catalogue — tag or no tag.

Reported
--------
> Artist greatest hits compilations are meant to be scored on the artist
> catalogue with all tracks being able to score 5 star.

Root cause
----------
``classify_compilation_category`` reads the type tag, the generic album-artist
names and a "4+ credited artists" heuristic — never the TITLE. A "Greatest
Hits" album whose MusicBrainz/Spotify match carried no compilation marking
therefore classified as ``""`` (not a compilation) and was rated on the
studio-album ladder: the album's own median band, the album-rank gate and the
5★ slot cap. Measured on such an album every track sat on the album's own band
(median ≈ 50) and almost nothing could reach 5★ — even though the album is
nothing but the artist's hits.

The fix
-------
``prepare_album_context`` promotes a greatest-hits TITLE to
``is_compilation`` / ``is_single_artist_compilation`` (guarded so a
Various-Artists record keeps its VA flags), which routes every track into the
compilation branch of ``_assign_stars`` — the branch that rates on the
artist's catalogue, locally when the library holds enough of it and otherwise
against the artist's online chart. ``_resolve_compilation_flags`` (the
end-of-run finalise fallback) applies the same title rule, so a re-finalise
cannot quietly undo the rating the scan just posted.

The title is a FALLBACK, never an override: a matched album type stays
authoritative (``test_a_plain_type_tag_wins_over_the_title``), exactly like
the live verdict in the same function and the album page's own
type-first category reader. Only an album with NO type at all lets the title
decide — which is precisely the album that was rated as a studio album.
"""

from __future__ import annotations

import pytest

#: Matches ``_DEFAULT_COMPILATION_ONLINE_CATALOGUE``: rank-percentile bands.
RULES = {
    "enabled": 1,
    "min_local_catalogue": 5,
    "rank_percentile_5star": 0.02,
    "rank_percentile_4star": 0.10,
    "rank_percentile_3star": 0.35,
    "rank_percentile_2star": 0.65,
    "min_catalogue_size": 5,
}

#: One representative title per token ``detect_greatest_hits_album`` matches.
GREATEST_HITS_TITLES = [
    "Greatest Hits",
    "Best of Feuerschwanz",
    "The Collection",
    "Anthology",
    "Singles",
]


def _tracks(album, *, count=10, artist="Feuerschwanz"):
    return [
        {"title": f"Track {i}", "artist": artist, "album": album}
        for i in range(count)
    ]


def _album_context(
    album,
    *,
    artist="Feuerschwanz",
    album_artist=None,
    mb_type=None,
    tracks=None,
):
    from services.popularity.scan_hooks import prepare_album_context

    return prepare_album_context(
        artist=artist,
        album=album,
        tracks=tracks if tracks is not None else _tracks(album),
        album_artist=album_artist,
        musicbrainz_album_type=mb_type,
    )


def _hit_track(score, *, title="Mid Album Hit", album="Greatest Hits"):
    """A plausible track row; NOT a detected single, so no single floor can
    rescue it on the studio ladder — the rating must come from the catalogue."""
    return {
        "track_id": f"id-{title.lower().replace(' ', '-')}",
        "artist": "Feuerschwanz", "album": album, "title": title,
        "popularity_score": float(score), "final_score": float(score),
        "lastfm_listeners": int(score * 500),
        "listenbrainz_listens": int(score * 90),
        "lastfm_score": 60.0, "listenbrainz_score": 60.0,
        "is_single": False, "single_confidence": "",
        "single_sources": "", "is_live": False,
        "popularity_marked": False, "exclude_from_stats": False,
    }


# ===========================================================================
# 1. The title is enough evidence to call it a compilation
# ===========================================================================


class TestTheTitlePromotesTheAlbumToACompilation:
    @pytest.mark.parametrize("title", GREATEST_HITS_TITLES)
    def test_a_hits_title_is_a_single_artist_compilation(self, title):
        ctx = _album_context(title)

        assert ctx["is_compilation"] is True
        assert ctx["is_single_artist_compilation"] is True
        assert ctx["is_va_compilation"] is False

    def test_the_title_fallback_matches_the_pages_category(self):
        """``is_greatest_hits`` is what the album page's category fallback
        reads. With no type tag to go on, the rating must read the title the
        same way instead of scoring the album as a studio release."""
        ctx = _album_context("Greatest Hits")

        assert ctx["is_greatest_hits"] is True
        assert ctx["is_compilation"] is True

    def test_a_studio_album_is_untouched(self):
        """CONTROL — the promotion must not leak onto ordinary albums."""
        ctx = _album_context("Memento Mori")

        assert ctx["is_compilation"] is False
        assert ctx["is_single_artist_compilation"] is False
        assert ctx["is_greatest_hits"] is False

    def test_a_plain_type_tag_wins_over_the_title(self):
        """The title is a FALLBACK, never an override — the convention the
        live verdict two lines above it in ``prepare_album_context`` uses. An
        album the sources call plain "album" stays a studio album whatever its
        title reads, which is also how the album page reads the stored type,
        so the badge and the rating cannot contradict each other."""
        ctx = _album_context("Greatest Hits", mb_type="album")

        assert ctx["is_compilation"] is False
        assert ctx["is_single_artist_compilation"] is False
        assert ctx["is_greatest_hits"] is True, (
            "the display flag still reports the title — only the RATING is "
            "governed by the type tag"
        )

    def test_a_real_best_of_with_plain_tags_stays_plain(self):
        """REGRESSION — the case that caught the first cut of this fix, which
        let the title override the tag: a genuine best-of whose MusicBrainz and
        Spotify types agree on plain "album"
        (``test_compilation_cover_and_rating`` pins the same album)."""
        ctx = _album_context(
            "Fingerprints: The Best of Powderfinger 1994-2000",
            artist="Powderfinger", mb_type="album",
        )

        assert ctx["is_compilation"] is False

    def test_a_compilation_tag_still_gets_the_same_verdict(self):
        """CONTROL — the tagged path is unchanged by the new rule."""
        ctx = _album_context("Memento Mori", mb_type="album+compilation")

        assert ctx["is_compilation"] is True
        assert ctx["is_single_artist_compilation"] is True


class TestVAAlwaysWins:
    """A Various-Artists record is NEVER promoted to single-artist."""

    def test_a_va_folder_titled_greatest_hits_keeps_va_flags(self):
        ctx = _album_context(
            "Greatest Hits", artist="Various Artists", album_artist="Various Artists"
        )

        assert ctx["is_va_compilation"] is True
        assert ctx["is_single_artist_compilation"] is False

    def test_the_four_artists_heuristic_still_wins(self):
        """Four credited artists, none of them the folder's artist: the VA
        heuristic fires, and the "Greatest Hits" title must not override it
        into a single-artist compilation."""
        tracks = [
            {"title": f"T{i}", "artist": f"Band {i}", "album": "Greatest Hits"}
            for i in range(4)
        ] + [{"title": "T4", "artist": "Band 0", "album": "Greatest Hits"}]

        ctx = _album_context("Greatest Hits", artist="Feuerschwanz", tracks=tracks)

        assert ctx["is_va_compilation"] is True
        assert ctx["is_single_artist_compilation"] is False


# ===========================================================================
# 2. The rating itself: the artist catalogue, not the album's own band
# ===========================================================================


class TestTheRatingRunsOnTheArtistCatalogue:
    """``_assign_stars`` receives the context's verdict — the promotion is only
    worth anything if the star rating changes with it."""

    #: The album sits at the TOP of its own band (95→90), so a track at 90.0 is
    #: ranked last on its own album. The artist's online chart is where it is
    #: the global #1 (rank 1 of 200 = top 0.5%).
    ALBUM_SCORES = [95.0, 94.0, 93.0, 92.0, 91.0, 90.0]

    def _assign(self, ctx, monkeypatch, track):
        import services.popularity.popularity_sources as ps
        from services.popularity.stages import finalise_stage as fs

        monkeypatch.setattr(
            "helpers.config_helpers.get_compilation_online_catalogue_config",
            lambda: RULES,
        )
        monkeypatch.setattr(
            ps, "get_online_artist_track_rank",
            lambda artist, title, lastfm_client=None: {
                "rank": 1, "total": 200, "percentile": 0.005,
                "listeners": 10_000, "top_listeners": 10_000,
                "source": "lastfm_artist_top_tracks",
            },
        )
        album_model = {
            "has_benchmark": True, "era": "peak",
            "catalog_cutoff": 95.0, "max_5star_slots": 4, "album_top_n": 3,
        }
        return fs._assign_stars(
            track, list(self.ALBUM_SCORES), list(self.ALBUM_SCORES),
            album_model=album_model,
            is_compilation=bool(ctx["is_compilation"]),
        )

    def test_a_mid_album_track_can_reach_five_stars(self, monkeypatch):
        """THE REPORTED CASE. The album is the artist's hits; a track that is
        ranked last on the album (but is the artist's global #1) reaches 5★
        because the rating runs on the artist catalogue."""
        ctx = _album_context("Greatest Hits")
        track = _hit_track(90.0)

        stars = self._assign(ctx, monkeypatch, track)

        assert stars == 5
        assert track["_compilation_rating_mode"] == "track_artist_online_catalogue"

    def test_the_same_track_on_a_studio_album_is_capped(self, monkeypatch):
        """CONTROL — without the promotion the studio ladder applies: the track
        is last on its own album and below the catalog cutoff, so it cannot
        reach 5★ however popular the artist's chart says it is."""
        ctx = _album_context("Memento Mori")
        track = _hit_track(90.0, album="Memento Mori")

        stars = self._assign(ctx, monkeypatch, track)

        assert stars < 5
        assert "_compilation_rating_mode" not in track, (
            "the studio ladder must not record a compilation rating mode"
        )


# ===========================================================================
# 3. The end-of-run finalise agrees (otherwise it re-rates the album)
# ===========================================================================


class TestTheFinaliseFallbackAgrees:
    """``_resolve_compilation_flags`` is the FALLBACK path used by
    ``finalise_scan``'s grouping when no album context is at hand. If it
    disagreed with ``prepare_album_context``, an end-of-run finalise would
    re-rate a greatest-hits album as a studio album — the same class of bug
    that path's type-tag/VA rules exist to prevent."""

    def _flags(self, album, *, album_artist="Feuerschwanz", type_text=""):
        from services.popularity.stages.finalise_stage import (
            _resolve_compilation_flags,
        )

        row = {
            "title": "Hit", "artist": "Feuerschwanz", "album": album,
            "album_artist": album_artist,
            "musicbrainz_album_type": type_text,
        }
        return _resolve_compilation_flags(
            album_results=[row], artist="Feuerschwanz", album=album
        )

    def test_a_tagless_hits_title_is_a_compilation(self):
        assert self._flags("Greatest Hits") == (True, False)

    def test_a_studio_album_is_not(self):
        """CONTROL."""
        assert self._flags("Memento Mori") == (False, False)

    def test_va_still_wins(self):
        assert self._flags("Greatest Hits", album_artist="Various Artists") == (
            True, True,
        )

    def test_a_compilation_tag_still_wins(self):
        """CONTROL — the tagged path is unchanged."""
        assert self._flags("Memento Mori", type_text="album+compilation") == (
            True, False,
        )

    def test_a_plain_type_tag_wins_over_the_title(self):
        """CONTROL — mirrors ``prepare_album_context``: the tag is
        authoritative, so a real best-of with plain tags is not promoted."""
        assert self._flags(
            "Fingerprints: The Best of Powderfinger 1994-2000",
            album_artist="Powderfinger",
            type_text="album album",
        ) == (False, False)

    def test_a_classification_failure_cannot_lose_the_title_evidence(
        self, monkeypatch
    ):
        """If the type-tag classifier raises, the title check must still run —
        otherwise an exception silently re-rated the album as a studio album."""
        import services.catalog.album_classification_service as acs

        def _boom(*args, **kwargs):
            raise RuntimeError("classifier exploded")

        monkeypatch.setattr(acs, "classify_compilation_category", _boom)

        assert self._flags("Greatest Hits") == (True, False)
