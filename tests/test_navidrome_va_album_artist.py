"""VA compilations must keep 'Various Artists' as the album artist.

Reported: a Triple J compilation appeared under ``Various Artists • HIM``,
``Various Artists • Martyr AD`` and ``Various Artists • Satyricon``. The album
view carries the multi-artist string, but the album endpoint returns only one
component (``HIM``), which used to win the resolution.
"""

from __future__ import annotations

from services.scanning.navidrome_import import (
    _claim_album_content,
    _claim_album_id,
    _has_various_artist_part,
    album_artist_from_sources,
    reset_album_dedupe,
)


def _resolve(api="", view="", canonical="Various Artists", existing=None):
    return album_artist_from_sources(
        api_album_artist=api,
        album_artist_from_artist_view=view,
        canonical_artist_name=canonical,
        album_name="Triple J's Hottest 100 (1997)",
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


class TestAlbumArtistResolution:
    def test_album_view_va_beats_single_performer_endpoint(self):
        assert _resolve(api="HIM", view="Various Artists • HIM") == "Various Artists"

    def test_album_view_va_with_no_endpoint_artist(self):
        assert _resolve(api="", view="Various Artists • Martyr AD") == "Various Artists"

    def test_existing_composite_va_is_normalised(self):
        existing = {
            "Triple J's Hottest 100 (1997)": "Various Artists • Satyricon"
        }
        assert _resolve(api="Satyricon", view="", existing=existing) == "Various Artists"

    def test_existing_va_is_preserved_against_a_performer(self):
        existing = {"Triple J's Hottest 100 (1997)": "Various Artists"}
        assert _resolve(api="HIM", view="HIM", existing=existing) == "Various Artists"

    def test_a_normal_album_is_not_changed(self):
        assert _resolve(api="A Perfect Circle", view="A Perfect Circle", canonical="A Perfect Circle") == "A Perfect Circle"

    def test_a_normal_album_keeps_the_album_endpoint_artist(self):
        assert _resolve(api="Muse", view="", canonical="Muse") == "Muse"


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
