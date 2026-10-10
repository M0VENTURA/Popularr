"""Regression: the deep cover pass reuses the metadata scan's work/recording
MBIDs instead of re-searching MusicBrainz for every track.

REPORT: "The cover detection at the end of an album seems very slow. Doesn't
the work details get grabbed during the metadata scan so then that could be
used to check for covers for songs rather to then use what's found in the local
db to initiate the cover detection for only those tracks, skipping ones that
don't have it logged?"

The report was CORRECT, and the detail was worse than "not reused":

1. ``track_stage`` put the resolved work id in ``payload["work_mbid"]`` — a key
   that is NOT a tracks column. ``_execute_save`` filters payload keys by the
   real column set, so the value was silently DROPPED. Only
   ``musicbrainz_workid`` is a column.
2. ``CoverDetectorImpl._resolve_recording_mbid`` read only ``track["mbid"]``
   while the scan persists ``recording_mbid`` (+``mbid``) and the Navidrome
   import persists ``recording_mbid``/``musicbrainz_trackid`` — so the cover
   pass re-searched every track even when the metadata scan had just resolved
   it.
3. Step 6's pre-fetched-work fast path read ``track["work_mbid"]``, which is
   never on a DB row — so the fast path never fired from stored data and the
   work fallback always re-resolved the recording.
4. ``_resolve_cover_chain`` re-fetched the seed recording that
   ``_detect_via_recording_relation`` had just fetched.

Fix:
- track_stage persists BOTH ``work_mbid`` (in-memory) and ``musicbrainz_workid``
  (the real column).
- The track result dict publishes the resolved ``recording_mbid``/``work_mbid``;
  scan_stage_runner overlays them onto the deep-pass rows (``resolved_track_mbids``),
  the same mechanism that already overlays the resolved artist.
- `_resolve_recording_mbid` accepts every identity column the scan writes.
- Step 6 reads ``musicbrainz_workid`` as well.
- New ``_track_has_logged_identity`` gate: Steps 3 & 6 skip tracks with no
  logged MusicBrainz identity (the "skip ones that don't have it logged" half).
- ``_resolve_cover_chain`` accepts the already-fetched seed.
"""

from __future__ import annotations

from services.enrichment.cover_detector_impl import CoverDetector


class _StubMB:
    """Minimal MusicBrainz stub recording what the detector asked for."""

    def __init__(self, recording=None, search_results=None):
        self._recording = recording or {}
        self._search_results = search_results or []
        self.get_recording_calls: list[str] = []
        self.search_calls: list[str] = []

    def get_recording(self, mbid, inc=None):
        self.get_recording_calls.append(str(mbid))
        return dict(self._recording)

    def search_recordings(self, query, limit=15):
        self.search_calls.append(query)
        return [dict(r) for r in self._search_results]

    def lookup_by_isrc(self, isrc, inc=None):
        return []

    def get_release(self, mbid, inc=None):
        return {}

    def get_work(self, wid, inc=None):
        return {}


def _detector(mb=None, **kwargs):
    det = CoverDetector(db_connection=None)
    det.mb = mb or _StubMB()
    det._load_cover_checked_map = lambda ids: {}
    det._collect_track_writers = lambda *a, **k: {}
    for k, v in kwargs.items():
        setattr(det, k, v)
    return det


class TestTrackHasLoggedIdentity:
    """The skip gate: only tracks the metadata pass resolved are deep-checked."""

    def test_logged_recording_mbid_counts(self):
        det = _detector()
        assert det._track_has_logged_identity({"recording_mbid": "rec-1"}) is True

    def test_logged_work_mbid_counts(self):
        det = _detector()
        assert det._track_has_logged_identity({"musicbrainz_workid": "work-1"}) is True

    def test_logged_album_mbid_counts(self):
        # A release MBID lets _resolve_recording_mbid resolve the recording
        # from the (cached) album tracklist — no title search needed.
        det = _detector()
        assert det._track_has_logged_identity({"musicbrainz_album_mbid": "rel-1"}) is True

    def test_nothing_logged_is_skipped(self):
        det = _detector()
        assert det._track_has_logged_identity({"title": "Song", "artist": "A"}) is False

    def test_empty_strings_do_not_count(self):
        det = _detector()
        assert det._track_has_logged_identity({"recording_mbid": "", "mbid": None}) is False


