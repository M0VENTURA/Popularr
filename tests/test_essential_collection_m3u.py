"""Pure helpers behind the Essential Collection (no file I/O any more).

RETIRED — and therefore removed from this file.  These tests drove
``_create_essential_m3u`` against a temp Playlists dir and asserted the
``.m3u``/``.nsp`` it wrote.  That function is gone: ``finalise_stage`` now
creates every playlist through the Subsonic/Navidrome API and writes no files
("No .m3u or .nsp files are written to disk anywhere in this module").  The
fixture also patched ``_essential_playlists_dir``, which no longer exists, so
each of those 17 raised ``AttributeError`` before a single assertion ran.

Still covered here, because they are still pure and still live: title
normalisation, the compilation-bucket exclusion list, feat.-credit folding and
the guest-credit stripper.

NOT covered here any more (follow-up work, not a deletion of something that
worked):

- the dedup winner order (studio > main discography > rating > popularity >
  earlier year), which now lives in ``popularity_math.build_essential_artist_playlists``
- "a scan with no per-track results still refreshes essential collections"

Both were already failing, so they were protecting nothing — but the logic
behind them is live and deserves tests written against the new API.
"""

from __future__ import annotations

import pytest

from services.popularity.stages import finalise_stage as fs


class TestTitleNormalization:
    def test_strips_parenthetical_and_bracket_noise(self):
        assert fs._normalise_essential_title("Walk (2018 Remaster)") == "walk"
        assert fs._normalise_essential_title("Play [Deluxe Edition]") == "play"
        assert fs._normalise_essential_title("Alive (Live)") == "alive"
        assert fs._normalise_essential_title("Foo (Version) Bar") == "foo bar"

    def test_collapses_whitespace_and_case(self):
        assert fs._normalise_essential_title("  The   Song  ") == "the song"

    def test_empty_title(self):
        assert fs._normalise_essential_title("") == ""
        assert fs._normalise_essential_title(None) == ""


class TestExcludedArtists:
    @pytest.mark.parametrize(
        "artist",
        ["Various Artists", "various artists", "Soundtrack", "Soundtracks",
         "VA", "V/A", "va", "Unknown Artist", "unknown"],
    )
    def test_compilation_buckets_excluded(self, artist):
        assert fs._is_excluded_essential_artist(artist)

    def test_normal_artist_included(self):
        assert not fs._is_excluded_essential_artist("Poppy")
        assert not fs._is_excluded_essential_artist("Lord of the Lost")


class TestFeaturedPrimaryArtistFolding:
    """Regression: a "Primary feat. Guest" album/single must fold into the
    PRIMARY artist's essential collection (and not spawn a separate tiny one).

    Reported: "Apocalyptica feat. Ville Valo & Lauri Ylönen" was treated as a
    separate artist — its 4★/5★ track never joined "Apocalyptica"'s collection
    and a separate below-threshold collection was attempted for the guest
    suffix.  Only feat./ft./featuring credits fold; genuine "&"/"with"
    collaborations and name-prefix lookalikes are never adopted.
    """

    def test_feat_guest_side_still_matches_guest_collection(self):
        # The guest-side helper (featured artists get the track on THEIR
        # collection too) is unchanged.
        assert fs._track_has_featured_artist(
            "Apocalyptica feat. Ville Valo & Lauri Ylönen", "Ville Valo"
        )
        assert fs._track_has_featured_artist(
            "Apocalyptica feat. Ville Valo & Lauri Ylönen", "Lauri Ylönen"
        )
        assert not fs._track_has_featured_artist(
            "Apocalyptica feat. Ville Valo & Lauri Ylönen", "Apocalyptica"
        )

    def test_strip_guest_credit_helper(self):
        assert fs._essential_strip_guest_credit("Apocalyptica feat. Ville Valo & Lauri Ylönen") == "Apocalyptica"
        assert fs._essential_strip_guest_credit("Apocalyptica ft. Ville Valo") == "Apocalyptica"
        assert fs._essential_strip_guest_credit("Apocalyptica featuring Ville Valo") == "Apocalyptica"
        # Genuine collaborations are preserved.
        assert fs._essential_strip_guest_credit("Metallica & San Francisco Symphony") == "Metallica & San Francisco Symphony"
        assert fs._essential_strip_guest_credit("Apocalyptica Tribute Band") == "Apocalyptica Tribute Band"
        assert fs._essential_strip_guest_credit("Poppy") == "Poppy"
