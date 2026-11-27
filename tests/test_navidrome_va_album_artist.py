"""Album artist resolution: a compilation keeps VA, a real album does not.

Two reports, in order:

1. *"a Triple J compilation appeared under ``Various Artists • HIM``…"* — the
   album endpoint returns only ONE component of a multi-artist credit, which used
   to win over the album view's "Various Artists • HIM".

2. *"A heap of albums are being imported as various artists even though they are
   in Navidrome under their regular artist name"* — with the user's own
   diagnosis, which was exactly right: *"It seems to be artists that also appear
   as a track artist on other various artist collections."*

Report 2's mechanism: the prefetch deliberately loads rows where the artist is a
CONTRIBUTOR, and the stored-album-artist slot was keyed by ARTIST alone — last
row wins. One track on a VA compilation therefore set
``existing["Gorillaz"] = "Various Artists"``, and every Gorillaz album resolved
to VA — including *The Mountain*, whose tags say ``albumartist: Gorillaz``.
"""

from __future__ import annotations

from services.scanning.navidrome_import import (
    _album_artist_state_key,
    _claim_album_content,
    _claim_album_id,
    _has_various_artist_part,
    album_artist_from_sources,
    reset_album_dedupe,
)


def _resolve(
    api: str = "",
    view: str = "",
    canonical: str = "Various Artists",
    album: str = "Triple J's Hottest 100 (1997)",
    existing: dict | None = None,
):
    return album_artist_from_sources(
        api_album_artist=api,
        album_artist_from_artist_view=view,
        canonical_artist_name=canonical,
        album_name=album,
        existing_album_artists=existing or {},
    )


class TestVariousArtistDetection:
    def test_bullet_separator(self):
        assert _has_various_artist_part("Various Artists • HIM") is True
        assert _has_various_artist_part("HIM • Various Artists") is True
        assert _has_various_artist_part("Various Artists") is True

    def test_slash_and_semicolon_separators(self):
        assert _has_various_artist_part("Various Artists / HIM") is True
        assert _has_various_artist_part("Various Artists; HIM") is True

    def test_normal_artist(self):
        assert _has_various_artist_part("A Perfect Circle") is False


class TestACompilationStaysVariousArtists:
    def test_album_view_va_beats_single_performer_endpoint(self):
        assert _resolve(api="HIM", view="Various Artists • HIM") == "Various Artists"

    def test_album_view_va_with_no_endpoint_artist(self):
        assert _resolve(api="", view="Various Artists • Martyr AD") == "Various Artists"

    def test_existing_va_is_preserved_when_navidrome_is_silent(self):
        """Navidrome gave nothing specific: keep the stored classification."""
        existing = {
            _album_artist_state_key(
                "Various Artists", "Triple J's Hottest 100 (1997)"
            ): "Various Artists"
        }
        assert _resolve(
            api="", view="", canonical="Various Artists", existing=existing
        ) == "Various Artists"

    def test_a_composite_stored_value_is_normalised(self):
        existing = {
            _album_artist_state_key(
                "Various Artists", "Triple J's Hottest 100 (1997)"
            ): "Various Artists • Satyricon"
        }
        assert _resolve(
            api="", view="", canonical="Various Artists", existing=existing
        ) == "Various Artists"


