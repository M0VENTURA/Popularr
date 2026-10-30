"""Remix albums must match their REMIX recordings, not the originals.

REPORT: *"Some of these tracks are still incorrectly matching the original
release instead of the remix"* — Limp Bizkit's "New Old Songs" is a remix
album whose secondary type the album stage already recognises, but the
per-track MusicBrainz search ranked candidates only by live affinity,
annotation and title similarity. An original recording whose title was a
closer match therefore won over the remix recording.

The fix carries an ``is_remix_release`` hint from the album stage / track
title into ``get_suggested_mbid`` and ranks remix affinity above title
similarity, exactly as live affinity already works.
"""

from __future__ import annotations

from services.enrichment import musicbrainz_service as mbs
from services.popularity.stages import track_stage as fs


def _recording(mbid: str, title: str, *, secondary=(), releases=True):
    release = {
        "title": "New Old Songs" if secondary else "Significant Other",
        "release-group": {
            "title": "New Old Songs" if secondary else "Significant Other",
            "secondary-types": list(secondary),
        },
    }
    return {
        "id": mbid,
        "title": title,
        "releases": [release] if releases else [],
    }


class _FakeHttp:
    def __init__(self, recordings):
        self.recordings = recordings
        self.queries: list[str] = []

    def search_recordings(self, query, limit=25, inc="", log_context=None):
        self.queries.append(query)
        return list(self.recordings)


def _service(recordings):
    service = mbs.MusicBrainzService(http_client=_FakeHttp(recordings), enabled=True)
    # Keep the test off the on-disk MBID cache.
    service._record_mbid = lambda *a, **k: None  # type: ignore[assignment]
    service._maybe_flush_cache = lambda *a, **k: None  # type: ignore[assignment]
    return service


class TestRemixAffinity:
    def test_a_title_marker_marks_a_remix(self):
        assert mbs._recording_remix_affinity(
            _recording("r1", "Break Stuff (DJ Lethal Mix)", releases=False)
        ) is True

    def test_a_remix_release_group_marks_a_remix(self):
        assert mbs._recording_remix_affinity(
            _recording("r1", "Break Stuff", secondary=["Remix"])
        ) is True

    def test_an_original_recording_is_not_a_remix(self):
        assert mbs._recording_remix_affinity(
            _recording("r1", "Break Stuff")
        ) is False

    def test_no_evidence_is_unknown(self):
        assert mbs._recording_remix_affinity(
            _recording("r1", "Break Stuff", releases=False)
        ) is None


class TestRankingPrefersTheRemix:
    def _recordings(self):
        return [
            _recording("rec-original", "Break Stuff"),
            _recording(
                "rec-remix",
                "Break Stuff (DJ Lethal Mix)",
                secondary=["Remix"],
            ),
        ]

    def test_a_remix_release_selects_the_remix_recording(self):
        service = _service(self._recordings())

        mbid, _score = service.get_suggested_mbid(
            "Break Stuff", "Limp Bizkit", is_remix_release=True,
        )

        assert mbid == "rec-remix", (
            "the remix album must take its remix recording even though the "
            "original's title is a closer match"
        )

    def test_a_studio_release_still_selects_the_original(self):
        service = _service(self._recordings())

        mbid, _score = service.get_suggested_mbid(
            "Break Stuff", "Limp Bizkit", is_remix_release=False,
        )

        assert mbid == "rec-original"

    def test_a_remix_title_prefers_the_remix_without_an_album_hint(self):
        """Callers without album context (e.g. genre lookups) still get it right."""
        service = _service(self._recordings())

        mbid, _score = service.get_suggested_mbid(
            "Break Stuff (DJ Lethal Mix)", "Limp Bizkit",
        )

        assert mbid == "rec-remix", (
            "a title that names a remix must not be penalised just because the "
            "caller did not pass an album context"
        )

    def test_the_cache_key_separates_remix_and_studio_lookups(self):
        assert mbs.MusicBrainzService._cache_key(
            "Break Stuff", "Limp Bizkit", is_remix=True,
        ) != mbs.MusicBrainzService._cache_key(
            "Break Stuff", "Limp Bizkit", is_remix=False,
        )


class TestTrackStageRemixHint:
    def test_album_type_detects_remix(self):
        assert fs._album_type_indicates_remix(
            {"musicbrainz_albumtype": "album+remix"}
        ) is True
        assert fs._album_type_indicates_remix(
            {"detected_album_type": "album"}
        ) is False

    def test_a_track_title_detects_a_remix(self):
        assert fs._track_title_indicates_remix("My Way (P. Diddy Remix)") is True
        assert fs._track_title_indicates_remix("My Way (Fred Durst Mix)") is True
        assert fs._track_title_indicates_remix("Break Stuff") is False
