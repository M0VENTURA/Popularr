# A peer that never sends is not picked again next cycle (2026-10-05)

**Report (queue log):** ATEEZ "HIGHER" and "Seeker" from `roqinghejing`, every
~45 minutes, indefinitely:

```
18:11:04 [DOWNLOADING] ATEEZ - HIGHER → downloading from roqinghejing
18:26:22 Cancelled stalled transfer … progress=0
18:26:23 Failed queue item for stalled transfer queue_id=5454
18:57:21 [DOWNLOADING] ATEEZ - HIGHER → downloading from roqinghejing
19:46:56 [DOWNLOADING] ATEEZ - HIGHER → downloading from roqinghejing
```

## The gap: one failure path forgot to remember

`download_pipeline_service` blocks the `(peer, file)` pair on **every other**
failure:

| path | blocked? |
|---|---|
| peer has no free upload slot | ✅ `_block_peer` |
| `slskd.download_file` returns False | ✅ `_block_peer` |
| downloaded file's metadata mismatched | ✅ `_block_peer` |
| **transfer starts, then never sends** | ❌ **was not blocked** |

The stall case is the one that *looks* successful: the download **request**
succeeds, so nothing blocks, the transfer sits at 0% for
`STALL_ZERO_PROGRESS_MINUTES` (15), the reaper cancels it, fails the queue
item — and the next search finds the same peer again. A 15-minute stall, every
cycle, forever, on a peer that was never going to send anything.

## Change

`reap_stalled_transfers()` now calls the same `_block_peer(username, filename)`
used everywhere else, as soon as it decides a transfer is stalled — with the
same key and the same TTL (`SLSKD_BLOCKED_PEER_TTL_SECONDS`, default 7200).

Because the key is the **pair**, one dead file does not ban a peer: the same
peer's healthy files stay selectable. And because the block lands in the
existing `_filter_blocked_peers` used by the automatic search, the next cycle
either picks a different file or reports `no_results` and backs off — instead
of burning another stall.

The import is lazy (`services.downloads.download_pipeline_service`), matching
how `download_completion_service` already reaches `_block_peer`.

## Deliberately unchanged

- `STALL_ZERO_PROGRESS_MINUTES` (15) and `STALL_MID_TRANSFER_MINUTES` (60) —
  the thresholds decide *whether* a transfer is stalled; that is a separate
  question from what to do once it is.
- The block TTL — a peer that recovered within the window stays usable after
  it expires.

## Tests

`tests/test_stalled_transfer_blocks_peer.py` — **7 tests**:

- a 0%-for-20-minutes transfer blocks that `(peer, file)` **and** is still
  cancelled;
- a mid-transfer stall (5% after 70 min, speed 0) blocks too;
- **controls**: a progressing transfer, a young one (5 min), and one with an
  unparseable `startedAt` are never blocked;
- the loop actually breaks: after a stall, `_filter_blocked_peers` drops that
  exact result while keeping the same peer's *other* file.

Oracle: stashing only `slskd_reaper_service.py` → **3 of 7 fail** — precisely
the three that assert blocking; the four "must not block" controls pass either
way, which is what makes them controls.
