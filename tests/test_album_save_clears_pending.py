"""A completed save must retire the album page's "pending" badge.

``metadata_dirty_for_navidrome`` drives that badge. It was cleared only by a
full Navidrome import, so every saved album stayed "pending" indefinitely —
the reported "all the albums I'm updated are showing Pending, but not removing
the pending once the save has completed".
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _empty_tracks(db_session):
    db_session.execute(text("DELETE FROM tracks"))
    db_session.commit()


def _seed(db_session, track_id: str, *, dirty: bool = True):
    db_session.execute(
        text("""
            INSERT INTO tracks (
                id, artist, album, album_artist, file_path,
                local_tags_written_at, metadata_dirty_for_navidrome
            ) VALUES (
                :id, 'Korn', 'Greatest Hits', 'Korn', :path,
                CURRENT_TIMESTAMP, :dirty
            )
            ON CONFLICT DO NOTHING
        """),
        {
            "id": track_id,
            "path": f"/music/Korn/Greatest Hits/{track_id}.mp3",
            "dirty": dirty,
        },
    )
    db_session.commit()


def _row(db_session, track_id: str) -> dict:
    row = db_session.execute(
        text(
            "SELECT metadata_dirty_for_navidrome, local_tags_written_at "
            "FROM tracks WHERE id = :id"
        ),
        {"id": track_id},
    ).fetchone()
    return {"dirty": bool(row[0]), "stamped": row[1] is not None}


def test_a_finished_save_clears_the_pending_flag(db_session):
    from services.scanning.reconciliation import confirm_local_writes

    _seed(db_session, "t1")

    assert confirm_local_writes(["t1"]) == 1
    db_session.expire_all()

    got = _row(db_session, "t1")
    assert got["dirty"] is False, "the badge would stay 'pending'"
    assert got["stamped"] is True, (
        "local_tags_written_at must survive: the import's freshness gate uses it"
    )


def test_the_disk_phase_clears_after_a_successful_write_and_rescan():
    import routes.ui_routes as ui_routes

    source = inspect.getsource(ui_routes._run_album_file_sync_phases)
    assert "confirm_local_writes" in source
    assert "rescanned and file_failures == 0" in source, (
        "clearing when a file write failed would hide an unfinished save"
    )


def test_the_scan_also_retires_a_pending_row_it_verified():
    """A scan that read the file and matched (or filled) it is confirmation."""
    import services.metadata.album_tag_sync_service as sync

    source = inspect.getsource(sync)
    assert "confirm_local_writes" in source
    assert "_failed_ids" in source, (
        "a failed tag write must keep the pending flag — otherwise the badge "
        "disappears while the file still holds the old values"
    )


def test_the_import_still_rescans_after_a_recent_local_write():
    """Clearing the flag must not let a stale Navidrome index be imported."""
    from services.scanning.pipelines import navidrome_pipeline

    source = inspect.getsource(navidrome_pipeline.sync_remote_navidrome_before_import)
    assert "latest_local_tag_write_epoch" in source
    assert "local_write_unimported" in source, (
        "the dirty flag is no longer the only reason to scan; recent local "
        "writes must force the same freshness check"
    )
