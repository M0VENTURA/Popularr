# The album page's `#page-data` block was not JSON — Lookup MBID returned 400

**Date:** 2026-10-17
**Scope:** `templates/pages/album_detail.html` +
`test_site/templates/Pages/album_detail.html` (both trees) + a test.

## The report

> Could not load MusicBrainz metadata: HTTP 400: artist, album and release_mbid
> are required
>
> And lookup mbid seems broken, it's not auto filling the artist and album.
> It was a couple of updates ago.

## Root cause — a `<script>` nested inside a JSON `<script>`

Commit **`4bf0e503`** ("Album page: one action row, a 4-up stats grid, and a hero
that fits") added the shared scan sheet's controller to the album page and placed
the tag **inside** the page-data element:

```html
<script id="page-data" type="application/json">
<script src="{{ versioned_static('js/ui/scan-modal.js') }}"></script>   <!-- NOT JSON -->
{{ { "artistName": artist_name, "albumName": album_name } | tojson | safe }}
</script>
```

So the page shipped:

```js
const _pd = JSON.parse(document.getElementById('page-data').textContent);
```

against text beginning `<script src="/static/js/ui/scan-modal.js">`. `JSON.parse`
threw, `_pd` was never assigned, and `window._pageData` stayed **`undefined`**.
(HTML additionally ends a script element at the first `</script>`, so the element
was also truncated mid-content.)

Every reader of the block then read `''`:

```js
function pageArtist() { return global._pageData ? global._pageData.artistName : ''; }
function pageAlbum()  { return global._pageData ? global._pageData.albumName  : ''; }
```

`metadata-review.js` posts those values to `/api/album/metadata-proposal`, whose
guard is exactly the reported message:

```python
if not artist or not album or not release_mbid:
    return jsonify({"error": "artist, album and release_mbid are required"}), 400
```

⇒ **one nested tag** produced both symptoms: the 400 *and* the fields never
auto-filling. The other pages that load `scan-modal.js` (artist, dashboard) place
it as a normal script **after** the block and were never affected.

## The fix

Move the tag out of the element — the same place the artist/dashboard pages put
it:

```html
{% block scripts %}
<script src="{{ versioned_static('js/ui/scan-modal.js') }}"></script>
<script id="page-data" type="application/json">
{{ { "artistName": artist_name, "albumName": album_name } | tojson | safe }}
</script>
```

## Tests

`tests/test_album_page_data_block_is_valid_json.py` — parametrized over BOTH
templates. It **renders** the element's content through Jinja (with
`versioned_static` stubbed) and `json.loads` it — so it fails on the real bug
rather than pattern-matching the fix. A non-greedy `(.*?)</script>` capture is
deliberate: with the regression present the first `</script>` is the *nested*
one, so the capture stops exactly where the browser's textContent would.

* no `<script` nested in the element;
* the rendered block parses and carries `artistName` / `albumName`;
* **CONTROL** — the page still loads `js/ui/scan-modal.js` (the fix MOVES the tag;
  deleting it would pass the first two assertions);
* the two `metadata-review.js` modules still resolve `pageArtist()`/`pageAlbum()`
  from the block (so the two facts line up).

### Oracle

| | result |
|---|---|
| without the fix | **4 failed** / 4 passed |
| with the fix | **8 passed** |

The 4 failures are the two nesting and two JSON-parse assertions (one each per
tree); the failure text is `json.decoder.JSONDecodeError: Expecting value: line 2
column 1` — i.e. the parse the browser does. The 4 passing are the controls.

Regression sweep of the album-page / scan-sheet suites
(`test_album_lookup_missing_tracks_populate`, `test_mobile_layout_density`,
`test_album_page_missing_tracks_list`, `test_artist_layout_completion`,
`test_scan_preflight_gate`): **149 passed**.

## Files

* `templates/pages/album_detail.html`
* `test_site/templates/Pages/album_detail.html`
* `tests/test_album_page_data_block_is_valid_json.py` (new)
