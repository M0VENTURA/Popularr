"""Tests for the album release-picker contract fix.

Symptom: on the album page, using the MusicBrainz lookup → select an album
→ "The automatic match was not confident enough" modal opens but NO releases
load.

Root cause: ``get_release_group_releases`` returned RAW MusicBrainz release
dicts (``media`` arrays), but the release-picker renderer expects the
normalised shape (``formats`` list, ``disc_count``, ``track_count``,
``disambiguation``, ``cover_art_url``).  ``r.formats.join(' + ')`` threw on
the raw shape, the render died silently and the results div stayed empty.

Fixes pinned here:
1. ``get_release_group_releases`` normalises every release to the same
   contract ``get_musicbrainz_best_release`` uses.
2. ``get_musicbrainz_best_release`` browses with ``inc=media+labels`` (the
   release browse endpoint can reject some inc combinations).
"""

from __future__ import annotations

import pytest


class TestReleaseFormatPreference:
    def test_digital_beats_cd_and_vinyl_on_equal_tracklists(self, monkeypatch):
        from services.enrichment import musicbrainz_service as mbs

        releases = [
            {
                "id": "vinyl", "title": "The Album", "date": "2024-01-01",
                "status": "Official", "track_count": 10,
                "formats": ["Vinyl"],
            },
            {
                "id": "cd", "title": "The Album", "date": "2024-01-01",
                "status": "Official", "track_count": 10,
                "formats": ["CD"],
            },
            {
                "id": "digital", "title": "The Album", "date": "2024-01-01",
                "status": "Official", "track_count": 10,
                "formats": ["Digital Media"],
            },
        ]
        monkeypatch.setattr(
            mbs, "_browse_group_releases", lambda *a, **k: releases
        )
        monkeypatch.setattr(mbs, "_get_local_track_count", lambda *a, **k: 10)

        result = mbs.get_musicbrainz_best_release(
            "Artist", "The Album", "format-preference-rg"
        )

        assert result["best_release"]["id"] == "digital"
        assert mbs._release_format_preference(releases[1]) == 2
        assert mbs._release_format_preference(releases[0]) == 1

    def test_single_disc_beats_multi_disc_on_an_equal_tracklist(self, monkeypatch):
        """REPORTED: a 2-disc repackaging and a 1-disc digital edition expose
        the same tracks. The one-disc edition is the practical target."""
        from services.enrichment import musicbrainz_service as mbs

        releases = [
            {
                "id": "multi", "title": "The Album", "date": "2024-01-01",
                "status": "Official", "track_count": 10, "disc_count": 2,
                "formats": ["Digital Media"],
            },
            {
                "id": "single", "title": "The Album", "date": "2024-01-01",
                "status": "Official", "track_count": 10, "disc_count": 1,
                "formats": ["Digital Media"],
            },
        ]
        monkeypatch.setattr(mbs, "_browse_group_releases", lambda *a, **k: releases)
        monkeypatch.setattr(mbs, "_get_local_track_count", lambda *a, **k: 10)

        result = mbs.get_musicbrainz_best_release(
            "Artist", "The Album", "single-disc-rg"
        )

        assert result["best_release"]["id"] == "single"

    def test_a_more_complete_multi_disc_edition_still_wins(self, monkeypatch):
        """CONTROL — the one-disc bonus is below one track, so a genuinely
        longer edition is never discarded (the double-CD report)."""
        from services.enrichment import musicbrainz_service as mbs

        releases = [
            {
                "id": "single", "title": "The Album", "date": "2024-01-01",
                "status": "Official", "track_count": 10, "disc_count": 1,
                "formats": ["Digital Media"],
            },
            {
                "id": "multi", "title": "The Album", "date": "2024-01-01",
                "status": "Official", "track_count": 20, "disc_count": 2,
                "formats": ["Digital Media"],
            },
        ]
        monkeypatch.setattr(mbs, "_browse_group_releases", lambda *a, **k: releases)
        monkeypatch.setattr(mbs, "_get_local_track_count", lambda *a, **k: 10)

        result = mbs.get_musicbrainz_best_release(
            "Artist", "The Album", "complete-edition-rg"
        )

        assert result["best_release"]["id"] == "multi"


