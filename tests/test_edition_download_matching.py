"""Regression tests: edition-annotated downloads must not match plain queue items.

Reproduces the Feuerschwanz "Knightclub" scan follow-up, where "(Epic Edition)"
downloads were falsely imported against the plain-track queue item. The single
detection and MusicBrainz/Discogs enrichment paths already gate on
``edition_annotations_compatible``; the download/queue matchers did not, and
they all strip brackets during normalization — so "Valhalla (Epic Edition)"
collapsed onto "Valhalla" and the wrong file was moved/imported.

Covers every matcher that pairs a downloaded file with a queue item:
- ``_metadata_matches_queue_item`` (embedded tags)
- ``match_engine.filename_matches_queue_item`` (filename/path)

``filename_matches_queue_item`` used to live in
``services.queue.queue_matching_helpers``; it now lives in
``services.downloads.match_engine`` (one function, one home), so the old
import path is gone — these tests import the current one.
"""

from __future__ import annotations


_PLAIN_ITEM = {
    "artist": "Feuerschwanz",
    "album_artist": "Feuerschwanz",
    "title": "Valhalla",
    "album": "Knightclub",
    "track_number": "3",
    "duration": None,
}

_EDITION_ITEM = {
    "artist": "Feuerschwanz",
    "album_artist": "Feuerschwanz",
    "title": "Valhalla (Epic Edition)",
    "album": "Knightclub",
    "track_number": "4",
    "duration": None,
}

_COVER_ITEM = {
    "artist": "Feuerschwanz",
    "album_artist": "Feuerschwanz",
    "title": "Gangnam Style",
    "album": "Knightclub",
    "track_number": "8",
    "duration": None,
}


def _make_flac(path: str, title: str, artist: str = "Feuerschwanz") -> str:
    """Write a tiny FLAC with the given embedded tags and return its path."""
    import numpy as np
    import soundfile as sf
    from mutagen.flac import FLAC as MFLAC

    sr = 44100
    data = np.zeros(int(sr * 2), dtype=np.float32)
    sf.write(path, data, sr, format="FLAC")
    audio = MFLAC(path)
    audio["title"] = title
    audio["artist"] = artist
    audio.save()
    return path


class TestMetadataMatcherEditionGate:
    def test_edition_file_never_matches_plain_queue(self):
        from services.queue.queue_metadata_matcher import _metadata_matches_queue_item

        path = _make_flac("/tmp/te_meta_edition.flac", "Valhalla (Epic Edition)")
        try:
            assert _metadata_matches_queue_item(path, _PLAIN_ITEM) is False
        finally:
            import os
            os.remove(path)

    def test_edition_file_matches_edition_queue(self):
        from services.queue.queue_metadata_matcher import _metadata_matches_queue_item

        path = _make_flac("/tmp/te_meta_edition_match.flac", "Valhalla (Epic Edition)")
        try:
            assert _metadata_matches_queue_item(path, _EDITION_ITEM) is True
        finally:
            import os
            os.remove(path)

    def test_plain_file_still_matches_plain_queue(self):
        from services.queue.queue_metadata_matcher import _metadata_matches_queue_item

        path = _make_flac("/tmp/te_meta_plain.flac", "Valhalla")
        try:
            assert _metadata_matches_queue_item(path, _PLAIN_ITEM) is True
        finally:
            import os
            os.remove(path)

    def test_cover_annotation_still_matches_plain_queue(self):
        # "(PSY Cover)" is a cover annotation, not an edition — MB/queue titles
        # omit it, so it must keep matching the plain "Gangnam Style" item.
        from services.queue.queue_metadata_matcher import _metadata_matches_queue_item

        path = _make_flac("/tmp/te_meta_cover.flac", "Gangnam Style (PSY Cover)")
        try:
            assert _metadata_matches_queue_item(path, _COVER_ITEM) is True
        finally:
            import os
            os.remove(path)

    def test_radio_edit_variant_still_matches_plain_queue(self):
        # Radio-edit markers are same-song variants, not editions — they must
        # keep matching the plain queue item.
        from services.queue.queue_metadata_matcher import _metadata_matches_queue_item

        item = {**_PLAIN_ITEM, "title": "Valhalla (Radio Edit)"}
        path = _make_flac("/tmp/te_meta_radio.flac", "Valhalla (Radio Edit)")
        try:
            assert _metadata_matches_queue_item(path, item) is True
        finally:
            import os
            os.remove(path)


