# The Beets integration is actually deleted this time

Date: 2026-11-28
Scope: `routes/beets_routes.py`, `routes/ui_routes.py`,
`services/metadata/{tag_names,tag_file_service}.py`,
`test_site/templates/Pages/track_detail.html`, `documentation/UPGRADE_PLAN.md`.

---

## The file survived its own deletion notice

`documentation/Change Logs/Main/2026-08-14-beets-removal-and-api-hardening.md`
says, under "Beets integration removed":

> - `routes/beets_routes.py` — the `/api/beets/*` blueprint

It was not removed. The file was still tracked and on disk: ~240 lines of
`/api/beets/{status,import,configure,auto-import,sync-metadata,update-album,album-folders}`
endpoints, defining `beets_bp`.

What the same changelog *did* remove is the reason nobody noticed:

* blueprint registration was deleted from `helpers/app_bootstrap.py`, so the
  endpoints were never served;
* nothing imported the module — `grep beets_routes` across the live tree found
  only itself.

So the file was unreachable, unimported, and unserved — but still counted as
code, still appeared in searches, and was still listed in `UPGRADE_PLAN.md` as
"✅ Migrated".

**Deleted.** No behaviour changes: none of those routes could ever be hit.

## The retired columns

The same changelog said the `beets_*` DB columns were kept "because they are
metadata fields, not part of the beets API integration". That reasoning did not
survive: `beets_mbid`, `beets_year` and `beets_album_artist` are in **no**
schema, model or migration, and the importer that wrote them went to
`old_system/deprecated/` long ago. Every read was a permanent `None`.

Cleaned up:

| Where | What |
|---|---|
| `ui_routes.py` track-page fallback chains | `beets_mbid` removed as the third link in both `musicbrainz_trackid` / `mbid` chains, and the synthesised-value block below them deleted — it copied `mbid` into a column that is never persisted |
| `ui_routes.py` MBID save | `beets_mbid` dropped from the multi-column write list (it was already skipped by the `existing_columns` guard on every save) |
| `tag_file_service._COLUMN_TO_TAG_FIELD` | the `"beets_mbid": "mbid"` entry — nothing can post that column |
| `tag_names.CANONICAL_TAG_NAMES` | the `"beets_mbid": "MUSICBRAINZ_TRACKID"` internal alias |
| `track_detail.html` | the Recording-MBID input's and the MB-link's fallback chains; and the three always-null `#page-data` keys (`beetsMbid`, `beetsAlbumArtist`, `beetsYear`) |

The `#page-data` keys had **no reader** in any script under `static/js`.

## Two things deliberately kept

Both are still correct, and deleting them would be wrong:

1. **`tag_names.py`'s `MUSICBRAINZ_RECORDINGID` synonym**, with its note that
   "Picard/beets and some players call the same entity the RECORDING id". That
   is about FILE-TAG spelling — the same recording MBID can sit in a file under
   two descriptions, and the declared synonym is what clears the stale one on
   rewrite. Nothing to do with the integration.

   ⚠️ CORRECTION: an earlier draft of this entry said the file "still mentions
   `beets_mbid`". It does not — `beets_mbid` was the **internal field alias**
   in `CANONICAL_TAG_NAMES`, which this change removed. The surviving reference
   is the `MUSICBRAINZ_RECORDINGID` synonym, which is what actually needs
   protecting, and the guard's control now asserts on that.
2. **The config page's and `config_helpers.py`'s read-only advice** — "Run an
   external tagger (Beets, MusicBrainz Picard)? Set the master toggle off…".
   That is describing the read-only mode, which is a real feature and names
   Beets only as an example of an external tagger.

`tests/test_beets_integration_is_gone.py` has a CONTROL for each, so a future
cleanup sweep cannot mistake them for leftovers.

## Also corrected

`documentation/UPGRADE_PLAN.md` listed `routes/beets_routes.py` as
"✅ Migrated"; struck through and marked deleted.

## The guard

`tests/test_beets_integration_is_gone.py` pins:

* the module is gone;
* no live module defines `beets_bp` or imports `beets_routes`;
* no `.js`/`.html` calls `/api/beets`;
* the track page's `#page-data` carries none of the three retired columns;
* **CONTROLS** that the tag-synonym note and the read-only advice still exist.

---

## Verification

**The suite WAS run** (Python 3.14 via the `py` launcher).

| | Failed | Passed | Skipped | Errors |
|---|---|---|---|---|
| Baseline (all changes stashed) | 415 | 10,706 | 317 | 29 |
| With this change | **411** | **10,722** | 317 | 29 |

Four fewer failures, sixteen more passes, errors identical — no regressions.

The 29 errors are **pre-existing and unrelated**: the `test_navidrome_*` and
`test_folder_match_*` suites raise `no such table: tracks` because they rely on a
schema the fixture does not create. Verified identical on a stashed tree.

`tests/test_migrations_idempotent.py` cannot be collected here
(`ModuleNotFoundError: alembic`) and is excluded from both runs.

## Three bugs the run caught in my own work

1. **The Beets guard failed against its own source.** A plain-text scan for
   `beets_bp` / `beets_routes` matched the guard file itself. Fixed by excluding
   the file by resolved path — not by loosening the needle, which would have
   stopped the check working.
2. **A control asserted the wrong thing.** It claimed `tag_names.py` still
   contains `beets_mbid`; it does not — that was the *internal alias* this
   change removed. The surviving file-tag synonym that actually needs protecting
   is `MUSICBRAINZ_RECORDINGID`, and the control now asserts on that. The
   paragraph above was corrected to match.
3. **`/bookmarks` blocked the event loop.** The new route called `db_session()`
   directly from an async handler; `test_async_routes_do_not_block_event_loop.py`
   caught it. Moved into a sync `_load_bookmarks_for_page()` invoked through
   `asyncio.to_thread`.

## Bookmark tests added

The `/bookmarks` fix had shipped with **no test at all** — nothing in the suite
touched the page, which is how a route that always rendered the empty state
survived. `tests/test_bookmarks_page_renders_rows.py` now drives the real route:
a stored favourite appears, `?filter=` actually filters, an unknown filter does
not blank the page, the empty state still works (control), a track favourite is
enriched from `tracks`, and the normaliser handles both column sets.

## Note on `old_system/`

Everything still mentioning Beets beyond the above is under `old_system/` —
the archived pre-migration app, including `deprecated/beets_integration.py`
and `deprecated/beets_auto_import.py`. That tree is excluded from the live
build, so it is history rather than code. If it is ever removed wholesale,
these notes go with it.
