# Upcoming releases: a same-titled single no longer swallows the upcoming album

**Date:** 2026-10-11
**Area:** upcoming / MusicBrainz
**Commit:** `fix(upcoming): a same-titled single no longer swallows the upcoming album`

## Symptom

The MusicBrainz upcoming lookup appeared to find only upcoming **singles**, not
**albums**. MusicBrainz itself has the albums (verified directly against the MB
API — e.g. Clutch has RG "Wasted Lands" as both Single@2026-09-25 and
Album@2027-01-22), but only one row per artist+album ever reached the
`upcoming_releases` table.

## Root cause

The table's dedup key was `(artist_name, album_name)` **only**. The ubiquitous
"album named after its lead single" pattern therefore collapsed the single and
the album into ONE row:

1. The single's write INSERTed the row (past date).
2. The album's write matched the same row; `_UPDATE_RELEASE_SQL`'s
   earliest-date rule kept the single's **past** date and the COALESCE took the
   album's type — producing rows like `album@2026-09-25` for Clutch.
3. The album's **future** release date was silently discarded, so the upcoming
   album vanished from the display window.

Bootstrap compounded it: `ensure_upcoming_releases_schema()` deleted
same-source duplicate (artist, album) rows at **every startup**, re-collapsing
any pair that did exist.

## Fix

The dedup identity is now `(artist_name, album_name, primary_type)` with
`primary_type` normalised to `''` when absent:

- **Typed match first** (`musicbrainz_fetcher_service`): a typed MusicBrainz
  write updates its own typed row — MusicBrainz is the authority for its own
  release group, so the date **refreshes** to the incoming value
  (`_UPDATE_TYPED_RELEASE_SQL`) instead of being pinned by the earliest-date
  legacy merge.
- **Legacy absorb**: with no typed match, a write falls back to the untyped
  (`''`) row for the same artist+album — Wikipedia rows and pre-existing
  legacy rows are still absorbed, with the original earliest-date merge
  semantics (`_UPDATE_RELEASE_SQL`).
- Otherwise a new typed row is INSERTed — same-titled single + album now live
  as two rows with their own dates and MBIDs.
- The INSERT's `ON CONFLICT` target, the Wikipedia scraper's upsert, and the
  manual "add upcoming release" route's dedupe all follow the new key.
- `db/bootstrap.py`'s same-source dedupe now only collapses rows **within the
  same primary type**, and creates the 3-column unique index
  (`uq_upcoming_artist_album_type`), dropping the old 2-column one.
- Migration `019_upcoming_releases_type_in_key`: normalises NULL types to
  `''`, makes the column NOT NULL (PostgreSQL), swaps the key, and is a no-op
  on fresh databases (bootstrap creates the new key directly).

## Notes

- The API display window (`upcoming_releases_lookahead_days`, default 14–28
  days) legitimately hides far-future albums — a 2027 release only *shows*
  after widening that window in Config. But it must first **exist** in the
  database; that existence was the actual defect.
- A collapsed legacy pair (e.g. the live Clutch row) self-heals on the next
  daily scan: the album inserts its own row with its true future date.

## Tests

- `tests/test_upcoming_same_title_single_and_album.py` (new, 6 tests):
  same-titled single+album both persist with own dates/MBIDs; idempotent
  re-run; typed date refresh; untyped wiki row absorbed (by single or album);
  same-typed case-variant upsert unchanged.
- Oracle at baseline `9bb2ae5a`: 5 of 6 fail (bug present), 6 of 6 pass with
  the fix.
- Sweep (`test_upcoming_musicbrainz_discovery`,
  `test_upcoming_releases_sources`, `test_upcoming_release_window`,
  `test_wikipedia_scraper`, `test_migrations_idempotent`,
  `test_artist_upcoming_releases`, `test_upcoming_search_handler_wiring`,
  `test_login_no_plaintext_fallback`): failing set identical to baseline
  (7 pre-existing environment failures — bare-tracks/PG-flavoured chain and
  the live-Navidrome login test).
