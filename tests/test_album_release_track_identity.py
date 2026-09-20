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
to the identically titled STUDIO recordings. Consequences seen in the log:

* ``[MB] recording suggestion completed mbid='3b8b3a70-…' track='Für immer
  Dein'`` — the studio recording, not ``3c76f8e9-…``.
* ``"Farewell (feat. Patty Gurdy)" | Score: 90.4 (LF: 7.5k)`` — the studio
  single's catalogue-wide Last.fm listeners on an album whose other tracks sit
  at 300-500, which is what locked it as a global 5★.
* the two identically titled "Helden X Hymnen" rows in the results table: the
  studio one and the unplugged one both reported the SAME score, because both
  had been resolved to the same (studio) recording MBID.

Root cause: the album's release tracklist was already being fetched and
position-matched onto the library (duration guarded) by
``get_listenbrainz_album_tracklist_with_release``, but the resolved IDENTITY was
discarded unless the recording happened to have ListenBrainz listens — and the
per-track batch that should have carried it was built from
``search_releases(album)``, whose RELEASE-shaped entries keyed by album title
could never match ``track_stage``'s ``artist::track title`` lookups, so every
track paid for an ambiguous search instead.

These tests pin each link of the chain plus the two safety properties that a
wrong pairing must still be refused.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ---------------------------------------------------------------------------
# Real MusicBrainz data for the reported release.
# ---------------------------------------------------------------------------

ABU = "3b8b3a70-754b-41d5-84d2-a0c5ab2acb98"   # Für immer Dein (studio)
ABU_UNPLUGGED = "3c76f8e9-6333-4fc7-8cf2-e6fd7fc760d"
HERZBLUT_UNPLUGGED = "2402e085-77ec-421f-a7cf-7e23f41c101f"
HELDEN_UNPLUGGED = "0dfb1b40-0328-4e72-b7b7-430f03172d2d"
FAREWELL_UNPLUGGED = "756e6139-9b84-48f9-9bf7-373cfd7ababf"
FAREWELL_STUDIO = "5c5b3fd1-0000-4000-8000-000000000001"  # stand-in

# (disc, position, release title, recording mbid, length in ms)
RELEASE_TRACKS: list[tuple[int, int, str, str, int]] = [
    (1, 1, "Helden X Hymnen", "cde5e8df-a9a9-4368-a7fa-6011d5600207", 201093),
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
    # ---- the four unplugged renditions ----
    (1, 13, "Für immer Dein (Unplugged Version)", ABU_UNPLUGGED, 242867),
    (1, 14, "Herzblut [Unplugged Version]", HERZBLUT_UNPLUGGED, 194400),
    (1, 15, "Helden X Hymnen (Unplugged Version)", HELDEN_UNPLUGGED, 201160),
    (1, 16, "Farewell [Unplugged Version]", FAREWELL_UNPLUGGED, 180640),
]

# The library as the log shows it: three of the four markers are gone.
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
    # marker LOST on three, kept on one
    {"title": "Für immer Dein", "artist": "DArtagnan", "disc_number": 1, "track_number": 13, "duration": 243},
    {"title": "Herzblut (Unplugged Version)", "artist": "DArtagnan feat. Melissa Bonny", "disc_number": 1, "track_number": 14, "duration": 194},
    {"title": "Helden X Hymnen", "artist": "DArtagnan", "disc_number": 1, "track_number": 15, "duration": 201},
    {"title": "Farewell (feat. Patty Gurdy)", "artist": "DArtagnan feat. Patty Gurdy", "disc_number": 1, "track_number": 16, "duration": 181},
]


def _media_payload() -> list[dict]:
    """The release's ``media`` array as ``_index_release_tracklist`` consumes it."""
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
    """``(titles_to_mbids, position_index, recording_mbids)`` for the release."""
    from services.popularity.popularity_sources import _index_release_tracklist

    titles_to_mbids: dict = {}
    position_index: dict = {}
    recording_mbids: list = []
    _index_release_tracklist(_media_payload(), titles_to_mbids, position_index, recording_mbids)
    return titles_to_mbids, position_index, recording_mbids


class TestReleaseTracklistIndex:
    def test_every_release_track_is_indexed_by_position(self):
        _titles, position_index, _mbids = _index_release_tracklist()
        assert len(position_index) == 16
        assert position_index[(1, 13)]["mbids"] == [ABU_UNPLUGGED]
        assert position_index[(1, 16)]["mbids"] == [FAREWELL_UNPLUGGED]

    def test_position_entry_carries_the_release_title(self):
        """The release title is the only place the version marker survives."""
        _titles, position_index, _mbids = _index_release_tracklist()
        assert position_index[(1, 13)]["title"] == "Für immer Dein (Unplugged Version)"
        # MusicBrainz's own square-bracket convention.
        assert position_index[(1, 16)]["title"] == "Farewell [Unplugged Version]"


