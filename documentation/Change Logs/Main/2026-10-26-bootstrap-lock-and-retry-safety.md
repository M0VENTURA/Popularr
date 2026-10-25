# Data-safety and request-correctness fixes from the issue register

**Date:** 2026-10-26
**Area:** `db/bootstrap.py` · `services/upcoming_releases/wikipedia_scraper_service.py` ·
`api_clients/http_utils.py`
**Commit:** `fix(db): release the schema lock where it was taken, and stop the dedupe deleting real rows`

## Verification first

The register was checked against the code line by line before anything was
changed. Roughly half of it described either the OLD system or code that is
already correct — it is a lead list, not a defect list. Confirmed real and fixed
here:

| Claim | Verdict |
|---|---|
| Advisory lock released on a different connection | **real** |
| Tolerated DDL left the transaction aborted | **real** |
| Destructive upcoming-release dedupe (no MBID / override protection) | **real** |
| Sanitiser deletes a row on ANY rename failure | **real** |
| Sanitiser runs over MusicBrainz-sourced rows | **real** |
| Negative / NaN `Retry-After` reaches `time.sleep` | **real** |
| `172.` / `10.` prefix private-IP test | **real** |
| MusicBrainz retries bypass the deployment-wide throttle | **real** |

Ruled out (already correct or absent): empty-string Lucene escape entry, nested
client/transport retry loops, per-row savepoints in the scraper, driver-aware
connect args in `db/engine.py`, `flock` + `os.replace` in the rate limiter, and
everything referencing `SPTNR_QUEUE_PROCESSOR_MANAGED_EXTERNALLY` (that prefix
exists only in `old_system/`).

## 1. `db/bootstrap.py`

**The lock was released on a connection that never took it.**
`pg_try_advisory_lock` is session-scoped — PostgreSQL ties it to the physical
connection — and `pg_advisory_unlock` from another connection returns `false`
and releases nothing. `ensure_full_schema` acquired inside one `db_session()`
block and released in a `finally` that opened a SECOND one. It appeared to work
only because SQLAlchemy's pool is LIFO and usually returns the same connection;
when it did not, the lock stayed held until that connection died, so a crashed
bootstrap could wedge every later one.

The release now happens in a `finally` inside the acquiring block, after an
explicit `session.commit()` so no other process can take the lock and see
uncommitted DDL. The same `finally` treatment was applied to the two fixups
that released only on their success path.

**DDL failures aborted the transaction.** On PostgreSQL a failed statement
kills the whole transaction, so the old `try: ... except: pass` around the
`ALTER TABLE` meant every statement AFTER it failed with "current transaction is
aborted" — and the function reported the first error as if nothing else had
been attempted. New `_ddl_savepoint()` wraps each statement in
`begin_nested()`, the same fix already used per row in the scraper, and the
unique-index failure is now reported rather than fatal.

**The dedupe could delete real data, on every boot.** It keyed on
`(artist_name, album_name)` and kept whichever row had the lower `id`:

* a row carrying a resolved `release_group_mbid` lost to a scraped row with
  none — silently undoing MBID matching;
* a row with `mbid_manual_override` was deletable — silently undoing a manual
  confirmation;
* the first DELETE had **no `primary_type` term**, so a distinct single could be
  removed because an album with the same artist/title existed.

`_dedupe_upcoming_releases()` now ranks rows
override → `release_group_mbid` → MusicBrainz provenance → lower id, never
deletes an overridden row, and treats `primary_type` as part of the identity.
If a conflict survives that cannot be resolved without destroying information,
the unique index is NOT created and the log says so — keeping the table usable
without the index beats deleting a manually-confirmed row.

## 2. `wikipedia_scraper_service._sanitize_stored_rows`

The rename was wrapped in `except Exception:` and the handler **deleted the
row**, so a lock timeout, serialization failure or dropped connection destroyed
a perfectly valid row. Now a delete happens only when
`_is_unique_violation()` recognises a real duplicate (SQLSTATE 23505,
"duplicate key value", "UNIQUE constraint failed"); anything else logs a warning
and leaves the row for the next pass.

The `SELECT` previously read three columns and sanitised everything, including
rows whose identity came from MusicBrainz. It now reads `source`,
`release_group_mbid` and `mbid_manual_override`, and skips any row that is
human-confirmed, MBID-resolved, or MusicBrainz-sourced — the pass exists to
repair names a WIKIPEDIA SCRAPE mangled, and running the Wikipedia word-splitter
over an authoritative name can only corrupt it.

## 3. `api_clients/http_utils.py`

**`Retry-After` was fed straight to `min(float(x), 60)`.** That passes `"-5"`
and `"nan"` through, and `time.sleep(-5)` / `time.sleep(nan)` both RAISE —
inside the retry policy, so a recoverable 503 became a hard error.
`parse_retry_after()` accepts both RFC 9110 forms, rejects non-finite and
negative values, clamps the maximum, and **never raises**. A negative value
falls back to the backoff schedule rather than to 0: waiting zero is the
opposite of what a `Retry-After` is for.

**Private-network detection was a string prefix.** `host.startswith("172.")`
matches PUBLIC space — `172.217.x.x` is Google, `172.64.x.x` is Cloudflare — so
every request to those skipped rate limiting entirely; `"10."` matches any
hostname beginning with those digits. `is_private_host()` uses `ipaddress`
(correct CIDR boundaries, IPv6, link-local, zone IDs) plus an explicit name list
for `localhost`.

**MusicBrainz retries bypassed the deployment-wide throttle.** `get()` reserves
a slot once per CALL, before its own attempt loop, so attempts 2+ reached the
wire having reserved nothing — invisible in one process, wrong across the four
web workers plus the queue worker. The transport now reserves a slot on each
retry attempt. ⚠️ Deliberately not on attempt 1: that slot is already charged,
and charging it twice would double the interval and halve scan throughput.

## Verification

`tests/test_bootstrap_lock_and_retry_safety.py` (61).

Oracle — source reverted with the tests present: **59 failed / 2 passed**;
restored → 61 passed.

Regression sweep, 42 DB/API/upcoming-release suites, one process per file:
patched **17** failing ids vs baseline **17**, `Compare-Object` NEW **empty**.

`import app` → 425 routes. The `DELETE … USING` dedupe is PostgreSQL-only
syntax and so cannot run in the SQLite harness; it was reviewed by rendering the
generated statement rather than executed.
