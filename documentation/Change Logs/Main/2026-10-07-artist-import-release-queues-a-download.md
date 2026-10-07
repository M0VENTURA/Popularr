# Artist page: the Import button queues a download again (2026-10-07)

## Reported

> When tracks are added to the download queue from the artist, it's
> automatically adding them to the releases before the download has completed.

## Root cause — the button called the placeholder-row endpoint

The routed artist page (`pages/artist_detail_v2.html`) loads
`static/js/artist-releases.js` (rebuilt: `test_site/static/js/pages/artist-releases.js`).
Its `importMissingRelease` posted to **`/api/artist/import-release`**:

```js
postJson('/api/artist/import-release', { artist, release_id, title })
  .then(function (data) {
    if (data && (data.success || data.queued || data.download_id)) {
      toastSuccess('Import queued for “' + album + '”.');
```

That endpoint is documented in `artist_scan_service.import_release()` as
*"Import a missing release as **placeholder track records**"*:

```python
track_record = { "id": ..., "title": ..., "file_path": None, "stars": 0, ... }
save_to_db(track_record)      # INSERT INTO tracks
...
DELETE FROM missing_releases WHERE ... release_id = :release_id
```

So one click wrote a `tracks` row **per track with `file_path: NULL`** and
**deleted** the release's `missing_releases` row.

The artist page builds its OWNED releases with **no file-path guard**:

```sql
SELECT * FROM tracks
WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:name)
```

…so those rows made the release appear under **Releases** instantly, before a
single file existed — and `build_missing_and_upcoming_entries(owned_titles=…)`
then suppressed it from **Missing** (it is already gone from `missing_releases`
anyway). Nothing was ever queued, while the toast claimed it was.

**This is a regression of the 2026-08-19 fix**, which already documented the
exact defect and retired the endpoint from every button
(`2026-08-19-missing-releases-mb-only-and-import-via-soulseek.md`):

> The import button was a dead-end. Clicking Import on a missing album called
> `/api/artist/import-release`, which fetched the tracklist and created
> PLACEHOLDER database rows (no audio files) … **The old `/api/artist/import-release`
> placeholder-record flow is no longer wired to any button** (the route stays for
> API/backward compatibility).

That change edited `static/js/artist_detail.js` — the sibling file that is a
**Jinja template served as JavaScript** (discarded by the browser, see
`tests/test_artist_page_contract.py::test_release_module_is_not_jinja`) — plus
`templates/pages/missing_releases.html`. The artist page's release-section
module was added later by the artist-page rebuild and re-wired the button to the
old endpoint, so the placeholder behaviour survived **on exactly the page the
report came from**.

## The fix

`importMissingRelease` (both trees) now does what the missing-releases page
already does:

```js
global.openGlobalMbSearch(artist, album, function (selectedRelease) {
  if (!selectedRelease) return;
  if (typeof global.downloadMbRelease === 'function') {
    global.downloadMbRelease(selectedRelease.id, selectedRelease.title, selectedRelease.artist, 'slskd');
  } else if (typeof global.downloadReleaseViaSoulseek === 'function') {
    global.downloadReleaseViaSoulseek(selectedRelease.id, selectedRelease.title, selectedRelease.artist);
  } else {
    toastError('Soulseek download is not available on this page.');
  }
});
```

Available on that page: `openGlobalMbSearch` comes from `static/js/main.js:852`
(both trees' `base.html` loads `main.js`, and both `base.html` files include
`components/_musicbrainz_search_modal.html` — live:179, test_site:210); the
`downloadMbRelease` fallback to `downloadReleaseViaSoulseek` matters because
neither helper is loaded on the artist page itself.

The button is no longer disabled (it is only ever covered by the modal), which
removes the stuck-spinner risk when a picker session never calls back. The
endpoint route and `import_release()` are **kept** — the fix unwires the button,
it does not delete the API.

The two module copies stay **byte-identical** (the leading `/* … */` header is
what `test_shared_module_copies_do_not_drift` strips before comparing).

## Files

* `static/js/artist-releases.js` — rewire
* `test_site/static/js/pages/artist-releases.js` — rewire (byte-identical)
* `tests/js/import-missing-release-probe.js` — behavioural probe (new)
* `tests/test_artist_import_release_queues_a_download.py` — 7 tests (new)

## Tests

`tests/js/import-missing-release-probe.js` (10 checks × 2 files) extracts the
**SHIPPED** `importMissingRelease` by brace/sibling-anchored slicing and drives
it against stubs: never POSTs the endpoint, the endpoint is absent from the code
(**comments stripped** — the new docstring deliberately names it), opens the
picker prepopulated with artist+album, queues via `downloadMbRelease`, falls back
to `downloadReleaseViaSoulseek`, queues nothing on a dismissed picker, reports
(no throw) a missing picker or download helper, and does not disable the button.

`tests/test_artist_import_release_queues_a_download.py` (7): the probe through a
`node`-gated runner that **asserts output exists** (an empty run must not read as
a pass), the wiring markers, the comment-stripped negative check, and two
**controls** that the route and `import_release()` still exist — documenting that
the fix unwires rather than deletes.

**Oracle:** reverting both JS files → probe `exit=1` with the offending POST
printed (`posts=[{"url":"/api/artist/import-release", …}]`) and the Python test
**5 failed / 2 passed** (the two endpoint-stays controls green); restored →
**7 passed**.

**Sweep** (14 artist-page / missing-release / queue suites, clean `3ff4b85e`
worktree vs this change): **BASE `24 failed, 269 passed` = NEW `24 failed,
269 passed`, `Compare-Object` empty** → identical failing sets, all pre-existing.
A narrower 4-suite check separately confirmed the 5 `test_import_release.py`
failures are pre-existing at `3ff4b85e`.

> ⚠️ HARNESS NOTE (third occurrence): the first baseline attempt reported
> `base=0` **again** because the shared file list was built from the CHANGED
> tree and included the brand-new test file, so pytest aborted with
> `ERROR: file or directory not found` and ran nothing. The summary guard I added
> to catch that matched `Select-String 'error'` against that very ERROR line and
> passed on a false positive. Both flaws fixed: the baseline list is built from
> files that exist in **both** trees, and the guard now requires
> `passed|failed`.

## Recovery for rows already created (manual, optional)

Albums that were "imported" this way still hold phantom rows: `tracks` with no
file. Review before deleting:

```sql
SELECT id, artist, album, title, file_path FROM tracks WHERE file_path IS NULL;
```

If those are exactly the affected releases, remove them and refresh the missing
list (it is a scan-refreshed snapshot, so the release returns to **Missing**
after a scan for that artist):

```sql
DELETE FROM tracks WHERE file_path IS NULL;
```

Nothing automatic was added here — deleting library rows is destructive, and a
`NULL` `file_path` is not by itself proof of an `import_release` placeholder.