class TestPositionIdentityIsEmittedWithoutListens:
    """The reported discard: a resolved identity thrown away because the
    recording has zero ListenBrainz listens."""

    def _resolve(self, monkeypatch, counts: dict):
        from services.popularity import popularity_sources as ps

        monkeypatch.setattr(ps, "lb_get_recording_popularity_batch", lambda mbids: counts)
        monkeypatch.setattr(ps, "_resolve_release_mbid", lambda artist, album, tracks: "rel-1")
        monkeypatch.setattr(ps, "lb_get_release_metadata_batch", lambda mbids: {})
        monkeypatch.setattr(
            ps,
            "get_shared_mb_client",
            lambda: pytest.fail("position index already came from the release payload"),
        )
        return ps

    def test_zero_listen_recording_still_yields_its_identity(self, monkeypatch):
        """``total <= 0`` used to ``continue`` before writing ``out``."""
        ps = self._resolve(monkeypatch, {})
        # Only the unplugged recordings are populated in the index for this test;
        # the point is that an EMPTY count map must not drop them.
        titles_to_mbids, position_index, recording_mbids = _index_release_tracklist()

        monkeypatch.setattr(
            ps, "_resolve_release_mbid", lambda artist, album, tracks: "rel-1"
        )
        monkeypatch.setattr(
            ps,
            "lb_get_release_metadata_batch",
            lambda mbids: {"rel-1": {"media": _media_payload()}},
        )
        monkeypatch.setattr(ps, "lb_get_recording_popularity_batch", lambda mbids: {})

        out, release_mbid = ps.get_listenbrainz_album_tracklist_with_release(
            "DArtagnan", "Helden X Hymnen", LOCAL_TRACKS
        )

        assert release_mbid == "rel-1"
        # The three marker-less unplugged tracks must carry the UNPLUGGED
        # recording, not the studio one.
        assert out["für immer dein"]["recording_mbid"] == ABU_UNPLUGGED
        assert out["helden x hymnen"]["recording_mbid"] in {
            HELDEN_UNPLUGGED,
            "cde5e8df-a9a9-4368-a7fa-6011d5600207",
        }
        assert out["farewell feat patty gurdy"]["recording_mbid"] == FAREWELL_UNPLUGGED
        # Identity, not counts, is the point — and the entry is emitted.
        assert out["für immer dein"]["listenbrainz_listens"] == 0
        assert out["für immer dein"]["release_track_title"] == "Für immer Dein (Unplugged Version)"

    def test_failed_count_lookup_does_not_lose_the_identity(self, monkeypatch):
        from services.popularity import popularity_sources as ps

        monkeypatch.setattr(ps, "_resolve_release_mbid", lambda artist, album, tracks: "rel-1")
        monkeypatch.setattr(
            ps,
            "lb_get_release_metadata_batch",
            lambda mbids: {"rel-1": {"media": _media_payload()}},
        )

        def _boom(mbids):
            raise RuntimeError("listenbrainz unavailable")

        monkeypatch.setattr(ps, "lb_get_recording_popularity_batch", _boom)

        out, _release = ps.get_listenbrainz_album_tracklist_with_release(
            "DArtagnan", "Helden X Hymnen", LOCAL_TRACKS
        )
        assert out["für immer dein"]["recording_mbid"] == ABU_UNPLUGGED


class TestDurationGuardStillRefusesWrongPairs:
    """The position match must not become a licence to mis-assign MBIDs."""

    def test_a_duration_mismatch_blocks_the_pair(self, monkeypatch):
        from services.popularity import popularity_sources as ps

        # Track 13 claims to be 400s long; the release's track 13 is 242.9s.
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
        assert "für immer dein" not in out, "a 157s duration gap must not pair"


class TestAlternatePerformanceTestCoversMusicBrainzBrackets:
    def test_square_brackets_are_recognised(self):
        from services.popularity.popularity_sources import _is_alternate_performance_title

        assert _is_alternate_performance_title("Farewell [Unplugged Version]")
        assert _is_alternate_performance_title("Für immer Dein (Unplugged Version)")
        assert _is_alternate_performance_title("Song [Live at Wembley]")
        # A plain title, and a bracketed NON-performance suffix, stay plain.
        assert not _is_alternate_performance_title("Farewell")
        assert not _is_alternate_performance_title("Song (feat. Someone)")


class _FakeLastFm:
    """Serves one artist catalogue entry plus a title search, like the real one."""

    def __init__(self, catalogue: dict[str, int], search_hits: dict[str, int] | None = None):
        self.catalogue = catalogue
        self.search_hits = search_hits or {}

    def get_artist_top_tracks(self, artist):
        return [{"name": name, "listeners": listeners} for name, listeners in self.catalogue.items()]

    def search_track(self, artist, title, limit=20):
        return [
            {"name": name, "listeners": listeners}
            for name, listeners in self.search_hits.items()
        ]

    def get_track_info(self, artist, title, **kwargs):
        return {"listeners": 0, "track_play": 0}


