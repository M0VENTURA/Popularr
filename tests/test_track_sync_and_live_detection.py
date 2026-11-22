"""Regression tests for two reported issues.

Issue 1 — track edit modal on the album page reported "database updated, but
tags weren't saved to file" while the track page worked.  Root cause:
``sync_track_tags_to_file`` looked up ``file_path`` from the ``get_track_tags``
dict, which only contains ``EDITABLE_FIELDS`` (``file_path`` is NOT one of
them), so the file path was always ``None`` and the sync always returned
``False``.  The fix resolves the path from the DB directly (with relative-path
handling) and only writes non-empty tags.

Issue 2 — no tracks became 5★ on the album "(how to live) as ghosts".  Root
cause: the bare ``\blive\b`` album pattern matched "live" inside the title
phrase, so the album was treated as a live album and ``_assign_stars`` capped
every track at 4★.  The fix restores the format-tag-only live patterns.
"""

from __future__ import annotations

import os
import tempfile


class TestSyncTrackTagsToFilePathResolution:
    """sync_track_tags_to_file must find the real file path from the DB."""

    def test_resolve_absolute_path(self):
        from services.metadata.tag_file_service import _resolve_music_file_path

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            path = f.name
        try:
            assert _resolve_music_file_path(path) == path
        finally:
            os.unlink(path)

    def test_resolve_relative_under_music_root(self, monkeypatch):
        from services.metadata.tag_file_service import _resolve_music_file_path

        with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as f:
            path = f.name
        root = os.path.dirname(path)
        rel = os.path.basename(path)
        monkeypatch.setenv("MUSIC_ROOT", root)
        try:
            assert _resolve_music_file_path(rel) == path
        finally:
            os.unlink(path)

    def test_resolve_missing_returns_none(self):
        from services.metadata.tag_file_service import _resolve_music_file_path

        assert _resolve_music_file_path("/nonexistent/track.mp3") is None
        assert _resolve_music_file_path("") is None
        assert _resolve_music_file_path(None) is None

    def test_sync_uses_db_path_not_tags_dict(self, monkeypatch):
        """The file path must come from the DB, not the EDITABLE_FIELDS dict.

        ``get_track_tags`` only selects ``EDITABLE_FIELDS`` — ``file_path`` is
        not among them — so a sync that reads the path from the tags dict can
        never succeed.  This pins the fix that reads it separately.
        """
        from services.metadata import tag_file_service as tfs

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            path = f.name
        calls = []
        try:
            monkeypatch.setattr(
                tfs, "get_track_tags",
                lambda track_id: {
                    "title": "Song", "artist": "Artist", "album": "Album",
                    "genres": "Rock", "mbid": None, "year": None,
                },
            )
            monkeypatch.setattr(tfs, "_get_track_file_path", lambda track_id: path)
            monkeypatch.setattr(
                tfs, "write_tags_to_file",
                lambda fp, tags: calls.append((fp, tags)) or True,
            )
            assert tfs.sync_track_tags_to_file("track-1") is True
            fp, tags = calls[0]
            assert fp == path
            # None/empty values must be filtered — writing them would DELETE
            # the frame from the file (see ``_set_text_frame``).
            assert "mbid" not in tags
            assert "year" not in tags
            assert tags["title"] == "Song"
        finally:
            os.unlink(path)

    def test_sync_no_path_returns_false(self, monkeypatch):
        from services.metadata import tag_file_service as tfs

        monkeypatch.setattr(tfs, "get_track_tags", lambda track_id: {"title": "Song"})
        monkeypatch.setattr(tfs, "_get_track_file_path", lambda track_id: None)
        monkeypatch.setattr(tfs, "write_tags_to_file", lambda fp, tags: True)
        assert tfs.sync_track_tags_to_file("track-1") is False


class TestLiveAlbumFalsePositive:
    """'(how to live) as ghosts' must NOT be a live album (no 4★ cap)."""

    def test_how_to_live_is_not_live_album(self):
        from services.catalog.album_classification_service import (
            detect_live_album_type,
            is_live_or_alternate_album,
        )

        assert is_live_or_alternate_album("(how to live) as ghosts") is False
        assert detect_live_album_type("(how to live) as ghosts") == ""

    def test_real_live_albums_still_detected(self):
        from services.catalog.album_classification_service import (
            detect_live_album_type,
            is_live_or_alternate_album,
        )

        for title in ["The Wall (Live)", "Live at Pompeii", "Song - Live",
                      "Live in Tokyo", "MTV Unplugged", "Album Live"]:
            assert is_live_or_alternate_album(title) is True, title
            assert detect_live_album_type(title) != "", title

    def test_prepare_album_context_does_not_flag_how_to_live(self):
        from services.popularity.scan_hooks import prepare_album_context

        ctx = prepare_album_context(
            artist="Ghosts",
            album="(how to live) as ghosts",
            tracks=[{"id": "t1", "title": "Song", "artist": "Ghosts",
                     "album": "(how to live) as ghosts"}],
        )
        assert ctx["is_live_album"] is False
        assert ctx["live_album_type"] == ""

