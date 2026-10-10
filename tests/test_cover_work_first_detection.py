"""The cover pass answers the question from the work the metadata scan logged.

REPORT: "the cover detection at the end of an album seems very slow".

WHY IT WAS SLOW
---------------
The album-end cover pass (``CoverDetector.detect_covers_for_album``) ran its
full MusicBrainz fan-out per track, PER SCAN:

* ISRC lookup (1 request/track),
* recording-relation fetch (2 requests/track),
* writer detection — the dominant cost — ~1 title search + up to 8 recording
  fetches PER WRITER (≈ 9 requests/track/writer) at the shared 1 req/s budget.

Meanwhile the metadata scan had ALREADY resolved each track's work MBID
(``_recording_to_metadata`` reads the recording's work-rels). But it was
written under ``work_mbid`` — NOT a ``tracks`` column (the column is
``musicbrainz_workid``) — so ``popularity_repository._execute_save``'s
``if k in columns`` filter silently DROPPED it. The cover pass never saw a
work id and re-derived everything over the network.

THE CHANGE
----------
1. ``track_stage`` persists the work MBID under ``musicbrainz_workid`` (and
   keeps the in-memory ``work_mbid`` key for consumers that read it), and the
   per-track scan result carries ``work_mbid``;
2. ``scan_stage_runner`` overlays the freshly-resolved work ids onto the rows
   the cover pass receives (same mechanism as ``resolved_track_artists``);
3. ``CoverDetector`` reads the work id (``work_mbid`` or the ``musicbrainz_workid``
   column), then answers the cover question with ONE ``browse_work_recordings``
   call per work — a cover and its original are BOTH performances of the SAME
   work, so the work's own recording list is the complete candidate set. Tracks
   the work resolves conclusively are SKIPPED from the expensive fan-out.

Tri-state contract of ``_detect_via_work_id``: a dict = cover found; ``{}`` =
conclusive negative (every credited work recording is the performer's own);
``None`` = inconclusive, fall through to the existing deep pipeline.
"""

from __future__ import annotations

from contextlib import contextmanager

from services.enrichment.cover_detector_impl import CoverDetector


def _recording(mbid: str, title: str, artist: str, year: int) -> dict:
    """A MusicBrainz browse-recording result carrying a credit + a year."""
    return {
        "id": mbid,
        "title": title,
        "artist-credit": [
            {"artist": {"name": artist, "id": "artist-" + artist.lower().replace(" ", "")}}
        ],
        "first-release-date": f"{year}-01-01",
    }


class _FakeMB:
    """Records every MusicBrainz call so tests can assert WHAT did not run."""

    def __init__(
        self,
        work_recordings=None,
        isrc_recordings=None,
        search_results=None,
        recordings=None,
    ):
        self.work_cfg = work_recordings or []
        self.isrc_cfg = isrc_recordings or []
        self.search_cfg = search_results or []
        self.recording_cfg = recordings or {}
        self.calls: list[tuple] = []

    def browse_work_recordings(self, work_mbid: str, inc: str = "", limit: int = 100):
        self.calls.append(("browse_work_recordings", work_mbid, inc))
        return self.work_cfg

    def lookup_by_isrc(self, isrc: str, inc: str = ""):
        self.calls.append(("lookup_by_isrc", isrc))
        return self.isrc_cfg

    def search_recordings(self, query: str, limit: int = 10):
        self.calls.append(("search_recordings", query))
        return self.search_cfg

    def get_recording(self, mbid: str, inc: str = ""):
        self.calls.append(("get_recording", mbid, inc))
        return self.recording_cfg.get(mbid, {})

    def get_release(self, release_mbid: str, inc=None, timeout=30.0):
        self.calls.append(("get_release", release_mbid))
        return {}


def _detector(fake: _FakeMB) -> CoverDetector:
    det = CoverDetector(db_connection=None)
    det.mb = fake
    return det


class TestTrackStagePersistsTheWorkMbid:
    def test_metadata_resolution_writes_the_real_column(self, monkeypatch):
        """``work_mbid`` must reach ``musicbrainz_workid`` or it is dropped.

        The repository filters payload keys against the real columns
        (``if k in columns``), so a value logged only under ``work_mbid`` was
        silently discarded — which is why the cover pass could never use it.
        """
        from services.popularity.stages import track_stage

        class _MB:
            def lookup_recording_metadata(self, *a, **k):
                return {
                    "recording_mbid": "rec-1",
                    "confidence": 0.9,
                    "work_mbid": "work-1",
                    "title": "Song",
                    "artist": "Artist",
                    "artist_mbid": "art-1",
                    "isrc": "ISRC-X",
                    "album": "Album",
                    "year": "2001",
                }

            def get_composers_for_recording(self, *a, **k):
                return []

        monkeypatch.setattr(track_stage, "get_shared_mb_service", lambda: _MB())

        payload = track_stage._resolve_track_mb_metadata(
            track_id="t1",
            track={"id": "t1", "title": "Song", "artist": "Artist", "album": "Album"},
            track_title="Song",
            track_artist="Artist",
            frozen_track=False,
            force_meta=True,
            options={},
        )["payload"]

        assert payload.get("musicbrainz_workid") == "work-1", (
            "the work MBID must be published under the REAL column name or the "
            "save path drops it"
        )
        assert payload.get("work_mbid") == "work-1", (
            "the in-memory name kept for consumers (cover_data, track result)"
        )


