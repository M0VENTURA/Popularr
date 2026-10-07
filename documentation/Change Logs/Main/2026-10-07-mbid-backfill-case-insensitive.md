# Enrichment can now save the MBIDs Subsonic never sends (2026-10-07)

## Reported

> 1. **The Subsonic API Blindspot:** Navidrome reads every raw MusicBrainz
>    tag, but the Subsonic API only transmits the Release ID
>    (`musicBrainzId`) — it omits the Release Group ID and Artist ID, so
>    `navidrome_import.py` saves those fields blank.
> 2. **The Case-Sensitive Save Bug:** the background Enrichment stage finds
>    the missing IDs on MusicBrainz, but its `UPDATE` fails on a casing
>    mismatch (`WHERE artist = 'Afi'` against a row imported as `AFI`) →
>    `rows_updated=0` → the fields stay blank in the UI.

## Analysis

**Half 1 is real but already handled forward-looking.** The Subsonic response
(`Child`/`AlbumID3`) has no release-group id and no artist MBID — only
`musicBrainzId` (release, on the album object). `payload_builder` puts every
MBID column in `MBID_IDENTITY_FIELDS ⊆ PRESERVE_WHEN_EMPTY_FIELDS`, so an
import **omits** an empty MBID instead of wiping a stored one. A *new* track
therefore starts blank, and enrichment is the only thing that can fill it —
which makes half 2 the operative bug.

**Half 2** — every artist/album-scoped statement in the backfill chain used
exact-cased `=` matching:

| Statement | Role |
|---|---|
| `album_stage` release-group persist | **the reported `rows_updated=0`** (`[ENRICH] release-group MBID persisted`) |
| `album_stage` release persist | resolved release id → `musicbrainz_album_mbid`/`_albumid` |
| `album_stage` `_needs_release_mbid` | gate — a miss claimed "every track already has a release MBID" and **skipped resolution entirely** |
| `album_stage` artist-MBID reads ×3 | find a stored `musicbrainz_artistid` (else a redundant MB search) |
| `album_stage` extended-fields persist, Discogs artist id, soundtrack rename | same scoping pattern, same stage |
| `musicbrainz_persistence_service.lookup_and_save_artist_mbid` | **the reported Artist ID** — the lookup found the id, then `WHERE artist = :artist` / `LIKE 'Afi %'` selected nothing |
| `metadata.update_album_mbid_fields` | manual link paths (`rows_updated` in the API response) |
| `album_service.update_album_ids` | release/RG/Discogs id writer + its companion file-tag SELECT |
| `artist_service.apply_album_mbid` ×2, `album_service._album_file_paths` | companion reads — without them the DB would update while files stay blank |

The codebase's standard scope is `LOWER(COALESCE(NULLIF(album_artist, ''),
artist)) = LOWER(:artist)`; these were the holdouts.

## Fix

- Every statement above now scopes `LOWER(col) = LOWER(:artist)` /
  `LOWER(album) = LOWER(:album)` / `LOWER(artist) LIKE LOWER(:pattern)` —
  pattern and semantics unchanged, only the comparison.
- `lookup_and_save_artist_mbid`'s batch UPDATE gained
  `bindparam("ids", expanding=True)`: a raw tuple only expands under
  psycopg2 — on SQLite it rendered `IN ?` and raised a syntax error. The
  expansion is identical on Postgres (and matches the pattern `album_stage`
  already uses for its own `IN` lists).

## Tests

`tests/test_mbid_backfill_case_insensitive.py` (14):

- import preservation: all four MBID columns are in `MBID_IDENTITY_FIELDS`
  **and** `PRESERVE_WHEN_EMPTY_FIELDS` (half 1 cannot regress into a wipe);
- release-group + release persists fill rows despite `Afi`/`AFI` and
  `sing the sorrow`/`Sing The Sorrow` mismatches; the release gate answers
  `True` (with an exact-case control and a filled-row control);
- `lookup_and_save_artist_mbid('Afi')` writes the found id onto `AFI` rows
  (exact + `feat.` LIKE branch) and leaves an already-valid id alone;
- `update_album_mbid_fields` returns `rows_updated == 1`; `update_album_ids`
  writes the RG id; `_album_file_paths` finds the rows (DB and file passes
  must see the same set);
- source guards: the lowercase scope exists in each touched file, scoped to
  the `update_album_mbid_fields` **function** (a whole-file check passed at
  HEAD because sibling helpers already used the pattern).

## Oracle

Stashed the five sources → **11 failed / 3 passed** (the 3 are the intended
controls: import preservation, exact-case rowcount, exact-case gate) →
popped → markers present → stash list back to 3.

## Verification

- Affected set (`album_stage` / `update_album_mbid_fields` /
  `lookup_and_save_artist_mbid` / `update_album_ids` / `_album_file_paths` /
  `apply_album_mbid`): 16 files, 25 failed / 375 passed → **0 new** vs the
  baseline subset (25 = 25, none fixed — no test failed on casing).
- Full suite → baseline reconciliation (`_mbid_full.txt`).

## Note

Pre-existing, untouched: `artist_service.py` has a `\s` SyntaxWarning in a
docstring at HEAD too; `services/metadata/album_service.py`'s
`get_majority_artist` / `get_track_recommendations` and `album_stage`'s
country/compilation writes keep exact-cased scoping — they are reads/flags,
not MBID backfills, and were outside this report.
