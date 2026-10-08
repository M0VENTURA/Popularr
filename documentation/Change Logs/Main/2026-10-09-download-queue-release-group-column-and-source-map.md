# `download_queue.musicbrainz_releasegroupid` + a per-import source map (2026-10-09)

## Reported

> Add `musicbrainz_releasegroupid` as a real `download_queue` column, populated
> at queue time — so it no longer depends on a JSON blob and can't be lost by a
> fallback path that skips metadata.
>
> Add a diagnostic first — one log line per import naming which source supplied
> each field (stored / sibling / MusicBrainz / missing), so the next import
> tells us instead of us guessing.

## Part 1 — the column

### Why the blob was the wrong home

The release group was persisted **only** inside `download_queue.metadata` as
`metadata.album_metadata.musicbrainz_releasegroupid`, and
`_resolve_album_level_metadata` read it as its **second** source — after the
row's own columns. Consequences:

* every queue path that does not build `album_metadata` leaves the key absent
  entirely: the single-row `queue_add` fallback in `api_musicbrainz_download`
  (which passes only artist/title/album/source), a re-queue, discovered/local
  rows, `queue_add_batch` payloads that omit it;
* it could not be read by a plain `SELECT … musicbrainz_releasegroupid`, nor
  written by `update_queue_item`, without parsing JSON first;
* the artist page has keyed albums on `musicbrainz_releasegroupid` since
  2026-10-06-albums-split-by-release-group, so a row without it imports as its
  **own album** next to the release it was downloaded for.

### What was added

| Surface | Change |
|---|---|
| `db/schema.py` | `COLUMN_REGISTRY["download_queue"]["musicbrainz_releasegroupid"] = "TEXT"` → picked up by `_ensure_columns` on every boot |
| `db/models.py` | `DownloadQueue.musicbrainz_releasegroupid` (ORM ↔ registry parity — a registry-only column is unreadable through the ORM) |
| `migrations/versions/017_add_download_queue_release_group.py` | inspector-guarded `add_column` / `drop_column`, revises `016_add_tracks_raw_score` |
| `migrations/ensure_queue_startup_schema.py` | the `entrypoint.sh` pre-start pass also ensures it |
| `db/repositories/queue.py` | `UPDATE_ALLOWED_COLUMNS` + `insert_queue_item`'s INSERT/params |
| `services/queue/queue_processing_service.py` | `add_release_tracks_to_queue_detailed` INSERTs it from `_album_meta`; `queue_add` / `queue_add_batch` forward it from the payload; docstring |

It is **additive**: `metadata.album_metadata` still carries the field, so rows
queued before the migration keep resolving through source 1b (the blob), the
library sibling, or the MusicBrainz refresh, and the next queueing of the same
release writes the column.

The import needed **no change** to start using it — step 1 of
`_resolve_album_level_metadata` already loops `_ALBUM_LEVEL_COLUMNS` over
`item.get(column)`, so the column is now simply the first source to answer.

## Part 2 — the diagnostic

`_resolve_album_level_metadata` consults three sources (stored → sibling →
MusicBrainz) and previously logged **nothing** unless the MusicBrainz refresh
raised, and only at DEBUG. A silently-split album was therefore unreachable
from the log.

It now attributes every one of `_ALBUM_LEVEL_COLUMNS` to the source that filled
it and writes exactly **one line per import** through `log_unified` — the same
channel as the neighbouring `[QUEUE] … imported to library` line:

```
[QUEUE] album metadata sources stored=[musicbrainz_releasegroupid] sibling=[disctotal recordlabel] MusicBrainz=[-] missing=[barcode media …] queue_id=42 album='Fingerprints'
```

Design notes:

* **All four labels always print**, empty ones as `[-]`. That distinguishes
  "MusicBrainz was consulted and answered nothing" from "the MusicBrainz step
  never ran" — the difference that decides whether the fix is a re-fetch or a
  queue-time write.
* **Once per import**, via a private sentinel key on the queue-row dict.
  `_apply_stored_metadata` writes the same row twice (pre-move and post-move)
  and calls this resolver both times; two lines would be "per write", not "per
  import". The sentinel is deliberately **not** in `UPDATE_ALLOWED_COLUMNS`, so
  the private flag can never reach a column.
* **Wrapped in `try/except`** — a diagnostic must never be able to break the
  import it describes. A test drives a `log_unified` that raises.
* The existing WARNING for "no release-group MBID after stored, inherited and
  MusicBrainz sources" is unchanged and still fires when it matters.

## Tests

`tests/test_download_queue_release_group_column.py` — **21**:

* `TestTheColumnIsDeclaredEverywhereTheSchemaIs` — the four declarations
  (registry / ORM / migration / startup helper), each a different way for the
  column to silently not exist;
* `TestItIsPopulatedAtQueueTime` — the MB release adder writes it **and** the
  blob still carries it; a release with no metadata leaves it NULL (no invented
  id); `insert_queue_item`, `queue_add` and `update_queue_item` all reach it;
  the private sentinel is not writable;
* `TestTheFallbackCannotLoseTheReleaseGroup` — a row with **no** `metadata` blob
  still resolves from the column; the column beats a stale blob; nothing is
  invented when no source has it; it reaches the file tags;
* `TestTheDiagnosticNamesEverySource` — the formatter always names all four
  labels; stored / sibling are attributed correctly; MusicBrainz is credited
  only when it answers and unfilled fields are named `missing`; **exactly one
  line per import** but one line per *different* row; a raising logger does not
  break the import; the call sits inside the resolver before its `return`.

## Verification

* New suite → **21 passed**.
* **Oracle, split so each half is attributed:**
  * revert `download_completion_service.py` only → **7 failed / 14 passed** —
    every diagnostic test plus the sentinel; all column tests still pass;
  * revert the five column files only → **8 failed / 13 passed** — the
    declaration tests and the queue-time tests; the diagnostic tests still pass;
  * restore → **21 passed**.
* **Sweep** — the 52 test files referencing any touched module, clean
  `origin/develop` vs this change: **baseline 34 failures, changed 33**,
  `Compare-Object` on the sorted `^(FAILED|ERROR) tests/` lines = **0
  regressions**; the single baseline-only entry is the known
  `test_download_completion_not_found_loop::TestDeepFileSearch::test_sibling_torrents_root_is_searched`
  Windows-path flake (passes in isolation on both trees).
* **Migration chain driven for real** on a fresh SQLite file: upgrade to head →
  column present, `alembic_version = 017_add_download_queue_release_group`;
  upgrade again → no-op; `downgrade -1` → column gone; upgrade → back. (The
  pre-existing `test_migrations_idempotent` failures are stale assertions that
  still expect head to be `011_add_tracks_album_artist` — unchanged both ways.)
* `import app` → **392 routes**.

## Not changed

* **No backfill of the column for already-queued rows.** Their value still
  resolves through the blob / sibling / MusicBrainz sources, and rewriting
  `download_queue` rows at boot is a data migration better done deliberately
  than implicitly. Say the word and I'll add a one-shot backfill that copies
  `metadata->album_metadata->musicbrainz_releasegroupid` into the column.
* **The three existing test tables gained the column**, not the product —
  `test_discovered_file_dedupe` (its PRAGMA backfill list, which also serves
  the shared in-memory table other queue tests reuse), `test_terminal_queue_rows_do_not_block_requeue`
  and `test_download_import_matches_mb_release`. Their DDLs are hand-written,
  so a widened INSERT fails on a missing column rather than degrading.
* Nothing in `old_system/`.