class TestWorkFirstPositive:
    def test_work_first_finds_a_cover_without_the_deep_fan_out(self):
        fake = _FakeMB(
            work_recordings=[
                _recording("rec-orig", "Song", "The Original", 1969),
                _recording("rec-self", "Song", "Covers Band", 2001),
            ]
        )
        det = _detector(fake)

        results = det.detect_covers_for_album(
            "Covered!",
            "Covers Band",
            [{"id": "t1", "title": "Song", "artist": "Covers Band",
              "work_mbid": "work-1", "writer": "A Writer", "isrc": "GB-1XA-01"}],
        )

        assert len(results) == 1
        assert results[0]["is_cover"] is True
        assert results[0]["original_artist"] == "The Original"
        assert results[0]["original_year"] == 1969
        # The expensive re-derivation must NOT run when the work answered.
        assert fake.calls == [("browse_work_recordings", "work-1", "artist-credits+releases")], (
            "the ISRC / recording-relation / writer fan-out ran anyway, so the "
            "work-first path did not engage"
        )

    def test_the_tracks_own_recording_is_never_the_original(self):
        """The performer's own performance must not be reported as an original."""
        fake = _FakeMB(
            work_recordings=[
                _recording("rec-self", "Song", "Covers Band", 2001),
            ]
        )
        det = _detector(fake)

        results = det.detect_covers_for_album(
            "Covered!",
            "Covers Band",
            [{"id": "t1", "title": "Song", "artist": "Covers Band",
              "work_mbid": "work-1", "writer": "A Writer"}],
        )

        assert results == []
        assert all(c[0] == "browse_work_recordings" for c in fake.calls)

    def test_the_musicbrainz_workid_column_is_read_as_work_mbid(self):
        """DB rows carry the COLUMN name; the detector must normalise it."""
        fake = _FakeMB(
            work_recordings=[_recording("rec-orig", "Song", "The Original", 1969)]
        )
        det = _detector(fake)

        results = det.detect_covers_for_album(
            "Covered!",
            "Covers Band",
            [{
                "id": "t1",
                "title": "Song",
                "artist": "Covers Band",
                "musicbrainz_workid": "work-1",
                "writer": "A Writer",
                "isrc": "GB-1XA-01",
            }],
        )

        assert len(results) == 1
        assert results[0]["original_artist"] == "The Original"
        assert not any(c[0] == "lookup_by_isrc" for c in fake.calls)

    def test_tracks_sharing_a_work_share_one_browse(self):
        fake = _FakeMB(
            work_recordings=[_recording("rec-orig", "Song", "The Original", 1969)]
        )
        det = _detector(fake)

        results = det.detect_covers_for_album(
            "Covered!",
            "Covers Band",
            [
                {"id": "t1", "title": "Song", "artist": "Covers Band", "work_mbid": "work-1", "writer": "A Writer"},
                {"id": "t2", "title": "Song", "artist": "Covers Band", "work_mbid": "work-1", "writer": "A Writer"},
            ],
        )

        assert len(results) == 2
        browses = [c for c in fake.calls if c[0] == "browse_work_recordings"]
        assert len(browses) == 1, "two tracks of the same work must not browse twice"