class TestARealAlbumKeepsItsArtist:
    def test_a_normal_album_is_not_changed(self):
        assert _resolve(
            api="A Perfect Circle", view="A Perfect Circle",
            canonical="A Perfect Circle",
        ) == "A Perfect Circle"

    def test_a_normal_album_keeps_the_album_endpoint_artist(self):
        assert _resolve(api="Muse", view="", canonical="Muse") == "Muse"

    def test_a_stale_va_slot_does_not_override_a_specific_artist(self):
        """⭐ THE REPORTED BUG.

        Gorillaz has one track on a Various Artists compilation, so the prefetch
        loaded it. Under the old ARTIST-only key that set
        ``existing["Gorillaz"] = "Various Artists"``, and every Gorillaz album
        became a compilation. The key is per album now, so the compilation's
        entry cannot be read for *The Mountain*.
        """
        stale = {
            _album_artist_state_key(
                "Gorillaz", "Now That's What I Call Music 42"
            ): "Various Artists"
        }
        assert _resolve(
            api="Gorillaz", view="Gorillaz",
            canonical="Gorillaz", album="The Mountain",
            existing=stale,
        ) == "Gorillaz", (
            "a compilation the artist contributed to must not mark their own "
            "album as a compilation"
        )

    def test_the_albums_own_stored_va_is_still_preserved(self):
        """The key change must not stop a REAL compilation being preserved."""
        stored = {
            _album_artist_state_key("Various Artists", "The Mountain"): "Various Artists"
        }
        assert _resolve(
            api="", view="", canonical="Various Artists",
            album="The Mountain", existing=stored,
        ) == "Various Artists"

    def test_another_artist_cannot_claim_this_album(self):
        other = {
            _album_artist_state_key("Gorillaz", "The Mountain"): "Various Artists"
        }
        assert _resolve(
            api="", view="", canonical="Blur", album="The Mountain",
            existing=other,
        ) == "Blur"


class TestTheStateKeyIsScopedToTheAlbum:
    def test_different_albums_have_different_keys(self):
        assert _album_artist_state_key("Gorillaz", "The Mountain") != (
            _album_artist_state_key("Gorillaz", "Demon Days")
        )

    def test_the_key_is_case_insensitive(self):
        assert _album_artist_state_key("GORILLAZ", "the mountain") == (
            _album_artist_state_key("gorillaz", "The Mountain")
        )

    def test_an_edition_marker_does_not_split_the_key(self):
        assert _album_artist_state_key("Gorillaz", "The Mountain (Deluxe)") == (
            _album_artist_state_key("Gorillaz", "The Mountain")
        )


class TestTheHealCannotRewriteARealCompilation:
    def _heal(self, track_artists, resolved):
        from services.scanning.navidrome_import import (
            _heal_contaminated_va_album_artist,
        )

        payloads = [
            {"id": f"t{i}", "artist": artist}
            for i, artist in enumerate(track_artists)
        ]
        return _heal_contaminated_va_album_artist(payloads, resolved)

    def test_one_differing_performer_means_it_is_a_compilation(self):
        # Never touches the DB: the guard returns 0 before the UPDATE.
        assert self._heal(["Gorillaz", "Muse"], "Gorillaz") == 0

    def test_a_single_track_is_not_enough_evidence(self):
        assert self._heal(["Gorillaz"], "Gorillaz") == 0

    def test_a_va_resolved_artist_is_never_healed(self):
        assert self._heal(["Various Artists", "Various Artists"], "Various Artists") == 0

    def test_an_absent_track_artist_blocks_the_heal(self):
        assert self._heal(["Gorillaz", ""], "Gorillaz") == 0


class TestAlbumDedupe:
    def test_the_same_album_id_is_claimed_once(self):
        reset_album_dedupe()
        assert _claim_album_id("album-1") is True
        assert _claim_album_id("album-1") is False
        assert _claim_album_id("album-2") is True

    def test_the_same_content_under_a_different_album_id_is_claimed_once(self):
        reset_album_dedupe()
        ids = {"song-1", "song-2"}
        assert _claim_album_content("Triple J's Hottest 100 (1998)", ids) is True
        assert _claim_album_content("Triple J's Hottest 100 (1998)", ids) is False
        # A genuinely different tracklist is not suppressed.
        assert _claim_album_content(
            "Triple J's Hottest 100 (1998)", {"song-3"}
        ) is True

    def test_reset_clears_both_claims(self):
        reset_album_dedupe()
        assert _claim_album_id("album-1") is True
        assert _claim_album_content("X", {"s1"}) is True
        reset_album_dedupe()
        assert _claim_album_id("album-1") is True
        assert _claim_album_content("X", {"s1"}) is True

    def test_the_pipeline_enables_dedupe(self):
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1]
            / "services/scanning/pipelines/navidrome_pipeline.py"
        ).read_text(encoding="utf-8")
        assert "reset_album_dedupe()" in source
        assert "dedupe_albums=True" in source
