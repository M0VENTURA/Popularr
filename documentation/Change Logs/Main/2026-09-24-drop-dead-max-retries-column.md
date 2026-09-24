# Removed the dead `download_queue.max_retries` column

**Date:** 2026-09-24
**Area:** `db/models.py`, `db/schema.py`, `db/repositories/queue.py`,
`migrations/versions/015_drop_download_queue_max_retries.py`

## Why

`max_retries` was never enforced. `mark_failed` read only the retry **delay**
(`_queue_retry_defaults()[0]`) and ignored the ceiling entirely, so an item was
retried forever no matter what the column said. It was dead on every other
surface too:

| Surface | Status |
|---|---|
| `mark_failed` (the only retry gate) | read the delay only — ceiling ignored |
| Writers | **none** — nothing ever wrote the column, only read it |
| `GET /api/musicbrainz/downloads` (the endpoint behind the download table) | never returned it, so the JS `(Retry N/5)` badge always fell back to its own client-side constant |
| The JS `max_retries` field posted to `/api/musicbrainz/download` | never read by any backend handler |
| `queue.max_retries` in `config.yaml` | no default shipped and no Config-page field, so the lookup could only ever return the hard-coded `5` |

Retries are deliberately **unbounded**: a track that fails to download must
return to the queue rather than being abandoned — which is also why `mark_failed`
never writes the terminal `'failed'` status (that status exists only for
`cleanup_stuck_items` and explicit cancellation). Removing the column makes that
policy explicit instead of advertising a limit that does not exist.

> Found during the slskd download/retry wiring audit (`9598547d`), which had
> flagged the column as "dead but misleading". Confirming *why* it was dead —
> by grepping for every writer — is what made removal the right call rather than
> wiring it up.

## What changed

* **`migrations/versions/015_drop_download_queue_max_retries.py`** — new revision
  chaining onto `014_add_tracks_release_detail`. Existence-guarded and
  dialect-portable, matching the rest of the chain, so it also runs cleanly
  against the SQLite test engine (which creates `download_queue` without the
  column). `downgrade()` restores the original shape exactly: `INTEGER`,
  `server_default=5`, `nullable=True`.
* **`db/models.py`** — dropped the ORM column, with a comment pointing at the
  migration so it is not re-added.
* **`db/schema.py`** — removed the bootstrap definition. This one mattered:
  a leftover entry here would **re-add the column on boot**, silently undoing
  the migration.
* **`db/repositories/queue.py`** — removed `max_retries` from the column list and
  replaced the tuple-returning `_queue_retry_defaults()` with a single-purpose
  `_queue_retry_delay_minutes()`. The old helper's second return value had
  exactly one consumer (`[0]`), so the ceiling it computed was pure noise.
* **Tests** — `test_downloads_ux_fixes.py` had a test literally named
  `test_failed_items_requeue_even_past_max_retries`, asserting a ceiling that
  never existed; renamed to `..._even_with_many_prior_attempts` and re-documented.
  Its fixture dropped the column.

## Deliberately NOT removed

These share the `max_retries` name but are unrelated, live, and correct:

* `retry_delay_minutes` (column) and `queue.failure_retry_delay_minutes`
  (config) — the real tunable backoff window, still written and read.
* `retry_count` — still incremented on every failure.
* `api_clients/discogs_http.py`, `api_clients/musicbrainz_http.py`,
  `db/repositories/cover_detection_repository.py` (`apply_cover_metadata_batch`),
  `db/repositories/popularity_repository.py` (`DB_LOCK_MAX_RETRIES`),
  `helpers/config_helpers.py` (Last.fm `max_retries`, which **does** have a
  Config-page field at `templates/pages/config.html`) — all genuine bounded
  HTTP/DB retry loops.

## Verification

* **Migration round-trip against a real database:** `max_retries` present at
  014 → **absent** at 015 → **restored** on downgrade, with
  `retry_delay_minutes` and `retry_count` intact throughout.
* **New tests:** `tests/test_max_retries_column_removed.py` (15) — the ORM model
  no longer declares it, the repository/schema reference it nowhere, no later
  migration re-adds it, the migration chains onto 014 and is existence-guarded,
  the downgrade restores the original shape, the delay helper no longer reads a
  ceiling, `mark_failed` still supplies the delay, and the JS badge keeps the
  client-side fallback that is now load-bearing.
* **Pre-existing failures confirmed unchanged:** the 15 failures in
  `test_download_completion_compilation.py` and
  `test_download_completion_unmatched_artist.py` are environmental —
  `soundfile`/libsndfile raises `LibsndfileError ... System error` writing FLAC
  fixtures to `/tmp/` on Windows. Verified **identical counts (15 failed /
  6 passed) at the pre-change commit** in a throwaway worktree, so the removal
  did not cause them.
* **Oracle sweep:** full suite run with and without the change in separate
  worktrees, failing sets compared — **0 new failures**.

## Note on the test for `_queue_retry_delay_minutes`

The first version of that test used `assert "max_retries" not in source`, which
failed on the helper's own docstring — the docstring *explains* what was removed,
so it contains the word. The comment-matching trap again. The test now parses the
AST, drops the docstring, strips trailing comments, and asserts on executable
lines only.
