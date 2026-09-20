"""The album's OWN MusicBrainz release is the authority for track identity.

Reported defect (dArtagnan — "Helden X Hymnen"): the album is correctly linked
to MusicBrainz release ``b01f7815-17fd-4239-9246-a817ef105aff`` (16 tracks,
2026-07-24), and that release's tracklist marks FOUR of its tracks as unplugged
renditions:

    13. Für immer Dein (Unplugged Version)        -> 3c76f8e9-…
    14. Herzblut [Unplugged Version]              -> 2402e085-…
    15. Helden X Hymnen (Unplugged Version)       -> 0dfb1b40-…
    16. Farewell [Unplugged Version]              -> 756e6139-…

Three of those four library titles had lost the marker ("Für immer Dein",
"Helden X Hymnen", "Farewell (feat. Patty Gurdy)"), so every per-title check
failed for exactly those tracks and the per-track recording SEARCH resolved them
to the identically titled STUDIO recordings. Additionally TWO rows share the
title "Helden X Hymnen" (the title track, position 1, and its unplugged
rendition, position 15) and were therefore reported with identical scores.

Root cause: the album's release tracklist was already being fetched and
position-matched onto the library (duration guarded) by
``get_listenbrainz_album_tracklist_with_release``, but the resolved IDENTITY was
discarded unless the recording happened to have ListenBrainz listens — and the
per-track batch that should have carried it was built from
``search_releases(album)``, whose RELEASE-shaped entries keyed by album title
could never match ``track_stage``'s ``artist::track title`` lookups, so every
track paid for an ambiguous search instead.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# NOTE: the identity-key helpers are resolved LAZILY. Importing them at module
# scope would make this whole file a COLLECTION ERROR on a tree without the fix,
# which hides every other guard's verdict behind a single import error.


def _norm_key(value: str) -> str:
    from services.popularity.popularity_sources import normalize_for_aggregation
    return normalize_for_aggregation(value)


def _identity_key(local_title_key: str, disc=None, position=None) -> str:
    from services.popularity.popularity_sources import track_identity_key
    return track_identity_key(local_title_key, disc, position)


def _batch_key(artist: str, title: str, disc=None, track_number=None) -> str:
    from services.popularity.popularity_sources import album_recording_batch_key
    return album_recording_batch_key(artist, title, disc, track_number)

ABU = "3b8b3a70-754b-41d5-84d2-a0c5ab2acb98"   # Für immer Dein (studio)
ABU_UNPLUGGED = "3c76f8e9-6333-4fc7-8cf2-e6fd7fc760d"
HERZBLUT_UNPLUGGED = "2402e085-77ec-421f-a7cf-7e23f41c101f"
HELDEN_UNPLUGGED = "0dfb1b40-0328-4e72-b7b7-430f03172d2d"
FAREWELL_UNPLUGGED = "756e6139-9b84-48f9-9bf7-373cfd7ababf"
HELDEN_STUDIO = "cde5e8df-a9a9-4368-a7fa-6011d5600207"

RELEASE_TRACKS: list[tuple[int, int, str, str, int]] = [
    (1, 1, "Helden X Hymnen", HELDEN_STUDIO, 201093),
    (1, 2, "Wake Me Up", "58ac9276-03e8-48e4-93cf-39c1ca040fe2", 199747),
    (1, 3, "Holding out for a Hero", "28e88579-cf14-4f6d-afa1-64c08d1c759a", 215533),
    (1, 4, "Crazy Train", "8f38ab16-5274-4a51-bdd4-1722315d748b", 232693),
    (1, 5, "In the Air Tonight", "f0519713-1e52-4c02-9b21-3d0d3a0890bc", 207453),
    (1, 6, "My Heart Will Go On", "c3358de7-d821-410f-9287-c559657d20a0", 177467),
    (1, 7, "Basket Case", "a15adecb-f9e7-431f-a1c6-9007deb2b4ba", 170667),
    (1, 8, "Alles aus Liebe", "c45fce15-1180-41b8-b0d9-5b4281a8b60e", 208947),
    (1, 9, "Moonlight Shadow", "6d742655-13b8-4ac0-a4fd-fa04585c271f", 184480),
    (1, 10, "Ein Kompliment", "f560a530-d0e5-4002-8a76-312078bb6c0a", 188773),
    (1, 11, "Go Your Own Way", "4ba6692f-bc3e-4f53-aca3-5a8622e1be1", 188973),
    (1, 12, "Believer", "be2d0146-f8f1-4eb0-b138-b78c32e34c03", 192307),
    (1, 13, "Für immer Dein (Unplugged Version)", ABU_UNPLUGGED, 242867),
    (1, 14, "Herzblut [Unplugged Version]", HERZBLUT_UNPLUGGED, 194400),
    (1, 15, "Helden X Hymnen (Unplugged Version)", HELDEN_UNPLUGGED, 201160),
    (1, 16, "Farewell [Unplugged Version]", FAREWELL_UNPLUGGED, 180640),
]

LOCAL_TRACKS: list[dict] = [
    {"title": "Helden X Hymnen", "artist": "DArtagnan", "disc_number": 1, "track_number": 1, "duration": 201},
    {"title": "Wake Me Up (feat. Piper.Ally)", "artist": "DArtagnan feat. Piper.Ally", "disc_number": 1, "track_number": 2, "duration": 200},
    {"title": "Holding out for a Hero", "artist": "DArtagnan", "disc_number": 1, "track_number": 3, "duration": 216},
    {"title": "Crazy Train", "artist": "DArtagnan", "disc_number": 1, "track_number": 4, "duration": 233},
    {"title": "In the Air Tonight", "artist": "DArtagnan", "disc_number": 1, "track_number": 5, "duration": 207},
    {"title": "My Heart Will Go On", "artist": "DArtagnan", "disc_number": 1, "track_number": 6, "duration": 177},
    {"title": "Basket Case", "artist": "DArtagnan", "disc_number": 1, "track_number": 7, "duration": 171},
    {"title": "Alles aus Liebe", "artist": "DArtagnan", "disc_number": 1, "track_number": 8, "duration": 209},
    {"title": "Moonlight Shadow", "artist": "DArtagnan", "disc_number": 1, "track_number": 9, "duration": 184},
    {"title": "Ein Kompliment", "artist": "DArtagnan", "disc_number": 1, "track_number": 10, "duration": 189},
    {"title": "Go Your Own Way", "artist": "DArtagnan", "disc_number": 1, "track_number": 11, "duration": 189},
    {"title": "Believer", "artist": "DArtagnan", "disc_number": 1, "track_number": 12, "duration": 192},
    {"title": "Für immer Dein", "artist": "DArtagnan", "disc_number": 1, "track_number": 13, "duration": 243},
    {"title": "Herzblut (Unplugged Version)", "artist": "DArtagnan feat. Melissa Bonny", "disc_number": 1, "track_number": 14, "duration": 194},
    {"title": "Helden X Hymnen", "artist": "DArtagnan", "disc_number": 1, "track_number": 15, "duration": 201},
    {"title": "Farewell (feat. Patty Gurdy)", "artist": "DArtagnan feat. Patty Gurdy", "disc_number": 1, "track_number": 16, "duration": 181},
]


def _media_payload() -> list[dict]:
    tracks = [
        {
            "position": pos,
            "number": str(pos),
            "title": title,
            "length": length,
            "recording": {"id": mbid},
        }
        for _disc, pos, title, mbid, length in RELEASE_TRACKS
    ]
    return [{"position": 1, "tracks": tracks}]


def _index_release_tracklist():
    from services.popularity.popularity_sources import _index_release_tracklist

    titles_to_mbids: dict = {}
    position_index: dict = {}
    recording_mbids: list = []
    _index_release_tracklist(_media_payload(), titles_to_mbids, position_index, recording_mbids)
    return titles_to_mbids, position_index, recording_mbids


def _identity(out: dict, title: str, track_number: int) -> dict:
    return out.get(_identity_key(_norm_key(title), 1, track_number)) or {}


class TestReleaseTracklistIndex:
    def test_every_release_track_is_indexed_by_position(self):
        _titles, position_index, _mbids = _index_release_tracklist()
        assert len(position_index) == 16
        assert position_index[(1, 13)]["mbids"] == [ABU_UNPLUGGED]
        assert position_index[(1, 16)]["mbids"] == [FAREWELL_UNPLUGGED]

    def test_position_entry_carries_the_release_title(self):
        _titles, position_index, _mbids = _index_release_tracklist()
        assert position_index[(1, 13)]["title"] == "Für immer Dein (Unplugged Version)"
        assert position_index[(1, 16)]["title"] == "Farewell [Unplugged Version]"


class TestPositionIdentityIsEmittedWithoutListens:
    def _patch(self, monkeypatch):
        from services.popularity import popularity_sources as ps

        monkeypatch.setattr(ps, "_resolve_release_mbid", lambda artist, album, tracks: "rel-1")
        monkeypatch.setattr(
            ps,
            "lb_get_release_metadata_batch",
            lambda mbids: {"rel-1": {"media": _media_payload()}},
        )
        return ps

    def test_zero_listen_recording_still_yields_its_identity(self, monkeypatch):
        """Identity used to be dropped entirely when ``total <= 0``."""
        ps = self._patch(monkeypatch)
        monkeypatch.setattr(ps, "lb_get_recording_popularity_batch", lambda mbids: {})

        out, release_mbid = ps.get_listenbrainz_album_tracklist_with_release(
            "DArtagnan", "Helden X Hymnen", LOCAL_TRACKS
        )

        assert release_mbid == "rel-1"
        immer = _identity(out, "Für immer Dein", 13)
        assert immer["recording_mbid"] == ABU_UNPLUGGED
        assert immer["listenbrainz_listens"] == 0
        assert immer["release_track_title"] == "Für immer Dein (Unplugged Version)"

        farewell = _identity(out, "Farewell (feat. Patty Gurdy)", 16)
        assert farewell["recording_mbid"] == FAREWELL_UNPLUGGED

    def test_same_titled_rows_get_their_own_recording(self, monkeypatch):
        """Positions 1 and 15 are both titled 'Helden X Hymnen' locally."""
        ps = self._patch(monkeypatch)
        monkeypatch.setattr(ps, "lb_get_recording_popularity_batch", lambda mbids: {})

        out, _release = ps.get_listenbrainz_album_tracklist_with_release(
            "DArtagnan", "Helden X Hymnen", LOCAL_TRACKS
        )

        assert _identity(out, "Helden X Hymnen", 1)["recording_mbid"] == HELDEN_STUDIO
        assert _identity(out, "Helden X Hymnen", 15)["recording_mbid"] == HELDEN_UNPLUGGED

    def test_the_title_keyed_entry_is_left_alone(self, monkeypatch):
        """The alias is additive — listen-count semantics must not change."""
        ps = self._patch(monkeypatch)
        monkeypatch.setattr(
            ps, "lb_get_recording_popularity_batch",
            lambda mbids: {HELDEN_STUDIO: {"total_listen_count": 42, "total_user_count": 7}},
        )

        out, _release = ps.get_listenbrainz_album_tracklist_with_release(
            "DArtagnan", "Helden X Hymnen", LOCAL_TRACKS
        )
        # The plain, title-keyed entry is what it always was.
        assert out["helden x hymnen"]["listenbrainz_listens"] == 42

    def test_failed_count_lookup_does_not_lose_the_identity(self, monkeypatch):
        ps = self._patch(monkeypatch)

        def _boom(mbids):
            raise RuntimeError("listenbrainz unavailable")

        monkeypatch.setattr(ps, "lb_get_recording_popularity_batch", _boom)

        out, _release = ps.get_listenbrainz_album_tracklist_with_release(
            "DArtagnan", "Helden X Hymnen", LOCAL_TRACKS
        )
        assert _identity(out, "Für immer Dein", 13)["recording_mbid"] == ABU_UNPLUGGED


class TestDurationGuardStillRefusesWrongPairs:
    def test_a_duration_mismatch_blocks_the_pair(self, monkeypatch):
        from services.popularity import popularity_sources as ps

        tracks = [
            {"title": "Für immer Dein", "artist": "DArtagnan",
             "disc_number": 1, "track_number": 13, "duration": 400},
        ]
        monkeypatch.setattr(ps, "_resolve_release_mbid", lambda artist, album, t: "rel-1")
        monkeypatch.setattr(
            ps, "lb_get_release_metadata_batch",
            lambda mbids: {"rel-1": {"media": _media_payload()}},
        )
        monkeypatch.setattr(ps, "lb_get_recording_popularity_batch", lambda mbids: {})

        out, _release = ps.get_listenbrainz_album_tracklist_with_release(
            "DArtagnan", "Helden X Hymnen", tracks
        )
        assert _identity(out, "Für immer Dein", 13) == {}


class TestAlternatePerformanceTestCoversMusicBrainzBrackets:
    def test_square_brackets_are_recognised(self):
        from services.popularity.popularity_sources import _is_alternate_performance_title

        assert _is_alternate_performance_title("Farewell [Unplugged Version]")
        assert _is_alternate_performance_title("Für immer Dein (Unplugged Version)")
        assert _is_alternate_performance_title("Song [Live at Wembley]")
        assert not _is_alternate_performance_title("Farewell")
        assert not _is_alternate_performance_title("Song (feat. Someone)")


class _FakeLastFm:
    def __init__(self, catalogue: dict[str, int], search_hits: dict[str, int] | None = None):
        self.catalogue = catalogue
        self.search_hits = search_hits or {}

    def get_artist_top_tracks(self, artist):
        return [{"name": name, "listeners": listeners} for name, listeners in self.catalogue.items()]

    def search_track(self, artist, title, limit=20):
        return [{"name": name, "listeners": listeners} for name, listeners in self.search_hits.items()]

    def get_track_info(self, artist, title, **kwargs):
        return {"listeners": 0, "track_play": 0}


class TestAlternateRenditionDoesNotAbsorbTheStudioCount:
    def test_plain_target_absorbs_the_studio_count(self):
        """Baseline — documents the contamination the flag exists to stop."""
        from services.popularity.popularity_sources import get_aggregated_lastfm_popularity

        client = _FakeLastFm({"Farewell": 7500}, {"Farewell": 7500})
        agg = get_aggregated_lastfm_popularity(
            "DArtagnan feat. Patty Gurdy", "Farewell (feat. Patty Gurdy)",
            lastfm_client=client,
        )
        assert agg["listeners"] == 7500

    def test_alternate_rendition_refuses_the_studio_count(self):
        from services.popularity.popularity_sources import get_aggregated_lastfm_popularity

        client = _FakeLastFm({"Farewell": 7500}, {"Farewell": 7500})
        agg = get_aggregated_lastfm_popularity(
            "DArtagnan feat. Patty Gurdy", "Farewell (feat. Patty Gurdy)",
            lastfm_client=client,
            target_is_alt_rendition=True,
        )
        assert agg["listeners"] == 0

    def test_a_matching_alternate_candidate_is_still_accepted(self):
        from services.popularity.popularity_sources import get_aggregated_lastfm_popularity

        client = _FakeLastFm(
            {},
            {"Farewell": 7500, "Farewell [Unplugged Version]": 18},
        )
        agg = get_aggregated_lastfm_popularity(
            "DArtagnan feat. Patty Gurdy", "Farewell (feat. Patty Gurdy)",
            lastfm_client=client,
            target_is_alt_rendition=True,
        )
        assert agg["listeners"] == 18


class _FakeMbService:
    def __init__(self, mapping):
        self.mapping = mapping

    def lookup_recordings_by_mbid_bulk(self, mbids, *, album_name=None, **kwargs):
        return {m: self.mapping[m] for m in mbids if m in self.mapping}


class TestBatchBuilderCarriesTheIdentity:
    def test_rows_are_keyed_with_their_position(self, monkeypatch):
        from services.popularity import scan_stage_runner as runner

        prefetched = {
            _identity_key(_norm_key("Für immer Dein"), 1, 13): {
                "recording_mbid": ABU_UNPLUGGED,
                "release_track_title": "Für immer Dein (Unplugged Version)",
            },
        }
        track_dicts = [
            {"title": "Für immer Dein", "artist": "DArtagnan", "disc_number": 1, "track_number": 13},
            {"title": "No Identity", "artist": "DArtagnan", "disc_number": 1, "track_number": 14},
        ]

        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.get_shared_mb_service",
            lambda: _FakeMbService({
                ABU_UNPLUGGED: {"recording_mbid": ABU_UNPLUGGED,
                                "title": "Für immer Dein (Unplugged Version)",
                                "writer": '["Someone"]'},
            }),
        )

        batch = runner._build_album_recording_batch(
            album="Helden X Hymnen",
            track_dicts=track_dicts,
            prefetched_popularity=prefetched,
        )

        precise = _batch_key("DArtagnan", "Für immer Dein", 1, 13)
        assert precise in batch
        assert batch[precise]["recording_mbid"] == ABU_UNPLUGGED
        assert batch[precise]["title"] == "Für immer Dein (Unplugged Version)"
        # Bulk metadata is preserved — taking the batch path must not drop it.
        assert batch[precise]["writer"] == '["Someone"]'
        # The title is unique here, so the legacy key is written too.
        assert _batch_key("DArtagnan", "Für immer Dein") in batch

    def test_same_titled_rows_do_not_collapse_onto_one_identity(self, monkeypatch):
        """The reported duplicate: positions 1 and 15 share a title."""
        from services.popularity import scan_stage_runner as runner

        prefetched = {
            _identity_key(_norm_key("Helden X Hymnen"), 1, 1): {
                "recording_mbid": HELDEN_STUDIO,
                "release_track_title": "Helden X Hymnen",
            },
            _identity_key(_norm_key("Helden X Hymnen"), 1, 15): {
                "recording_mbid": HELDEN_UNPLUGGED,
                "release_track_title": "Helden X Hymnen (Unplugged Version)",
            },
        }
        track_dicts = [
            {"title": "Helden X Hymnen", "artist": "DArtagnan", "disc_number": 1, "track_number": 1},
            {"title": "Helden X Hymnen", "artist": "DArtagnan", "disc_number": 1, "track_number": 15},
        ]

        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.get_shared_mb_service",
            lambda: _FakeMbService({
                HELDEN_STUDIO: {"recording_mbid": HELDEN_STUDIO, "title": "Helden X Hymnen"},
                HELDEN_UNPLUGGED: {"recording_mbid": HELDEN_UNPLUGGED,
                                   "title": "Helden X Hymnen (Unplugged Version)"},
            }),
        )

        batch = runner._build_album_recording_batch(
            album="Helden X Hymnen",
            track_dicts=track_dicts,
            prefetched_popularity=prefetched,
        )

        assert batch[_batch_key("DArtagnan", "Helden X Hymnen", 1, 1)][
            "recording_mbid"] == HELDEN_STUDIO
        assert batch[_batch_key("DArtagnan", "Helden X Hymnen", 1, 15)][
            "recording_mbid"] == HELDEN_UNPLUGGED
        # Ambiguous title: NO title-only key may be written, or an unrelated
        # caller would silently pick one of the two recordings.
        assert _batch_key("DArtagnan", "Helden X Hymnen") not in batch

    def test_no_identities_means_no_batch(self, monkeypatch):
        from services.popularity import scan_stage_runner as runner

        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.get_shared_mb_service",
            lambda: pytest.fail("no MB work should happen without an identity"),
        )
        assert runner._build_album_recording_batch(
            album="Anything",
            track_dicts=[{"title": "T", "artist": "A"}],
            prefetched_popularity={},
        ) == {}


class TestTrackStageAppliesTheReleaseIdentity:
    def test_a_wrong_stored_mbid_is_corrected_from_the_release(self, monkeypatch):
        from services.popularity.stages import track_stage

        monkeypatch.setattr(track_stage, "_has_real_genres", lambda track: True)
        monkeypatch.setattr(
            track_stage, "get_shared_mb_service",
            lambda: pytest.fail("a batch hit must not search or fetch"),
        )

        result = track_stage._resolve_track_mb_metadata(
            track_id="t1",
            track={
                "title": "Für immer Dein",
                "artist": "DArtagnan",
                "recording_mbid": ABU,
                "musicbrainz_genres": '["Folk"]',
                "disc_number": 1,
                "track_number": 13,
            },
            track_title="Für immer Dein",
            track_artist="DArtagnan",
            frozen_track=False,
            force_meta=False,
            options={
                "mb_batch_metadata": {
                    _batch_key("DArtagnan", "Für immer Dein", 1, 13): {
                        "recording_mbid": ABU_UNPLUGGED,
                        "title": "Für immer Dein (Unplugged Version)",
                    }
                }
            },
        )

        assert result["payload"]["recording_mbid"] == ABU_UNPLUGGED
        assert result["payload"]["mbid"] == ABU_UNPLUGGED
        assert result["payload"]["musicbrainz_title"] == "Für immer Dein (Unplugged Version)"

    def test_a_same_titled_row_gets_its_own_recording(self, monkeypatch):
        """Position 15 must not inherit position 1's recording."""
        from services.popularity.stages import track_stage

        monkeypatch.setattr(track_stage, "_has_real_genres", lambda track: True)
        monkeypatch.setattr(
            track_stage, "get_shared_mb_service",
            lambda: pytest.fail("a batch hit must not search or fetch"),
        )

        result = track_stage._resolve_track_mb_metadata(
            track_id="t15",
            track={
                "title": "Helden X Hymnen",
                "artist": "DArtagnan",
                "recording_mbid": "",
                "musicbrainz_genres": '["Folk"]',
                "disc_number": 1,
                "track_number": 15,
            },
            track_title="Helden X Hymnen",
            track_artist="DArtagnan",
            frozen_track=False,
            force_meta=False,
            options={
                "mb_batch_metadata": {
                    _batch_key("DArtagnan", "Helden X Hymnen", 1, 1): {
                        "recording_mbid": HELDEN_STUDIO,
                        "title": "Helden X Hymnen",
                    },
                    _batch_key("DArtagnan", "Helden X Hymnen", 1, 15): {
                        "recording_mbid": HELDEN_UNPLUGGED,
                        "title": "Helden X Hymnen (Unplugged Version)",
                    },
                }
            },
        )

        assert result["payload"]["recording_mbid"] == HELDEN_UNPLUGGED

    def test_the_unchanged_case_still_short_circuits(self, monkeypatch):
        from services.popularity.stages import track_stage

        monkeypatch.setattr(track_stage, "_has_real_genres", lambda track: True)
        monkeypatch.setattr(
            track_stage, "get_shared_mb_service",
            lambda: pytest.fail("nothing to do — must not search"),
        )

        result = track_stage._resolve_track_mb_metadata(
            track_id="t1",
            track={
                "title": "Für immer Dein",
                "artist": "DArtagnan",
                "recording_mbid": ABU_UNPLUGGED,
                "musicbrainz_genres": '["Folk"]',
                "disc_number": 1,
                "track_number": 13,
            },
            track_title="Für immer Dein",
            track_artist="DArtagnan",
            frozen_track=False,
            force_meta=False,
            options={
                "mb_batch_metadata": {
                    _batch_key("DArtagnan", "Für immer Dein", 1, 13): {
                        "recording_mbid": ABU_UNPLUGGED,
                        "title": "Für immer Dein (Unplugged Version)",
                    }
                }
            },
        )

        assert result["payload"] == {}
