# Similar artists: artist-level refresh, DB-only album path

Date: 2026-10-11
Branch: develop

## Asked

*"Should similar artist detection be removed for albums and only run for
artists, then have the albums pages be populated with the artist results?
This could speed up the process. But if a single album scan is ran and no
similar artists exist, then it will run the artist similar lookup first."*

## Answer: the architecture already existed — two defects made it behave like per-album detection

Verified in code before changing anything:

* The fetch was already **artist-level + DB-cached**: `_fetch_similar_artists`
  read `artists.similar_artists_lastfm/listenbrainz/last_updated` first and
  refreshed only when the cache was older than 90 days.
* The artist page (`artist_metadata_service.get_similar_artists`) is
  **DB-only** — it never fetches.
* **No consumer even reads the album-context copy** of the lists (grep:
  the only readers of `similar_artists_*` are the artist-page API and the
  UI route — both straight off the `artists` table).

But two defects made it behave exactly like per-album detection:

1. **Checked-empty was not remembered.** The cache write stored NULL for an
   empty result (`if result["lastfm"] else None`) while stamping the
   timestamp anyway; freshness required a non-empty list — so an artist
   with **no similar artists re-fired Last.fm + the artist-MBID lookup +
   ListenBrainz for EVERY album of EVERY scan, forever**. The same loop
   hit any failing source (fresh timestamp, empty list → not "fresh").
2. **The fetch trigger sat inside per-album `_run_full_enrichment`** — so
   the empty/failure cases paid the network per album, and every album paid
   a cache read it did not need to be able to trigger.

## Fix — the proposed design, as implemented

* **`refresh_similar_artists(artist, options)`** (renamed from
  `_fetch_similar_artists`) runs **once per artist** from the scan
  runner's per-artist background prefetch thread (next to the popularity
  prefetch, inside the same budget and singles-pass gate). A scan of N
  albums pays the stale sources at most once per artist; a single-album
  scan fills the artist-level cache on its way past — the artist page
  reads the table directly whenever it is opened (your lazy-fill clause).
* **The per-album path is now a pure DB read** (`_read_similar_artists`)
  — no network can be triggered from album processing at all.
* **Per-source checked/unchecked bookkeeping** (the semantic core):
  * a checked result — **including an empty one** — is persisted as a JSON
    list (`[]`) and honoured for `_SIMILAR_ARTIST_CACHE_DAYS` (90): an
    artist with no similar artists is an ANSWER, not a cache miss;
  * a failure or an unavailable dependency (no artist MBID for
    ListenBrainz) leaves the column NULL and retries on the next **artist**
    pass — never the next album, and never remembered as "no similar
    artists";
  * a disabled integration is not a check at all and costs nothing to
    reconsider.

## Behaviour changes to note

* VA/compilation artists no longer fetch similar artists at all — the
  refresh runs through the runner's prefetch thread, which is gated on
  non-compilation artists (the old per-album fetch happily asked Last.fm
  for "Various Artists" on every compilation album).
* The album-context injection of the lists is unchanged (harmless, no
  consumers) so album pages/templates are untouched.

## Tests

* New `tests/test_similar_artists_artist_level_only.py` (14): the album
  read is network-proof (client construction patched to fail); NULL vs
  `[]` semantics; checked-empty remembered across artist passes; failures
  retried per artist pass and never persisted as empty; no-MBID leaves
  ListenBrainz unchecked; singles pass touches nothing; the wiring
  (runner refreshes once per artist, albums read only, old fetch name
  gone); controls (the artists table is still filled with both lists and a
  timestamp; a disabled integration constructs no client).
* **Oracle** at `7c25cd83`: **14 failed / 0 passed** (every test
  discriminates).
* **Sweep** 11 enrichment/similar-artist suites: base `3 failed / 171
  passed` vs new `3 failed / 171 passed`, failing sets **identical** →
  **0 regressions**.
