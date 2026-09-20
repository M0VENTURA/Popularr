# Artist page track loading was reading nothing, and asking for a route that did not exist

**Date:** 2026-09-20
**Area:** `ui`, `api`, `db`, `metadata`
**Status:** fixed

## Report

> "The track loading on the artist page is failing, can you confirm it's currently
> looking for the tracks from the local database for albums in my collection?
>
> How much would it slow down the missing releases import during the artist scan,
> if the tracks table for those missing releases was imported at the same time as
> they are added? Or could it be set that they are populated in the background
> when no scanning is happening and the task is halted once a scan starts?"

Answering the first question meant tracing both halves of the expander, and both
were broken.

## Defect 1 — owned albums *were* read from the DB, but with an exact match

The expander already pointed owned albums at `/api/album/tracklist`, which goes
`routes/album_routes.py` → `services/metadata/album_service.get_album_tracklist_from_db`
→ `db/repositories/metadata.fetch_album_tracklist`. That is the local database,
and it is the right source — no MusicBrainz involved.

But the SQL compared **exactly**:

```sql
WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
  AND album = :album
```

while the artist page had selected that very album with

```sql
WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:name)
```

(`routes/ui_routes.py::_build_artist_detail_payload`). The album row is therefore
only guaranteed to be the artist's *up to case*, and the page then passed the
URL's own spelling of the artist back in as an exact-match term. Whenever the two
differed, the album was listed as owned and simultaneously answered
"Tracks not found" (HTTP 404), which the module renders as "Error loading
tracks." — exactly the reported symptom.

**Fix:** the lookup now uses the same case-insensitive comparison as the album
page and as the page's own album grouping:

```sql
WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
  AND LOWER(COALESCE(album, '')) = LOWER(:album)
```

## Defect 2 — missing releases requested a route that has never existed

```js
var url = isMissing
  ? '/api/musicbrainz/release/tracks?mbid=' + … + '&release_id=' + encodeURIComponent(summary.getAttribute('data-release-id') || '')
  : '/api/album/tracklist?…';
```

There is no `GET /api/musicbrainz/release/tracks`. The only rule on that path is
`POST /api/album/musicbrainz/release/tracks` — **a different blueprint, a
different HTTP method, and its argument arrives as `release_mbid` in a JSON
body**. Every missing release on every artist page 404'd.

The same line also had a second, independent defect: it read `data-release-id`
off `.release-summary`, but the attribute was only ever emitted on the *Import
button* inside `.release-actions`. `getAttribute` therefore returned `null`, and
the request was built with an empty id even had the path been right.

**Fix:**

* `.release-summary` now emits `data-release-id` (both `_release_section.html`
  copies), and the JS reads it there.
* New `GET /api/artist/release/tracklist?release_id=…&artist=…`
  (`routes/artist_routes.py`), which serves the **cached** tracklist first and
  only reaches MusicBrainz when the cache is empty.
* The JS calls the new route for `data-status="missing"` rows and keeps
  `/api/album/tracklist` for owned ones.

## Defect 3 — the cache could never fill (found while fixing 2)

`missing_releases.release_id` stores a MusicBrainz **release-GROUP** id
(`_build_missing_release_items` writes `rg["id"]`). The previous cache-filler
called `client.get_release(release_id, inc="recordings")` on it directly, so
**every** fetch raised a 404, was caught by a bare `except`, logged at DEBUG, and
left the row's `tracklist` NULL forever. The filler only selects rows whose
tracklist is NULL/empty, so it re-attempted the same releases on every run and
never made progress.

Two more problems in the same path:

* it constructed a fresh `MusicBrainzHttpClient(...)` instead of
  `get_shared_mb_client()`, giving these requests their own HTTP session; and
* `_persist_missing_releases` is a DELETE + INSERT of the same rows every sweep,
  and it did not carry the `tracklist` column across — so whatever *had* been
  gathered was thrown away on the next scan.

