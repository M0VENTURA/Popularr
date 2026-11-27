# Soulseek search timeouts, compilation queue artists, and the rescan question

**Date:** 2026-11-27
**Commit:** `fix(search): fail fast on slskd polls; queue the track artist`

## 1. Compilation tracks queued as the album artist — FIXED

**Reported:** a Various Artists compilation queues with `Various Artists` as the
artist:

```
I Got a Girl
Various Artists - Triple J's Hottest 100 (1995)   ← wrong
```

**Root cause — one dropped field.** `missing_album_tracks.track_artist` holds
the right performer and the server selects and returns it
(`services/metadata/album_missing_service.py`). But `missingRowToTrackComp` in
`album.js` never copied it into the object the queue button reads, so the
payload's chain

```js
artist: trackComp.track_artist || trackComp.mb_artist || pageArtist()
```

fell all the way through to `pageArtist()` — the album header, i.e.
"Various Artists".

The *Compare* path was always correct (its `trackComp` carries `mb_artist` from
the server), which is why the bug only appeared on the normal page-load list.

**Fixed** in `test_site/static/js/pages/album.js`:
- `missingRowToTrackComp` now copies `track_artist` (and `mb_artist`).
- The payload also stores it in `metadata.track_artist`, which is the key the
  queue worker's placeholder-recovery reads — the same shape Exportify album
  mode already sends.
- The row's `— <performer>` label now renders on compilations too; it had the
  same hole.

**Why it mattered beyond the label:** with a generic artist the search collapses
to the bare title (`build_search_query`) and the artist gate is skipped
(`download_pipeline_service.py`), so *any* artist's recording of that title
could be accepted.

## 2. Manual Soulseek search failing and timing out — FIXED

Five compounding causes, all traced.

**a. 4× retry on a user-facing poll.** Every slskd call went through the shared
`create_retry_client(retries=3)` — four attempts. One poll does two GETs
(`state` + `responses`), so a slow slskd could take ~80–120 s to answer, against
the browser's 30 s default. Every poll was abandoned. Added a `fast=True` client
(no retries, 8 s read timeout) used only by the interactive search routes; the
queue worker keeps the retrying session.

**b. The loop treated abandonment as transient.** `onError` only
`console.warn`ed and kept going for all 120 attempts — up to an hour of
"Searching…" then a bare "Timed out". Added `MAX_CONSECUTIVE_POLL_ERRORS = 5`;
a successful poll resets the streak. The loop now fails with a stated reason
within seconds.

**c. A failed slot check reported a FREE slot.** `GET /api/slskd/search-slot`
returned `{"slotFree": true}` with HTTP 200 on any error, so a broken check was
indistinguishable from a free one: the client proceeded, the search failed, and
nothing said why. It now returns 503 with `unreachable: true`.

**d. Everything collapsed into "No results found".** Results that were returned
and then dropped (unapproved container, or peers with no free slot) rendered the
same text as a genuinely empty search. The server now reports
`formatFiltered`, and the UI explains the difference.

**e. Docker service names were rate-limited as PUBLIC hosts.**
`is_private_host("slskd")` returned False — no dot, not an IP — so every request
to `http://slskd:5030` was serialised through the 1 req/s domain throttle. The
same deployment on `localhost` was fast. Single-label hostnames are now private.

Also: the search-start log write (a file write plus a synchronous Postgres
INSERT) ran on the event loop; it is now offloaded. `_manual_search_lock` no
longer wraps that I/O. The unreachable-slskd getters were `logger.debug`
(invisible at the default INFO level) and are now warnings.

## 3. Should the Navidrome rescan be scheduled instead of on-demand?

**Recommendation: keep triggering it on write, but make it (a) never block a
request and (b) backstopped by a schedule.** Do not replace it with a 30-minute
schedule.

**Why a 30-minute schedule alone is not enough — it can reintroduce a data
bug.** The reason the rescan exists is in `navidrome_rescan_service`: Popularr
writes corrections to *audio file tags*, but Navidrome serves its *own index*.
If an import reads Navidrome before it has scanned those files, the import
upserts the STALE rows over the correction — "the next import puts the old data
back". That is why `sync_remote_navidrome_before_import` blocks until it can
*verify* `lastScan` is newer than `latest_local_tag_write`, and refuses the
import otherwise. A wall-clock schedule cannot give that guarantee: an import at
minute 29 reads pre-save data.

**But you are right that it is causing trouble.** The current design already
carries the fixes for the specific complaint:
- `request_rescan` is fire-and-forget (daemon thread) — the save never waits.
- It coalesces a burst into ≤ 2 runs and rate-limits to one run per
  `MIN_SCAN_INTERVAL_SECONDS` (300 s), measured from the previous run's start.
- A cross-process advisory lock keeps exactly one scan running deployment-wide.

So a *save* is not what times out. What times out is
`trigger_and_wait_for_scan`, which BLOCKS up to `max_wait_seconds=1800`
polling `getScanStatus`. That runs on the **import** path, not the save path,
and the import is already a background job.

**What I would do next (not implemented — needs your call):**
1. **Only scan when it is actually needed.** `sync_remote_navidrome_before_import`
   already short-circuits when `lastScan >= latest_local_tag_write`. If the
   importing scan has no pending local writes, it should take that branch and
   never trigger a scan. Confirm the dirty/`local_tags_written_at` comparison is
   not being defeated by a timestamp written on every scan.
2. **Add a scheduled rescan as a BACKSTOP, not the mechanism.** An APScheduler
   job every 30–60 minutes that calls `request_rescan("scheduled")` gives
   Navidrome a guaranteed refresh cadence (the coalescer already makes it cheap
   and single-run). This helps the *common* case — no one is importing, but you
   want Navidrome's own UI current — without ever being the thing correctness
   depends on.
3. **Shorten the import's wait.** 1800 s is generous; a full library scan on a
   healthy Navidrome finishes well inside it. Lowering the ceiling turns a hung
   scan into a fast, explicit "deferred" rather than a 30-minute stall.

The honest summary: the rescan is not the timeout — the *polling wait for it* is.
Fix the wait, and add a schedule for freshness. Replacing the trigger with the
schedule would trade a timeout for a stale-import bug.

## Tests

`tests/test_compilation_queue_artist_and_search_timeouts.py` — the mapper
carries `track_artist`, the metadata key is sent, the fast client is selected by
the three interactive routes, a failed slot check is not "free", the poll loop
caps consecutive errors and stops itself, the empty state explains filtering, and
single-label hosts are private.

`tests/test_queue_track_artist.py` updated for the trimmed-payload form.
