"""A saved album id must survive the next Navidrome import.

REPORT

> "They did save, then Navidrome joined them, but then something updated and
>  unlinked them again"

THE UNDO

The album-wide id writer corrected every row and every file, and Navidrome
merged the album. Then the next import split it again.

The import protects a row from being overwritten ONLY when the row is stamped
``metadata_dirty_for_navidrome``:

    if _existing.get("metadata_dirty_for_navidrome"):
        if local_authority_conflicts(existing, payload):  -> local wins

``_persist_album_track_payloads`` stamps the rows the page save itself wrote.
``update_album_ids`` — and ``apply_album_wide_ids``, which delegates to it —
corrected EVERY row of the album but stamped NONE of them. The rows outside the
page (exactly the ones carrying the stale id) were therefore unprotected, and
the payload's ``musicbrainz_albumid`` was written straight over the correction.

A second, independent defect: that writer did not pass ``force=True`` to
``update_file_tags``, so with ``tagging.fill_missing_only`` on, a populated
MUSICBRAINZ_ALBUMID frame was left untouched and the FILE kept the old id —
which Navidrome reads.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest

from services.metadata import album_service
from services.scanning.reconciliation import local_authority_conflicts

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SAVED = "8c16bb1b-7a86-3e3e-9dc6-70f125f04f7c"
REMOTE = "c7cb0b76-fd91-48ed-a3a2-b6f0b723474c"
GROUP = "6d79d579-213d-3172-a860-1cd9bde0ecd5"


def _src(func) -> str:
    return inspect.getsource(func)


class TestTheCorrectionIsStampedAsLocalAuthority:
    def test_the_album_id_writer_stamps_its_rows(self) -> None:
        src = _src(album_service.update_album_ids)
        assert "_mark_local_correction" in src, (
            "the album-wide id write left its rows unprotected, so the next "
            "import overwrote the correction and the album re-split"
        )

    def test_the_clear_path_stamps_too(self) -> None:
        """Deleting an id is a correction; unstamped it simply comes back."""
        src = _src(album_service.apply_album_wide_ids)
        start = src.index("if clear_album_mbid:")
        end = src.index("if album_mbid:", start)   # the next branch
        assert "_mark_local_correction" in src[start:end]

    def test_the_stamp_helper_exists_and_is_used(self) -> None:
        assert hasattr(album_service, "_mark_local_correction")
        assert "metadata_dirty" in _src(album_service._mark_local_correction) or (
            "mark_local_tags_written_for_track_ids"
            in _src(album_service._mark_local_correction)
        )

    def test_it_stamps_every_row_the_update_touched(self) -> None:
        """Not merely the rows whose file path happens to resolve."""
        src = _src(album_service.update_album_ids)
        start = src.index("stamped_ids")
        block = src[start:start + 1200]
        assert "SELECT id FROM tracks" in block, (
            "ids must come from the same predicate as the UPDATE, not from the "
            "file loop, which skips unresolvable paths"
        )


class TestTheStampedRowBeatsTheRemoteValue:
    def test_a_differing_remote_album_id_is_a_conflict(self) -> None:
        existing = {
            "id": "t1", "album": "A", "album_artist": "Various Artists",
            "musicbrainz_albumid": SAVED, "musicbrainz_album_mbid": SAVED,
            "musicbrainz_releasegroupid": GROUP,
            "metadata_dirty_for_navidrome": True,
        }
        payload = {
            "id": "t1", "album": "A", "album_artist": "Various Artists",
            "musicbrainz_albumid": REMOTE, "musicbrainz_album_mbid": REMOTE,
            "musicbrainz_releasegroupid": GROUP,
        }
        conflicts = local_authority_conflicts(existing, payload)
        assert "musicbrainz_albumid" in conflicts
        assert "musicbrainz_album_mbid" in conflicts

    def test_matching_values_are_not_a_conflict(self) -> None:
        same = {
            "id": "t1", "album": "A", "album_artist": "Various Artists",
            "musicbrainz_albumid": SAVED, "musicbrainz_album_mbid": SAVED,
        }
        assert local_authority_conflicts(same, dict(same)) == []


class TestTheFileActuallyGetsTheId:
    def test_the_fan_out_forces_the_write(self) -> None:
        """With ``fill_missing_only`` a populated frame is otherwise skipped."""
        src = _src(album_service.update_album_ids)
        assert "force=True" in src, (
            "the file kept its old MUSICBRAINZ_ALBUMID, which Navidrome reads"
        )

    def test_the_clear_also_forces(self) -> None:
        src = _src(album_service._clear_album_id_file_tags)
        assert "force=True" in src
