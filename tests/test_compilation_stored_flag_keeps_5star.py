"""A compilation recorded as one must stay a compilation when re-rated.

Reported as: "Compilations are meant to be able to score any track as a 5★
track, but it seems to still be limited to the 3 or 4 track limit."

Compilations are exempt from the era 5★ slot cap (``peak`` 4 / ``solid`` 3 /
``minor`` 2), which is the cap that limits an ordinary album to a handful of
5★ tracks. The exemption is keyed on ``is_compilation``.

``_resolve_compilation_flags`` is what supplies that flag whenever the caller
cannot hand it down — which is exactly the Star Rating Resync, whose rows are
loaded straight from ``tracks``. It re-derived the verdict from the type tag, a
generic album-artist name and the album TITLE, and never read the stored
``is_compilation`` column the row was carrying. A compilation with no
"greatest hits"-style title and no re-readable type therefore came back as a
studio album and was slot-capped, so it could never hold more than 3-4 5★
tracks.
"""

from __future__ import annotations

import pytest

from services.popularity.stages import finalise_stage as fs


def _rows(album: str, *, stored: int, album_type: str = "", n: int = 8,
          album_artist: str = "Artist"):
    out = []
    for i in range(n):
        score = 95.0 - i
        out.append({
            "track_id": f"id-{i}",
            "title": f"Hit {i}",
            "artist": "Artist",
            "album_artist": album_artist,
            "album": album,
            "popularity_score": score,
            "final_score": score,
            "lastfm_listeners": int(score * 500),
            "listenbrainz_listens": int(score * 90),
            "is_single": True,
            "single_confidence": "high",
            "is_live": 0,
            "is_compilation": stored,
            "musicbrainz_albumtype": album_type,
            "year": 2005,
            "release_year": 2005,
            "exclude_from_stats": False,
        })
    return out


class TestTheStoredVerdictIsHonoured:
    """The column already says "compilation"; the resolver must not undo it."""

    @pytest.mark.parametrize(
        "album",
        [
            "Todsünden",        # a covers record — no hits-style title
            "Untouchables",     # an ordinary-looking title
            "Methwold",         # anything at all
        ],
    )
    def test_a_stored_compilation_with_no_title_evidence(self, album: str) -> None:
        flags = fs._resolve_compilation_flags(
            album_results=_rows(album, stored=1),
            artist="Artist",
            album=album,
        )
        assert flags == (True, False), (
            f"'{album}' is stored as is_compilation=1 but resolved to {flags}; "
            "the star pass would then apply the era 5★ slot cap"
        )

    def test_a_plain_album_is_still_plain(self) -> None:
        """CONTROL — the stored flag must not promote an ordinary album."""
        assert fs._resolve_compilation_flags(
            album_results=_rows("Felsenfest", stored=0),
            artist="Artist",
            album="Felsenfest",
        ) == (False, False)

    def test_a_va_compilation_keeps_its_subtype(self) -> None:
        """The VA subtype must still be recovered where the evidence exists —
        an existing path, pinned so the stored-flag check cannot pre-empt it."""
        assert fs._resolve_compilation_flags(
            album_results=_rows("Little Nicky", stored=1,
                                album_artist="Various Artists"),
            artist="Various Artists",
            album="Little Nicky",
        ) == (True, True)


class TestTheEraCapIsSkipped:
    """The consequence the user reported: more than 3-4 5★ tracks."""

    @pytest.mark.parametrize("era,slots", [("peak", 4), ("solid", 3), ("minor", 2)])
    def test_a_compilation_is_not_slot_capped(self, era: str, slots: int) -> None:
        rows = _rows("Todsünden", stored=1, n=8)
        album_scores = [r["popularity_score"] for r in rows]
        artist_scores = album_scores + [60.0 - 2 * i for i in range(24)]
        model = {
            "has_benchmark": True, "era": era, "catalog_cutoff": 89.0,
            "max_5star_slots": slots, "album_top_n": 3,
        }

        is_comp, is_va = fs._resolve_compilation_flags(
            album_results=rows, artist="Artist", album="Todsünden",
        )
        assert is_comp is True

        for track in rows:
            track["stars"] = fs._assign_stars(
                track, album_scores, artist_scores,
                album_model=model, is_compilation=is_comp,
                is_va_compilation=is_va,
                artist_listen_distribution=artist_scores,
            )

        five_star = sum(1 for t in rows if t["stars"] == 5)
        assert five_star > slots, (
            f"a compilation must be able to exceed the {era} slot cap of "
            f"{slots}, but only {five_star} track(s) reached 5★"
        )
