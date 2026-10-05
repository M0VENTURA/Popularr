"""Album page: select multiple tracks → delete them all.

Request
-------
> When selecting multiple tracks on the album page, I want to be able to
> select to delete all selected

What already existed, and why the user still could not do it:

* ``POST /api/v1/albums/<artist>/<album>/bulk-delete`` — whose docstring
  already says it *"restores the album page's multi-select 'Delete Selected'
  flow"*, i.e. the endpoint was written for this button;
* ``confirmBulkDeleteTracks`` / ``deleteDatabaseOnly`` / ``deleteWithFiles`` /
  ``_performBulkDelete`` in **both** trees' album JS;
* the selection toolbar with its "Rename Selected" button.

What was missing was the **button and the modal**: ``#bulkDeleteModal`` and
``#deleteTrackCount`` were referenced by JS that nothing could reach, so the
entire flow was dead code — selecting tracks offered only Rename.

Pinned here:

* the toolbar offers the delete action and routes it to
  ``confirmBulkDeleteTracks``;
* the modal exists with the exact IDs the JS reads/writes, and offers Cancel,
  database-only and files+database;
* both album templates actually include that modal;
* both scripts still ship the flow, the endpoint and ``delete_files``;
* the destructive option stays behind a confirmation — deleting audio from
  disk is not a one-click action;
* the JS path and the registered route agree (an anti-drift check: a renamed
  endpoint would otherwise 404 only when a user finally clicks it).
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = [
    REPO_ROOT / "templates" / "pages" / "album_detail.html",
    REPO_ROOT / "test_site" / "templates" / "Pages" / "album_detail.html",
]
PARTIALS = [
    REPO_ROOT / "templates" / "components" / "modals" / "_bulk_delete.html",
    REPO_ROOT / "test_site" / "templates" / "components" / "modals" / "_bulk_delete.html",
]
SCRIPTS = [
    REPO_ROOT / "static" / "js" / "album_detail.js",
    REPO_ROOT / "test_site" / "static" / "js" / "pages" / "album.js",
]

TREES = ["live", "test_site"]


# ===========================================================================
# 1. The toolbar offers it
# ===========================================================================
class TestTheToolbarOffersDelete:
    @pytest.mark.parametrize("template", TEMPLATES, ids=TREES)
    def test_a_delete_button_sits_beside_rename(self, template: Path):
        html = template.read_text(encoding="utf-8")
        assert 'onclick="confirmBulkDeleteTracks()"' in html, (
            f"{template.name}: selecting tracks still only offers Rename — the "
            "bulk-delete flow exists in JS and has no way to be reached"
        )
        toolbar = html.split('id="bulkActionsToolbar"')[1].split("</div>")[0]
        assert "Delete" in toolbar, "the delete action must live in the toolbar"
        assert "renameSelectedTracks" in toolbar, (
            "delete must sit alongside rename, not replace it"
        )

    @pytest.mark.parametrize("template", TEMPLATES, ids=TREES)
    def test_the_button_is_danger_styled(self, template: Path):
        """A destructive action must not look like Rename."""
        html = template.read_text(encoding="utf-8")
        row = next(
            line for line in html.splitlines()
            if "confirmBulkDeleteTracks()" in line
        )
        assert "btn-danger" in row or "outline-danger" in row, row.strip()


# ===========================================================================
# 2. The modal the JS drives exists
# ===========================================================================
class TestTheModalExists:
    @pytest.mark.parametrize("partial", PARTIALS, ids=TREES)
    def test_it_carries_the_ids_the_javascript_uses(self, partial: Path):
        html = partial.read_text(encoding="utf-8")
        # confirmBulkDeleteTracks() writes this before showing the modal.
        assert 'id="deleteTrackCount"' in html, (
            "the JS fills #deleteTrackCount, so a modal without it shows a stale 0"
        )
        assert 'id="bulkDeleteModal"' in html
        assert 'data-bs-dismiss="modal"' in html, "Cancel must be able to close it"

    @pytest.mark.parametrize("partial", PARTIALS, ids=TREES)
    def test_both_outcomes_are_offered(self, partial: Path):
        html = partial.read_text(encoding="utf-8")
        assert 'onclick="deleteDatabaseOnly()"' in html, (
            "rows can come back from a Navidrome rescan — keep the reversible option"
        )
        assert 'onclick="deleteWithFiles()"' in html

    @pytest.mark.parametrize("template", TEMPLATES, ids=TREES)
    def test_the_album_page_includes_the_partial(self, template: Path):
        html = template.read_text(encoding="utf-8")
        assert "components/modals/_bulk_delete.html" in html, (
            f"{template.name} never includes the modal, so the buttons open nothing"
        )


# ===========================================================================
# 3. The scripts still drive it
# ===========================================================================
class TestTheScriptsDriveTheFlow:
    @pytest.mark.parametrize("script", SCRIPTS, ids=TREES)
    def test_the_flow_is_present(self, script: Path):
        source = script.read_text(encoding="utf-8")
        for name in ("confirmBulkDeleteTracks", "deleteDatabaseOnly", "deleteWithFiles"):
            assert name in source, f"{script.name} no longer ships {name}"

    @pytest.mark.parametrize("script", SCRIPTS, ids=TREES)
    def test_it_posts_the_endpoint_the_route_registers(self, script: Path):
        source = script.read_text(encoding="utf-8")
        assert "/bulk-delete" in source
        assert "delete_files" in source, (
            "the body must say whether audio goes too — the server defaults it"
        )
        assert "track_ids" in source

    @pytest.mark.parametrize("script", SCRIPTS, ids=TREES)
    def test_the_count_is_written_before_the_modal_opens(self, script: Path):
        source = script.read_text(encoding="utf-8")
        assert "deleteTrackCount" in source, (
            "the modal's count element and the JS that fills it must agree"
        )

    @pytest.mark.parametrize("script", SCRIPTS, ids=TREES)
    def test_deleting_from_disk_stays_behind_a_confirmation(self, script: Path):
        """Audio removal is irreversible; a stray click must not do it."""
        source = script.read_text(encoding="utf-8")
        start = source.index("deleteWithFiles")
        body = source[start: start + 2000]
        assert "confirm" in body.lower(), (
            f"{script.name}: deleteWithFiles() asks no questions before "
            "removing files from disk"
        )


# ===========================================================================
# 4. JS and server agree on the endpoint
# ===========================================================================
class TestTheEndpointIsRegistered:
    def test_the_route_the_scripts_call_exists(self):
        source = (REPO_ROOT / "routes" / "api_v1" / "albums.py").read_text(
            encoding="utf-8"
        )
        assert '/albums/<path:artist>/<path:album>/bulk-delete' in source, (
            "both album scripts POST here; a rename would 404 only when clicked"
        )
        assert 'methods=["POST"]' in source

    def test_the_api_v1_package_is_actually_registered(self):
        """`routes/api_v1` once sat unimported, so every call 404'd.

        The file looked correct and was documented as part of the package —
        only the missing import kept it out of the app. The button I am adding
        would be dead in exactly the same way.
        """
        source = (REPO_ROOT / "helpers" / "app_bootstrap.py").read_text(
            encoding="utf-8"
        )
        assert "from routes.api_v1 import api_v1_bp" in source
        assert "api_v1_bp" in source.split("register_all_blueprints")[1]

    def test_the_service_reads_the_body_the_scripts_send(self):
        source = (
            REPO_ROOT / "services" / "metadata" / "album_service.py"
        ).read_text(encoding="utf-8")
        assert 'payload.get("track_ids")' in source
        assert 'payload.get("delete_files"' in source