**Fix:** resolution goes through `_resolve_mb_release` (which tries the id
directly, then browses the group's releases); `populate_missing_release_tracklists`
now delegates to the shared helper instead of duplicating the broken call; and
`_persist_missing_releases` reads the existing tracklists first and re-inserts
them.

## The performance question, answered

**Filling the tracklists inline in the artist scan would be expensive.** Because
`release_id` is a group id, one tracklist costs up to **three** MusicBrainz
requests:

1. `get_release(<group-id>)` → 404, wasted
2. `get("release", release-group=…)` → one concrete release id
3. `get_release(<release-id>, inc=recordings)`

MusicBrainz is globally throttled to ~1 req/s by
`api_clients/musicbrainz_http.py::_strict_throttle`. A 20-release backfill is
therefore **~60 s of the shared budget per artist**, added on top of the scan's
own MusicBrainz calls — which is the same "the scan looks stuck" failure the
page-load probe storm produced.

**So it is background work instead**, which is what the second half of the
question asked for. `backfill_missing_release_tracklists()` runs inside the
missing-releases sweep, which:

* is already a background daemon thread,
* already refuses to start while a popularity scan is running, and
* already pauses mid-sweep on `_wait_while(_popularity_scan_active, …)`.

On top of that the backfill **stops outright** (rather than pausing) at the first
sign of a scan or a tripped circuit breaker, and is bounded per artist by
`features.missing_release_tracklist_limit` (default **10**, `0` disables). The
sweep retries the artist on its next pass, so nothing is lost.

## Verification

| Check | Result |
|---|---|
| `tests/test_artist_tracklist_loading.py` (new, 33 tests) on the **unpatched** tree | **29 failed / 4 passed** — each failure names a real defect ("calls ['/api/musicbrainz/release/tracks'], which no route serves", "does not emit data-release-id on .release-summary", "is not registered, so every missing release 404s", the `LOWER()` case-insensitivity message) |
| Same suite on the patched tree | **33 passed** |
| Related suites (missing-releases, release-category, album-missing, artist-page contract, navidrome delta, scan continuation, release-title naming, static-JS-is-not-Jinja, album MB tag files) | **13 failed / 162 passed on BOTH trees** — byte-identical failure lists, **0 new** |

The 13 are pre-existing environment failures (missing DB fixtures and
`static/js/artist_detail.js` genuinely being a Jinja page served as JS).

### Test-harness note

The cache/backfill tests use a **recording fake session**, not the DB. The
`missing_releases` table is created by no conftest fixture, and
`db.engine._is_transient_db_error` returns True for **every** `OperationalError`
— including "no such table" — so creating it in-test makes the engine dispose and
erase the in-memory SQLite database. That mattered here because the functions
under test **swallow a missing table and return an empty result**, so a naive
test passes for entirely the wrong reason. Several tests in the new file
therefore assert their own setup (e.g. that the backfill's `LIMIT :limit`
reached SQL) rather than only the outcome.

## Files

* `db/repositories/metadata.py` — case-insensitive `fetch_album_tracklist`
* `services/metadata/artist_scan_service.py` — `fetch_missing_release_tracklist`,
  `backfill_missing_release_tracklists`, `get_tracklist_backfill_limit`,
  `_tracklist_titles_from_release`, `_cache_missing_release_tracklist`;
  `_persist_missing_releases` preserves cached tracklists; the sweep calls the
  backfill
* `services/popularity/release_cache_service.py` — `populate_missing_release_tracklists`
  delegates to the resolving helper
* `routes/artist_routes.py` — `GET /api/artist/release/tracklist`
* `templates/components/_release_section.html` + `test_site/…` — `data-release-id`
  on `.release-summary`
* `static/js/artist-releases.js` + `test_site/static/js/pages/artist-releases.js`
  — correct endpoint for missing rows
* `templates/pages/config.html` + `test_site/templates/Pages/config.html` —
  "Missing-Release Tracklists per Artist"
* `tests/test_artist_tracklist_loading.py` — new