class TestResolveRecordingMbidUsesStoredId:
    """No search when the scan already resolved the recording."""

    def test_uses_recording_mbid_column_without_network(self):
        mb = _StubMB()
        det = _detector(mb)
        got = det._resolve_recording_mbid(
            {"title": "Song", "artist": "A", "recording_mbid": "rec-stored"}
        )
        assert got == "rec-stored"
        assert mb.search_calls == []

    def test_uses_musicbrainz_trackid_column(self):
        mb = _StubMB()
        det = _detector(mb)
        got = det._resolve_recording_mbid(
            {"title": "Song", "artist": "A", "musicbrainz_trackid": "rec-navidrome"}
        )
        assert got == "rec-navidrome"
        assert mb.search_calls == []

    def test_no_stored_id_still_searches(self):
        # Preserve the fallback for genuinely unresolved tracks (e.g. a
        # metadata pass that never ran for this album).
        mb = _StubMB(search_results=[{"id": "rec-found", "title": "Song",
                                      "artist-credit": [{"artist": {"name": "A"}}]}])
        det = _detector(mb)
        got = det._resolve_recording_mbid({"title": "Song", "artist": "A"})
        assert got == "rec-found"
        assert mb.search_calls, "the fallback search must still exist"


class TestStep3SkipsTracksWithNoLoggedIdentity:
    """The 'skip ones that don't have it logged' half, at the album level."""

    def _run_album(self, track):
        det = _detector()
        det._resolve_recording_mbid = lambda *a, **k: "rec-1"  # already resolved
        calls = {"n": 0}
        original = det._detect_via_recording_relation

        def _spy(*a, **k):
            calls["n"] += 1
            return None

        det._detect_via_recording_relation = _spy
        det._is_cover_flagged = lambda t: True
        det.detect_covers_for_album("Album", "Artist", [track])
        return calls["n"]

    def test_unresolved_track_never_reaches_recording_relation(self):
        n = self._run_album({"id": "t1", "title": "Song", "artist": "A"})
        assert n == 0, "a track with no logged identity must not be searched"

    def test_resolved_track_still_reaches_recording_relation(self):
        n = self._run_album({"id": "t1", "title": "Song", "artist": "A",
                             "recording_mbid": "rec-1"})
        assert n >= 1, "a resolved track must still be deep-checked"


class TestChainSkipsSeedRefetch:
    """The chain walk reuses the recording _detect_via_recording_relation fetched."""

    def test_no_duplicate_seed_fetch(self):
        mb = _StubMB(recording={
            "id": "rec-1",
            "title": "Song",
            "recording-relation-list": [],
            "artist-credit": [{"artist": {"name": "A"}}],
            "first-release-date": "1990-01-01",
        })
        det = _detector(mb)
        got = det._detect_via_recording_relation(
            "rec-1", "Song", "Artist"
        )
        assert got is None
        # seed fetched once by _detect_via_recording_relation; the chain walk
        # reuses it instead of fetching again.
        assert mb.get_recording_calls.count("rec-1") == 1


class TestWorkMbidFastPathFiresFromStoredValue:
    """Step 6 uses musicbrainz_workid without re-resolving the recording."""

    def test_stored_work_id_feeds_the_fast_path(self):
        det = _detector()
        used: dict = {}
        det._earliest_work_recording = lambda **kw: used.update(kw) or {
            "artist": "Original", "year": 1990, "confidence": "medium"
        }
        det._resolve_recording_mbid = lambda *a, **k: None
        det._find_original_via_work_lookup = lambda *a, **k: used.update(
            {"via_lookup": True}) or None
        det._is_cover_flagged = lambda t: True
        det._load_cover_checked_map = lambda ids: {}

        results = det.detect_covers_for_album(
            "Album", "Artist",
            [{"id": "t1", "title": "Song", "artist": "Artist",
              "musicbrainz_workid": "work-1", "is_cover": 1}],
        )
        assert used.get("work_ids") == {"work-1"}
        assert "via_lookup" not in used, (
            "the stored work id must feed step 6's fast path without a "
            "recording-relation lookup"
        )
        assert results and results[0]["original_artist"] == "Original"