# `/metadata-compare` retired into `/correcting`

Date: 2026-11-28
Scope: `routes/ui_routes.py`, `routes/preview_routes.py`,
`tests/test_async_routes_do_not_block_event_loop.py`, and three deleted assets.

Rebuild item from `documentation/web-ui-audit.md` §6 (candidate 5).

---

## The audit was right for the wrong reason

`documentation/web-ui-audit.md` listed this page as "unbounded full-table
`GROUP BY`, mislabelled Beets column, `metadata_locked` written without a
confirm". Investigating it turned up something more basic: **the page could not
work at all.** Three independent faults.

### 1. It queried a column that does not exist

```python
SELECT album, …, year AS navidrome_year, beets_year, …
```

`beets_year` is absent from:

* `db/schema.py` → `COLUMN_REGISTRY["tracks"]` (the additive-column registry a
  live install is reconciled against),
* `db/models.py` → `Track`,
* every migration in `migrations/`.

`grep beets_year` across the current tree returns **only** `old_system/`
(`deprecated/beets_integration.py`, `deprecated/beets_auto_import.py`) and
`old_system/app.py`. The Beets importer that populated it was retired with the
old system, and the column went with it.

So on any current database the query raised `UndefinedColumn`, the handler's
`except` caught it, flashed "Error loading metadata comparison" and redirected
to the dashboard. The page could never render.

### 2. The "Beets" column was not Beets

```python
nav_genres_raw  = row.get("navidrome_genres") or ""
beets_genres_raw = row.get("musicbrainz_genres") or ""
...
has_genre_mismatch = sorted(nav_genres) != sorted(beets_genres)
```

The column labelled **"Beets Data"** in the template was
`musicbrainz_genres` — a MusicBrainz value, not a Beets one. And even
`navidrome_genres` is not Navidrome's: it is Popularr's own copy.
`navidrome_genres` and `musicbrainz_genres` are both JSONB and both written by
the enrichment pipeline, so the "mismatch" was between two of this app's own
provenance buckets, presented as an external disagreement.

### 3. Both write actions were dead

| Action | Wrote | Exists? |
|---|---|---|
| `apply-musicbrainz` | `year`, **`beets_year`**, `musicbrainz_genres`, `musicbrainz_album_mbid`, **`mb_override`** | two do not |
| `accept-navidrome` | **`metadata_locked = TRUE`** | does not |

`mb_override` and `metadata_locked` appear in no registry, model or migration.
Neither SQL statement can succeed, so "Apply" and "Keep This Data" both
returned a 500 — and `accept-navidrome`'s comment said it was silently setting a
lock, which it never did.

---

## Why nothing is lost

`/correcting` already does this work, correctly:

* `services/metadata/conflict_service.py` declares **`genres`** and **`year`**
  as `PROTECTED_FIELDS`, and records any provider disagreement in
  `metadata_conflicts` — which is exactly the first half of `/correcting`,
  with working Accept/Keep buttons.
* `services/metadata/correction_service.py` lists both `genres` and `year` in
  `ALBUM_CONSISTENCY_FIELDS`, so cross-track year/genre drift is reported there
  too.
* The MusicBrainz side is the per-album **Lookup MusicBrainz** action plus the
  metadata-proposal review, both of which write through the real allow-list.

So the page's two fields are covered twice over, by the page the nav actually
links to.

---

## The change

* `metadata_compare` is now a **redirect to `/correcting`**, not a deletion —
  an existing bookmark lands somewhere useful instead of a 404.
* The three JSON endpoints are deleted:
  `metadata_compare_search_mb`, `metadata_compare_apply_mb`,
  `metadata_compare_accept_navidrome` (~205 lines). Nothing else called them;
  their only client was this page's own controller.
* `MusicBrainzService` is no longer imported by `ui_routes.py` — those
  endpoints were its only user there.
* Deleted: `templates/Pages/metadata_compare.html`,
  `static/js/pages/metadata-compare.js`, `static/css/metadata-compare.css`.
* `PREVIEWABLE` / `NOT_PREVIEWABLE` updated (the page was in the latter).

### New guard

`tests/test_metadata_compare_is_retired.py` pins:

* the route survives **and redirects** (never renders);
* all three old endpoint functions and all three `/api/metadata-compare/*`
  routes are gone;
* **the diagnosis stays true** — a CONTROL asserts `beets_year`,
  `metadata_locked` and `mb_override` are still absent from `db/schema.py`. If
  one is ever legitimately added, the test fails and the justification in the
  route's docstring needs revisiting rather than being quietly wrong;
* the three assets stay deleted, and no template links to the page.

The three entries were also removed from
`test_async_routes_do_not_block_event_loop.py`'s known-offenders list.

---

## Verification

* LSP diagnostics clean on every touched Python file.
* **The suite was not run** — no `python` on PATH and no Docker here. Before
  merging:
  `pytest tests/test_metadata_compare_is_retired.py tests/test_async_routes_do_not_block_event_loop.py tests/test_metadata_compare_version_markers_and_genres.py`

  The last one is **unaffected**: despite its name it tests
  `metadata_proposal_service` and `musicbrainz_service` — the surviving
  MusicBrainz path — not the retired page. Its name is now misleading and could
  be renamed to `test_genre_comparison_and_version_markers.py`.

## Still open

* `/search` is still a stub that opens the flyout.
* `artist_list` server-renders ~1000 rows with no search or pagination.
* The dashboard polls five endpoints every 5 seconds.
* Splitting `album.js` (4 011 lines), `config.js` (3 733), `playlist.js` (2 318)
  and `download-queue.js` (2 089).
* Phase 1, the real bundle.
* `routes/beets_routes.py` still ships a `/api/beets/*` blueprint that is not
  registered in `helpers/app_bootstrap.py` — unrelated to this change, but it
  is the same retired integration.
