"""Tests for the Various Artists compilation tracklist guard.

WHY THIS EXISTS
Every MusicBrainz release-group credited "Various Artists" scores a free ``1.0``
on the artist half of ``calculate_match_score`` (title 0.6 / artist 0.4), so a
compilation contributes a flat 0.4 that no ordinary album gets.  Against the 0.6
acceptance floor that means only ~0.33 of title similarity is needed, and a
generic title ("Greatest Hits") happily binds to an unrelated VA release-group —
storing its MBID and adopting its album type.

The guard requires the candidate's TRACKLIST to resemble the local album as
well.  The load-bearing test here is
``TestTracklistSimilarityUsesTitlesNotPositions``: the obvious implementation
(compare by track number/position) would pass every case in this file and still
be useless, because any two twelve-track compilations pair one-to-one on
position and score a perfect 1.0.
"""
from __future__ import annotations

import pytest

from services.enrichment import musicbrainz_service as mbm
from services.popularity.stages import album_stage


def _library(*titles: str) -> list[dict[str, object]]:
    return [{"title": t} for t in titles]


def _mb_metadata(*titles: str, disc: int = 1) -> dict[str, object]:
    return {
        "tracks": [
            {"mb_title": t, "mb_track_number": i + 1, "mb_disc_number": disc}
            for i, t in enumerate(titles)
        ]
    }


@pytest.fixture
def patch_release(monkeypatch):
    """Patch the release fetch/browse behind the similarity helper."""

    def _apply(metadata, *, release_id="rel-1"):
        monkeypatch.setattr(
            mbm, "fetch_musicbrainz_release_metadata", lambda _rid: metadata
        )
        monkeypatch.setattr(
            mbm,
            "get_musicbrainz_best_release",
            lambda *a, **k: {"best_release": {"id": release_id}},
        )
        return release_id

    return _apply


class TestTracklistSimilarityUsesTitlesNotPositions:
    """A position-based comparison would score 1.0 here — that is the bug."""

    def test_same_positions_but_unrelated_titles_scores_zero(self, patch_release):
        patch_release(_mb_metadata("Alpha", "Beta", "Gamma"))
        similarity = mbm.release_group_tracklist_similarity(
            "rg-unrelated",
            _library("Completely", "Different", "Songs"),
            artist="Various Artists",
            album="Greatest Hits",
        )
        assert similarity == 0.0

    def test_same_positions_but_unrelated_titles_fails_the_gate(self, patch_release):
        patch_release(_mb_metadata("Alpha", "Beta", "Gamma"))
        passed, similarity = mbm.release_group_tracklist_passes(
            "rg-unrelated",
            _library("Completely", "Different", "Songs"),
            artist="Various Artists",
            album="Greatest Hits",
        )
        assert passed is False
        assert similarity == 0.0

    def test_matching_titles_score_one(self, patch_release):
        patch_release(_mb_metadata("Alpha", "Beta", "Gamma"))
        similarity = mbm.release_group_tracklist_similarity(
            "rg-real",
            _library("Alpha", "Beta", "Gamma"),
            artist="Various Artists",
            album="Greatest Hits",
        )
        assert similarity == 1.0

    def test_title_order_does_not_matter(self, patch_release):
        """A compilation's local ordering may differ; titles are what count."""
        patch_release(_mb_metadata("Gamma", "Alpha", "Beta"))
        similarity = mbm.release_group_tracklist_similarity(
            "rg-reordered",
            _library("Alpha", "Beta", "Gamma"),
            artist="Various Artists",
            album="Greatest Hits",
        )
        assert similarity == 1.0


