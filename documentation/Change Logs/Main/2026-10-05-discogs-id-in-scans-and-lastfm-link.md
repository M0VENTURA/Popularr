# Scans fill in the Discogs album id, and the Edit tab links Last.fm (2026-10-05)

**Report:**

> Discogs album ID doesn't seem to be populating correctly during scans, should
> it also have a last.fm field for each album that links to the last.fm release
> on the album page under the edit tab?

## Root cause (Discogs)

Nothing in any scan path ever wrote `tracks.discogs_album_id`. Grepping every
service turns up exactly **two** writers, both on the album page's manual flow:

- `services/metadata/album_service.py::apply_discogs_id_to_album` →
  `db.repositories.metadata.update_album_discogs_fields` (the *Apply* button),
- `services/metadata/album_service.py::update_album_ids` (the identifiers form).

The column exists (`tracks.discogs_album_id`, per-album value on every track),
the Edit-tab field exists — and a scan could only ever leave it blank. The scan
*does* fetch the Discogs **artist** id and Discogs **cover art**, and
`artist_release_cache` already holds each artist's Discogs releases with their
release ids; nothing connected them to the album.

## Changes (Discogs)

- `db/repositories/metadata.py`
  - `album_missing_discogs_id(artist, album)` — one indexed `SELECT 1 … LIMIT 1`
    asking whether any track of the album still has an empty id. This is the
    short-circuit that stops a re-scan re-searching Discogs for albums already
    filled in.
  - `fill_album_discogs_id(artist, album, discogs_id)` — writes the id to every
    track of the album, **guarded per row** by
    `COALESCE(discogs_album_id, '') = ''`, so an id the user set by hand (or one
    written by an earlier run) is never clobbered while a half-populated album
    is completed rather than skipped.

- `services/popularity/stages/album_stage.py`
  - `_resolve_discogs_album_id()` — two sources, cheapest first:
    1. the artist's **cached** Discogs release list (no API call, no rate-limit
       budget — the scan already fetched it);
    2. one Discogs search when the cache is stale or empty.

    Only an **exact title match** counts, after the same normalisation both
    sides use (`normalize_title_for_lookup(_sanitize_release_name(…))`). A
    wrong release id is worse than a blank one, because it is written to every
    track of the album.
  - `_record_discogs_album_id()` — the orchestrator: returns immediately when
    there is no token, returns when the album is already complete, otherwise
    resolves and fills. Any failure is logged at **debug** and swallowed, so a
    Discogs hiccup can never abort the enrichment that is already running.
  - Wired into `enrich_album_extras()` — the per-album step the scan already
    runs (post-singles enrichment, `scan_stage_runner.py`) — as the **last**
    action, because it is the only step there that can spend an API call. The
    token is read **once** and shared with `_run_full_enrichment`, so the
    existing "Discogs token unavailable" line is not doubled.

## Changes (Last.fm)

Last.fm has no album id: a release page is addressed by
`/music/<artist>/<album>`. So there is nothing to resolve, cache or store —
per the confirmed decision the link is **derived and needs no column**.

Both Edit tabs (`templates/pages/album_detail.html`,
`test_site/templates/Pages/album_detail.html`) gained a *Last.fm Release*
tile in the **Identifiers & Linking** block, right after the Discogs Release
ID: each URL segment is encoded on its own so the two slashes stay path
separators, and it opens in a new tab with `rel="noopener noreferrer"`. It
mirrors the artist-level "View on Last.fm" button the artist page already has.

## Tests

`tests/test_album_discogs_id_and_lastfm_link.py` — 26 tests:

- resolution: a cached title match costs **no** search; a stale cache falls
  back to exactly one search; no exact title match → nothing written; a failed
  search is not a match; an id-less cache row does not mask the search;
- persistence: no token → the database is never touched; an already-complete
  album is never re-resolved; a resolved id reaches `fill_album_discogs_id`
  with the right arguments; an unresolved album writes nothing; a database
  failure does not escape into the caller;
- the SQL itself is guarded (`COALESCE(discogs_album_id, '') = ''` on both the
  precheck and the backfill, blank arguments never reach the database);
- the scan is wired (`enrich_album_extras` calls the recorder with the token it
  already read);
- the Last.fm link: present in both templates, `target="_blank"` +
  `rel="noopener noreferrer"`, positioned after the Discogs field inside the
  Identifiers block, and **no** `album_lastfm*` input was introduced.

Oracle: reverting the four source files fails **24/26** (the two that survive
are the "no new column" pins — correct either way).
