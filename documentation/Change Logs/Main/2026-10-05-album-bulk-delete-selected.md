# Album page: Delete Selected (2026-10-05)

**Report:**

> When selecting multiple tracks on the album page, I want to be able to select
> to delete all selected

## The flow existed end to end — except for the button

Every layer below the UI was already written:

| layer | what was there |
|---|---|
| endpoint | `POST /api/v1/albums/<artist>/<album>/bulk-delete`, whose docstring already says it *"restores the album page's multi-select **Delete Selected** flow"* |
| service | `bulk_delete_tracks({track_ids, delete_files})` — deletes the rows and, when asked, the audio (best-effort per track) |
| JS (both trees) | `confirmBulkDeleteTracks()` → modal → `deleteDatabaseOnly()` / `deleteWithFiles()` → `_performBulkDelete()` |
| selection | `#bulkActionsToolbar`, `#selectedCount`, `_getSelectedTrackIds()` — all live, powering **Rename Selected** |

What was missing was the **button and the modal**. `#bulkDeleteModal` and
`#deleteTrackCount` were referenced by JS that nothing could reach, so
selecting tracks offered only Rename — the endpoint's own docstring was
describing a button that did not exist.

## Changes

- **Toolbar** (both album templates): a `Delete Selected` button, danger-styled
  and sat beside `Rename Selected`, calling `confirmBulkDeleteTracks()`.

- **Modal** — new partials following the repo's existing convention
  (`components/modals/_*.html`), included from both album templates:
  - `templates/components/modals/_bulk_delete.html`
  - `test_site/templates/components/modals/_bulk_delete.html`

  Three outcomes, because they are not equally reversible:
  - **Cancel** (`data-bs-dismiss`);
  - **Database only** — the rows go, the files stay, a Navidrome rescan can
    bring the rows back;
  - **Files + database** — the audio goes permanently. The JS already puts this
    behind a confirmation (`confirm()` live, a typed `DELETE` confirmation in
    the rebuilt tree), so the modal can offer it without a one-click trap.

  `#bulkDeleteModal` and `#deleteTrackCount` are the contract with the existing
  JS and are documented as such — renaming either would silently break the flow
  again.

## Tests

`tests/test_album_bulk_delete_selected.py` — 17 tests:

- the toolbar offers the action beside Rename and is danger-styled;
- both partials carry the IDs the JS reads/writes and offer Cancel +
  database-only + files-and-database;
- both album templates actually **include** the partial (an unincluded modal is
  the same class of dead code as an uncalled function);
- both scripts still ship the flow, the endpoint and `delete_files`, fill the
  count before opening, and keep disk deletion behind a confirmation;
- the JS path matches the registered route, and `routes/api_v1` is actually
  registered — that package once sat unimported, so every `/api/v1/albums/...`
  call 404'd while the file looked correct. The new button would have been dead
  in exactly the same way.
