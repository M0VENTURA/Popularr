# Similar-artists blocks called two endpoints that do not exist (2026-10-14)

## What was reported (browser console)
```
GET /api/search/unified?q=Samurai%20Pizza%20Cats              404 (Not Found)  ×10
GET /api/artist/BABYMETAL%20%26%20Electric%20Callboy/image    404 (Not Found)  ×10
```

## Root cause
Neither URL is a route.

1. **`/api/search/unified` matches nothing.** The real search endpoint is
   `POST /api/search` (the one `unified_search.js` calls).
2. **`/api/artist/image` is a QUERY-PARAM route** (`?name=`), so the
   `/api/artist/<name>/image` form never matched it.

The cost was not just the failed requests:

* every similar-artist card fell back to the placeholder image (the `onerror`
  handler fired for all of them);
* the in-collection split **re-derived an answer the payload already carried** —
  `/api/artist/<artist>/similar` annotates EVERY entry with `in_collection`
  (`artist_metadata_service._annotate_similar_artist`, a punctuation/"The"-
  tolerant key, i.e. *more* accurate than the client's exact lowercase compare).
  Each failed request pushed its artist into "missing", so the whole grid read
  "Not in Collection".

Both defects were present in **both trees** — the live `templates/pages/*.html`
inline JS had them too, which is what the reported console shows.

## The change
In all four files (`test_site/static/js/pages/album.js`, `…/track.js`,
`templates/pages/album_detail.html`, `templates/pages/track_detail.html`):

1. **Image**: `/api/artist/image?name=<encoded>` — the route that exists.
2. **Collection split**, in order of authority:
   1. the server's **per-entry `in_collection` flag** (no request at all);
   2. the pre-split `in_collection`/`missing` arrays;
   3. a **`POST /api/search`** lookup, only for an entry carrying no flag.
   `track.js`'s `dedupe` now carries the flag through — dropping it was what
   forced the lookup to re-derive a known answer.

## Tests
`tests/test_similar_artists_endpoints.py` (31):
* the endpoints exist (and the two reported URLs are NOT rules — asserted
  against the app's own URL map, so this class of bug cannot come back);
* no live code (comments stripped — they deliberately quote the broken URLs)
  still builds either broken URL;
* the replacement URLs are built correctly, and the search call is a POST;
* the per-entry flag is read BEFORE any fallback, and the server really does
  annotate every entry (behavioural, via `_annotate_similar_artist`).

**Oracle**: reverting the four files (tests kept) → **24 failed / 7 passed** —
the 7 are the URL-map controls and the server-annotation tests, which do not
depend on the JS edits.

## Verification
`test_templates_tag_attribute_integrity` 120, `test_rebuilt_pages_have_no_dead_buttons`
6, `test_similar_artists_artist_level_only` 14, `test_unified_search_enter_submits`
12, `test_navbar_system_menu_and_search_shortcut` 25,
`test_test_site_mobile_and_pwa` 72, `test_track_detail_album_link_artist` 5 —
all green. `node --check` clean on both edited JS files.
`test_static_js_is_not_jinja` 1F/91P is **pre-existing** (identical at
baseline; `static/js/artist_detail.js`, untouched here).

## Also fixed (found by the sweep, pre-existing at `origin/develop`)
`services/scanning/payload_builder.py:363` called `logger.debug(...)` with **no
`logger` imported** — a `NameError` raised *inside* an `except` handler (the
album-type guess failure path), so it would abort the caller rather than log.
Added the house-pattern `structlog` logger. This was the only offender in
`test_no_undefined_names_in_production`, which is green again (7 passed).
