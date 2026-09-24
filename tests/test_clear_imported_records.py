"""Tests for clearing the IMPORTED history so tracks can be downloaded again.

REPORTED: "I want to be able to clear imported. Files were removed from the
database and I need to redownload them."

THE DEAD END (verified, not assumed):

1. ``queue_clear`` hard-coded ``DELETE FROM download_queue WHERE status !=
   'imported'`` — imported rows were EXCLUDED, and that was the ONLY behaviour.
   The UI's "Clear Queue" button even said "(keeps imported records)". So there
   was no way to remove them at all.

2. ``album_missing_service.get_missing_tracks`` treats an ``imported`` queue row
   as QUEUE COVERAGE — its status list is
   ``('queued','searching','downloading','processing','moving','imported',
   'in_collection','matched','completed')`` and any track matching one of those
   rows is skipped from the missing set.

So a stale ``imported`` row for a track whose files were deleted keeps the track
OFF the missing list, and "Download Missing Tracks" never offers it again. That
is the actual reason the redownload was impossible — deleting the library rows
alone is not enough.

``filters.status`` is the deliberate escape hatch: the default clear still keeps
imported (wiping library history as a side effect of a routine clear would be
destructive), but it can now be requested explicitly.
"""

from __future__ import annotations

import inspect
import os
import tempfile

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


# ---------------------------------------------------------------------------
# 1. The service contract
# ---------------------------------------------------------------------------

class TestQueueClearAcceptsAnImportedFilter:
    """The escape hatch that did not exist."""

    def test_the_default_branch_still_excludes_imported(self):
        """⚠️ The safeguard must SURVIVE this change.

        Clearing the queue must not silently wipe the library history as a side
        effect — that is why the exclusion exists. This pins it stays the
        default while the explicit filter is available.
        """
        import services.queue.queue_processing_service as qps

        source = inspect.getsource(qps.queue_clear)
        code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
        assert "status != :status" in code, (
            "the default clear must still exclude one status (imported)"
        )
        assert '"imported"' in code, (
            "the default clear must exclude 'imported' specifically, or clearing "
            "the queue destroys the record of what is already in the library"
        )

    def test_imported_can_be_selected_explicitly(self):
        """The fix: ``{"filters": {"status": "imported"}}`` must reach a DELETE
        of exactly that status, rather than being caught by the default branch."""
        import services.queue.queue_processing_service as qps

        source = inspect.getsource(qps.queue_clear)
        assert "filters.get(\"status\")" in source, (
            "queue_clear must read filters.status, or the imported status can "
            "never be selected and the rows are unremovable"
        )
        # The selected-status branch must run BEFORE the default branch.
        assert source.index('filters.get("statuses")') < source.index("status != :status"), (
            "the explicit-filter branch must be checked before the default, or an "
            "explicit 'imported' request falls through to the exclusion"
        )

    def test_an_unknown_status_is_rejected_not_silently_noop(self):
        """⭐ A DELETE with a status that matches nothing reports success and
        deletes 0 rows, which reads to the user as "cleared". Reject it."""
        import services.queue.queue_processing_service as qps

        source = inspect.getsource(qps.queue_clear)
        assert "ALL_QUEUE_STATUSES" in source, (
            "queue_clear must validate the requested statuses; otherwise a typo "
            "reports a successful clear that removed nothing"
        )
        assert "Unknown queue status" in source

    def test_a_list_of_statuses_is_supported(self):
        """Terminal history can be the pair imported + in_collection."""
        import services.queue.queue_processing_service as qps

        source = inspect.getsource(qps.queue_clear)
        assert 'filters.get("statuses")' in source, (
            "a multi-status clear must be possible for the terminal-history pair"
        )
        assert "status IN (" in source, "the multi-status branch must use IN (...)"


# ---------------------------------------------------------------------------
# 2. Behaviour against a real table
# ---------------------------------------------------------------------------

def _make_engine():
    tmp = tempfile.mkdtemp()
    return create_engine(f"sqlite:///{os.path.join(tmp, 'q.db')}")


