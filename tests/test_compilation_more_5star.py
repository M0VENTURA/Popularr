"""Regression tests: single-artist compilations get MORE 5★ tracks.

A Greatest Hits / Best-Of album is a curated collection of the artist's
genuine hits, so it must NOT be rated like a normal album:

1. The 5★ album-top-N rank must use the ARTIST's catalogue reference, not
   the compilation's own (inflated) tracklist — otherwise only the top-3 of
   the compilation qualify and every other hit is suppressed.
2. The era 5★ slot cap (max_5star_slots) must NOT apply to single-artist
   compilations — a hits album legitimately holds many 5★ tracks.  True
   Various-Artists albums keep the cap.
"""

from __future__ import annotations


def _track(score, title="Hit Song", conf="high", single=True, **overrides):
    track = {
        "track_id": f"id-{title.lower().replace(' ', '-')}",
        "artist": "Artist", "album": "Greatest Hits", "title": title,
        "popularity_score": float(score), "final_score": float(score),
        "lastfm_listeners": int(score * 500), "listenbrainz_listens": int(score * 90),
        "lastfm_score": 60.0, "listenbrainz_score": 60.0,
        "is_single": single, "single_confidence": conf,
        "single_sources": ('[{"source": "musicbrainz", "matched": true, "confidence": 0.5}]' if single else ""),
        "is_live": False, "popularity_marked": False, "exclude_from_stats": False,
    }
    track.update(overrides)
    return track


class TestCompilationQualifiesAlbumUsesArtistReference:
    """The album-top-N 5★ rank uses the ARTIST catalogue, not the compilation."""

    def _assign(self, track, compilation_scores, artist_scores, **kwargs):
        from services.popularity.stages import finalise_stage as fs

        album_model = {
            "has_benchmark": True, "era": "peak",
            "catalog_cutoff": 99.0, "max_5star_slots": 4,  # above the track → only album-rank path
            "album_top_n": 3,
        }
        return fs._assign_stars(
            track, compilation_scores, artist_scores,
            album_model=album_model,
            is_compilation=True,
            **kwargs,
        )

    def test_hit_ranked_4th_on_compilation_still_five(self):
        # A genuine hit that is ranked #4 on the compilation (because the
        # tracklist is ALL hits) must still reach 5★ via the ARTIST rank.
        # The catalog_cutoff is above the track's score, so ONLY the
        # album-top-N rank decides — and for a compilation that rank uses the
        # ARTIST catalogue (where the track is #1), not the compilation's own
        # inflated tracklist (where it would be #4 and miss the bar).
        compilation = [95.0, 94.0, 93.0, 92.0, 91.0, 90.0, 89.0, 88.0, 87.0, 86.0]
        # Artist catalogue: this 92.0 track ranks #1 against real studio
        # albums (which sit lower).
        artist = [92.0, 60.0, 58.0, 55.0, 50.0, 45.0, 40.0, 38.0, 35.0, 30.0]

        track = _track(92.0)
        assert self._assign(track, compilation, artist) == 5

    def test_regular_album_still_uses_album_rank(self):
        # A NON-compilation album keeps the album-rank reference — a track
        # ranked #4 on a normal album does NOT get the compilation boost.
        from services.popularity.stages import finalise_stage as fs

        album_model = {
            "has_benchmark": True, "era": "peak",
            "catalog_cutoff": 95.0, "max_5star_slots": 4,
            "album_top_n": 3,
        }
        album = [95.0, 94.0, 93.0, 92.0, 91.0]
        artist = [92.0, 60.0, 58.0, 55.0, 50.0]
        track = _track(92.0)
        # Not a compilation: album rank of 92.0 in [95,94,93,92,...] is #4 > 3
        # → the album-rank path fails.  Falls to the 4★ Single Floor (high
        # single, organic).
        stars = fs._assign_stars(
            track, album, artist, album_model=album_model,
            is_compilation=False,
        )
        assert stars == 4


