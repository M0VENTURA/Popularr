"""Missing tracks must be measured against the EDITION the library holds.

REPORTED: album "Planets" showed 2 missing tracks (#11, #14) while the release
picker offered 17 / 16 / 14-track versions and the library has 14 — the extras
were a deluxe edition's bonus cuts. Same for Against Me.

CAUSE

Nothing in the missing-track path asked which edition matched the library, and
the scan's own choice favoured the BIGGEST: ``resolve_release_id`` did
``max(candidates, key=_total_tracks)``, so the stored release id was the deluxe
edition. Every comparison then measured 14 owned tracks against 17.
"""

from __future__ import annotations

from services.metadata import album_missing_service as ams


def _release(release_id: str, tracks: int, group: str = "rg-1", status="Official"):
    return {
        "release_mbid": release_id,
        "release_group_mbid": group,
        "tracks": [{"title": f"T{i}", "track_number": str(i)} for i in range(1, tracks + 1)],
        "status": status,
    }


def _group_releases(*track_counts):
    return {
        "success": True,
        "releases": [
            {"id": f"rel-{n}", "track_count": n, "status": "Official"}
            for n in track_counts
        ],
    }


class TestEditionMatching:
    def test_a_matching_edition_replaces_the_deluxe_one(self, monkeypatch):
        import services.enrichment.musicbrainz_service as mbs

        monkeypatch.setattr(
            mbs, "get_release_group_releases",
            lambda group, include_track_counts=False: _group_releases(17, 16, 14),
        )
        monkeypatch.setattr(
            mbs, "fetch_musicbrainz_release_metadata",
            lambda rid: _release(rid, 14) if rid == "rel-14" else _release(rid, 17),
        )
        chosen = ams._prefer_edition_matching_library(
            _release("rel-17", 17), artist="Adema", album="Planets", library_count=14
        )
        assert chosen["release_mbid"] == "rel-14"
        assert len(chosen["tracks"]) == 14

    def test_an_already_matching_edition_is_kept(self, monkeypatch):
        """CONTROL: no group browse needed when the count already matches."""
        import services.enrichment.musicbrainz_service as mbs

        called = {"n": 0}
        monkeypatch.setattr(
            mbs, "get_release_group_releases",
            lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {},
        )
        kept = ams._prefer_edition_matching_library(
            _release("rel-14", 14), artist="Adema", album="Planets", library_count=14
        )
        assert kept["release_mbid"] == "rel-14"
        assert called["n"] == 0

    def test_no_matching_edition_keeps_the_original(self, monkeypatch):
        import services.enrichment.musicbrainz_service as mbs

        monkeypatch.setattr(
            mbs, "get_release_group_releases",
            lambda *a, **k: _group_releases(17, 16),
        )
        kept = ams._prefer_edition_matching_library(
            _release("rel-17", 17), artist="Adema", album="Planets", library_count=14
        )
        assert kept["release_mbid"] == "rel-17"

    def test_a_failure_degrades_to_the_original(self, monkeypatch):
        import services.enrichment.musicbrainz_service as mbs

        def _boom(*a, **k):
            raise RuntimeError("musicbrainz down")

        monkeypatch.setattr(mbs, "get_release_group_releases", _boom)
        kept = ams._prefer_edition_matching_library(
            _release("rel-17", 17), artist="Adema", album="Planets", library_count=14
        )
        assert kept["release_mbid"] == "rel-17"

    def test_a_non_official_edition_is_not_preferred_for_its_count(self, monkeypatch):
        import services.enrichment.musicbrainz_service as mbs

        monkeypatch.setattr(
            mbs, "get_release_group_releases",
            lambda *a, **k: {"success": True, "releases": []},
        )
        kept = ams._prefer_edition_matching_library(
            _release("rel-17", 17), artist="A", album="B", library_count=14
        )
        assert kept["release_mbid"] == "rel-17"


class TestResolveReleaseIdPrefersTheLibraryEdition:
    def test_a_matching_edition_wins_over_the_biggest(self, monkeypatch):
        from services.enrichment import musicbrainz_service as mbs

        class _Http:
            def get_release(self, rid, inc=""):
                raise RuntimeError("not a release")  # force the group path

            def browse_releases_for_group(self, gid, inc="", limit=50):
                return [
                    {"id": "big", "status": "Official",
                     "media": [{"track-count": 17}]},
                    {"id": "std", "status": "Official",
                     "media": [{"track-count": 14}]},
                ]

        class _Svc:
            http = _Http()

        monkeypatch.setattr(mbs, "_get_service", lambda: _Svc())
        assert mbs.resolve_release_id("rg-1", expected_track_count=14) == "std"

    def test_without_an_expected_count_the_old_behaviour_holds(self, monkeypatch):
        from services.enrichment import musicbrainz_service as mbs

        class _Http:
            def get_release(self, rid, inc=""):
                raise RuntimeError("not a release")

            def browse_releases_for_group(self, gid, inc="", limit=50):
                return [
                    {"id": "big", "status": "Official",
                     "media": [{"track-count": 17}]},
                    {"id": "std", "status": "Official",
                     "media": [{"track-count": 14}]},
                ]

        class _Svc:
            http = _Http()

        monkeypatch.setattr(mbs, "_get_service", lambda: _Svc())
        assert mbs.resolve_release_id("rg-1") == "big"


class TestBonusIsReportedSeparately:
    def test_trailing_tracks_are_bonus(self):
        """A track past the library's last position is a bonus cut."""
        assert ams.is_bonus_missing_track(
            {"title": "Extra", "track_number": "16"}, {1: 14}
        ) is True

    def test_a_gap_inside_the_span_is_not_bonus(self):
        """CONTROL: #11 of 14 is a gap, not a bonus."""
        assert ams.is_bonus_missing_track(
            {"title": "Bad Triangle", "track_number": "11"}, {1: 14}
        ) is False

    def test_a_bonus_title_marker_is_enough(self):
        assert ams.is_bonus_missing_track(
            {"title": "Song (Bonus Track)", "track_number": "3"}, {1: 14}
        ) is True
