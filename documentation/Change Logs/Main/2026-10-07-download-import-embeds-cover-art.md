# Download imports embed the album's cover art (2026-10-07)

## Reported

> I've also noticed that the album art isn't always downloading with the
> metadata for the downloaded tracks.

## Root cause

Nothing in the import path embedded art. Completion wrote the stored
metadata to the file and moved it — cover art only arrived if a **later
scan** happened to run its art pass (config-dependent, album-level), which is
exactly "isn't always": a freshly imported track could sit art-less until
some future scan reached its album.

## Fix

`_apply_stored_metadata` (the choke point every import — Soulseek downloads
and the new library-reuse copies — passes through) embeds the album's art
**after** the tag write (the writer may rebuild frames; art embedded first
would be dropped), via the existing `embed_album_art` helper (MP3 APIC +
FLAC pictures).

`_fetch_import_art` sources, in order:

1. the queue row's own `cover_art_url` (a manual MusicBrainz match stores
   one) — fetched directly, MIME sniffed from magic bytes;
2. `get_or_fetch_album_art(album_artist/artist, album)` — the cache-first
   provider chain (stored → Navidrome → MusicBrainz/CAA → Discogs → AudioDB);
3. Cover Art Archive by the release MBID.

Every source is wrapped: art is enrichment, never a reason to fail an import
(failures log at WARNING/debug, the import continues).

**Gated on a real file** (`os.path.isfile(file_path)`): a synthetic path must
not trigger provider/URL lookups — in the shared in-memory test DB a single
missing-table `OperationalError` makes `db_session` **dispose the engine**,
wiping every table, and the next test's teardown died with
`no such table: tracks` (six teardown errors in
`test_download_import_matches_mb_release.py` were exactly that bomb).

## Test-schema hardening

`tests/conftest.py::_recreate_test_schema` now also recreates `album_art`
and `missing_releases` (from `TABLES_TO_ENSURE`) before every test, for the
same reason it recreates `tracks`: the cover-art lookup reads them, and a
miss was fatal twice over — the error itself AND the engine dispose behind
it.

## Tests

`tests/test_import_embeds_cover_art.py` (12): MIME sniffing; all three
sources in order (row URL, album cache with sniff fallback, CAA by MBID) and
the no-source case; the embed happens with the metadata, **after** the tag
write; a synthetic path never triggers the lookup; missing art and a failing
embed never fail the import (controls).

## Oracle

Stashed `download_completion_service.py` → **8 failed / 5 fixture errors**
(all dependency) → popped → markers present → stash list back to 3.

## Verification

- Affected set (31 files): only the known flake and the order-dependent
  `test_refresh_returns_none_when_no_longer_downloading` (9/9 isolated;
  its earlier failure was the same engine-wipe bomb, now fixed) show as
  "new".
- Full suite → baseline reconciliation (`_reuse_full.txt`, shared with the
  library-reuse change — disjoint file sets, one run validates the combined
  tree).
