# Queue counts now describe the rows they render

**Date:** 2026-09-24
**Area:** `ui` / `queue`

## Reported

> "Active Queue and Monitor shows 74 queued / 0 active / 0 ready in the area
> below the download queue processor, but then below in the active queue it
> shows 18 items. None of the ones I've added are showing in the active queue,
> but the active queue is working and searching."

## Root cause — two halves of one defect

**1. The counts and the lists came from different definitions of "the queue".**

| Source | Definition |
|---|---|
| "Queued" pill | a hand-written list **inside the client** (`downloads.js:2239`, `download-queue.js:654`) — `queued, searching, unmatched, pending_match, discovered, queried, matched` |
| Active Queue list | `get_active_queue` — `ACTIVE \| FAILED \| PENDING_RETRY`, **plus** `source NOT IN ('local','discovered')` |

The pill counted a strict **superset** of what could ever be rendered. Four
statuses were counted but unlistable, and every local/discovered row was counted
but filtered out:

```
counted but NEVER renderable: discovered, matched, pending_match, unmatched
```

No amount of paging can reconcile these, because paging cannot produce rows the
query does not select. That is the "74 above 18".

**2. The list used the WORK query for DISPLAY.**

`get_active_queue` is not display-only — the slskd reaper cancels stalled
transfers from it (`slskd_reaper_service.py:85`) and the folder matcher resolves
album tracks from it (`download_folder_service.py:1228`). It *must* ignore
local/discovered sources, because a disk folder is not an active transfer.
Reusing it to render the page is what left rows counted-but-invisible.

## The fix — one partition, both sides derived from it

The page's three cards are now **status sets that partition every displayable
status**, defined once in `services/queue/queue_constraints.py`:

| Constant | Contents |
|---|---|
| `ACTIVE_SECTION` | `queued, searching, processing, downloading, queried, copy_recommended, backed_off, pending_release, matched, pending_match, discovered` |
| `READY_SECTION` | `completed, moving, possible_duplicate, unmatched` |
| `FAILED_SECTION` | `failed` |
| `QUEUE_DISPLAY_STATUSES` | **the union of the three** — not written out by hand |

Server side, `api_queue` queries each section by its own constant and computes
each count from the *same* constant. Client side, the pills now read the
server's `section_counts` instead of re-deriving them, and each card renders its
own section list. So a pill reading "N" has exactly N rows beneath it **by
construction** — there is no second definition left to drift.

`unmatched` deliberately sits in **READY**, not ACTIVE: that is where the UI has
always shown un-matched disk folders (with a warning badge). Putting it in ACTIVE
would have recreated the bug with the cards swapped.

`removed`/`cancelled`/`deleted` are now **excluded** from the displayable set.
They are tombstones, `get_failed_queue` only ever returns `failed`, and the old
code counted them into the Failed badge — the same defect in miniature.

### Not widened: the work query

A separate display query (`get_queue_display_items(statuses, limit, offset)`)
was added rather than widening `get_active_queue`. **The boundary is pinned by a
test**: local/discovered rows must still be invisible to the work query, or the
reaper would treat a disk folder as an active transfer and cancel it.

## Second fix — a dedupe no longer reports success

`add_to_queue` returned `{"success": True, "already_queued": True}` when an
existing row blocked the insert, and **every caller treated `success` as "a row
was added"**:

- `downloads.js` alerted "✅ Added to queue" unconditionally,
- `album_detail.js` set the green ☑ tick,
- `pages/album.js` called `buttonState.setDone`.

So the user was told the track was queued while nothing was inserted — and the
blocker was often *invisible*: `matched`/`pending_match`/`duplicate` were in
`BLOCKING_REQUEUE_STATUSES` but rendered by no list, and `unmatched` disk rows
were deliberately hidden. "Already queued" + "not visible anywhere" is the
contradiction behind *"files I'm adding to download aren't showing"*.

Now the server returns `inserted: false`, the blocking row's `status`, a
`message` naming it, and **`displayable`** — false when the blocker is a status
the queue page cannot render, which the UI escalates loudly rather than showing a
quiet tick. All three callers branch on it.

## Tests

- `tests/test_queue_counts_match_the_rendered_rows.py` (12) — the partition
  invariant (pairwise disjoint, union == displayable), plus a seeded queue where
  **each section's count is asserted equal to the rows its own query returns**
  (the reported symptom, pinned directly), and the work-query boundary.
- `tests/test_queue_add_reports_dedupe_honestly.py` (12) — the service contract
  and all three callers.
- `tests/test_terminal_queue_rows_do_not_block_requeue.py` — updated. Two
  assertions were **inverted** with the reason recorded in each docstring
  (`test_unmatched_is_not_in_the_active_section`,
  `test_total_counts_pending_retry_rows`), because they encoded the old
  invariant that `unmatched`/`matched` must not be listable — which is precisely
  what was reported as broken.

Mutation-verified: `READY_SECTION` subtraction removed (creates a real
two-card overlap) → caught; `matched` dropped from `ACTIVE_SECTION` → caught;
`inserted: True` on a dedupe → caught; the client's dedupe branch disabled →
caught; the dedupe message losing its status → caught.

⭐ **The guard was initially defeated by its own comments.** Substituting
`if (false)` for the dedupe condition left the suite GREEN, because the fix's
explanatory comment above it contains the identifier `already_queued` and the
assertion was a plain substring check. Both suites now strip JS comments and
require a live `if (... already_queued ...)` condition. (Same class as the
earlier `window.X === 'function'` trap.)

## Verification

- 58 passed across the new + pinning queue suites.
- 19 failures in `test_download_match_lifecycle.py`,
  `test_edition_download_matching.py` and `test_metadata_update_album_split.py`
  are **pre-existing and unrelated** — proven by running the same three files in
  a baseline worktree at `HEAD` (no uncommitted changes): **10/9/3 failures
  both at base and with the changes**. They fail from `no such table:
  download_queue`, i.e. missing fixtures, not this work.
