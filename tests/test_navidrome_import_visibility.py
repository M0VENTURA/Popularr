"""Regression tests for Navidrome-import progress visibility.

Two independent defects made the scan runner look like it hung during the
Navidrome import phase:

1. **Filtered logging.**  ``services/log_service._scan_activity_filter`` is the
   allow-list the dashboard scanning panel (and the /logs unified view) applies
   in non-verbose mode.  It matched the literal string ``Navidrome Import``
   (with a space) but NONE of the bracketed prefixes the import actually emits
   (``[NAVIDROME_IMPORT]``, ``[NAVIDROME_SCAN]``).  The user therefore saw
   ``Step 1/3: Navidrome import for album '...'`` and then nothing at all for
   the whole import — indistinguishable from a stalled scan.

2. **Silent phases.**  ``scan_artist_to_db`` logged at entry and then again
   only at the first album, so the DB prefetch, the row normalisation and the
   single ``getArtist`` call (a 60 s-timeout request that returns the FULL
   album list, even for an album-filtered scan) produced no output whatsoever.

3. **Silent no-match.**  An ``album_filter`` that matched no Navidrome album
   returned ``None`` with no message, so the pipeline advanced to step 2/3
   with no import having happened and no explanation.
"""

from __future__ import annotations

import pytest

from db.engine import db_session
from services.log_service import _scheduler_noise_filter, _scan_activity_filter
from services.scanning.navidrome_import import scan_artist_to_db
from sqlalchemy import text


def _kept(line: str) -> bool:
    return bool(
        _scan_activity_filter().search(line)
        and not _scheduler_noise_filter().search(line)
    )


@pytest.fixture(autouse=True)
def _isolated_tracks():
    """Empty the shared in-memory ``tracks`` table around every test."""
    def _wipe() -> None:
        with db_session() as session:
            session.execute(text("DELETE FROM tracks"))

    _wipe()
    yield
    _wipe()


# ---------------------------------------------------------------------------
# 1. The scan-activity filter must keep the import's bracketed prefixes
# ---------------------------------------------------------------------------


class TestImportPrefixesVisible:
    """Bracketed Navidrome import prefixes must reach the scanning panel."""

    def test_navidrome_import_prefix_visible(self):
        line = "[NAVIDROME_IMPORT] Importing artist: Various Artists (artist_id=abc, force=False, processed=0)"
        assert _kept(line)

    def test_navidrome_scan_prefix_visible(self):
        line = "[NAVIDROME_SCAN] fetch_artist_albums returned empty — skipping import + cleanup"
        assert _kept(line)

    def test_popularity_pipeline_prefix_visible(self):
        line = "[POPULARITY_PIPELINE] Starting scan (artist=Muse, verbose=True, force=False)"
        assert _kept(line)

    def test_spaced_navidrome_import_still_visible(self):
        """The pre-existing ``Navidrome Import`` alternative must not regress."""
        line = "Navidrome Import - Muse - Album 1/12: Absolution"
        assert _kept(line)

    def test_album_pipeline_step_lines_visible(self):
        assert _kept("Step 1/3: Navidrome import for album 'Muse - Absolution'")
        assert _kept("Step 2/3: Popularity scan for album 'Muse - Absolution'")
        assert _kept("Step 3/3: Auto-detecting album type for 'Muse - Absolution'")


class TestQueueNoiseStillExcluded:
    """Broadening the filter must not drag queue traffic into the scan panel."""

    def test_queue_line_still_filtered(self):
        line = "[QUEUE] Muse - Hysteria → imported to library (match=metadata)"
        assert not _kept(line)

    def test_scheduler_noise_still_filtered(self):
        line = "APScheduler: registered download_queue_processor (every 60 s)"
        assert not _kept(line)


# ---------------------------------------------------------------------------
# 2 + 3. The import must narrate its phases and report a no-match filter
# ---------------------------------------------------------------------------


class _FakeAlbumClient:
    """Minimal Navidrome client exposing just the album fetch."""

    def __init__(self, albums):
        self._albums = albums
        self.album_fetches = 0

    def fetch_artist_albums(self, artist_id):
        self.album_fetches += 1
        return self._albums

    def fetch_album_tracks(self, album_id):
        return {"tracks": [], "artist": "", "artistId": "", "name": "", "id": album_id}


@pytest.fixture
def _captured_logs(monkeypatch):
    """Capture ``log_unified`` output from ``navidrome_import``."""
    import services.scanning.navidrome_import as ni

    logged: list[str] = []
    monkeypatch.setattr(ni, "log_unified", lambda msg: logged.append(str(msg)))
    # Keep the DB-row housekeeping quiet and side-effect free.
    monkeypatch.setattr(ni, "normalize_existing_artist_rows_safe", lambda **k: None)
    monkeypatch.setattr(ni, "sanitize_artist_rows_safe", lambda **k: None)
    return logged


class TestImportPhaseLogging:
    """Each silent phase must emit a progress line."""

    def test_prefetch_and_fetch_phases_logged(self, _captured_logs):
        client = _FakeAlbumClient([{"id": "al-1", "name": "Absolution", "songCount": 0}])

        scan_artist_to_db("Muse", "ar-1", force=True, client=client)

        text_all = "\n".join(_captured_logs)
        assert "Prefetch complete for 'Muse'" in text_all
        assert "known track(s)" in text_all
        assert "Normalised local rows for 'Muse'" in text_all
        assert "Fetched 1 album(s) for 'Muse'" in text_all

    def test_album_filter_no_match_warns(self, _captured_logs):
        """A filter matching no Navidrome album must explain itself."""
        client = _FakeAlbumClient([{"id": "al-1", "name": "Absolution", "songCount": 3}])

        scan_artist_to_db("Muse", "ar-1", force=True, album_filter="Nonexistent", client=client)

        text_all = "\n".join(_captured_logs)
        assert "No album matched the filter 'Nonexistent'" in text_all
        assert "none of which matched by name" in text_all

    def test_album_filter_match_is_not_reported_as_missing(self, _captured_logs):
        """The no-match warning must not fire when the filter DOES match."""
        client = _FakeAlbumClient([{"id": "al-1", "name": "Absolution", "songCount": 0}])

        scan_artist_to_db("Muse", "ar-1", force=True, album_filter="Absolution", client=client)

        assert "No album matched the filter" not in "\n".join(_captured_logs)
