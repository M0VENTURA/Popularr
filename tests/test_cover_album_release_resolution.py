"""Cover detection resolves the album's release before searching each track.

Reported: covers still not found when a track carries no recording MBID.

A row the metadata pass never resolved has no recording/work identity, and if
the album rows carry no release MBID either, ``_resolve_recording_mbid`` had
nothing but a bare title search — and a NON-Various-Artists album was skipped
by step 3's identity gate altogether. The detector now resolves the album's
release ONCE per pass (stored id first, then a MusicBrainz artist+album search)
and uses its tracklist to resolve each unresolved recording.
"""

from __future__ import annotations

from services.enrichment import cover_detector_impl as impl
from services.enrichment.cover_detector_impl import CoverDetector

ALBUM = "Iowa"
ARTIST = "Slipknot"


class _StubMB:
    def __init__(self, releases=(), release_payloads=None):
        self._releases = list(releases)
        self._release_payloads = release_payloads or {}
        self.release_queries: list[str] = []
        self.get_release_calls: list[str] = []
        self.search_calls: list[str] = []

    def search_releases(self, query, limit=10, inc=""):
        self.release_queries.append(query)
        return list(self._releases)

    def get_release(self, mbid, inc="", **kwargs):
        self.get_release_calls.append(str(mbid))
        return dict(self._release_payloads.get(str(mbid), {}))

    def search_recordings(self, query, limit=15):
        self.search_calls.append(query)
        return []


def _detector(mb) -> CoverDetector:
    det = CoverDetector(db_connection=None)
    det.mb = mb
    det._load_cover_checked_map = lambda ids: {}
    det._clear_unconfirmed_verdicts = lambda *a, **k: None
    det._persist_checked = lambda *a, **k: None
    det._update_file_metadata = lambda *a, **k: True
    return det


class TestAlbumReleaseResolution:
    def test_a_stored_row_mbid_wins_without_a_search(self) -> None:
        mb = _StubMB(releases=[{"id": "rel-other", "title": ALBUM}])
        got = _detector(mb)._resolve_album_release_mbid(
            ALBUM, ARTIST, [{"id": "t1", "musicbrainz_album_mbid": "rel-stored"}]
        )
        assert got == "rel-stored"
        assert mb.release_queries == []

    def test_the_release_is_searched_by_artist_and_album(self) -> None:
        mb = _StubMB(releases=[{"id": "rel-1", "title": ALBUM}])
        got = _detector(mb)._resolve_album_release_mbid(ALBUM, ARTIST, [{"id": "t1"}])
        assert got == "rel-1"
        assert len(mb.release_queries) == 1
        assert 'artist:"Slipknot"' in mb.release_queries[0]
        assert 'release:"Iowa"' in mb.release_queries[0]

    def test_a_non_matching_release_is_rejected(self) -> None:
        mb = _StubMB(releases=[{"id": "rel-x", "title": "A Different Album"}])
        got = _detector(mb)._resolve_album_release_mbid(ALBUM, ARTIST, [{"id": "t1"}])
        assert got == ""

    def test_a_va_compilation_does_not_search_for_a_release(self) -> None:
        mb = _StubMB(releases=[{"id": "rel-1", "title": ALBUM}])
        got = _detector(mb)._resolve_album_release_mbid(
            ALBUM, "Various Artists", [{"id": "t1"}]
        )
        assert got == ""
        assert mb.release_queries == []


class TestTheReleaseTracklistResolvesTheRecording:
    def test_the_default_release_is_used_when_the_row_has_none(self) -> None:
        release = {
            "id": "rel-release-first",
            "media": [
                {
                    "tracks": [
                        {
                            "title": "People = Shit",
                            "recording": {
                                "id": "rec-people",
                                "title": "People = Shit",
                                "artist-credit": [
                                    {"artist": {"id": "a-slip", "name": "Slipknot"}}
                                ],
                            },
                        }
                    ]
                }
            ],
        }
        mb = _StubMB(release_payloads={"rel-release-first": release})
        det = _detector(mb)

        got = det._resolve_recording_mbid(
            {"id": "t1", "title": "People = Shit", "artist": "Slipknot"},
            ARTIST,
            ALBUM,
            default_release_mbid="rel-release-first",
        )

        assert got == "rec-people"
        assert "rel-release-first" in mb.get_release_calls


class TestAnUnresolvedRowOnANonVaAlbumIsNowAssessed:
    def test_the_release_unlocks_step_three(self, monkeypatch) -> None:
        mb = _StubMB(releases=[{"id": "rel-unlock", "title": ALBUM}])
        det = _detector(mb)
        det._collect_track_writers = lambda *a, **k: {}
        monkeypatch.setattr(impl, "apply_cover_metadata_batch", lambda conn, ups: [])
        asked: list[str] = []
        det._resolve_recording_mbid = (
            lambda track, *a, **k: asked.append(str(track.get("id"))) or None
        )

        det.detect_covers_for_album(
            ALBUM,
            ARTIST,
            [
                {"id": "t1", "title": "People = Shit", "artist": ARTIST,
                 "album_artist": ARTIST, "album": ALBUM},
                {"id": "t2", "title": "Disasterpiece", "artist": ARTIST,
                 "album_artist": ARTIST, "album": ALBUM},
            ],
            force=True,
        )

        assert set(asked) == {"t1", "t2"}