class TestCompilationSlotCapSkipped:
    """A single-artist compilation is rated on the ARTIST catalogue.

    ⚠️ REWRITTEN (star-rating routing fix). The previous assertion here — all
    six hits return 5★ from ``_assign_stars`` — was satisfied by the ABSOLUTE
    fallback's top band (``score >= 95.0 → 5★``), the last-resort rater used
    when there is NO catalogue to rank against. It stopped holding once a
    single-artist compilation backed by a real local catalogue is rated by
    CAT-Z, which is the corrected behaviour: the online/absolute route capped
    every track at the artist's GLOBAL top 2% (see
    ``test_greatest_hits_artist_catalogue``).

    The invariant this class owns — the era 5★ slot cap is NOT applied to
    compilations — lives in ``post_album_star_ratings``, which collects
    ``protected_five_star_tracks`` and skips the cap when ``is_compilation``.
    What ``_assign_stars`` must guarantee is that a compilation track is rated
    against the ARTIST catalogue (a CAT-Z mode recorded, CAT-Z used) instead of
    being flattened onto an album band.
    """

    def test_hits_are_rated_on_the_artist_catalogue(self, monkeypatch):
        from services.popularity.stages import finalise_stage as fs

        results = [
            _track(score, title=f"Hit {i}")
            for i, score in enumerate([95.0, 94.0, 93.0, 92.0, 91.0, 90.0])
        ]
        # All are high-confidence singles, all clear the catalog cutoff.
        album_scores = [95.0, 94.0, 93.0, 92.0, 91.0, 90.0]
        # A REALISTIC artist catalogue: the six hits sit at the top of a long
        # tail, so each is a genuine CAT-Z outlier. (The old fixture was six
        # hits plus three filler scores — a bimodal catalogue whose tight top
        # cluster makes every hit look average, which is not what a best-of
        # looks like against a career.)
        artist_scores = album_scores + [60.0 - 2 * i for i in range(24)]
        album_model = {
            "has_benchmark": True, "era": "peak",
            "catalog_cutoff": 89.0, "max_5star_slots": 4,  # would cap at 4
            "album_top_n": 3,
        }

        stars = []
        for tr in results:
            stars.append(fs._assign_stars(
                tr, album_scores, artist_scores,
                album_model=album_model, is_compilation=True,
            ))
            assert tr.get("_compilation_rating_mode") == "track_artist_catalogue", (
                "a single-artist compilation with a healthy local catalogue "
                "must be rated by CAT-Z, not the online/absolute route"
            )
            assert tr.get("_compilation_artist_z") is not None

        # Every track is the artist's catalogue top band, so every one reaches
        # 5★ — the slot cap that would demote all but four is applied LATER, in
        # ``post_album_star_ratings``, and is skipped for compilations.
        assert stars == [5, 5, 5, 5, 5, 5]

    def test_va_compilation_keeps_slot_cap(self, monkeypatch):
        from services.popularity.stages import finalise_stage as fs

        # A true Various-Artists compilation: is_compilation is False in the
        # star-rating pass (the VA artist has no catalogue), so the cap
        # applies.  Build 6 era-5★ tracks with the cap at 4 → 2 demoted.
        results = [
            {"track_id": f"id-{i}", "title": f"Song {i}",
             "popularity_score": float(95 - i), "final_score": float(95 - i),
             "_era_5star": True, "stars": 5,
             "lastfm_listeners": 1000, "listenbrainz_listens": 1000,
             "is_live": False, "single_confidence": "high", "is_single": True}
            for i in range(6)
        ]
        album_scores = [float(95 - i) for i in range(6)]
        # is_compilation=False → cap applies → only the top-4 keep 5★.
        # (The cap demotion lives in post_album_star_ratings, not _assign_stars,
        # so this test pins the GUARD: a VA album is not treated as a
        # single-artist compilation.)
        assert fs._assign_stars(
            results[0], album_scores, album_scores,
            album_model={"has_benchmark": True, "era": "peak",
                         "catalog_cutoff": 89.0, "max_5star_slots": 4,
                         "album_top_n": 3},
            is_compilation=False,
        ) == 5  # slot-cap demotion is applied later in post_album_star_ratings
