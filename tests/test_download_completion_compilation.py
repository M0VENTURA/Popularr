"""Regression tests for download-completion matching of compilation albums.

The reported bug: downloads for compilation albums (e.g. "Greatest Hits") showed
as failed even though the files were present in the downloads folder. Two defects
combined to cause it:

1. ``_metadata_matches_queue_item`` hard-rejected files whose embedded artist tag
   was missing or a generic compilation placeholder ("Various Artists"/"VA"),
   even when the title matched and the filename was a perfect match.  The
   completion matching then skipped those files without trying filename scoring,
   so they were never imported and eventually marked failed.
2. ``_monitored_downloads_dir`` / the completion walk resolved the downloads
   directory with the ``Music`` subfolder preference, while the discovery scan
   that surfaces files on the monitor page scans the downloads root.  With a
   ``Music`` subfolder present, the completion walk never saw the downloaded
   files at all.
"""

from __future__ import annotations

from services.queue.queue_metadata_matcher import _metadata_matches_queue_item


class TestMetadataMatcherCompilationTolerance:
    """A compilation-track file must defer to filename scoring, not hard-reject."""

    def _queue_item(self) -> dict:
        return {
            "artist": "Spice Girls",
            "title": "Headlines (Friendship Never Ends)",
            "album": "Greatest Hits (2007)",
            "album_artist": "Spice Girls",
            "track_number": "13",
            "duration": None,
        }

    def test_missing_artist_tag_defers_to_filename(self, monkeypatch):
        # A FLAC whose embedded metadata has a matching title but no artist tag.
        # _metadata_matches_queue_item returns True/False/None; a missing artist
        # on a compilation must return None (defer), never False (hard reject).
        import os
        import numpy as np
        import soundfile as sf
        from mutagen.flac import FLAC as MFLAC

        path = "/tmp/test_download_completion_compilation_missing.flac"
        sr = 44100
        data = np.zeros(int(sr * 60), dtype=np.float32)
        sf.write(path, data, sr, format="FLAC")
        audio = MFLAC(path)
        audio["title"] = "Headlines (Friendship Never Ends)"
        audio["album"] = "Greatest Hits (2007)"
        audio.save()
        try:
            assert _metadata_matches_queue_item(path, self._queue_item()) is None
        finally:
            os.remove(path)

    def test_various_artists_tag_never_hard_rejects(self, monkeypatch):
        # The downloaded FLAC carries the compilation album artist while the
        # queue item expects the per-track artist.  The critical regression is
        # that this must NOT be a hard `False` (which made completion skip the
        # file without trying the filename match).  `True` is acceptable when
        # the queue album_artist is also generic (it is a legitimate match);
        # `None` defers to filename scoring.  Either way the download imports.
        import os
        import numpy as np
        import soundfile as sf
        from mutagen.flac import FLAC as MFLAC

        path = "/tmp/test_download_completion_compilation_various.flac"
        sr = 44100
        data = np.zeros(int(sr * 60), dtype=np.float32)
        sf.write(path, data, sr, format="FLAC")
        audio = MFLAC(path)
        audio["title"] = "Headlines (Friendship Never Ends)"
        audio["artist"] = "Various Artists"
        audio["album"] = "Greatest Hits (2007)"
        audio.save()
        try:
            assert (
                _metadata_matches_queue_item(path, self._queue_item()) is not False
            )
        finally:
            os.remove(path)

    def test_compilation_album_artist_defers(self, monkeypatch):
        import os
        import numpy as np
        import soundfile as sf
        from mutagen.flac import FLAC as MFLAC

        path = "/tmp/test_download_completion_compilation_albumartist.flac"
        sr = 44100
        data = np.zeros(int(sr * 60), dtype=np.float32)
        sf.write(path, data, sr, format="FLAC")
        audio = MFLAC(path)
        audio["title"] = "Headlines (Friendship Never Ends)"
        audio["album_artist"] = "Various Artists"
        audio["album"] = "Greatest Hits (2007)"
        audio.save()
        try:
            assert _metadata_matches_queue_item(path, self._queue_item()) is None
        finally:
            os.remove(path)

    def test_correct_artist_tag_matches(self, monkeypatch):
        import os
        import numpy as np
        import soundfile as sf
        from mutagen.flac import FLAC as MFLAC

        path = "/tmp/test_download_completion_compilation_correct.flac"
        sr = 44100
        data = np.zeros(int(sr * 60), dtype=np.float32)
        sf.write(path, data, sr, format="FLAC")
        audio = MFLAC(path)
        audio["title"] = "Headlines (Friendship Never Ends)"
        audio["artist"] = "Spice Girls"
        audio["album"] = "Greatest Hits (2007)"
        audio.save()
        try:
            assert _metadata_matches_queue_item(path, self._queue_item()) is True
        finally:
            os.remove(path)

    def test_wrong_title_still_rejects(self, monkeypatch):
        import os
        import numpy as np
        import soundfile as sf
        from mutagen.flac import FLAC as MFLAC

        path = "/tmp/test_download_completion_compilation_wrong.flac"
        sr = 44100
        data = np.zeros(int(sr * 60), dtype=np.float32)
        sf.write(path, data, sr, format="FLAC")
        audio = MFLAC(path)
        audio["title"] = "Completely Different Song"
        audio["artist"] = "Spice Girls"
        audio.save()
        try:
            assert _metadata_matches_queue_item(path, self._queue_item()) is False
        finally:
            os.remove(path)

    def test_wrong_artist_still_rejects_when_title_partial(self, monkeypatch):
        # A genuinely wrong artist (not a generic compilation placeholder) with a
        # non-exact title must still hard-reject — the compilation tolerance must
        # not open the door to wrong-version imports.
        import os
        import numpy as np
        import soundfile as sf
        from mutagen.flac import FLAC as MFLAC

        path = "/tmp/test_download_completion_compilation_wrongartist.flac"
        sr = 44100
        data = np.zeros(int(sr * 60), dtype=np.float32)
        sf.write(path, data, sr, format="FLAC")
        audio = MFLAC(path)
        audio["title"] = "Totally Unrelated Title (edit)"
        audio["artist"] = "Some Other Band"
        audio.save()
        try:
            assert _metadata_matches_queue_item(path, self._queue_item()) is False
        finally:
            os.remove(path)


class TestMonitoredDownloadsDirResolution:
    """The completion walk must scan the downloads root — the ONLY downloads dir.

    ``resolve_downloads_dir`` no longer takes (or honors) a
    ``prefer_music_subfolder`` kwarg: the Music-subfolder preference was
    retired after a stray empty ``downloads/Music`` directory hijacked every
    default-resolving caller. Passing the old kwarg now raises TypeError, so
    this pins that the call site passes NOTHING.
    """

    def test_resolves_downloads_root(self, monkeypatch):
        from services.downloads import download_completion_service as dcs

        seen = {}

        def fake_resolve(*args, **kwargs):
            seen.update(kwargs)
            return "/downloads"

        monkeypatch.setattr(
            "services.downloads.download_scan_service.resolve_downloads_dir",
            fake_resolve,
        )
        assert dcs._monitored_downloads_dir() == "/downloads"
        assert seen == {}
