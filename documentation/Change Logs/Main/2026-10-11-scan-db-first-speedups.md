# Full-scan DB-first audit: all seven gaps landed (G1-G7)

Date: 2026-10-11
Branch: develop
Commits: `a96c7097` (G1/G2/G4/G5/G6) + this commit (G3/G7)

## Context

Audit of the full scan pipeline asking "what can check the DATABASE first
instead of searching online each time". The pipeline is already largely
DB-first (popularity prefetch, per-album MB recording batch, singles-verdict
TTL, `artist_release_cache`, album-type "already on the row", album-art
cache, in-process MB LRUs) — the wins were in the gaps. A full scan's wall
clock is dominated by the **global 1 req/s MusicBrainz throttle** and
per-track Last.fm fan-out, so every avoided call is ~1s (MB) or ~0.5s (LF).

## G1 — negative MBID resolutions are now recorded and served

`get_suggested_mbid` searched MB for every track the album batch couldn't
identity-map (duplicate titles without positions, releases MB lacks) — and
**recorded nothing for a miss**, while the read path refused empty entries
anyway (`if mbid`). Every scan PROCESS therefore re-spent a full MB second
per unresolvable track. Now:

* a miss is recorded via the same `_record_mbid` machinery;
* the read path serves it for `_MBID_NEGATIVE_TTL_SECONDS` (**7 days** —
  shorter than the 30-day positive TTL so a genuinely new recording is still
  found within the week);
* a live positive can never be clobbered by a miss (the read path returns
  positives before any search).

## G2 — the artist Last.fm map covers deep cuts from the one bulk call

`_lf_top_tracks_map` fetched only the top **200** tracks per
`artist.getTopTracks` — ONE call per artist. Every title outside the map
fell to the deep-cut path: search-aggregation + `track.getInfo` + variant
lookups, **3-6 Last.fm calls each, per TTL**. Widened to
`_LF_TOP_TRACKS_LIMIT = 500` (a constant, like the other cache knobs here):
zero extra calls per artist, and it covers the deep cuts of most
discographies. Last.fm may cap lower; a shorter answer is a smaller map,
never an error.

## G4 — the album release-year SELECT runs once per album

`_album_release_year` ran the identical `MIN(release_year)` SELECT **per
TRACK** in the release-date check. Memoised per (artist, album) per process,
negatives ("no year stored") memoised too.

## G5 — definitive non-single verdicts live 3× the base TTL

A stamped verdict is a COMPLETED verdict (track_stage never stamps on
error), so `is_single` falsy = a definitive "not a single" — and a track's
release history does not change. Re-running the full singles arm battery
(up to ~4 MB calls + Last.fm + Discogs per track) at the positive cadence
for old deep cuts was the dominant singles-pass cost. Definitive negatives
now use `_SINGLE_NEGATIVE_TTL_MULTIPLIER = 3` (7d → 21d, 3d → 9d, 24h →
72h); positives and unverdicted rows keep the base ladder. Updated
`test_an_expired_timestamp_is_not_fresh` to the multiplied boundary (its
contract — expiry works — is unchanged).

## G6 — the dead `appears_on_various_artists` arm removed

The arm in `detect_single_for_track` was guarded by `hasattr(mb_client,
"appears_on_various_artists")` — **the method was never ported from
`old_system/`**, so the flag could never be set and the whole
`mb_compilation_confirmed` plumbing (flag, source entry, level default,
config key, medium-source counter) was dead. Removed. (The old-system
implementation was several MB calls per track — if the VA signal is wanted
later it should be designed with artist-level caching then.)

## Tests

* New `tests/test_scan_db_first_speedups.py` (15): miss recorded + served +
  expiry + positive-never-clobbered (fake MB http, call-counting); the
  widened map (limit asserted, rank-300 title answers from one call with
  `get_track_info`/`search_track` patched to fail); the album-year memo
  (12 tracks → 1 query, negatives memoised); the negative TTL ladder (old
  negative fresh, positive control unchanged, multiplied window expires);
  the dead arm fully gone.
* **Oracle** at `3cc48aa3`: **10 failed / 3 passed / 2 errors** (3 =
  either-way controls).
* **Sweep** 17 affected suites: base `36 failed / 227 passed` vs new
  `36 failed / 227 passed`, failing sets **identical** → **0 regressions**
  (36 pre-existing singles-detection failures, untouched).

## G3 — the album-tracklist recording identity is persisted (migration 018)

`scan_stage_runner` resolves each album's recording MBIDs once per album per
pass via `get_listenbrainz_album_tracklist_with_release` (a ListenBrainz
metadata lookup), and the result feeds the per-album MB recording batch and
track_stage's MBID-keyed LB lookup. The identity was already persisted per
(artist, title) as `source='album_tracklist'` rows — **but without the
MBID**, so the runner's guard (which skips the lookup only when the seeded
entry carries source AND mbid) could never fire from database-seeded data:
every pass re-ran the lookup for every album.

* **Migration `018_track_popularity_cache_recording_mbid`** adds
  `track_popularity_cache.recording_mbid` (inspector-guarded, dialect
  portable; skips gracefully when the table itself is bootstrap-created, so
  a fresh chain-run converges — the same trap the chain suite caught).
* `db/schema.py` DDL updated (fresh bootstraps create the column).
* `upsert_track_popularity_bulk` writes it with
  `COALESCE(EXCLUDED.recording_mbid, cache.recording_mbid)` — the
  artist-prefetch writer (listeners only, runs far more often) can never
  clobber a known MBID with NULL.
* the reader SELECT + `_row_to_entry` pass it through, and the runner's
  album-tracklist persist block now carries it — closing the loop: pass N
  resolves, pass N+1 reads, zero ListenBrainz calls.

## G7 — conditional GETs on the MusicBrainz client

`MusicBrainzHttpClient.get()` now revalidates identical requests with
`If-None-Match`: a 304 serves the cached body without re-downloading it.
The cache key includes **every query param (sorted)**, so two different
queries can never collide onto one payload; responses without an ETag are
never cached; `clear_caches()` clears the layer. Honest scope: this is a
**bandwidth** win across process restarts — the 1 req/s throttle still
applies to the revalidation request itself. Bodies for browse/search/release
calls run into the hundreds of KB, so it is not nothing, but G1-G5's
avoided calls are the wall-clock wins.

## Tests

* New `tests/test_scan_db_first_persistence.py` (11): the MBID round-trip
  (schema, write→read→entry mapping, the NULL-preserving COALESCE, a fresh
  MBID still overwriting, the runner persist block, migration
  portability); the conditional-GET layer (first request unvalidated, the
  identical request revalidates and serves the cached body on a body-less
  304, different params never share a validator, untagged responses never
  cached, `clear_caches()` clears the layer) — all with call-counting fake
  sessions.
* **Oracle** at `a96c7097`: **11 failed / 0 passed** (every test
  discriminates).
* **Sweep** 30 migration/popularity-cache/MB-client suites: base
  `40 failed` vs new `40 failed`, failing sets **identical** → **0
  regressions** (the chain suite's 011-era pre-existing failures untouched;
  the two chain tests G3 first broke — bootstrap-created table on a fresh
  chain — are fixed by the graceful skip).
