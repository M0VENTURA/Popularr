# The four-hourly Navidrome import was deletable by an unrelated toggle, and Upcoming Releases was a 30-second query

**Date:** 2026-10-22 · **Area:** `scheduler` · `api` · `upcoming releases`

## Reported

> "The navidrome scan isn't running every 4 hours like it's supposed to, also the
> upcoming releases from musicbrainz doesn't seem to be updating. The page at
> /downloads/discover/upcoming is only showing 2026-10-19 as the most recent date
> under the Musicbrainz Daily Collection, then when selecting it again it gets
> **Error:** Request timed out after 30s"

Two independent defects. Neither was a scheduling or a MusicBrainz problem.

| symptom | actual cause |
|---|---|
| Navidrome scan "isn't running every 4 hours" | the job was **not registered** — it inherited the Watcher's *Auto Import* toggle |
| "Error: Request timed out after 30s" | the list query re-evaluated a regex **4000 × 40000** times |

---

## 1. The four-hourly import was governed by the wrong switch

`services/scheduler/scheduler_service.py`:

```python
if navidrome_import_interval <= 0 or not _enabled("navidrome_import", "auto_import_enabled", True):
    _remove_job("navidrome_import")
```

`_enabled(job_id, watcher_key, default)` falls back to `watcher.<watcher_key>`.
For this job that meant **`watcher.auto_import_enabled`** — the Watcher card's
**Auto Import** checkbox. Unticking it **deleted the four-hourly job**.

That is close to the opposite of what the setting advertises. The Config page's
own help text for the sibling job reads:

> Library Sync Interval (minutes) — … **0 = never** (equivalent to unticking **Auto Import**)

and the Navidrome Import card says the job exists so `tracks` follows the library
"on its own". So the toggle that the UI documents as *the* off-switch for the
**library sync** was silently also the off-switch for the **import**.

⚠️ **NOTHING REPORTED THE LOSS.** The job was simply absent from
`scheduler.get_jobs()`. There is no health check for "a job I expect is missing",
and the scheduler continued to describe itself as healthy — the only signal was
that the library stopped following Navidrome, which looks like a Navidrome
problem rather than a Popularr one.

### Fixed

The fallback is now **opt-in**, and `navidrome_import` passes `None`:

```python
def _enabled(job_id: str, watcher_key: str | None, default: bool = True) -> bool:
    job_cfg = jobs.get(job_id, {}) or {}
    if "enabled" in job_cfg:          # the job's OWN key always wins
        return bool(job_cfg.get("enabled"))
    if watcher_key is None:           # this job does not inherit a Watcher toggle
        return default
    return bool(watcher.get(watcher_key, default))
```

| job | enable flag |
|---|---|
| `navidrome_import` | **its own** `scheduler.jobs.navidrome_import.enabled` (or `interval_minutes: 0`) |
| `library_sync` | own key, else `watcher.auto_import_enabled` |
| `popularity_scan` | own key, else `watcher.auto_popularity_scan` |

Verified by registering jobs into an in-memory scheduler:

```
default (no entry)                -> navidrome_import interval[4:00:00]  ✅
watcher.auto_import_enabled=False -> navidrome_import interval[4:00:00]  ✅  (was: absent)
watcher.auto_import_enabled=False -> library_sync ABSENT                 ✅  (unchanged)
jobs.navidrome_import.enabled=False -> navidrome_import ABSENT           ✅
jobs.navidrome_import.interval_minutes=0 -> navidrome_import ABSENT      ✅
```

---

## 2. The Upcoming Releases list query was quadratic

`routes/upcoming_releases_routes.py`. Three clauses were written in shapes
PostgreSQL can only plan as **nested loops**, because the normalising expression
is applied to the *scanned* table and then correlated to the *outer* row.

### 2a. The library anti-join

```sql
NOT EXISTS (
    SELECT 1 FROM tracks t
    WHERE LOWER(REGEXP_REPLACE(COALESCE(NULLIF(t.album_artist,''), t.artist),
                               '[^a-zA-Z0-9]', '', 'g'))
        = LOWER(REGEXP_REPLACE(upcoming_releases.artist_name, '[^a-zA-Z0-9]', '', 'g'))
      AND LOWER(REGEXP_REPLACE(t.album, '[^a-zA-Z0-9]', '', 'g'))
        = LOWER(REGEXP_REPLACE(upcoming_releases.album_name, '[^a-zA-Z0-9]', '', 'g'))
)
```

