# Album save finishes inside Cloudflare's origin timeout

**Date:** 2026-10-06
**Reported:**

> I keep getting a cloudflare timeout error when saving
> `# A timeout occurred — Error code 524`

## What 524 means here

Cloudflare closes the connection when the origin has not answered within
**100 seconds**. So this was never "the save failed" — the save was still
running when Cloudflare gave up on it, which is also why the work *did*
eventually land (and why retrying felt like it sometimes "worked"). Two costs
in `album_detail`'s POST path could reach that on their own:

### 1. N sequential disk passes

Every track in the album had its file rewritten **inside** the per-track loop,
and one write is not a small operation:

* `write_tags_to_file` reads the file **twice** (once before, once after) when
  `preserve_file_timestamps` is on — the default — so it can tell whether the
  bytes really changed, and
* then rewrites it through the atomic temp + `os.replace` writer.

Measured in this repo: **367 ms per write** for an 8 MB MP3 on a local disk
(20 writes → 7.35 s). On a NAS or with an AV scanner re-checking each
written file it is seconds *each*, so an album-sized save is tens of seconds
of pure sequencing — one track at a time, for no reason, since the files are
independent.

### 2. A MusicBrainz call that sleeps

When the release changed, the save runs `_fetch_album_mb_backfill` — two calls
through the **shared** MusicBrainz client, whose throttle *sleeps* to claim a
slot in a 1 req/s budget that a running scan also spends. The code already
documented **30–40 s calls** in production. Off the event loop (the earlier
fix) stopped it freezing *other* requests, but it was still on this request's
critical path: the response could not be sent until it returned.

## What changed

**File writes became one concurrent phase.** The loop now *collects* the work
(`_file_jobs`) and a single `await _write_album_track_files_concurrently(...)`
runs it after the database writes are durable:

* `asyncio.to_thread` — the write never touches the event loop;
* `asyncio.Semaphore(_FILE_WRITE_CONCURRENCY)` (4) — four at a time keeps the
  wall clock near a quarter of the sequential cost without burying one disk or
  one network mount under the whole album;
* results come back **in order**, so a failure still names its own track, and
  one crashing file reports a failure instead of costing the rest of the album
  their tags.

Ordering is unchanged — genres → persist → file tags — and it is still the
same `_write_album_track_file_tags` phase, so every tagging-policy gate
(`write_tags_to_file`, `ratings_only`, `fill_missing_only`) still applies.

**The backfill got a deadline.** `_MB_BACKFILL_DEADLINE_SECONDS = 15.0`,
applied with `asyncio.wait_for`. Past it, the save logs a WARNING and continues
with the values the form already carries: the backfill only supplies *additive*
fields (artist MBID, type, status, country, year, the per-recording map), and a
later save or scan fills anything a deadline dropped. `wait_for` abandons the
await, not the work — the thread finishes on its own and its result is
discarded, so nothing is left half-written.

**The save now reports its own phases.** One INFO line:

```
Album save phases  artist=… album=… tracks=16 updated=16 db_failures=0 \
    file_failures=0 mb_backfill_ms=0.0 write_loop_ms=41.2 \
    file_tags_ms=1180.4 total_ms=1231.7
```

Every slow phase in this handler has produced a timeout report at some point,
and without numbers the next one is a guess.

## Deliberately not done

* **Moving the file writes out of the request entirely** (fire-and-forget
  background job) would cap the response at a few hundred milliseconds, but it
  changes what the user is told: `file_sync_failures` and the "N track(s)
  updated in the database but NOT in the audio files" warning would have to
  become a log line, and the post-save Navidrome rescan would have to move
  behind the job or race it. Measured cost says it is not needed — the
  concurrency plus the deadline puts an album save in the seconds. The phase
  split the save already documents as its next step remains the place for that.
* **The cover block** (`cover_url` → download → embed) is untouched. It only
  runs when the form carries a `cover_art_url`, and no page currently fills
  that hidden input — worth its own investigation, separately.

## Tests

`tests/test_album_save_phases.py` — **25** (was 15; +10, 2 rewritten):

* the deadline exists, is **below 100 s**, and is what wraps the fetch — with
  the release-change gate and `asyncio.to_thread` still in place;
* a timed-out backfill falls back to empty enrichment rather than raising
  (the save survives);
* the route queues file jobs after the DB write and starts the phase once;
* the phase log carries all four timings;
* **writes actually overlap** (peak ≥ 2) **and respect the cap** (peak ≤ 4),
  and 8 × 200 ms finishes in under a second instead of 1.6 s;
* results stay in job order; one crashing track reports `(False, …)` while the
  others still write; an empty album writes nothing.

**Oracle:** with `routes/ui_routes.py` stashed → **11 failed / 14 passed** —
exactly the 11 tests that describe the new structure, the other 14 being
controls. Restored, 25/25; stash count back to 3.

## Verification

- new suite: **25 passed**
- affected set (32 files): **648 passed / 10 failed**, against a baseline
  subset of 10 for the same files → **0 new, 0 fixed** (re-run after the
  final whitespace fix to a flash block: identical result)
- full suite: **225 failed / 4714 passed / 2 skipped** in 721 s against a
  baseline of 303 failures → **79 fixed, 0 real new**. The single NEW id is
  `test_download_completion_not_found_loop.py::TestDeepFileSearch::
  test_sibling_torrents_root_is_searched`, the known Windows path-case flake
  this suite carries between runs.

## Files

- `routes/ui_routes.py`
- `tests/test_album_save_phases.py`
