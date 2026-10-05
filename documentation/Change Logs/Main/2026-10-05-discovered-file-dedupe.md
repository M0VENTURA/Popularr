# The downloads scan stops re-logging a dedupe it already knows about (2026-10-05)

**Report (queue log):**
`Duplicate skipped: already in queue artist='Professor Green' title='Read All About It' queue_id=5732`
— every two to five minutes, all day, for the same row.

## The dedupe was right; the QUESTION was asked too late

`enqueue_discovered_files` has two pre-checks, and both look at the **file**:

- `find_existing_discovered_file` — by `file_path` / `found_filename`
- `_queue_has_active_match` — by parsed metadata

A file whose **track** is already queued — under a path neither of them
recognised — fell through both, reached `insert_queue_item`, which dedupes on
`(artist, title)` and logs at **INFO** when it hits. Every scan. Forever.

The insert's own dedupe was never in doubt; it just answered the question
after deciding to log.

## Changes

- **New `db.repositories.queue.find_blocking_queue_item(artist, title, source,
  session=None)`** — the dedupe decision extracted from `insert_queue_item`
  so there is **one** definition of "is this track already queued?", used by
  the insert *and* by the scan. The `BLOCKING_REQUEUE_STATUSES` list, the
  `ORDER BY created_at` and the source-locality half of the rule all move with
  it, so the two callers can never drift apart.
- `insert_queue_item` delegates to it; its behaviour (and its INFO line) are
  unchanged for every other caller.
- The discovery scan now asks the same question **before** inserting, and
  counts the hit as `already_in_queue` instead of reaching the insert at all —
  so the log line disappears while the statistic still reports it.
- Artist/title/album are derived **once** in the scan and passed to both the
  check and the insert, so the pre-check and the insert cannot disagree about
  identity.

## The guard that caught the refactor

`tests/test_terminal_queue_rows_do_not_block_requeue.py` has
`TestNoHandWrittenStatusLists`, which asserts that every dedupe decision
consults `BLOCKING_REQUEUE_STATUSES` — and it reads the *source* of
`insert_queue_item`. Moving the constant broke it, which is exactly what a
guard should do.

It now points at `find_blocking_queue_item` (where the decision lives), and a
new test pins the delegation instead: `insert_queue_item` must call
`find_blocking_queue_item(` and must **not** grow its own `status IN (...)`.
Both protections survive the move; neither is relaxed.

## Not changed

`BLOCKING_REQUEUE_STATUSES` is untouched — `completed` / `imported` / `failed`
still do not block, so re-downloading a finished track keeps working (that is
the "files I'm adding to download aren't showing" bug this list exists for).

Also **not** changed: whether a discovered file should be *linked* to the row
it duplicates. Today it is skipped; linking it would be a product decision,
not a logging fix.

## Tests

`tests/test_discovered_file_dedupe.py` — **8 tests**:

- the shared decision: no row → `None`; a blocking row is found; a
  **terminal** status does not block; the locality half of the rule (a
  discovered caller does not match a soulseek row); case-insensitivity;
- the scan: a known track is counted (`already_in_queue == 1`) and the insert
  is **never called**; an unknown track still queues; identity values match
  the insert's.

The table is hand-written rather than created from the ORM because
`download_queue.metadata` is JSONB and SQLite cannot render it — which is why
six other tests do the same. Without the table the helper's own `except`
swallows "no such table" and returns `None`, which would make the first test
pass for entirely the wrong reason.

Oracle: stashing both source files → collection error (the module-level
import of `find_blocking_queue_item` fails); both markers restored.