The predicate references `upcoming_releases` **inside the `tracks` scan**, so the
expression cannot be indexed or hashed: it is re-evaluated for every candidate
track of every release row — **N releases × M tracks × 4 regexes**. On a modest
library that is tens of millions of regex evaluations for one page load.

Rewritten so `tracks` is normalised **once, in its own DISTINCT subquery**, and
then hash-joined against the 2N release keys. Identical predicate — a release is
hidden when no library track normalises to its (artist, album) — and no outer
`DISTINCT` is needed because one match is enough to remove a row.

### 2b / 2c. The per-page batch lookups

`album_pairs` and `queued_pairs` built **one `OR` branch per release on the
page**, again over `tracks` / `download_queue`:

```python
pair_conditions = " OR ".join(
    f"(LOWER(REGEXP_REPLACE(COALESCE(NULLIF(album_artist,''), artist), …)) = :a{i} AND … = :b{i})"
    for i in range(len(album_pairs))
)
```

PostgreSQL cannot hash that either, so it scanned the table and re-evaluated up
to `limit` branch tests per row — the same N × M pass, on the same request.
Rewritten to declare the wanted pairs as a derived table and **JOIN** on the
normalised keys. `UNION ALL` is used rather than `VALUES`/`unnest` so the same SQL
runs on SQLite (the test polyfill) and PostgreSQL.

### Measured

A replica was generated with **40 000 tracks, 4 000 releases, 500 queue rows**.
Absolute times are SQLite, so they do not transfer to PostgreSQL; the **shape**
does, and `old_rows == new_rows` in both cases.

| query | old shape | new shape |
|---|---|---|
| list (`hide_in_library=1`) | **292 861 ms** | **119 ms** |
| per-page album pair lookup | **3 198 ms** | **135 ms** |

The 30 s figure the user saw is the client's own `DEFAULT_TIMEOUT_MS` in
`test_site/static/js/utils/api.js` — the request was genuinely far past it.

---

## Tests

New suites (both fail on the pre-fix tree — 7 failures — and pass after):

| suite | tests | fails pre-fix |
|---|---|---|
| `tests/test_scheduler_job_enable_flags.py` | 13 | 2 |
| `tests/test_upcoming_list_query_shape.py` | 17 | 5 |

⭐ The split is deliberate. `test_scheduler_job_enable_flags.py` encodes the
**contract** ("Auto Import must not stop the import"; "the two toggles are
independent"). `test_upcoming_list_query_shape.py` pins the **semantics** (12
tests, green on BOTH trees, proving the rewrite changed no result) separately from
the **shape** (5 tests, red before, green after — asserting `tracks` is not
correlated and the `" OR ".join(...)` predicates are gone).

The shape guard is worth its brittleness: it is the only thing that would catch
this being rewritten back into an unindexable predicate by someone reading only
for correctness.

### Fixture completed, not weakened

`tests/test_scheduler_jobstore_race.py::TestStoreAwareRegistration._DISABLED_CFG`
set `watcher.auto_import_enabled: True` while disabling every other job — which is
exactly the trap in §1, so `navidrome_import` registered during a test whose point
is "no other job is registered", and
`test_existing_job_in_store_is_not_re_registered` failed.

⚠️ That failure was **already red on the baseline** (`1 failed, 6 passed` on a
pristine `ed70e757`), so it could not be blamed on this change — and it could not
be fixed by loosening the assertion either. `navidrome_import` is now disabled by
its **own** key in the fixture, which both repairs the test and pins the new
contract.

## Regression sweep

15 related suites: baseline **8 failed / 237 passed** → fixed **0 failed / 245
passed**.

Wider keyword sweep (`upcoming|scheduler|scan_windows|navidrome|library_sync|
queue_retry_scheduler`, 585 tests): baseline **17 failed / 568 passed** → fixed
**9 failed / 576 passed**. The 8-test difference is exactly §1 and §2. The
remaining **9 are byte-identical on both trees** and unrelated (Navidrome
playlist POST fakes, genre backslash splitting, login fallback, track-genre
Navidrome fallback).

`import app` → **427 routes**, unchanged.