class TestLiveAlbumTypeIsAuthoritative:
    """The album TYPE is the live-album signal; title heuristics are fallback.

    Live-album detection always uses the album type field when the album is
    matched (MusicBrainz/Spotify).  A matched type that says "album" wins even
    when the title contains live-looking text; the title heuristic only runs
    for albums with NO matched type.
    """

    def test_type_album_overrides_live_looking_title(self):
        from services.catalog.album_classification_service import detect_live_album_type

        assert detect_live_album_type("The Wall (Live)", "album") == ""
        assert detect_live_album_type("(how to live) as ghosts", "album") == ""

    def test_type_live_overrides_plain_title(self):
        from services.catalog.album_classification_service import detect_live_album_type

        assert detect_live_album_type("Anything At All", "album+live") == "live"
        assert detect_live_album_type("Anything At All", "album+acoustic") == "acoustic"

    def test_no_type_uses_title_fallback(self):
        from services.catalog.album_classification_service import detect_live_album_type

        assert detect_live_album_type("Song - Live") == "live"
        assert detect_live_album_type("Live at Pompeii") == "live"
        assert detect_live_album_type("(how to live) as ghosts") == ""

    def test_prepare_album_context_type_wins(self):
        from services.popularity.scan_hooks import prepare_album_context

        ctx = prepare_album_context(
            artist="Pink Floyd",
            album="The Wall (Live)",
            tracks=[{"id": "t1", "title": "Song", "artist": "Pink Floyd",
                     "album": "The Wall (Live)", "musicbrainz_albumtype": "album"}],
            musicbrainz_album_type="album",
        )
        assert ctx["is_live_album"] is False
        assert ctx["live_album_type"] == ""

        ctx2 = prepare_album_context(
            artist="Pink Floyd",
            album="The Wall (Live)",
            tracks=[{"id": "t1", "title": "Song", "artist": "Pink Floyd",
                     "album": "The Wall (Live)", "musicbrainz_albumtype": "album+live"}],
            musicbrainz_album_type="album+live",
        )
        assert ctx2["is_live_album"] is True
        assert ctx2["live_album_type"] == "live"

    def test_should_exclude_uses_album_type(self):
        from services.catalog.album_classification_service import should_exclude_track_from_stats

        # Matched studio album whose title looks live: tracks stay eligible.
        assert should_exclude_track_from_stats(
            "Song", "The Wall (Live)", album_type="album"
        ) is False
        # Unmatched album falls back to the title heuristic.
        assert should_exclude_track_from_stats("Song", "The Wall (Live)") is True

    def test_mb_secondary_live_type_maps_to_live(self, monkeypatch):
        import services.enrichment.musicbrainz_service as mbm
        from services.popularity.stages import album_stage as a

        # ``_lookup_musicbrainz_album_type`` calls the PROCESS-SINGLETON
        # ``get_shared_mb_service()``, not ``MusicBrainzService(...)`` inline.
        # Patching the class therefore only affects the first call (the
        # singleton caches the instance created under it). Patch the seam the
        # function actually uses, and give each run a fresh fake.
        def run(secondary):
            class FakeSvc:
                def __init__(self, *args, **kwargs):
                    pass

                def search_releasegroup_matches(self, artist, album, limit=3):
                    return [{"id": "rg1", "title": album, "primary_type": "album",
                             "secondary_types": secondary, "match_score": 0.95}]

            monkeypatch.setattr(a, "get_shared_mb_service", lambda: FakeSvc())
            return a._lookup_musicbrainz_album_type("Pink Floyd", "The Wall (Live)")

        assert run(["live"]) == ("album+live", "rg1")
        assert run([]) == ("album", "rg1")
        assert run(["remix"]) == ("album+remix", "rg1")
        # The composite type carries exactly ONE secondary label, chosen by the
        # ordered ``_SECONDARY_ORDER`` in ``_lookup_musicbrainz_album_type``,
        # which ranks ``compilation`` above ``live``. A release that is BOTH
        # therefore resolves to ``album+compilation``.
        assert run(["live", "compilation"]) == ("album+compilation", "rg1")


class TestHowToLiveStarRating:
    """'(how to live) as ghosts' must not cap tracks at 4★ (no live detection)."""

    def test_how_to_live_title_is_not_treated_as_live(self):
        """'(how to live) as ghosts' must not be flagged as a live recording.

        The album title is a STUDIO album whose name merely contains the word
        "live"; the classification must not read it as a live release. (The
        old assertion pinned 5★ from single status alone, but there is no
        unconditional single→5★ rule — the point this test guards is the LIVE
        misclassification, so assert on that directly.)
        """
        from services.catalog.album_classification_service import (
            is_live_or_alternate_track_title,
        )
        from services.popularity.stages.finalise_stage import _assign_stars

        assert is_live_or_alternate_track_title("Song") is False

        track = {
            "track_id": "t1", "artist": "Ghosts", "album": "(how to live) as ghosts",
            "title": "Song", "popularity_score": 99.0,
            "is_single": True, "single_confidence": "high",
            "is_live": False, "popularity_marked": False, "single_sources": "",
        }
        album = [40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 99.0]
        # Not a live track → it is eligible for the album's top band (4★); it is
        # NOT capped to 3★ the way a live recording would be.
        assert _assign_stars(track, album, album) == 4