class TestWorkFirstNegativeIsStillAssessed:
    def test_a_work_negative_is_skipped_from_the_deep_fan_out(self):
        fake = _FakeMB(
            work_recordings=[_recording("rec-self", "Song", "Covers Band", 2001)]
        )
        det = _detector(fake)

        results = det.detect_covers_for_album(
            "Covered!",
            "Covers Band",
            [{"id": "t1", "title": "Song", "artist": "Covers Band",
              "work_mbid": "work-1", "writer": "A Writer", "isrc": "GB-1XA-01"}],
        )

        assert results == []
        assert all(c[0] == "browse_work_recordings" for c in fake.calls), (
            "a conclusive-negative track must not pay the ISRC / writer fan-out"
        )

    def test_a_flag_the_work_disproves_is_still_clearable(self, monkeypatch):
        """The work relations ARE the deep evidence.

        A stored ``is_cover`` whose work has no recording by any other artist
        is contradicted by the work itself — it must remain eligible for the
        deep pass's clear, not be shielded as "already confirmed".
        """
        import db.engine as engine

        updates: list[str] = []

        class _Result:
            rowcount = 1

            def fetchall(self):
                return []

            def fetchone(self):
                return None

        class _Session:
            def execute(self, statement, params=None):
                updates.append(str(statement))
                return _Result()

        @contextmanager
        def _fake_session(*_a, **_k):
            yield _Session()

        monkeypatch.setattr(engine, "db_session", _fake_session)

        fake = _FakeMB(
            work_recordings=[_recording("rec-self", "Song", "Covers Band", 2001)]
        )
        det = _detector(fake)

        results = det.detect_covers_for_album(
            "Covered!",
            "Covers Band",
            [{
                "id": "t1",
                "title": "Song",
                "artist": "Covers Band",
                "work_mbid": "work-1",
                "is_cover": 1,
                "writer": "A Writer",
            }],
        )

        assert results == []
        assert any("is_cover = 0" in u for u in updates), (
            "a verdict the logged work disproves must be cleared, exactly like a "
            "deep pass that finds nothing"
        )


class TestWorkFirstInconclusiveFallsThrough:
    def test_empty_work_data_keeps_the_deep_pipeline(self):
        """No work recordings -> no verdict -> the old per-track paths still run."""
        fake = _FakeMB(work_recordings=[], isrc_recordings=[])
        det = _detector(fake)

        results = det.detect_covers_for_album(
            "Covered!",
            "Covers Band",
            [{"id": "t1", "title": "Song", "artist": "Covers Band",
              "work_mbid": "work-1", "writer": "A Writer", "isrc": "GB-1XA-01"}],
        )

        assert results == []
        assert any(c[0] == "lookup_by_isrc" for c in fake.calls), (
            "an inconclusive work must fall through to the ISRC step, not skip "
            "detection silently"
        )


class TestScanRunnerOverlaysTheResolvedWork:
    def test_the_cover_pass_receives_the_freshly_resolved_work(self, monkeypatch):
        """Same mechanism as ``resolved_track_artists``: the raw DB rows are
        overlaid with what THIS pass's metadata resolution actually found."""
        from services.popularity import scan_stage_runner as runner

        captured: dict = {}

        def _fake_detect(album, artist, tracks, conn=None, force=False):
            captured["tracks"] = tracks
            return []

        monkeypatch.setattr(runner, "detect_covers_for_album", _fake_detect)

        runner._run_album_cover_detection(
            artist="Various Artists",
            album="Little Nicky",
            tracks=[{"id": "t1", "title": "Cave", "artist": "Various Artists"}],
            options={
                "resolved_track_artists": {"t1": "Muse"},
                "resolved_track_mbids": {
                    "t1": {"recording_mbid": "rec-1", "work_mbid": "work-1"}
                },
            },
        )

        out = captured["tracks"]
        assert out[0]["artist"] == "Muse", "the resolved artist must still overlay"
        assert out[0]["work_mbid"] == "work-1", (
            "the resolved work MBID must reach the cover pass"
        )

    def test_rows_without_a_resolved_work_are_left_alone(self, monkeypatch):
        from services.popularity import scan_stage_runner as runner

        captured: dict = {}

        def _fake_detect(album, artist, tracks, conn=None, force=False):
            captured["tracks"] = tracks
            return []

        monkeypatch.setattr(runner, "detect_covers_for_album", _fake_detect)

        runner._run_album_cover_detection(
            artist="Radiohead",
            album="OK Computer",
            tracks=[{"id": "t1", "title": "Karma Police", "artist": "Radiohead"}],
            options={},
        )

        out = captured["tracks"]
        assert "work_mbid" not in out[0]
        assert out[0]["artist"] == "Radiohead"


# ---------------------------------------------------------------------------
# Source-level guard: the track result must carry the work so the overlay has
# something to publish. Behavioural assert via the actual return contract.
# ---------------------------------------------------------------------------
class TestTrackResultCarriesTheWork:
    def test_process_track_result_exposes_work_mbid(self):
        """The per-track result dict (consumed by the runner's overlay) must
        carry the work MBID under ``work_mbid``."""
        import inspect

        from services.popularity.stages import track_stage

        source = inspect.getsource(track_stage.process_track)
        assert '"work_mbid": update_payload.get(' in source, (
            "the scan result must expose the resolved work under ``work_mbid`` "
            "or the runner has nothing to overlay"
        )