class TestReleaseGroupReleasesNormalization:
    def test_returns_normalized_release_shape(self, monkeypatch):
        """Raw MB releases (media arrays) become the picker contract."""
        from services.enrichment import musicbrainz_service as mbs

        class _FakeClient:
            def get_release_group(self, rg_mbid, inc="", timeout=10.0):
                return {
                    "releases": [
                        {
                            "id": "rel-1",
                            "title": "The Album",
                            "date": "2024-05-01",
                            "country": "US",
                            "status": "Official",
                            "disambiguation": "",
                            "media": [
                                {"format": "CD", "track-count": 10},
                                {"format": "CD", "track-count": 2},
                            ],
                        },
                    ],
                }

        monkeypatch.setattr(mbs, "get_shared_mb_client", lambda: _FakeClient())
        result = mbs.get_release_group_releases("rg-abc")
        assert result["success"] is True
        rel = result["releases"][0]
        assert rel["id"] == "rel-1"
        assert rel["formats"] == ["CD"]
        assert rel["disc_count"] == 2
        assert rel["track_count"] == 12
        assert rel["cover_art_url"].startswith("https://coverartarchive.org/release/rel-1/")

    def test_include_track_counts_browses_group(self, monkeypatch):
        """With include_track_counts=True the browse endpoint supplies real
        track counts even when the release-group payload has no media (the
        real-world MB shape).  Regression: _enrich_releases_with_track_counts
        derived the group from releases[0].get('release-group') which the
        flattened releases never carry, so no counts were ever attached."""
        from services.enrichment import musicbrainz_service as mbs

        class _FakeClient:
            def get_release_group(self, rg_mbid, inc="", timeout=10.0):
                # Real MB shape: releases WITHOUT media / track counts.
                return {
                    "releases": [
                        {"id": "rel-1", "title": "The Album", "country": "AU"},
                        {"id": "rel-2", "title": "The Album (Deluxe)", "country": "CA"},
                    ],
                }

            def browse_releases_for_group(self, rg_mbid, inc="media", limit=100):
                return [
                    {"id": "rel-1", "media": [{"format": "CD", "track-count": 10}]},
                    {"id": "rel-2", "media": [{"format": "CD", "track-count": 13}]},
                ]

        monkeypatch.setattr(mbs, "get_shared_mb_client", lambda: _FakeClient())
        result = mbs.get_release_group_releases("rg-abc", include_track_counts=True)
        assert result["success"] is True
        by_id = {r["id"]: r for r in result["releases"]}
        assert by_id["rel-1"]["track_count"] == 10
        assert by_id["rel-2"]["track_count"] == 13

    def test_track_counts_zero_without_include_flag(self, monkeypatch):
        """Without include_track_counts the flattened releases keep their
        (zero) media-derived counts — the endpoint does not double-fetch."""
        from services.enrichment import musicbrainz_service as mbs

        class _FakeClient:
            def get_release_group(self, rg_mbid, inc="", timeout=10.0):
                return {"releases": [{"id": "rel-1", "title": "The Album"}]}

            def browse_releases_for_group(self, rg_mbid, inc="media", limit=100):
                return [{"id": "rel-1", "media": [{"format": "CD", "track-count": 10}]}]

        monkeypatch.setattr(mbs, "get_shared_mb_client", lambda: _FakeClient())
        result = mbs.get_release_group_releases("rg-abc")
        assert result["releases"][0]["track_count"] == 0

    def test_no_media_yields_empty_formats(self, monkeypatch):
        from services.enrichment import musicbrainz_service as mbs

        class _FakeClient:
            def get_release_group(self, rg_mbid, inc="", timeout=10.0):
                return {"releases": [{"id": "rel-1", "title": "X"}]}

        monkeypatch.setattr(mbs, "get_shared_mb_client", lambda: _FakeClient())
        result = mbs.get_release_group_releases("rg-abc")
        rel = result["releases"][0]
        assert rel["formats"] == []
        assert rel["track_count"] == 0
        assert rel["disc_count"] == 0

    def test_error_returns_success_false(self, monkeypatch):
        from services.enrichment import musicbrainz_service as mbs

        class _BoomClient:
            def get_release_group(self, rg_mbid, inc="", timeout=10.0):
                raise RuntimeError("boom")

        monkeypatch.setattr(mbs, "get_shared_mb_client", lambda: _BoomClient())
        result = mbs.get_release_group_releases("rg-abc")
        assert result["success"] is False
        assert "error" in result


class TestBestReleaseBrowseInc:
    def test_browse_uses_media_plus_labels(self, monkeypatch):
        """best-release must NOT request ``recordings`` on the browse call —
        the release browse endpoint can reject inc combinations."""
        from services.enrichment import musicbrainz_service as mbs

        captured = {}

        class _FakeClient:
            def browse_releases_for_group(self, rg_mbid, inc="media", limit=50):
                captured["inc"] = inc
                return []

        # _get_local_track_count hits the DB — stub it to 0.
        monkeypatch.setattr(mbs, "get_shared_mb_client", lambda: _FakeClient())
        monkeypatch.setattr(mbs, "_get_local_track_count", lambda *a, **k: 0)
        result = mbs.get_musicbrainz_best_release("Artist", "Album", "rg-abc")
        assert captured["inc"] == "media+labels"
        assert "releases" in result
