# Invisible queue rows blocked re-adding, and `total` counted the finished backlog

**Date:** 2026-09-24
**Area:** `services/queue/queue_constraints.py`, `db/repositories/queue.py`,
`services/queue/queue_processing_service.py`, `routes/downloads.py`

## Reported

> "I have an error that a release already has items in the queue, but they
> aren't there. I think they still exist in an old version of the database, but
> no longer showing to be able to be selected. It's also showing 70 files in the
> download processor, but only 18 in the queue. Files I'm adding to download
> aren't showing."

Both symptoms are real, come from the same mistake in three places, and were
reproduced before any code changed.

## Root cause — terminal statuses treated as "active"

Rows in a **terminal** status (`completed`, `imported`, `in_collection`,
`unmatched`, `removed`, `cancelled`, `deleted`, `failed`) are deliberately
**not shown in the queue**. But three separate hand-written status lists
included some of them in the "is this already queued?" decision, so an
**invisible** row blocked a re-add.

### 1. `insert_queue_item` deduped against invisible rows

Its list was
`('queued','searching','downloading','completed','unmatched','imported','in_collection','matched','processing','moving')`.

An `imported` row — the track is in your library, and is **not shown in the
queue** — made the insert answer `already_queued` and insert **nothing**:

```
existing row status : imported (TERMINAL, invisible in the queue)
insert_queue_item   : SKIPPED as already_queued
rows in table       : 1 (still 1 -> nothing was added)
```

That is **"files I'm adding to download aren't showing"**.

### 2. `add_release_tracks_to_queue_detailed` skipped the whole release

Its list contained `completed`/`imported`/`in_collection`/`unmatched` too, and
a hit returned reason `already_active` — **"a release already has items in the
queue"** — for rows the user cannot see.

It also had a **second, different** copy of the list for its per-track
duplicate probe. The two copies had already drifted apart, which is why a
single fix in one place would not have held.

### 3. `/api/downloads/queue` returned `total = sum(status_counts.values())`

Every row in the table, including the terminal backlog the queue never lists,
while `queue` was the active subset. On a real database that is a permanent
pile of finished rows, so the pager read **"showing 18 of 70" forever** and the
queue looked as though it had 52 invisible items. That is your **"70 in the
download processor, 18 in the queue"**.

## The fix

**One shared constant.** `BLOCKING_REQUEUE_STATUSES` in
`services/queue/queue_constraints.py` is now the single source of truth for
"may this row prevent re-adding the same track?".

It is deliberately **not** `ACTIVE_QUEUE_STATUSES`:

* **Excludes every terminal status.** A finished or removed row must never
  block. "You already own this" is a *different* check
  (`find_library_track`) with its own, accurate `all_in_library` message.
* **Includes `unmatched`**, which is neither active nor terminal — a file
  sitting on disk unmatched must not be downloaded twice.

All three call sites now reference it, so a fourth hand-written copy cannot
drift.

**`total` now describes the listed set.** Derived from the same three constants
`get_active_queue` filters on (`ACTIVE | FAILED | PENDING_RETRY`), exposed as
`QUEUE_LISTED_STATUSES`, with a test pinning the equality so the pager and the
list cannot disagree again.

> ⚠️ `removed`/`cancelled`/`deleted` **are** counted, and that is correct —
> they sit in `FAILED_STATUSES` so the Failed card can list them and offer
> Retry/Clear. My first version of the tests asserted the opposite and was
> wrong; the tests now document the real sets.

## ⚠️ A side effect I introduced and had to undo

The release path **DELETEs** "stale" rows before re-adding. Its purge list was
derived from "whatever is not blocking" — so loosening the blocking set made
`_stale_ids` suddenly include `completed`/`imported` rows, meaning a re-queue
would have **destroyed the user's import history for that release**.

Caught before committing. The purge is now scoped to `_SUPERSEDED_STATUSES`
(`removed`, `cancelled`, `deleted`, `failed`) — the statuses a fresh attempt
legitimately supersedes — with a test asserting the terminal-but-meaningful
statuses are never in that set.

## A guard that was wrong twice

I first wrote a blanket "no terminal status in any `status IN (...)` list"
guard. It immediately flagged **four legitimate selections**:

| Site | Why it must name terminal statuses |
|---|---|
| `album_missing_service` | missing-track detection must count `imported`/`completed` — owning the file is exactly what stops a track being "missing" |
| `queue.requeue_queue_item` | the whole point is to SELECT `failed`/`removed`/`cancelled` rows to revive them |
| `queue_admin.cleanup_orphaned` | prunes `imported`/`completed` rows, so it must select them |
| `musicbrainz` retry | requeues a release's `failed` rows |

The guard is now scoped to the **decision** that was broken: the two functions
whose job is "is this track already queued?" must use the shared constant and
must not hard-code a terminal status. A third test pins that the four
legitimate selections keep working, so a future over-broad rule cannot "fix"
them.

## Tests

`tests/test_terminal_queue_rows_do_not_block_requeue.py` (28):

* the constant itself — no terminal overlap, `unmatched` still blocks, every
  working status blocks, user-rejected statuses do not;
* `insert_queue_item` — an `imported` row no longer swallows the insert
  (parametrised across `completed`/`imported`/`in_collection`), while
  `queued`/`searching`/`downloading`/`processing`/`unmatched` still block, and
  a local/discovered row does not block a Soulseek add;
* the release path — `imported` rows no longer produce `already_active`, and
  genuinely active rows still do;
* the purge — scoped, and never includes `imported`/`completed`/`in_collection`;
* `total` — excludes the finished backlog, counts pending-retry and retryable
  rows, and is pinned to `get_active_queue`'s own constants;
* the two drift guards above.

131 tests pass across this file and every existing queue suite.

## Note on my own testing mistakes

Three test bugs worth recording, all fixed:

1. I asserted `removed`/`deleted` were excluded from `total` — they are not,
   and should not be.
2. I replayed the `total` expression against `ACTIVE_QUEUE_STATUSES` rather
   than the route's actual union, so `unmatched` expectations were wrong.
3. The "must not contain `sum(status_counts.values())`" assertion failed on the
   fix's **own explanatory comment**, which quotes the old expression — the
   comment-matching trap. Comments are now stripped before asserting.
