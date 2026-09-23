# Operations reported success when the work had not happened — audit pass

**Date:** 2026-09-24
**Area:** `services/metadata/{album_service,artist_metadata_service,correction_service,album_tag_sync_service,album_name_update_service}.py`,
`routes/misc_routes.py`

## Why this exists

The album-save bug had a specific shape that turned out to be repeated:

> return a **literal** `"success": True`, while an `except Exception` + `continue`
> loop swallows per-item failures, and the count in the payload is the number of
> items **attempted** — which the UI reads as the number **completed**.

Rather than audit by eye, I wrote an **AST scan** for exactly that shape (literal
`"success": True` inside a write-oriented function that also swallows and
continues) plus two neighbouring patterns. It found six sites. This change fixes
all six, and the scan's expectations are now a regression test.

## The six findings

### 1. `apply_genres_to_album` — `success: True` hard-coded

An album where **every** track failed still returned `success: True`, and the
route does `status = 200 if result.get("success")`. The stored path is also now
**resolved** before writing — the DB may hold a path relative to the music root,
and the writer returns `False` for anything not on disk.

### 2. `bulk_delete_tracks` — `success: True` hard-coded

Deleting nothing (every id already gone, or every `DELETE` refused) reported a
completed delete, and the UI announced *"Deleted 0 track(s)"* as a success.
A requested id that no longer exists is now counted as a failure rather than
silently `continue`d, and the status code reflects the outcome.

### 3. `apply_genres` (artist) — the path the rebuilt UI uses

The tag write was wrapped in a bare `except Exception: pass`, and the response
said `success: True, updated: N` unconditionally. A run where **every** file
write failed — an unresolved path, a read-only mount, the tagging master toggle
off — told the user the genres were applied. It now resolves the path, checks
the writer's return value, and reports `files_written` / `files_failed`.

### 4. `fix_album_field` — counted calls that did not write

`files_updated += 1` ran for any call that did not **raise**, so a writer
returning `False` still counted as a successful write. Now checks the return
value, resolves the path, and logs a warning when some files were not updated.

### 5. Tag writers writing an unresolved path

- `album_tag_sync_service` wrote the DB's stored path straight to the writer and
  logged failures at **DEBUG** (invisible in a normal log tail — the exact
  "I saved it and nothing changed" report).
- `album_name_update_service` did the same without resolving.

Both now resolve, and the sync's failure is a **WARNING**.

### 6. `api_apply_genres` (legacy `/api/genres/apply`) — hard-coded success

`static/js/genre-utils.js` branches on `data.error`, **not** `data.success`, so
an artist with no matching tracks still showed *"✅ Applied N genre(s)"*. Now
returns a populated `error` when nothing matched.

## Verification

| Check | Result |
|---|---|
| `tests/test_operations_report_real_outcomes.py` (new, 19) | **19 passed** |
| Oracle (source fix stashed) | **17 failed / 2 passed** |
| Regression sweep, 17 suites | pre-existing failing set **IDENTICAL** (29 → 29); owned 17 → 0 |

## ⚠️ A regression I introduced, and how it surfaced

Adding `resolve_music_file_path` to the tag-sync path **broke
`test_album_tag_sync_service.py::test_sync_fills_missing_and_records_corrections`**.
The test fakes files at `/tmp/1.mp3` and patches `os.path.exists`, but the
resolver uses `os.path.isfile` — so the path no longer resolved and the write was
(correctly) skipped.

The behaviour I added is right; the **test's seam was wrong**. It patched
`write_tags_to_file` but not the resolver, so it asserted against a path that the
production code can no longer reach. The test now patches the resolver too,
which is the actual seam.

⚠️ Worth noting: this is the reverse of the earlier lesson. Resolving paths fixed
real writes but **narrowed the set of paths that reach the writer**, so every
test that fakes a file path must fake the resolver as well.

## ⚠️ The comment-matching trap — fourth time this session

`assert '"success": True' not in body` failed against the **fixed** code, because
the new comment quotes the old line it replaced. The helper now strips comments
before both the assertions and the AST scan, and lives in the test module.

This has now cost time in four separate changes. Any assertion of the form "this
token must NOT appear" in a codebase that documents its own traps needs comment
stripping, and I should reach for it first rather than after the failure.

## Not changed

Two remaining hits from the same scan are **not** defects:

- `artist_service.delete_track` adds `file_sync_failures`-style detail but
  correctly reports success for a track that was deleted from the DB even when
  the FILE could not be removed (`deleted_file: False` already says so).
- `artist_service.clear_disc_number` / `merge_albums` report success on a
  `rowcount` of 0, which is arguably correct for an idempotent operation.

`except Exception: pass` appears 264 times across the tree. Most are genuinely
best-effort (cache warmers, optional enrichment, telemetry). Auditing all of
them would be churn; the scan is kept as a test so the **write-oriented** ones
stay fixed.
