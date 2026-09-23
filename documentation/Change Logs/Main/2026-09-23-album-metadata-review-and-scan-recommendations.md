# Album-page MusicBrainz review + scan-time recommendations

**Date:** 2026-09-23
**Area:** `api` / `ui` / `config` / `metadata`

## What was asked

1. On the album page, **Edit Album → Lookup MBID**: after selecting a release,
   show the *full* metadata a metadata import would check and fill every track in
   the UI with it — but **do not save until "Save Metadata" is pressed**. Show
   each change as an **orange bar below the field being updated**.
2. *"It would be nice if this is also added during a metadata scan and then when
   an album or artist page is browsed to it will show the recommended adjustments
   that could be saved or discarded (if metadata updating during scan isn't
   selected on the config.yaml)."*

## What changed

### 1. A preview engine — one answer, no writes

**NEW `services/metadata/metadata_proposal_service.py`**

`propose_album_metadata(artist, album, release_mbid)` returns
`{"album_changes": [...], "track_changes": [...], "missing": [...], "counts": {...}}`
where every change is `{field, label, current, proposed}`.

It **writes nothing** — the only database access is the read that supplies
"current" values. A test asserts on the SQL that actually reaches the session, so
a future edit that starts writing is caught.

Two design points:

* Track matching is delegated to the existing
  `compare_musicbrainz_release()`. A second, independent matcher would let the
  preview and the "Compare with MusicBrainz" diff disagree about the same album.
* Per-track enrichment (writer, cover verdict, genres) comes from
  `fetch_musicbrainz_release_metadata()` — the release-payload shape authority
  the album save already fans out with.

**NEW route** `POST /api/album/musicbrainz/propose` (`routes/album_routes.py`).

### 2. The album page shows the full import

**NEW `test_site/static/js/services/metadata-review.js`** and its live mirror
`static/js/metadata-review.js`.

* After a release is chosen, `applyAlbumMatch()` (rebuilt tree) and
  `applyAlbumMbid()` (live tree) call the proposal endpoint.
* Album-level values are written into the form's own inputs; an **orange bar**
  appears under each changed field showing `current → proposed` with **Keep** and
  **Discard**.
* Per-track changes render as orange rows under the affected track with
  Included/Ignore toggles.
* A banner summarises what is waiting and states plainly that nothing is saved
  until **Save Metadata**.

**Nothing is written on lookup.** Album values live in the form; per-track
changes are serialised into a new hidden input `#staged_track_updates` and posted
with it, so the whole review is **one atomic save**. A wrong release is undone by
reloading the page.

Rows use a **distinct class (`.mb-staged-row`)** rather than the existing
`.mb-update-row`. The compare flow's `.mb-update-row` Apply button POSTs
immediately (`/api/v1/tracks/<id>/apply-mb-field`); sharing the class would make
`clearComparison()`/`updateAllTracksFromMB()` pick up rows they must not touch,
and would make "Apply" mean two different things on one page. A test enforces
that the review module never references the immediate-write endpoints.

`routes/ui_routes.py::album_detail` (POST) now honours the staged payload against
a whitelist, applied **after** the album-level values so a per-track value the
user reviewed wins. A reviewed cover verdict still gets the library's
`"Title (Original Artist Cover)"` convention.

### 3. Scan-time recommendations

**NEW `services/metadata/pending_update_service.py`**

The store is **not new**: `tracks.pending_mb_updates` has existed since the
initial schema, `db/schema.py` documents it, and
`routes/artist_routes.py::api_missing_overview` already *reads* it ("persistent
MusicBrainz update banners"). It had no writer. This adds one.

**NEW config toggle** `metadata_update.apply_during_scan` (default `true`).

* `true` — unchanged behaviour.
* `false` — "Recommend only": `scan_stage_runner` stores the proposal on the
  album's track rows instead of applying it, **before** the file-tag sync so the
  same pass cannot write the metadata it just deferred.
* The gate **fails open**: an unreadable config must not silently stop a scan
  from applying metadata it has always applied.

**NEW routes**

* `GET  /api/album/metadata-recommendations` — one album's stashed proposals.
* `POST /api/album/metadata-recommendations/discard` — drop them.
* `GET  /api/artist/metadata-recommendations` — per-album counts.

The album page renders stashed recommendations **through the same review module**
as the lookup (identical orange bars, same Save/Discard), plus a **Discard all**
action. Saving clears the stored copy, since it has just been applied.

**NEW `test_site/static/js/pages/metadata-recommendations.js`** (+ live mirror)
adds a read-only per-album summary to the artist page. It is deliberately
read-only: saving belongs on the album page, against that album's actual
tracklist.

### 4. Config page (source of truth)

`metadata_update_apply_during_scan` was added to **both** trees
(`templates/pages/config.html` + `test_site/templates/Pages/config.html`) and read
by **both** `config.js` files.

## Tests

| Suite | Result |
|---|---|
| `tests/test_metadata_proposal_preview.py` | 19 passed |
| `tests/test_pending_metadata_recommendations.py` | 26 passed |
| `tests/test_album_metadata_review_wiring.py` | 24 passed |

**Oracle:** with the integration edits reverted (feature modules present), the
wiring + config-gate guards report **19 failed / 9 passed** — including every
endpoint-registration, both Lookup-flow hooks, both trees' config toggle and all
four config-page contract checks. Restored: **69 passed**.

**Regression sweep** (11 album/MB/tag/category suites, 291 tests): failing set
identical on both trees — **3 pre-existing failures, 0 regressions**:

* `test_album_musicbrainz_matching.py::test_best_release_confidence_scales_down_on_count_mismatch`
* `test_album_type_persistence.py::TestEnsureAlbumTypeReturnsVerdict::test_detects_when_missing`
* `test_release_categories.py::TestAlbumRow::test_no_type_falls_back_to_title`

Both `py_compile`/`import app` (392 routes) and `node --check` pass on every
changed file.

## Notes

* Marked `.mb-staged-row` rows are skipped by the mobile card layout for the same
  documented reason the existing `.mb-update-row` rows are — their cells are
  non-columnar.
* Both trees were updated for every UI change. `helpers/test_site_mode` serves the
  rebuilt tree first, so a single-tree edit is invisible to whichever tree the
  user is on.
