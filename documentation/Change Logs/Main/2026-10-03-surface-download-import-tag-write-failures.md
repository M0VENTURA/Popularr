# Download imports now surface tag-write failures (2026-10-03)

**Report:** *"When albums are downloaded from soulseek, the metadata on the
file isn't updated prior to moving it to the music directory.  Its using the
metadata from the downloaded file when copying."* — observed in Navidrome
(which only indexes `/music`, so the library file itself was carrying the
downloaded file's tags).

## Investigation: the happy path is correct

Verified three ways against the current tree:

* **Full end-to-end probe** of the real maintenance entry point
  `check_completed_downloads()` — a peer-tagged MP3 (`Peer Album`, 1985) in
  the downloads dir, a real `download_queue` row (queued album/1999, release +
  recording MBIDs, MB genres) → landed in the library with **queued**
  metadata: album, year, track number, `MUSICBRAINZ_ALBUMID`,
  `MUSICBRAINZ_TRACKID`, `MUSICBRAINZ GENRES` — peer values gone.
* `_move_and_import` direct probe: same result (tag write happens **before**
  the move, and again on the target after it).
* Config-gate probes reproduced the reported symptom **only** under
  `tagging.write_options.fill_missing_only: true` (peer frames never
  overwritten — and the writer still returns `True`) or
  `tagging.write_tags_to_file: false`, and for non-MP3/FLAC files
  (`.m4a/.ogg/.opus/…` → "Unsupported file format" → `False`).

## Root cause of the SILENCE

The import layer dropped the writer's outcome:

* `download_completion_service._apply_stored_metadata` called
  `update_file_metadata(...)` and **ignored the return value**; exceptions
  were logged at **DEBUG** only.
* `download_organize_service.organize_track` wrapped its call in a bare
  `except Exception: pass`.
* `organize_group_sync` and `process_completed_queue_item` also ignored the
  `False`.

So any refused/skipped write (config gate, unsupported format, writer
failure) moved the file with the downloaded file's own tags while the queue
logged a normal `imported` line — the failure was invisible anywhere except a
DEBUG line.

## Changes

* `services/downloads/download_completion_service.py` —
  `_apply_stored_metadata` returns `bool`; a `False`/raise logs **WARNING**
  (`"Imported file keeps its downloaded tags — tag write failed or was
  skipped"`) plus one `log_unified` queue line so the Logs page shows it.
  Failure never blocks the move (the file must not loop in re-download).
* `services/downloads/download_organize_service.py` — its
  `_apply_stored_metadata` returns `bool`; `organize_track` surfaces a
  `False` (WARNING) and logs exceptions instead of `pass`.
* `services/queue/queue_processing_service.py` —
  * `organize_group_sync` warns when the source tag write fails, and when the
    target **already exists** (the copy is skipped there) the metadata is
    written **onto the existing target** — previously the library file kept
    whatever tags it arrived with while the row flipped to `imported`.
  * `process_completed_queue_item` warns when its write fails.
* Tests: `tests/test_download_import_tag_failure_surfacing.py` (12) — pins
  the outcome-return, each WARNING, the pre-existing-target tag write, plus
  two controls (writer receives queued values; the group artist gate reads
  the real source file).

## Verification

* New suite: **12 passed** with the fix; **oracle 10 failed / 2 controls
  passed** with the three source files stashed (every behavioural assertion
  fails unpatched, each message naming its defect).
* Regression sweep (9 download/queue suites): failing set **identical** to
  the clean-tree baseline — the only baseline-only entry is the known
  Windows path flake `test_sibling_torrents_root_is_searched`; **0
  regressions**.
* `import app` OK (389 routes).

## Follow-up for the reported instance

With this in place, a reproduction now leaves a WARNING in `error.log` and a
`⚠ … file tags NOT written` line in the queue log, naming the file.  The
likely production causes to look for there: a writer failure
(`Failed to write tags atomically` / `Failed to write ID3/FLAC tags` in
`error.log`), the tagging config gates, or an album imported through the
Matched-Folders per-track **Move** action
(`move_folder_track_to_library`), which by design moves a single file using
its own tags because no queue/release metadata exists for it.