class TestMatchEngineFilenameEditionGate:
    def test_edition_file_never_matches_plain_queue(self):
        from services.downloads.match_engine import filename_matches_queue_item

        assert filename_matches_queue_item(
            "/downloads/Feuerschwanz - Valhalla (Epic Edition).flac", _PLAIN_ITEM
        ) is False

    def test_edition_file_matches_edition_queue(self):
        from services.downloads.match_engine import filename_matches_queue_item

        assert filename_matches_queue_item(
            "/downloads/Feuerschwanz - Valhalla (Epic Edition).flac", _EDITION_ITEM
        ) is True

    def test_plain_file_still_matches_plain_queue(self):
        from services.downloads.match_engine import filename_matches_queue_item

        assert filename_matches_queue_item(
            "/downloads/Feuerschwanz - Valhalla.flac", _PLAIN_ITEM
        ) is True

    def test_cover_annotation_still_matches_plain_queue(self):
        from services.downloads.match_engine import filename_matches_queue_item

        assert filename_matches_queue_item(
            "/downloads/Feuerschwanz - Gangnam Style (PSY Cover).flac", _COVER_ITEM
        ) is True


class TestFilenameEditionGateOnQueueItems:
    def test_edition_file_never_matches_plain_queue(self):
        from services.downloads.match_engine import filename_matches_queue_item

        assert filename_matches_queue_item(
            "/downloads/Feuerschwanz - Valhalla (Epic Edition).flac", _PLAIN_ITEM
        ) is False

    def test_edition_file_matches_edition_queue(self):
        from services.downloads.match_engine import filename_matches_queue_item

        assert filename_matches_queue_item(
            "/downloads/Feuerschwanz - Valhalla (Epic Edition).flac", _EDITION_ITEM
        ) is True

    def test_plain_file_still_matches_plain_queue(self):
        from services.downloads.match_engine import filename_matches_queue_item

        assert filename_matches_queue_item(
            "/downloads/Feuerschwanz - Valhalla.flac", _PLAIN_ITEM
        ) is True


class TestFindMatchingQueueItemEditionGate:
    def test_edition_queue_item_never_matches_plain_release_track(self):
        from services.downloads.download_matching_service import _find_matching_queue_item

        plain_track = {"title": "Valhalla", "track_number": "3"}
        edition_queue = {"id": 1, "title": "Valhalla (Epic Edition)", "track_number": "3"}
        assert _find_matching_queue_item(plain_track, [edition_queue], set()) is None

    def test_edition_queue_item_matches_edition_release_track(self):
        from services.downloads.download_matching_service import _find_matching_queue_item

        edition_track = {"title": "Valhalla (Epic Edition)", "track_number": "4"}
        edition_queue = {"id": 1, "title": "Valhalla (Epic Edition)", "track_number": "4"}
        matched = _find_matching_queue_item(edition_track, [edition_queue], set())
        assert matched is not None and matched["id"] == 1

    def test_plain_queue_item_still_matches_plain_release_track(self):
        from services.downloads.download_matching_service import _find_matching_queue_item

        plain_track = {"title": "Valhalla", "track_number": "3"}
        plain_queue = {"id": 1, "title": "Valhalla", "track_number": "3"}
        matched = _find_matching_queue_item(plain_track, [plain_queue], set())
        assert matched is not None and matched["id"] == 1


class TestSoulseekCandidateScorerEditionGate:
    def test_edition_candidate_scores_zero_for_plain_queue(self):
        from services.queue.queue_scoring import _score_soulseek_candidate

        assert _score_soulseek_candidate(
            "/downloads/Feuerschwanz - Valhalla (Epic Edition).flac", _PLAIN_ITEM
        ) == 0.0

    def test_edition_candidate_scores_for_edition_queue(self):
        from services.queue.queue_scoring import _score_soulseek_candidate

        assert _score_soulseek_candidate(
            "/downloads/Feuerschwanz - Valhalla (Epic Edition).flac", _EDITION_ITEM
        ) > 0.0

    def test_plain_candidate_still_scores_for_plain_queue(self):
        from services.queue.queue_scoring import _score_soulseek_candidate

        assert _score_soulseek_candidate(
            "/downloads/Feuerschwanz - Valhalla.flac", _PLAIN_ITEM
        ) > 0.0