@pytest.fixture()
def queue_db(monkeypatch):
    engine = _make_engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE download_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                artist TEXT, title TEXT, album TEXT, album_artist TEXT,
                status TEXT DEFAULT 'queued', source TEXT DEFAULT 'soulseek',
                track_number TEXT, disc_number INTEGER, file_path TEXT,
                matched_file_path TEXT, music_file_path TEXT,
                import_group TEXT, created_at TEXT, updated_at TEXT
            )
        """))

    class _S:
        def __init__(self, s):
            self._s = s

        def execute(self, *a, **kw):
            return self._s.execute(*a, **kw)

        def commit(self):
            self._s.commit()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, *exc):
            if exc_type is None:
                self._s.commit()
            self._s.close()
            return False

    session = factory()
    monkeypatch.setattr(
        "services.queue.queue_processing_service.db_session", lambda *a, **kw: _S(session)
    )

    def seed(*rows):
        with engine.begin() as conn:
            for status, title in rows:
                conn.execute(text(
                    "INSERT INTO download_queue (artist, title, album, status, source) "
                    "VALUES ('Artist', :t, 'Album', :s, 'soulseek')"
                ), {"t": title, "s": status})

    def titles():
        with engine.begin() as conn:
            return [r[0] for r in conn.execute(text("SELECT title FROM download_queue"))]

    return seed, titles


class TestClearImportedBehaviour:
    def test_clearing_imported_removes_only_imported(self, queue_db):
        """The reported need: the imported history goes, everything else stays."""
        seed, titles = queue_db
        seed(("imported", "Old One"), ("imported", "Old Two"),
             ("queued", "Active"), ("failed", "Broken"), ("completed", "Ready"))

        import services.queue.queue_processing_service as qps
        result = qps.queue_clear({"filters": {"status": "imported"}})

        assert result["success"] is True
        assert result["deleted"] == 2
        assert sorted(titles()) == ["Active", "Broken", "Ready"]

    def test_the_rows_can_then_be_cleared_again_without_effect(self, queue_db):
        """Idempotent: a second clear deletes nothing and says so."""
        seed, titles = queue_db
        seed(("imported", "Old One"))

        import services.queue.queue_processing_service as qps
        assert qps.queue_clear({"filters": {"status": "imported"}})["deleted"] == 1
        second = qps.queue_clear({"filters": {"status": "imported"}})
        assert second["success"] is True
        assert second["deleted"] == 0

    def test_the_default_clear_still_keeps_imported(self, queue_db):
        """⚠️ The safeguard is genuinely preserved end to end."""
        seed, titles = queue_db
        seed(("imported", "History"), ("queued", "Active"))

        import services.queue.queue_processing_service as qps
        qps.queue_clear({})

        assert "History" in titles(), (
            "a routine clear removed the imported history — that is destructive "
            "and must require the explicit filter"
        )
        assert "Active" not in titles()

    def test_an_unknown_status_deletes_nothing_and_reports_failure(self, queue_db):
        seed, titles = queue_db
        seed(("imported", "History"))

        import services.queue.queue_processing_service as qps
        result = qps.queue_clear({"filters": {"status": "not_a_status"}})

        assert result["success"] is False
        assert "valid_statuses" in result
        assert titles() == ["History"], "a rejected request must not delete anything"

    def test_a_status_list_clears_both_terminal_histories(self, queue_db):
        seed, titles = queue_db
        seed(("imported", "Imp"), ("in_collection", "Col"), ("queued", "Active"))

        import services.queue.queue_processing_service as qps
        result = qps.queue_clear({"filters": {"statuses": ["imported", "in_collection"]}})

        assert result["deleted"] == 2
        assert titles() == ["Active"]


# ---------------------------------------------------------------------------
# 3. Why this unblocks a redownload — the actual reported symptom
# ---------------------------------------------------------------------------

class TestTheImportedRowWasWhatBlockedTheRedownload:
    """⭐ Pins the MECHANISM so the UI button is not mistaken for the whole fix.

    An ``imported`` queue row counts as queue coverage in
    ``album_missing_service``, so the track is skipped from the missing set and
    "Download Missing Tracks" never offers it. Deleting only the library rows
    therefore does NOT restore it — the queue row must go too.
    """

    def test_the_missing_service_counts_imported_as_coverage(self):
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1]
            / "services" / "metadata" / "album_missing_service.py"
        ).read_text(encoding="utf-8")

        assert "get_missing_tracks" in source
        # Locate the queue-coverage query and assert 'imported' is in its status list.
        idx = source.index("Download-queue coverage")
        window = source[idx: idx + 2000]
        assert "'imported'" in window or '"imported"' in window, (
            "imported must be counted as queue coverage, which is WHY a stale "
            "imported row suppresses the missing-track list"
        )

    def test_clearing_imported_restores_the_track_to_the_missing_set(self):
        """The end-to-end consequence, expressed on the coverage rule itself.

        Reproduces the rule the service uses rather than driving MusicBrainz: a
        track is skipped when a queue row covers it. With the imported row
        present it is skipped; after clearing imported rows it is not.
        """
        coverage_statuses = {
            "queued", "searching", "downloading", "processing", "moving",
            "imported", "in_collection", "matched", "completed",
        }

        def is_covered(queue_rows, title):
            return any(
                row["title"] == title and row["status"] in coverage_statuses
                for row in queue_rows
            )

        rows = [{"title": "Lost Song", "status": "imported"}]
        assert is_covered(rows, "Lost Song"), "fixture sanity"

        # After the user clears the imported rows, nothing covers the track, so it
        # becomes visible as missing and can be downloaded again.
        remaining = [r for r in rows if r["status"] != "imported"]
        assert not is_covered(remaining, "Lost Song"), (
            "the track is still reported as covered after clearing imported, so "
            "it would still never be offered for download"
        )


# ---------------------------------------------------------------------------
# 4. The UI must expose it in BOTH trees
# ---------------------------------------------------------------------------

class TestBothTreesExposeClearImported:
    @pytest.mark.parametrize(
        "template",
        [
            "templates/components/search/_queue_status.html",
            "test_site/templates/components/search/_queue_status.html",
        ],
    )
    def test_the_button_exists_and_calls_a_real_handler(self, template):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / template).read_text(encoding="utf-8")
        assert "clearImportedRecords()" in source, (
            f"{template} offers no way to clear imported records"
        )
        # And the old misleading label must not imply imported is clearable by
        # the other button.
        assert "keeps the imported history" in source or "keep imported" in source

    @pytest.mark.parametrize(
        "rel",
        [
            "static/js/downloads.js",
            "test_site/static/js/pages/download-queue.js",
        ],
    )
    def test_the_handler_sends_the_imported_filter(self, rel):
        """The handler must select ``filters.status = imported`` explicitly. If it
        called the default clear it would delete everything EXCEPT imported —
        the exact opposite of what the button promises."""
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")
        assert "clearImportedRecords" in source, f"{rel} does not define the handler"
        assert "imported" in source

        start = source.index("clearImportedRecords")
        body = source[start: start + 1800]
        assert "filters" in body and "imported" in body, (
            f"{rel}: the handler must pass filters.status = 'imported'"
        )

    def test_the_rebuilt_handler_is_exported(self):
        """The rebuilt tree's onclick handlers resolve through globals; a
        definition without an export is a dead button."""
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1]
            / "test_site" / "static" / "js" / "pages" / "download-queue.js"
        ).read_text(encoding="utf-8")
        assert "global.clearImportedRecords = clearImportedRecords" in source, (
            "clearImportedRecords is defined but never published, so "
            "onclick=\"clearImportedRecords()\" throws ReferenceError"
        )


# ---------------------------------------------------------------------------
# 5. The UI must tell the truth about the extra step
# ---------------------------------------------------------------------------

class TestTheUiWarnsThatARescanIsNeeded:
    """⭐ Clearing `imported` is necessary but NOT sufficient on its own.

    The album page's missing-tracks list is read from ``missing_album_tracks``,
    a snapshot only the SCAN refreshes (``scan_stage_runner`` →
    ``get_missing_tracks``; the endpoint is deliberately database-only so an
    artist page load cannot fire one MusicBrainz call per owned album).

    So after clearing imported, the track is no longer *covered*, but it does not
    appear on the album page until a rescan recomputes the snapshot. Promising
    "they will show as missing again" without that caveat would send the user
    looking for a track that is not there yet — the same class of
    false-success the queue work already fixed.
    """

    @pytest.mark.parametrize(
        "rel",
        [
            "static/js/downloads.js",
            "test_site/static/js/pages/download-queue.js",
        ],
    )
    def test_the_handler_mentions_rescanning(self, rel):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")
        start = source.index("clearImportedRecords")
        body = source[start: start + 2600].lower()
        assert "scan" in body, (
            f"{rel}: the Clear Imported flow must tell the user to re-scan, or "
            "they will expect the track to appear immediately and it will not"
        )

    def test_the_missing_list_really_is_a_scan_refreshed_snapshot(self):
        """Pins the REASON for the caveat, so it cannot be 'cleaned up' by
        someone who assumes the endpoint recomputes."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        endpoint = (root / "routes" / "album_routes.py").read_text(encoding="utf-8")
        scanner = (root / "services" / "popularity" / "scan_stage_runner.py").read_text(
            encoding="utf-8"
        )

        assert "get_missing_tracks_from_db" in endpoint, (
            "the missing-tracks endpoint must stay database-only"
        )
        assert "get_missing_tracks(" in scanner, (
            "the scan must be the thing that refreshes the missing snapshot — "
            "if this moved, the Clear Imported caveat may no longer be needed"
        )