class TestTracklistFraction:
    @pytest.mark.parametrize(
        "local,expected",
        [
            (("Alpha", "Beta", "Gamma"), 1.0),
            (("Alpha", "Beta", "Nope"), pytest.approx(2 / 3)),
            (("Alpha", "Nope", "Nope2"), pytest.approx(1 / 3)),
            (("Nope", "Nope2", "Nope3"), 0.0),
        ],
    )
    def test_fraction_of_local_tracks_present(self, patch_release, local, expected):
        patch_release(_mb_metadata("Alpha", "Beta", "Gamma"))
        similarity = mbm.release_group_tracklist_similarity(
            "rg-1", _library(*local), artist="Various Artists", album="Best Of"
        )
        assert similarity == expected

    def test_floor_is_inclusive(self, patch_release):
        """2 of 3 ≈ 0.667 must pass a 0.6 floor."""
        patch_release(_mb_metadata("Alpha", "Beta", "Gamma"))
        passed, similarity = mbm.release_group_tracklist_passes(
            "rg-1",
            _library("Alpha", "Beta", "Nope"),
            floor=0.6,
            artist="Various Artists",
            album="Best Of",
        )
        assert passed is True
        assert similarity == pytest.approx(2 / 3)

    def test_custom_floor_can_reject_a_marginal_match(self, patch_release):
        patch_release(_mb_metadata("Alpha", "Beta", "Gamma"))
        passed, _ = mbm.release_group_tracklist_passes(
            "rg-1",
            _library("Alpha", "Beta", "Nope"),
            floor=0.9,
            artist="Various Artists",
            album="Best Of",
        )
        assert passed is False

    def test_parenthetical_variants_still_match(self, patch_release):
        """Normalisation must tolerate an edit suffix, as the fuzzy fallback does."""
        patch_release(_mb_metadata("Song", "Another"))
        similarity = mbm.release_group_tracklist_similarity(
            "rg-1",
            _library("Song (Radio Edit)", "Another"),
            artist="Various Artists",
            album="Best Of",
        )
        assert similarity == 1.0


class TestFailsClosed:
    def test_unreadable_release_scores_zero(self, patch_release):
        patch_release(None)
        assert mbm.release_group_tracklist_similarity(
            "rg-1", _library("Alpha"), artist="Various Artists", album="X"
        ) == 0.0

    def test_release_without_tracks_scores_zero(self, patch_release):
        patch_release({"tracks": []})
        assert mbm.release_group_tracklist_similarity(
            "rg-1", _library("Alpha"), artist="Various Artists", album="X"
        ) == 0.0

    def test_fetch_exception_scores_zero(self, monkeypatch):
        def _boom(_rid):
            raise RuntimeError("musicbrainz down")

        monkeypatch.setattr(mbm, "fetch_musicbrainz_release_metadata", _boom)
        monkeypatch.setattr(
            mbm,
            "get_musicbrainz_best_release",
            lambda *a, **k: {"best_release": {"id": "rel-1"}},
        )
        assert mbm.release_group_tracklist_similarity(
            "rg-1", _library("Alpha"), artist="Various Artists", album="X"
        ) == 0.0

    def test_empty_local_tracklist_scores_zero(self, patch_release):
        patch_release(_mb_metadata("Alpha"))
        assert mbm.release_group_tracklist_similarity(
            "rg-1", [], artist="Various Artists", album="X"
        ) == 0.0


class TestIsVariousArtistsCompilation:
    @pytest.mark.parametrize(
        "artist,album_artist",
        [
            ("Various Artists", None),
            ("various artists", None),
            ("VARIOUS ARTISTS", None),
            ("Various", None),
            ("VA", None),
            ("V/A", None),
            ("  Various Artists  ", None),
            ("Some Artist", "Various Artists"),
        ],
    )
    def test_va_credits_are_detected(self, artist, album_artist):
        assert album_stage._is_various_artists_compilation(artist, album_artist) is True

    @pytest.mark.parametrize(
        "artist",
        ["The Offspring", "Radiohead", "Pink Floyd", "A Perfect Circle", ""],
    )
    def test_normal_artists_are_not_va(self, artist):
        assert album_stage._is_various_artists_compilation(artist, None) is False

    def test_soundtrack_is_not_treated_as_va_from_the_name_alone(self):
        """'Soundtrack' is a SINGLE-artist compilation in the canonical
        classifier, so the VA-only gate must not fire on the name."""
        assert album_stage._is_various_artists_compilation("Soundtrack", None) is False

    def test_multi_artist_tracklist_is_detected_without_a_va_credit(self):
        """Not credited VA, but every track has a different artist."""
        tracks = [
            {"artist": "Artist A"},
            {"artist": "Artist B"},
            {"artist": "Artist C"},
            {"artist": "Artist D"},
        ]
        assert album_stage._is_various_artists_compilation(
            "Now That's Music", None, "Now 42", tracks
        ) is True

    def test_no_tracklist_and_no_va_credit_is_not_va(self):
        assert album_stage._is_various_artists_compilation(
            "Now That's Music", None, "Now 42", []
        ) is False


