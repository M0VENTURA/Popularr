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

1. **`tag_names.py`'s note that "Picard/beets and some players call the same
   entity the RECORDING id".** That is about FILE-TAG spelling — Beets and
   Picard write the same `MUSICBRAINZ_TRACKID` frame as everyone else, so the
   standardiser must still know the spelling to clear stray frames on rewrite.
   Nothing to do with the integration.
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

* LSP diagnostics clean on every touched file.
* **The suite was not run** — no `python` on PATH and no Docker here. Before
  merging:
  `pytest tests/test_beets_integration_is_gone.py tests/test_tag_name_standardisation.py tests/test_album_review_save_persists.py tests/test_metadata_compare_is_retired.py`

  The two middle ones exercise the maps I edited: `tag_name_standardisation`
  asserts specific spellings still resolve, and `album_review_save_persists`
  asserts every staged field has a tag mapping (it checks that required keys
  exist, not that extras are absent, so removing an unused key is safe).

## Note on `old_system/`

Everything still mentioning Beets beyond the above is under `old_system/` —
the archived pre-migration app, including `deprecated/beets_integration.py`
and `deprecated/beets_auto_import.py`. That tree is excluded from the live
build, so it is history rather than code. If it is ever removed wholesale,
these notes go with it.
