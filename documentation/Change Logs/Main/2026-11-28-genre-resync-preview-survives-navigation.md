# The genre-resync preview now survives navigating away

**Date:** 2026-11-28
**Commit:** `fix(config): restore the genre-resync preview (and apply result) on return`

## Reported

> "When running the genre playlist resync, if I navigate away from the page it
> forgets the preview, can this be kept until the system is reset so I don't need
> to keep running preview if I navigate away?"

## The result was never lost — the client threw it away

The preview is computed on a background thread and its full result is stored on
the preview's own `scan_states` row:

```python
_write_progress(
    running=False, percent=100.0, phase=PHASE_DONE, status="complete",
    result=result, scan_type=PREVIEW_SCAN_TYPE,
    progress_file=PREVIEW_PROGRESS_FILE,
)
```

so `GET /api/genres/playlist-resync/preview/status` returns it at any time,
including after a reload or a restart (only `is_running` rows are touched by
`reset_stale_scan_states`).

The loss was one line in `config.js`:

```js
if (silent) return;  // a stale finished row: leave the card untouched
```

`silent` is exactly what the page passes on load, to restore a job that was
started earlier. So the only case the early return suppressed was the one worth
keeping.

## The fix

`pollGenrePlaylistPreview(silent)` now restores a **completed** result instead of
returning:

- renders the summary and tables again;
- re-enables **Apply**;
- shows the completion time.

An idle row (never run) and an interrupted row still leave the card untouched.
The same restore was added to the apply poll, which lost its "Resync complete"
outcome the same way — with Apply deliberately left disabled, because that
preview has been consumed.

### Why a restored, possibly old preview is safe

**Apply recomputes server-side.** It re-runs `_genre_changes(dry_run=False)` and
rebuilds the playlists from stored evidence; the client never sends the preview
payload back (the apply route does not read `resync_result` at all). So an older
preview can only ever *describe* the current state — it cannot cause an outdated
write. That is also why the age is shown: it is the honest signal that the
numbers may predate the current library, not a correctness guard.

### The ordering rule

The preview and the apply share one card, and on page load **both** progress rows
are polled. Restoring both naively made the card flicker between
"Preview ready" and "Resync complete" depending on which fetch landed last, so
`_claimResyncCard()` lets only the **newest** result paint:

```js
function _claimResyncCard(iso) {
  const parsed = iso ? new Date(iso).getTime() : 0;
  const stamp = isNaN(parsed) ? 0 : parsed;
  if (stamp < _resyncCardStamp) return false;
  _resyncCardStamp = stamp;
  return true;
}
```

The last thing the user ran is the thing shown.

## Tests

`tests/test_genre_resync_preview_survives_navigation.py` — 13 cases:

- the safety premise: the apply route does not read a preview payload, and the
  service recomputes with `_genre_changes(dry_run=False)`;
- the silent path no longer returns before rendering; a completed preview is
  rendered and enables Apply; an idle row changes nothing;
- the ordering guard exists, is used by both pollers, and rejects an older
  result;
- the completion time reaches the rendered summary (preview and apply).

`config.js` also continues to pass the repo-wide JS parse/duplicate-declaration
guard (`tests/test_js_files_parse_without_duplicate_declarations.py`).