class TestGuardConfig:
    def _patch_config(self, monkeypatch, block):
        monkeypatch.setattr(
            "helpers.config_helpers.get_config", lambda: {"single_detection": block}
        )

    def test_defaults_are_enabled_at_0_6(self, monkeypatch):
        self._patch_config(monkeypatch, {})
        assert album_stage._compilation_tracklist_guard_config() == (True, 0.6)

    def test_can_be_disabled(self, monkeypatch):
        self._patch_config(monkeypatch, {"compilation_tracklist_guard": False})
        enabled, floor = album_stage._compilation_tracklist_guard_config()
        assert enabled is False
        assert floor == 0.6

    def test_floor_is_read(self, monkeypatch):
        self._patch_config(monkeypatch, {"compilation_tracklist_floor": 0.85})
        enabled, floor = album_stage._compilation_tracklist_guard_config()
        assert enabled is True
        assert floor == pytest.approx(0.85)

    @pytest.mark.parametrize("bad", [0, -1, 1.5, 42, "nonsense", None])
    def test_out_of_range_floor_falls_back_to_default(self, monkeypatch, bad):
        """A nonsense floor must not silently disable the gate (0) or reject
        every compilation (>1)."""
        self._patch_config(monkeypatch, {"compilation_tracklist_floor": bad})
        assert album_stage._compilation_tracklist_guard_config() == (True, 0.6)

    def test_unreadable_config_defaults_to_enabled(self, monkeypatch):
        def _boom():
            raise RuntimeError("config unavailable")

        monkeypatch.setattr("helpers.config_helpers.get_config", _boom)
        assert album_stage._compilation_tracklist_guard_config() == (True, 0.6)