class TestAlternateRenditionDoesNotAbsorbTheStudioCount:
    """The visible symptom: 7.5k listeners on an unplugged track."""

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
        """The guard must not blind the lookup — an unplugged candidate counts."""
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


class TestBatchBuilderCarriesTheIdentity:
    def test_entries_are_keyed_the_way_track_stage_reads_them(self, monkeypatch):
        from services.popularity import scan_stage_runner as runner

        prefetched = {
            "für immer dein": {
                "recording_mbid": ABU_UNPLUGGED,
                "release_track_title": "Für immer Dein (Unplugged Version)",
            },
            "helden x hymnen": {
                "recording_mbid": HELDEN_UNPLUGGED,
                "release_track_title": "Helden X Hymnen (Unplugged Version)",
            },
        }
        track_dicts = [
            {"title": "Für immer Dein", "artist": "DArtagnan"},
            {"title": "Helden X Hymnen", "artist": "DArtagnan"},
            {"title": "No Identity", "artist": "DArtagnan"},
        ]

        class _Svc:
            def lookup_recordings_by_mbid_bulk(self, mbids, *, album_name=None, **kwargs):
                return {
                    ABU_UNPLUGGED: {"recording_mbid": ABU_UNPLUGGED, "title": "Für immer Dein (Unplugged Version)",
                                    "writer": '["Someone"]'},
                    HELDEN_UNPLUGGED: {"recording_mbid": HELDEN_UNPLUGGED,
                                       "title": "Helden X Hymnen (Unplugged Version)"},
                }

        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.get_shared_mb_service", lambda: _Svc()
        )

        batch = runner._build_album_recording_batch(
            album="Helden X Hymnen",
            track_dicts=track_dicts,
            prefetched_popularity=prefetched,
        )

        assert set(batch) == {"dartagnan::für immer dein", "dartagnan::helden x hymnen"}
        assert batch["dartagnan::für immer dein"]["recording_mbid"] == ABU_UNPLUGGED
        # The release's own title wins, so the version marker reaches
        # ``musicbrainz_title`` (and with it the alternate-rendition test).
        assert batch["dartagnan::für immer dein"]["title"] == "Für immer Dein (Unplugged Version)"
        # Bulk metadata is preserved — taking the batch path must not drop it.
        assert batch["dartagnan::für immer dein"]["writer"] == '["Someone"]'

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

        monkeypatch.setattr(
            track_stage, "_has_real_genres", lambda track: True
        )
        monkeypatch.setattr(
            "services.popularity.stages.track_stage.get_shared_mb_service",
            lambda: pytest.fail("a batch hit must not search or fetch"),
        )

        result = track_stage._resolve_track_mb_metadata(
            track_id="t1",
            track={
                "title": "Für immer Dein",
                "artist": "DArtagnan",
                "recording_mbid": ABU,  # the STUDIO recording — the reported bug
                "musicbrainz_genres": '["Folk"]',
            },
            track_title="Für immer Dein",
            track_artist="DArtagnan",
            frozen_track=False,
            force_meta=False,
            options={
                "mb_batch_metadata": {
                    "dartagnan::für immer dein": {
                        "recording_mbid": ABU_UNPLUGGED,
                        "title": "Für immer Dein (Unplugged Version)",
                    }
                }
            },
        )

        assert result["payload"]["recording_mbid"] == ABU_UNPLUGGED
        assert result["payload"]["mbid"] == ABU_UNPLUGGED
        assert result["payload"]["musicbrainz_title"] == "Für immer Dein (Unplugged Version)"

    def test_the_unchanged_case_still_short_circuits(self, monkeypatch):
        """A track whose stored MBID IS the release's recording does no work."""
        from services.popularity.stages import track_stage

        monkeypatch.setattr(track_stage, "_has_real_genres", lambda track: True)
        monkeypatch.setattr(
            "services.popularity.stages.track_stage.get_shared_mb_service",
            lambda: pytest.fail("nothing to do — must not search"),
        )

        result = track_stage._resolve_track_mb_metadata(
            track_id="t1",
            track={
                "title": "Für immer Dein",
                "artist": "DArtagnan",
                "recording_mbid": ABU_UNPLUGGED,
                "musicbrainz_genres": '["Folk"]',
            },
            track_title="Für immer Dein",
            track_artist="DArtagnan",
            frozen_track=False,
            force_meta=False,
            options={
                "mb_batch_metadata": {
                    "dartagnan::für immer dein": {
                        "recording_mbid": ABU_UNPLUGGED,
                        "title": "Für immer Dein (Unplugged Version)",
                    }
                }
            },
        )

        assert result["payload"] == {}
