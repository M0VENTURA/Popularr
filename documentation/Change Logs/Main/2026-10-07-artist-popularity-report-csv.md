# The artist page downloads a popularity report

**Date:** 2026-10-07
**Requested:**

> Add a button to the artist page on test_site that downloads a report of all
> tracks for the artist. It will list the tracks along with the total listens
> on last.fm and musicbrainz and all other relevant popularity data ordered
> from most popular to least popular.

## What was built

**`GET /api/artist/popularity-report?artist=…`** (`routes/artist_routes.py`)
returns the whole artist's catalogue as CSV, most popular first:

```
rank, title, artist, album, disc_number, track_number, year,
final_score, popularity, stars,
lastfm_listeners, lastfm_playcount, lastfm_score,
listenbrainz_listens, listenbrainz_users, listenbrainz_score,
is_single, single_confidence, single_confidence_score,
is_live, popularity_frozen
```

(The two platforms' raw counts are Last.fm **listeners**/playcount and
ListenBrainz **listens**/users — MusicBrainz itself holds no listen counts, so
"the MusicBrainz side" of a popularity report is the recording identity and
score; both source scores are columns.)

**The button** sits in the artist page's **Actions** menu, beside Corrections
and Genre Management, in `test_site/templates/Pages/artist_detail_v2.html` —
a plain link, so the browser downloads it with no JS round-trip.

## Design decisions

* **Ordering matches the page it hangs off.** The artist page's own top-tracks
  rank is `final_score` → `popularity` → `stars`, so the SQL uses
  `COALESCE(final_score, popularity, stars, 0) DESC, LOWER(title)` — a report
  that disagreed with the screen would be worse than no report. `artist_z_score`
  is *read* by the page but never written by any writer (not a column), so it
  is not in the chain.
* **Query parameter, not a path segment.** Every other artist API in this file
  takes `?artist=`, and a `<path:…>` converter would swallow the trailing
  static segment.
* **UTF-8 BOM** — titles carry accents, and without the BOM Excel reads the
  file in the local codepage and mangles them.
* **Off the event loop**: the query reads every track the artist owns, so the
  handler wraps it in `asyncio.to_thread` — the async ratchet exempts a handler
  only when it offloads, and growing `_KNOWN_OFFENDERS` is not an option.
* **Test site only, as asked.** The route is shared (one endpoint serves both
  trees) but only the test-site page got the button; the live tree has its own
  copy of `artist_detail_v2.html` if/when cutover parity is wanted.

## Tests

`tests/test_artist_popularity_report.py` — **11**:

* returns `text/csv` with `attachment` and a filename derived from the artist;
* the header carries both platforms' counts, both source scores, the combined
  score and the stars;
* **rows come out most popular first** with `rank` following the order (not the
  insertion order);
* an unscored track still appears, at the bottom;
* only this artist's tracks — another artist's higher score cannot leak in;
* an album-artist row (`COALESCE(album_artist, artist)`) is included, matching
  the key every other artist query uses;
* listeners/listens/stars/score round-trip into the cells;
* CONTROL: no artist → 400 (not the whole catalogue);
* CONTROL: no session → not 200 (the endpoint stays behind the auth gate);
* the test-site Actions menu contains the download, and it sits with the other
  artist actions.

**Oracle:** with the route and the template stashed → **10 failed / 1 passed**,
the pass being the auth-gate control (a 404 is also "not 200"). Restored,
11/11.

One test-only trap worth recording: `_recreate_test_schema` does **not** wipe
`tracks` between tests, so a fixed `id` collided with an earlier test's row and
`ON CONFLICT DO NOTHING` skipped the insert — the report then held the previous
test's tracks. The file now clears the table per test.

## Verification

- new suite: **12 passed** (11 in the run below; the 12th,
  `test_the_named_endpoint_actually_exists`, was added after it started and
  verified on its own — it pins that `url_for`'s target resolves, because a
  typo there raises `BuildError` and 500s the whole artist page)
- affected set (15 files): **671 passed / 13 failed**, against a baseline
  subset of 13 for the same files → **0 new, 0 fixed** (all 13 pre-existing)
- full suite: **225 failed / 4761 passed / 2 skipped** in 698 s against a
  baseline of 303 failures → **79 fixed, 0 real new**. The single NEW id is
  `test_download_completion_not_found_loop.py::TestDeepFileSearch::
  test_sibling_torrents_root_is_searched`, the known Windows path-case flake.

  The first attempt died part-way with `Windows fatal exception: access
  violation` (RAM was down to ~400 MB); re-ran clean — the same environment
  flake seen several times before.

## Files

- `routes/artist_routes.py`
- `test_site/templates/Pages/artist_detail_v2.html`
- `tests/test_artist_popularity_report.py` (new)