class TestLookupGate:
    """The gate as wired into _lookup_musicbrainz_album_type."""

    @staticmethod
    def _patch_service(monkeypatch, *, score=0.95, secondary=("compilation",)):
        class FakeSvc:
            def __init__(self, *a, **k):
                pass

            def search_releasegroup_matches(self, artist, album, limit=3):
                return [{
                    "id": "rg-candidate",
                    "title": "Greatest Hits",
                    "primary_type": "album",
                    "secondary_types": list(secondary),
                    "match_score": score,
                }]

        monkeypatch.setattr(album_stage, "get_shared_mb_service", lambda: FakeSvc())

    def test_va_compilation_with_unrelated_tracklist_is_rejected(self, monkeypatch):
        """The headline case: a high title score alone must NOT bind."""
        self._patch_service(monkeypatch)
        monkeypatch.setattr(
            album_stage,
            "_tracklist_corroborates_release_group",
            lambda *a, **k: False,
        )
        assert album_stage._lookup_musicbrainz_album_type(
            "Various Artists",
            "Greatest Hits",
            "Various Artists",
            _library("Completely", "Different"),
        ) == (None, None)

    def test_va_compilation_with_matching_tracklist_is_accepted(self, monkeypatch):
        self._patch_service(monkeypatch)
        monkeypatch.setattr(
            album_stage,
            "_tracklist_corroborates_release_group",
            lambda *a, **k: True,
        )
        resolved, mbid = album_stage._lookup_musicbrainz_album_type(
            "Various Artists",
            "Greatest Hits",
            "Various Artists",
            _library("Alpha", "Beta"),
        )
        assert mbid == "rg-candidate"
        assert resolved is not None

    def test_normal_artist_album_skips_the_gate_entirely(self, monkeypatch):
        """A normal album must not pay for a tracklist fetch — and must not be
        affected when the guard would reject."""
        self._patch_service(monkeypatch, secondary=())

        calls: list[int] = []

        def _spy(*a, **k):
            calls.append(1)
            return False

        monkeypatch.setattr(album_stage, "_tracklist_corroborates_release_group", _spy)
        resolved, mbid = album_stage._lookup_musicbrainz_album_type(
            "The Offspring", "Smash", "The Offspring", _library("Nitro")
        )
        assert calls == []
        assert mbid == "rg-candidate"
        assert resolved == "album"

    def test_guard_can_be_disabled_for_va(self, monkeypatch):
        self._patch_service(monkeypatch)
        monkeypatch.setattr(
            "helpers.config_helpers.get_config",
            lambda: {"single_detection": {"compilation_tracklist_guard": False}},
        )
        calls: list[int] = []

        def _spy(*a, **k):
            calls.append(1)
            return True

        # The real function is used (not spied) so the config check runs.
        from services.enrichment.musicbrainz_service import (
            release_group_tracklist_passes,
        )

        monkeypatch.setattr(
            mbm, "release_group_tracklist_passes", lambda *a, **k: calls.append(1) or (True, 1.0)
        )
        resolved, mbid = album_stage._lookup_musicbrainz_album_type(
            "Various Artists", "Greatest Hits", "Various Artists", _library("Alpha")
        )
        # Disabled => the tracklist checker is never consulted.
        assert calls == []
        assert mbid == "rg-candidate"

    def test_two_argument_call_still_works(self, monkeypatch):
        """Existing callers (and tests) pass only artist+album."""
        self._patch_service(monkeypatch, secondary=())
        resolved, mbid = album_stage._lookup_musicbrainz_album_type("Pink Floyd", "The Wall")
        assert mbid == "rg-candidate"
        assert resolved == "album"

    def test_va_with_no_tracks_preserves_previous_behaviour(self, monkeypatch):
        """No tracklist to compare => fall back to title-only rather than
        silently rejecting every compilation with an empty track list."""
        self._patch_service(monkeypatch, secondary=())
        resolved, mbid = album_stage._lookup_musicbrainz_album_type(
            "Various Artists", "Greatest Hits", "Various Artists", []
        )
        assert mbid == "rg-candidate"
        assert resolved is not None

    def test_low_title_score_is_still_rejected_before_the_gate(self, monkeypatch):
        self._patch_service(monkeypatch, score=0.4)
        called: list[int] = []
        monkeypatch.setattr(
            album_stage,
            "_tracklist_corroborates_release_group",
            lambda *a, **k: called.append(1) or True,
        )
        assert album_stage._lookup_musicbrainz_album_type(
            "Various Artists", "Greatest Hits", "Various Artists", _library("Alpha")
        ) == (None, None)
        assert called == []


class TestCorroboratorIntegration:
    """_tracklist_corroborates_release_group against the real similarity helper."""

    def test_rejects_when_similarity_below_floor(self, monkeypatch, patch_release):
        patch_release(_mb_metadata("Alpha", "Beta", "Gamma"))
        assert album_stage._tracklist_corroborates_release_group(
            "Various Artists",
            "Greatest Hits",
            "Various Artists",
            _library("Totally", "Unrelated", "Stuff"),
            "rg-1",
            {},
        ) is False

    def test_accepts_when_similarity_meets_floor(self, monkeypatch, patch_release):
        patch_release(_mb_metadata("Alpha", "Beta", "Gamma"))
        assert album_stage._tracklist_corroborates_release_group(
            "Various Artists",
            "Greatest Hits",
            "Various Artists",
            _library("Alpha", "Beta", "Gamma"),
            "rg-1",
            {},
        ) is True

    def test_no_tracks_is_not_a_rejection(self, monkeypatch):
        assert album_stage._tracklist_corroborates_release_group(
            "Various Artists", "Greatest Hits", "Various Artists", [], "rg-1", {}
        ) is True
