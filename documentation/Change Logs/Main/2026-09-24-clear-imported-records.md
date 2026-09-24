# Clear imported records so deleted tracks can be downloaded again

**Date:** 2026-09-24
**Area:** `ui` / `queue`

## Reported

> "I want to be able to clear imported. Files were removed from the database and
> I need to redownload them."

## Root cause — a genuine dead end, in two parts

**1. `queue_clear` hard-coded the exclusion.**

```python
text("DELETE FROM download_queue WHERE status != :status"), {"status": "imported"}
```

That was the **only** behaviour. The UI's "Clear Queue" button even advertised it
(*"keeps imported records"*), and "Purge All" deletes the whole table *plus every
file in the downloads folder* — so there was no way to remove the imported rows
alone. Verified: `filters.status` was read by the service, but nothing ever sent
it.

**2. A stale `imported` row is what actually blocks the redownload.**

`services/metadata/album_missing_service.py` counts an `imported` **queue** row as
queue coverage:

```sql
status IN ('queued','searching','downloading','processing','moving',
           'imported','in_collection','matched','completed')
```

Any MB track matching one of those rows is skipped from the missing set. So a
stale `imported` row keeps the track **off** the missing list, and "Download
Missing Tracks" never offers it again.

⚠️ **This is the important half:** deleting the library rows alone does *not*
restore the track. The queue row has to go too. That is pinned by a test, so the
button is not mistaken for the whole fix.

## The fix

`queue_clear` now honours an explicit selection, while the **default keeps
excluding `imported`** — wiping library history as a side effect of a routine
clear would be destructive, so the escape hatch stays deliberate:

| Request | Effect |
|---|---|
| `{}` | unchanged — deletes everything **except** `imported` |
| `{"filters": {"status": "imported"}}` | deletes **only** imported rows |
| `{"filters": {"statuses": ["imported","in_collection"]}}` | multi-status clear |

Statuses are validated against `ALL_QUEUE_STATUSES`. A DELETE with a status that
matches nothing reports `success: true` and `deleted: 0`, which reads to the user
as *"cleared"* when nothing happened — so an unknown status is now **rejected**
with the valid list.

UI (both trees): a distinct **"Clear Imported"** button next to "Clear Queue",
with a confirm that states plainly that deleted tracks will show as missing again
and **no files on disk are touched** (unlike "Purge All"). The rebuilt tree
exports `global.clearImportedRecords` — a definition without the export is a dead
`onclick`.

## Tests

`tests/test_clear_imported_records.py` (16) covers: the safeguard staying the
default, the explicit filter, unknown-status rejection, the multi-status list,
real deletes against a seeded table (only imported rows go, idempotent on rerun),
**the coverage mechanism that blocked the redownload**, and the button/handler/
export in both trees.

Mutation-verified, 5/5 caught:

| Mutation | Result |
|---|---|
| the explicit status filter removed (original defect) | 7 failed |
| the default clear stops excluding imported (**destructive**) | 3 failed |
| the rebuilt handler defined but not exported (dead button) | 1 failed |
| the live template's button reverted | 1 failed |
| the handler sends no filter — clears everything *except* imported | 1 failed |

149 passed across this suite plus every queue/UI guard suite. Route contract
proven in-process: all three payload shapes reach `queue_clear` unchanged.

## Deliberately not changed

- **`Purge All`** already wipes imported rows, but it also deletes every file in
  the downloads folder, so it is not a substitute.
- **`download_verification_service`** already requeues an `imported` row when its
  file is missing from `/music`. That covers "the file vanished"; this change
  covers "I removed the tracks from the database and want them back" — in that
  case the file is genuinely gone, so the row must be cleared, not requeued.
