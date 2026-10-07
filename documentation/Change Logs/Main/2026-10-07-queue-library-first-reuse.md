# Queue: check the music library before downloading (2026-10-07)

## Reported

> Can we adjust the download queue so that it checks to see if a track exists
> in the main music directory first before it starts downloading. If a
> matching track is found on a different album (as long as it's not a
> different format such as live, or acoustic) and matches the track length,
> the file is copied to the downloads folder, metadata updated, then moved
> into the new location as if it were a downloaded file. This will save
> unnecessary downloads, especially for compilations.

## Implementation

`download_pipeline_service.process_queue_item` now asks
`_library_reuse_source` **before building a single Soulseek query** (finding
the track there saves the search too):

| Rule | How |
|---|---|
| exists in the main music directory | `tracks` rows with the same title, resolved through `resolve_music_file_path` and checked on disk |
| same track | normalised performer equality + title similarity ≥ 0.95 |
| matches the track length | the queue row's duration is **required** (no duration → never reuse) and the candidate must be within **2%**; millisecond-stored lengths are normalised |
| not a different format | `_library_track_format` compares `(is_live_or_alternate, is_acoustic)` for the queued album and the candidate's album — both directions (a live request never grabs studio audio either) |

On a hit, `_stage_library_copy`:

1. copies the file into `downloads/<artist> - <album>/<title><ext>`;
2. puts the row into `downloading` (no `file_path`), logs a `library_reuse`
   queue event and a unified `[QUEUE] … library reuse (copied, not
   downloaded)` line;
3. returns — the file then travels the **normal completion route**
   (`check_completed_downloads` → `_move_and_import`: stored metadata
   written → moved into the library → row `imported`), exactly like a file
   Soulseek delivered. A staging failure falls through to the normal search:
   a broken reuse never stops a download.

### Why the queue does NOT call the completion directly

`tests/test_scan_never_moves_files` walks a call graph from the four scan
entry points and forbids every file-relocating helper by name
(`_move_and_import`, `move_track_to_library`, `shutil.move`, …) — a metadata
pass must never be able to relocate audio. A direct completion call from the
queue module put those names on the scan graph (name resolution reaches
`process_queue_item` from both queue wrappers) and failed the guard. Staging
+ the decoupled completion honours both the guard and the request's wording:
the copy IS completed "as if it were a downloaded file" — by the code that
completes downloads. The staged name is built so the real completion matcher
(`_file_matches_queue_item`) recognises it — asserted in the tests.

## Tests

`tests/test_queue_library_reuse.py` (13): every matching rule (identical
recording found; live/acoustic rejected both directions; length mismatch,
unknown length, wrong performer, wrong title, missing file all rejected;
millisecond lengths accepted), plus the flow — a hit is staged under the
recognised name, the row goes `downloading`, **no mover runs**, the staged
file passes the real completion matcher, and a staging failure falls back to
searching (control: no candidate → normal flow).

## Oracle

Stashed `download_pipeline_service.py` → **12 failed / 1 control** → popped
→ markers present → stash list back to 3.

## Verification

- Affected set (19 files): only the known flaky
  `test_sibling_torrents_root_is_searched` and the order-dependent
  `test_refresh_returns_none_when_no_longer_downloading` (passes 9/9 in
  isolation and in every historical full run) appear as "new".
- Full suite → baseline reconciliation (`_reuse_full.txt`).
