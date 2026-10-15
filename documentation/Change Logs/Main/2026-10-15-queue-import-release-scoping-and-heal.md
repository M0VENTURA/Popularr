# Downloaded tracks no longer inherit a different release's album identity

**Date:** 2026-10-15
**Files:** `services/downloads/download_completion_service.py`,
`services/downloads/download_metadata_heal_service.py` (new),
`services/queue/queue_worker.py`, `routes/downloads.py`,
`helpers/config_helpers.py`, `routes/ui_routes.py`, both Config pages + their JS

Requested:

> When metadata is added to a track that is downloaded from the queue, it doesn't
> always match the metadata of the release that was added to the queue. Sometimes
> the album year or the release mbid is different.
>
> Could a heal also be added to tracks already in the queue to correct any that
> have the wrong information?

## The fault

`_resolve_album_level_metadata` fills the album-scoped MB identity (release
group, barcode, catalogue number, original year, release date, country, label,
media, disc total) from, in order: the queue row's own columns, its stored
`album_metadata`, a library **sibling**, then MusicBrainz.

The sibling step matched on artist+album **name** with no release constraint, and
deliberately **preferred** a sibling that carried a release group:

```sql
WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
  AND LOWER(COALESCE(album, ''))               = LOWER(:album)
ORDER BY CASE WHEN COALESCE(musicbrainz_releasegroupid, '') <> ''
              THEN 0 ELSE 1 END, id
```

So a library already holding the **same album name from a different release** —
the ordinary case of an original and a remaster, or an album previously imported
from another edition — donated *its* release group, barcode, catalogue number and
year to the incoming track. And because inheritance runs **before** the
MusicBrainz refresh and fills every column it can, the release actually queued was
then consulted only for the leftovers: the wrong identity was locked in and the
right one could never win.

Measured on the 1997 and 1994 editions of one album, with the row queued against
the **1994** release:

| field | before | after |
|---|---|---|
| `musicbrainz_releasegroupid` | the 1997 release's group | the 1994 release's group |
| `barcode` | `OLD-BARCODE` | `A-BARCODE` |
| `catalognumber` | `OLD-CAT` | `A-CAT` |
| `originalyear` | 1997 | 1994 |
| `releasedate` | 1997-08-25 | 1994-08-22 |

### The fix

A sibling may now only be inherited from when it **is the same release**, or
shares the same **release group**. A row naming no release at all keeps the
name-only behaviour — that is the case the fallback exists for, and the only case
where an artist+album match is the best evidence available. The lookup also now
prefers an **exact release match** over a group match, which the old ordering
could never do.

The local, zero-MusicBrainz-budget path is preserved: a same-release sibling is
still inherited with no HTTP call at all. Verified — one probe scenario asserts
`mb_calls == []`.

## The heal

Rows imported **before** the fix keep the wrong identity and nothing revisits
them: the track is internally consistent, so no later scan has a reason to touch
it. `services/downloads/download_metadata_heal_service.py` re-aligns them.

- **Reference is the queue row**, resolved MBID-first: the release the row names
  is fetched from MusicBrainz and wins over the row's own stored copy, which can
  itself have been written from an incomplete payload. A row naming **no release
  is skipped** — guessing from the name is the fault being repaired.
- **Corrects the tracks the queue row produced, and nothing else.** The unit of
  correction is the *release*, and the rows corrected are the landed tracks of the
  queue entries naming it — never every track sharing the album name. A library
  can legitimately hold two editions under one name, and this fault is one of them
  having donated its identity to the other; an album-wide sweep would do that
  damage in reverse and the two could never be told apart again.
- **Grouped by release**, so one MusicBrainz fetch serves every track of it
  rather than one per track — the client is rate-limited at 1 request/second.
- **`year` and `release_year` are mapped, not copied.** On a *queue* row `year` is
  the edition year; on a *tracks* row `year` is the release group's **original**
  year and `release_year` is the edition. The file's DATE tag takes the edition
  year and ORIGINALYEAR takes the original.
- **Drift-only writes.** The tag writers treat an empty value as "delete this
  frame", so sending a full payload with uncorrected keys blank would strip tags
  the heal was never asked to touch. Tag updates go through the same
  `build_tag_updates` mapping a manual album edit uses.
- **Idempotent**, so a repeat run is wasted work rather than damage.
- DB row and file tags are always corrected together (a metadata update must not
  leave the two disagreeing), and a failed file write is counted, not swallowed.

### Surfacing it

- `POST /api/downloads/metadata-heal` — `dry_run` **defaults to true**, so a
  stray call cannot rewrite a library; the destructive form must be asked for.
- Config: `downloads.metadata_heal` (`enabled`, `run_on_startup`, `limit`) on
  **both** Config pages, with the nested block registered in
  `_sanitize_config_sections` — without that entry the page save coerces it to
  `{}` and the settings silently stop applying (the `playlists.genre_exclusions`
  trap). Ships **disabled**.
- When enabled, a pass runs at queue-worker start in a **daemon thread** so a
  large first pass cannot delay the worker's first cycle; only the leader reaches
  that point, so two workers cannot heal concurrently. `limit` caps queue rows
  per run (0 = no cap).

## Verification

- `tests/test_album_metadata_release_scoping.py` (10) — the fix. **Oracle: 4
  failed / 6 passed.** The two tests that cannot both pass vacuously are the
  wrong-release refusal and the same-release inheritance: a query that silently
  raised and fell through to MusicBrainz passes the first and fails the second.
  Each scenario also asserts the sibling query ran cleanly, because a pass
  through the swallowed exception path is not a pass.
- `tests/test_queue_metadata_heal.py` (13) — the heal. **Oracle: 2 failed / 11
  passed** with the scope reverted to the album-name fan-out, confirming the
  scope guard is load-bearing.
- `tests/test_queue_metadata_heal_api.py` (24) — endpoint defaults, input
  rejection, auth gate, config round-trip, and UI parity across both trees.
- 47 tests. Baseline sweep over `download|queue|config|import|tag|metadata|
  static_js|template`: **34 failures at baseline, 34 with the change, zero
  attributable regressions.** One entry differs —
  `test_download_completion_not_found_loop::test_sibling_torrents_root_is_searched`,
  a `torrents` vs `Torrents` case-sensitivity assertion that **also fails on the
  untouched source in isolation**, so it is environmental and not this change.
- `test_download_import_mbids_and_disc::test_the_inheritance_query_skips_queue_stub_rows`
  asserted the old ORDER BY as a source string. Its stated intent — "prefer a
  sibling that carries the release group" — still holds and is now stronger, so
  the assertion was updated to the new CASE and a scope assertion added.

### Also found

The heal's own test module initially ERRORed only in the full suite. Two causes,
both worth knowing:

1. It created `download_queue` through `db_session`, which **disposes the shared
   engine** on any `OperationalError` — and with `sqlite:///:memory:` a dispose
   erases the whole database. Running after
   `test_download_completion_not_found_loop` (which leaves the engine disposed)
   reproduced it every time. DDL now goes through a raw connection.
2. It created a hand-listed table, making it a **competitor** to the other
   modules that create `download_queue`: whichever ran first defined the table and
   the loser's INSERTs failed on missing columns. It now builds the table from the
   production schema, so the test database is the production shape. That fixed an
   unrelated pre-existing failure
   (`test_download_match_lifecycle::test_discovered_rows_never_returned`, which
   needed `next_retry_at`).
