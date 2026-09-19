# Artist page froze the server on scan: page-load MusicBrainz probe storm

**Date:** 2026-09-20
**Area:** `static/js/artist-releases.js` / `test_site/static/js/pages/artist-releases.js` / `routes/ui_routes.py` / artist page templates
**Status:** fixed, guarded by `tests/test_artist_page_contract.py`

---

## Symptom

Starting a scan **from the artist page** froze the whole application — not just
that page, every request handled by the same worker — and the scan itself made
almost no progress. Reported as a regression from the artist-page rebuild
("this wasn't happening prior").

Note this is a *different* failure from the event-loop blocking fixed in
`2026-09-19-artist-page-scan-freeze-event-loop.md`. That fix moved
`routes/ui_routes.py::artist_detail`'s context build off the event loop. This one
is caused by the *page itself* hammering an endpoint that needs MusicBrainz.

## Root cause: one MusicBrainz request per owned album, fired on page load

The rebuilt artist page added `static/js/artist-releases.js`, which fills in the
per-row "**N missing**" badges:

```js
Array.prototype.forEach.call(doc.querySelectorAll('.release-item[data-status="library"][data-mbid]'), function (item) {
  getJson('/api/album/missing-tracks?...')   // <- one request PER ALBUM, all in parallel
});
```

and `fetchMissingTrackCounts()` is called from `init()`, i.e. on **every page
load**.

Three properties of that endpoint make the burst lethal:

1. **It calls MusicBrainz.** `routes/album_routes.py::api_album_missing_tracks`
   → `services/metadata/album_missing_service.py::get_missing_tracks` →
   `fetch_musicbrainz_release_metadata(mb_mbid)` (and, with no stored MBID, a
   `search_releases` search). (The `mbid` the page already sends is not even read
   by the route — the MBID is re-resolved from the DB.)
2. **It is a SYNCHRONOUS handler**, so Quart runs it in the event loop's
   **default executor** — verified in `quart/utils.py`:
   `loop.run_in_executor(None, ...)`. That is the *same pool* that
   `asyncio.to_thread` uses, including for `artist_detail`'s own context build.
3. **MusicBrainz is globally throttled to ~1 req/s** by
   `api_clients/musicbrainz_http.py::_strict_throttle`, which enforces the budget
   by **reserving** a future slot and only then sleeping to it:

   ```python
   if elapsed < 1.2:
       sleep_time = 1.2 - elapsed
       _LAST_MB_REQUEST_TIME = now + sleep_time   # slot claimed, right now
   ...
   if sleep_time > 0:
       time.sleep(sleep_time)                     # ...and then it waits
   ```

So loading the artist page for an artist with N owned albums:

- claims **N slots** of the shared MusicBrainz budget, pushing the running
  scan's own MusicBrainz calls ~`1.2s × N` further out — the scan looks stuck;
- holds **one executor thread per probe** while it sleeps;
- and therefore **starves the default executor**, so every other request in the
  worker — including the artist page's *own* render via `asyncio.to_thread` —
  cannot even start.

The trigger is structural: `scan_artist_custom` answers with a redirect back to
the artist page, so the probe burst lands **at the exact moment the scan
starts**. An artist with 20 albums issued ~20 MusicBrainz-backed requests
(~24 s of the global budget) against a worker pool of `min(32, cpu+4)` threads.

**Why it is a regression:** before the rebuild the live artist page had *no
working JavaScript at all* — `static/js/artist_detail.js` is a 5346-line Jinja
page template loaded via `<script src>`, so the browser discarded the entire
file. The rebuild replaced it with real modules, and with them came the
per-album probe burst.

## Fix

### 1. The probes are bounded (`MISSING_PROBE_CONCURRENCY = 3`)

`fetchMissingTrackCounts()` now collects the probe jobs first and runs them
through a small worker pool — each completion pulls the next job — so at most
three requests are ever in flight. The page can no longer monopolise the shared
executor or the MusicBrainz budget, whatever the album count.

### 2. The probes stand down entirely while a scan is running

Bounding is not sufficient on its own: a scan issues MusicBrainz calls
continuously, and any page-load probe still queues ahead of (or beside) them.
The route now publishes a flag and the module honours it:

- `routes/ui_routes.py::_build_artist_detail_payload` probes the canonical
  `services.scanning.pipelines.popularity_pipeline::is_popularity_scan_active()`
  (the accessor the scan routes and scheduler already use — it covers both the
  `popularity_scan` and `full_scan` progress rows) and returns
  `"scan_active": bool`;
- both `artist_detail_v2.html` templates render it on the module's own
  initialisation gate:

  ```jinja
  <div id="releases-sections" data-scan-active="{{ 1 if scan_active else 0 }}">
  ```

- the module returns early when it reads `data-scan-active="1"`.

The gate is deliberately **opt-in on the value** (`=== '1'`): an absent
attribute means "not scanning", so an older template or a cached copy of the
script degrades to the *bounded* path, never back to the storm.

The scan is about to rewrite this data anyway, so skipping cosmetic badges while
it runs costs nothing.

### 3. Pre-existing drift between the two copies repaired

`tests/test_artist_page_contract.py::test_shared_module_copies_do_not_drift` was
**already failing on `origin/develop`**: the live and rebuilt copies of
`artist-releases.js` differed in two places — the badges comment, and a
collapsed `if (…) { A } else { A }` in the click handler. Both copies are now
identical again (the `if/else` collapsed to the single call).

## Files changed

- `static/js/artist-releases.js` — bounded, scan-aware `fetchMissingTrackCounts()`;
  new `MISSING_PROBE_CONCURRENCY`, `scanIsActive()`; collapsed the redundant
  `if/else` so the two tree copies match again
- `test_site/static/js/pages/artist-releases.js` — same module body (the pair
  must stay identical)
- `routes/ui_routes.py` — `_build_artist_detail_payload` probes
  `is_popularity_scan_active()` and exports `scan_active`
- `templates/pages/artist_detail_v2.html`,
  `test_site/templates/Pages/artist_detail_v2.html` — render
  `data-scan-active` on `#releases-sections`
- `tests/test_artist_page_contract.py` — 7 new tests (probe cap present and
  small, cap actually used, scan gate present and value-scoped, both templates
  render the attribute, the route probes and exports it while staying
  synchronous)

## Verification

Worktree at `origin/develop` (`9a1b4958`), same edits replayed in both trees.

| Check | Result |
|-------|--------|
| `node --check` both copies | OK |
| Jinja parse both templates | OK |
| **Baseline (fix reverted, new tests kept)** | **8 failed** — naming the missing concurrency cap (×2), the missing scan gate (×2), the missing template flag (×2), the missing route export, and the pre-existing copy drift |
| Fixed | `tests/test_artist_page_contract.py` + `test_async_routes_do_not_block_event_loop.py` + `test_scan_async_signature_contract.py` → **37 passed** |

Pre-existing, unrelated (fails identically before and after):
`test_static_js_is_not_jinja.py::test_static_js_contains_no_jinja[static/js/artist_detail.js]`
— that file genuinely is a Jinja page template served as JavaScript, which is the
defect that file's own guard exists for.
