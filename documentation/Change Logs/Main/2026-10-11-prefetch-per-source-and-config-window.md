# Artist prefetch: per-source freshness and a Config-driven cache window

**Date:** 2026-10-11
**Area:** scan / prefetch / config
**Commit:** `fix(scan): the artist prefetch re-fetches per stale source and follows the prefetch cache window`

## Reported

> "Is the prefetch information saved to database? This shouldn't need to be
> grabbed each time that artist is scanned if it's not a forced scan … But
> that artist had ran 3 times each time the server was restarted, each time
> doing a prefetch again."

The log showed a **48.9 s** `artist_prefetch` section on a non-forced scan,
with `[RELEASE_CACHE] Artist releases cached … mb_count=46` — i.e. the
release cache re-fetched MusicBrainz on every run.

## Answer

**Yes — the prefetch is persisted**, in four places:

| Component | Table | Old freshness |
|---|---|---|
| Popularity (Last.fm / ListenBrainz) | `track_popularity_cache` | hardcoded 7 days |
| Artist releases (MusicBrainz / Discogs) | `artist_release_cache` | hardcoded 7 days |
| Similar artists | `artists` table | 90 days |
| Missing releases | `missing_releases` | rolling (`last_checked`) |

The popularity cache was working (the same log showed
`167 catalogue score(s) … pre-loaded` from the database). The **release
cache** was the one re-fetching.

## Root cause

`prefetch_artist_releases` skipped only when **both**:

```python
if _artist_cache_fresh(artist) and (_cache_has_source(artist, "discogs") or not discogs_artist_id):
```

The second clause meant: *"if a Discogs id is known, a Discogs row must
exist"*. Your log showed `discogs_count=0` — the Discogs fetch returned **zero
rows** for the artist. So `_cache_has_source(artist, "discogs")` was `False`
for ever, the whole gate was `False`, and **MusicBrainz was re-fetched on
every scan**, never remembering the empty Discogs check. That is the
"every restart prefetches again" symptom.

A second, latent gap: `artist_release_cache.updated_at` was present in the
`CREATE TABLE` but **not** in `db.schema.COLUMN_REGISTRY`, so a database
bootstrapped by an older build never gained the column — and the freshness
check (`MAX(updated_at)`) would then fail, returning "stale" for ever.

## Fix

1. **Per-source freshness.** `_artist_source_fresh(artist, source)` checks
   `MAX(updated_at)` **for one source**. MusicBrainz freshness is now
   independent of Discogs. An artist whose Discogs fetch returned 0 rows no
   longer forces a MusicBrainz re-fetch.
2. **Empty Discogs results are remembered.** When the Discogs prefetch runs
   and returns 0 rows, a reserved sentinel row
   (`title='__discogs_checked__'`, `release_type=''`) is written so the gate
   can skip next time. Every reader excludes it: the single/EP title queries
   filter on `release_type IN ('single','ep')`, the missing-releases diff
   filters on `source='musicbrainz'`, and `get_cached_artist_release_rows`
   now excludes it explicitly.
3. **The TTL follows Config.** `features.prefetch_cache_days` (default 7,
   matching the old behaviour) is read by **both** the popularity and
   release caches via a shared `_prefetch_cache_hours()`. `0` means "always
   re-fetch", matching the skip-window convention. Surfaced on the Config
   page (Scan Behaviour card, "Artist Prefetch Cache Window") in both the
   live and test_site trees.
4. **Schema registry gap closed.** `updated_at` is added to
   `COLUMN_REGISTRY["artist_release_cache"]` so any database missing the
   column gains it at bootstrap.
5. **Driver-robust freshness.** SQLite (and some drivers) return
   `CURRENT_TIMESTAMP` as text; the freshness check now parses ISO strings,
   so the cache works on both engines and is robust to driver differences.

## Tests

`tests/test_prefetch_cache_config_and_per_source.py` (14):

- The reported bug: an artist with a Discogs id and 0 Discogs rows — the
  second call skips **both** sources (baseline: re-fetches MusicBrainz).
- Per-source independence: a stale Discogs source re-fetches only Discogs;
  MusicBrainz stays skipped.
- The sentinel is written for an empty Discogs check and never leaks into
  `get_artist_single_titles`, `get_artist_promo_titles`,
  `get_cached_artist_release_rows`, or the missing-releases diff.
- `prefetch_cache_days = 0` never fresh; `30` keeps a 10-day-old cache
  fresh; unset defaults to 7 days.
- Both cache services read the shared Config knob.
- `updated_at` is present in the schema registry.

**Oracle** at `9757c078`: 10 of 14 fail (the bug + config tests); 4 pass
(controls). **Sweep**: 6 release-cache/discogs suites → 17 failed / 93
passed, failing set **identical** to baseline (pre-existing
`test_release_category_persistence` ×3 + `test_artist_tracklist_loading`
×4 and their companions); 5 popularity/scan suites → 57 passed;
`test_scan_windows_and_scheduled_scans.py` → 38 passed.

## Note

The similar-artists cache keeps its own 90-day window
(`_SIMILAR_ARTIST_CACHE_DAYS`) — similar artists change far more slowly than
popularity or release data, and aligning it to the (much shorter) prefetch
window would make that prefetch *more* frequent, not less.
