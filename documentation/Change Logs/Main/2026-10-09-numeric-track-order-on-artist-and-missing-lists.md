# Numeric track order on the artist page and the missing-track list

**Date:** 2026-10-09 - **Area:** artist page / album page
**Reported:**

> When expanding albums on the artist page, the order isn't correct as it goes
> **1, 10, 11, 12, 2**

> Similar ordering issue seems to happen sometimes when importing missing
> tracks on the album page

## Why

`tracks.disc_number` and `tracks.track_number` are **TEXT**, so
`ORDER BY track_number` - and any `sort()` over the raw strings - compares
`"10" < "2"`.

`2026-10-05-album-track-ordering` fixed the **album page** with
`helpers/track_ordering` (one key: disc numeric -> track numeric, unnumbered
last -> title). These were the surfaces that never joined it:

| surface | where the order was decided |
|---|---|
| artist page -> expand an album (`/api/album/tracklist`) | `db/repositories/metadata.fetch_album_tracklist` - `ORDER BY ... COALESCE(track_number, '999')`, TEXT |
| album page -> library half of the missing-track comparison | `album_missing_service.get_library_tracks` - `ORDER BY ... track_number`, TEXT |
| album page -> the **missing list** (`/api/album/missing-tracks`) | `get_missing_tracks_from_db` - `ORDER BY ... COALESCE(track_number, '999')`, TEXT |
| duplicate-track groups | `duplicates.sort(key=lambda g: (g["disc_number"], g["track_number"], ...))` - tuple over TEXT |

Each produced exactly `1, 10, 11, 12, 2`.

## Fix

The SQL `ORDER BY` stays as a **pre-sort** - the same contract the album page
documents - but the order the user sees is decided in Python through
`album_track_sort_key`, so every surface shares one rule and cannot drift:

* `fetch_album_tracklist` also selects `disc_number` - **appended last**, so
  the positional reads in `album_service.get_album_tracklist` (`r[0]`, `r[1]`,
  `r[2]`, `r[4]`) keep their meaning - and returns the rows `sorted(...)` by
  the shared key;
* `get_library_tracks` sorts its rows before returning;
* `get_missing_tracks_from_db` sorts `missing`;
* the duplicate groups sort with the shared key instead of raw strings
  (unnumbered groups now follow numbered ones).

Nothing about *what* is returned changed - only the order, and only where it
was text-ordered.

## Tests

`tests/test_track_order_on_artist_and_missing_lists.py` - **9 new**:

* the repository returns `1, 2, 10, 11, 12` (the reported order is the
  *failure*), and an unnumbered track sorts **after** the numbered ones;
* the artist page's rendered shape (`position`) is numeric;
* `get_library_tracks` and `get_missing_tracks_from_db` are numeric (the
  missing list builds its own `missing_album_tracks` table so the test drives
  the shipped SQL unmodified);
* the duplicate sort uses the shared key;
* both modules import the shared key, and `disc_number` is appended last so the
  positional reads cannot silently shift.

**Oracle** - the two source files reverted: **9 failed**; restored: **9 passed**.

**Sweep** - 30 test files touching `album_missing_service` / `album_service` /
`fetch_album_tracklist` / `get_missing_tracks` / `track_ordering`: baseline
**11** -> changed **11**, regressions **(none)**.

`import app` -> 394 routes, `ast.parse` clean on both changed modules.

## Known pre-existing (verified against a clean baseline, not mine)

`tests/test_artist_tracklist_loading.py` - 4 failures: 2 assert that
`artist-releases.js` calls `/api/musicbrainz/release/tracks` (no route serves
it), and 2 raise `NameError: RELEASE_GROUP_MBID` inside the test itself. They
fail identically with these changes stashed.

## Files

- `db/repositories/metadata.py`
- `services/metadata/album_missing_service.py`
- `tests/test_track_order_on_artist_and_missing_lists.py` (new)
