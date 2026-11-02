# Merged activity log, Navidrome import split and download matching guards

**Date:** 2026-10-31
**Area:** queue/search logging, log viewer, Navidrome import routing, download matching
**Files:** `helpers/logging_config.py`, `services/log_service.py`,
`services/queue/queue_diagnostics_service.py`, `services/queue/queue_worker.py`,
`services/downloads/download_pipeline_service.py`,
`services/downloads/download_completion_service.py`,
`services/scanning/navidrome_import.py`,
`services/scanning/pipelines/navidrome_pipeline.py`,
`routes/download_search_routes.py`, `routes/ui_routes.py`,
`helpers/file_manager.py`, `static/js/main.js`, `templates/base.html`,
`test_site/static/js/pages/logs.js`,
`test_site/templates/components/_log_modal.html`,
`tests/test_activity_log_merge.py` (new)

## 1. One activity timeline

`activity.log` is now the queue + search + verification/import timeline. Every
line carries a stable category and the applicable correlation IDs:

```
2026-10-31 08:26:15 [INFO] [ACTIVITY] [SEARCH] Maphra - End of You → 42 results | queue_id=812 attempt_id='812-a2'
2026-10-31 08:26:18 [INFO] [ACTIVITY] [SELECTION] Match selected: Maphra – End of You | queue_id=812 candidate_id='peer:path.flac' score=96.0 album_artist='Various Artists' requested_track_artist='Maphra'
2026-10-31 08:26:18 [INFO] [ACTIVITY] [DOWNLOAD] Download requested. | queue_id=812 candidate_id='peer:path.flac'
2026-10-31 08:26:20 [INFO] [ACTIVITY] [DOWNLOAD] Download started: Maphra – End of You | queue_id=812 transfer_id='…'
2026-10-31 08:27:02 [INFO] [ACTIVITY] [VERIFICATION] Download complete. Verifying the file...
2026-10-31 08:27:03 [INFO] [ACTIVITY] [VERIFICATION] Verification passed.
2026-10-31 08:27:04 [INFO] [ACTIVITY] [IMPORT] Imported successfully.
```

Categories map to the viewer filters: `search`, `download` (including
`selection`), `verification` + `import`, `retry` + `failure`,
`infrastructure`, and `manual`.

IDs carried where available: `queue_id`, `attempt_id`, `search_id`,
`candidate_id`, `transfer_id`. A retry keeps the queue ID and gets an
`attempt_id` (`<queue_id>-a<n>`). `request_id` is not stored on the current
queue row, so it is not fabricated.

* `log_queue_event` now puts `queue_id`/`attempt_id`/etc. into the activity line
  (previously they lived only in the in-memory event dict).
* `log_search` and `log_queue` write the legacy file **and** the activity log.
* Search events include `queue_id`; manual searches include `search_id`.
* Selection/download/verification/import/retry and DB outage transitions emit
  activity events.
* `services.downloads`/`services.queue` now have a `debug_file` handler so
  candidate rejection details (score components, version/title/album gates)
  remain available in `debug.log` instead of being dropped by the INFO level.
* DB outages emit one `Database unavailable — waiting…` event and one
  `Database connection restored.` event per transition, with the worker PID.
* Queue worker start includes its PID.

## 2. Viewer

* New sources: **Activity (Queue + Search)** and **Navidrome Import**.
* Legacy `queue.log`/`search.log` are still written for compatibility but are
  hidden from the viewer (`LOG_VIEW_HIDDEN` + `viewer_log_files()`); the live
  `/logs` page, the live modal tabs and the rebuilt picker all exclude them.
* Both viewers show the activity category selector:
  All activity / Searches / Downloads / Verification & import / Retries &
  failures / Infrastructure.
* The default source is still Scanner during a scan; when downloads are active
  it now opens Activity instead of the retired search log.
* `_LOG_SOURCE_FILES` maps legacy `queue`/`soulseek` export requests to
  `activity.log` and `navidrome` to `navidrome_import.log`.

## 3. Navidrome import split

* New `navidrome_import.log` with `popularr.navidrome_import` and
  `services.scanning.navidrome_import` routing (`propagate=False`, ERROR also
  copied to `error.log`).
* `services/scanning/navidrome_import.py` emits through
  `log_navidrome_import`; the module keeps `log_unified` as a compatibility
  alias pointing at the split logger.
* `services/scanning/pipelines/navidrome_pipeline.py` uses the
  `popularr.navidrome_import` logger, so the background import no longer
  interleaves with `unified_scan.log` while a popularity scan runs.
* `navidrome_import.log` is pre-created on boot and offered in the same viewer.

## 4. Matching: wrong-track investigation

What allowed a wrong version:

1. **Asymmetric variant gate.** `_score_result` rejected a candidate carrying a
   variant the request did not ask for, but accepted a candidate with **no**
   variant when the request was versioned. A requested live/acoustic/remix
   recording could therefore fall through to the plain studio original.
   Fixed: title-level requested variants must be present on the candidate.
2. **One shared meaningful word was enough.** Below 0.72 similarity the title
   gate only required any one meaningful word in common. This is the shape of
   the reported `Would I Lie to You?` → `I Could Give You (A Mirror)` match.
   Fixed: at least half of the requested meaningful words must be present; a
   clearly different title is rejected.
3. **Generic compilation artist.** A queue row storing `Various Artists` made
   the artist gate a no-op, so any performer's same-titled recording could pass.
   The pipeline now recovers the real performer from queue metadata
   (`track_artist` / `artist_credit` / `performer`) or a non-generic album
   artist before matching and before building the primary query. The selection
   event always shows `album_artist`, `requested_track_artist` and `match_basis`;
   when the performer is still unknown it says so instead of implying a verified
   artist match.
4. **Manual selection bypasses scoring.** A result explicitly chosen in the
   manual search UI is downloaded as chosen; the activity line still records the
   candidate and match basis.
5. A **mislabeled file** (filename claims the requested artist/title but the
   audio/metadata is another recording) can pass filename scoring; the
   post-download verification compares embedded metadata and duration and
   rejects it, but the transfer has already happened. This is why selection
   evidence is now logged separately from verification.

Rejection history remains durable in `download_queue.metadata.rejected_candidates`
and the `queue_rejections` table; skipped previously-rejected candidates are
reported in activity.

## 5. Manual search wording

* Start: `[MANUAL] Searching: <query>` (no longer `0 results`).
* Updates: `[MANUAL] N results received so far for <query>`.
* Completion: `[MANUAL] Search complete: N results for <query>`.
* Literal `undefined`/`null`/`none`/`nan` queries are rejected (400) instead of
  being searched.

## 6. Remaining limitations

* Manual-search state is per process; a poll landing on another Hypercorn worker
  may not write a search line. The start line and DB row still carry the
  `search_id`.
* No `request_id` column exists; correlation starts at `queue_id`.
* Candidate counts (received/unique/evaluated/eligible) are not separately
  measured; the activity summary uses the existing `candidates` count.
* `queue.log`/`search.log` remain on disk (hidden) for backward compatibility;
  only `activity.log` is offered in the viewer.
* The transfer/import states reported are the states the pipeline actually
  knows; "Download started" is emitted after slskd accepted the transfer, not
  on a progress percentage alone.

## Tests

`tests/test_activity_log_merge.py`: activity formatting/IDs, queue event
dual-write, search delegation, manual search wording, undefined-query rejection,
Navidrome logger routing, viewer hiding, JS category filter, and the title/version
matching guards.
