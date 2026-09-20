# One canonical name per MusicBrainz extended tag

**Date:** 2026-09-20
**Area:** `services/metadata/tag_names.py` (new) / `services/metadata/tag_file_service.py` / `services/scanning/metadata_extractor.py`
**Status:** implemented, guarded by `tests/test_tag_name_standardisation.py`

---

## Symptom

A single track carried several spellings of the same value:

```
MUSICBRAINZ ALBUMSTATUS       (legacy space form)
MUSICBRAINZ ALBUMTYPE
MUSICBRAINZ_ALBUMARTISTID     (underscore form, from the FLAC/extended map)
MUSICBRAINZ_ALBUMID
MUSICBRAINZ_ALBUMTYPE         (the same field again)
MUSICBRAINZ_ARTISTID
MUSICBRAINZ_RELEASEGROUPID
MUSICBRAINZ_RELEASETRACKID
MUSICBRAINZ_TRACKID
ORIGYEAR + ORIGINALYEAR       (two names AND a duplicate frame)
```

## Two mechanisms, both fixed

### 1. Several hand-written name tables disagreed

`tag_file_service.py` alone held FOUR independent lists of the same names:

| Where | Spelling |
|---|---|
| `_MB_TXXX_DESC` | `MUSICBRAINZ ALBUM ID` (spaces) |
| the TXXX literals in `write_id3_tags` | `MUSICBRAINZ ALBUM ID` (spaces) |
| the generic fallback `field.replace("_", " ").upper()` | `MUSICBRAINZ ALBUMTYPE`, `ORIGINALYEAR` (spaces) |
| `_VORBIS_FIELD_MAP` | `MUSICBRAINZ_ALBUMID`, `MUSICBRAINZ_ARTISTID` (underscores) |

Any two of them disagreeing on a field produced two frames for one value.

### 2. Deletion was exact-desc only

`ID3.delall("TXXX:ORIGNALYEAR")` does **not** remove a frame stored as
`TXXX:orignalyear`. So writing the "same" tag again left BOTH frames in place —
which is exactly the reported `ORIGINALYEAR` twice on one track. The same is true
for Vorbis comments: the spec is case-insensitive on read, but mutagen stores the
case it was given, so assigning `audio["musicbrainz_albumid"]` **adds a second
key** when the file already holds `MUSICBRAINZ_ALBUMID`.

## The fix

### `services/metadata/tag_names.py` — the single source of truth

- `CANONICAL_TAG_NAMES` — internal field → the ONE on-disk name.
- `KNOWN_TAG_SPELLINGS` — every spelling ever used, declared once.
  `clear_keys_for()` turns that into the normalised key set a write must remove,
  so **both** kinds of spelling are covered: separator/case variants (which
  normalisation finds on its own) and genuine **synonyms** — `ORIGYEAR` vs
  `ORIGINALYEAR`, `TOTALTRACKS` vs `TRACKTOTAL` — which no normaliser can equate
  and therefore *have* to be listed. Splitting these two categories wrongly was
  the first bug my own tests caught.
- `clear_id3_variants()` / `clear_vorbis_variants()` — delete every declared
  spelling of a field before writing the canonical one. **This is what cleans up
  existing files**: the next tag write removes the old frames.

### Canonical names verified against Navidrome's `mappings.yaml`

Navidrome matches aliases case-insensitively but does **not** strip separators,
so the separator choice is what matters. Every name below is one of Navidrome's
declared aliases:

| Field | Canonical | Navidrome alias that matches |
|---|---|---|
| album / artist / album-artist / recording / release-track / release-group / work IDs | `MUSICBRAINZ_ALBUMID`, `MUSICBRAINZ_ARTISTID`, … | `musicbrainz_albumid`, `musicbrainz_artistid`, … |
| album type | `RELEASETYPE` | `releasetype` |
| album status | `RELEASESTATUS` | `releasestatus` |
| release country | `RELEASECOUNTRY` | `releasecountry` |
| original year | `ORIGINALYEAR` | `originaldate` ← `originalyear` (and `origyear`) |
| cover markers | `IS_COVER`, `ORIGINAL_COVER_ARTIST` | app-specific; both readers already accept either form |

Deliberately **not** renamed (renaming would change behaviour, not fix a
duplicate): `musicbrainz_genres` (the FLAC writer maps it onto Navidrome's
`GENRE` field on purpose), `original_title`, `catalog`, `recordlabel`.

### Writers now use the registry

- `_MB_TXXX_DESC` is **derived** from `CANONICAL_TAG_NAMES` (it is the
  fill-missing pre-check's lookup key, so it must equal the name we write).
- The seven near-identical MB-ID branches in `write_id3_tags` collapsed into ONE
  branch driven by the registry — 54 lines of hand-written descs removed.
- The generic album-extended branch is now an explicit **field** allow-list with
  registry-derived names, instead of a list of hard-coded descs.
- `_VORBIS_FIELD_MAP` keeps only genuine Vorbis renames (`tracknumber`, `date`,
  `albumartist`, `genre`, …); MB names come from the registry for FLAC too, and
  `clear_vorbis_variants()` runs before every write.
- `_existing_non_empty_fields` (the `fill_missing_only` pre-check) now compares
  with `normalise_tag_name`, so a file still holding a LEGACY spelling counts as
  populated — respecting the user's "don't overwrite" policy rather than
  bypassing it. Standardisation happens on normal writes, which are the default.

### Readers

`services/scanning/metadata_extractor.py` gained `origyear` as an
`originalyear` alias — previously that tag was simply **ignored** on read-back.
The other readers (`album_tag_sync_service._read_file_values`) already normalise
case/separators, which is why old files keep reading correctly during the change.

## Verification

Worktree at `origin/develop`, same edits replayed (`tag_names.py` + 8 patches to
`tag_file_service.py` + 1 to `metadata_extractor.py`; code identical, comments
trimmed in the harness).

| Check | Result |
|---|---|
| `tests/test_tag_name_standardisation.py` (new, 40 tests) | **all pass** |
| `test_album_tag_sync_service` + `test_metadata_extractor_mb_readback` + `test_album_mb_tags_to_files` | 4 failed / 41 passed — the **same 4 pre-existing failures as the reverted baseline** (`_db_tag_candidates` signature drift ×2, `'object' object has no attribute 'delall'` mutagen double ×2), **0 new** |
| `get_errors` on every changed file | clean |

The new suite pins, among others:

- every declared spelling is removed by a write of its canonical name (the
  invariant that actually kills duplicates);
- `ORIGYEAR` + `ORIGINALYEAR` + `originalyear` on one track collapse to nothing
  before the canonical frame is written;
- an unrelated frame is never touched;
- `_MB_TXXX_DESC` matches the registry exactly;
- no writer may hard-code a MusicBrainz tag name again (a static guard over
  `tag_file_service.py` and `album_tag_sync_service.py`).

## One test updated (it encoded the replaced behaviour)

`tests/test_album_tag_sync_service.py::test_mp3_writer_frame_map_has_new_frames`
asserted `_MB_TXXX_DESC["musicbrainz_releasegroupid"] == "MUSICBRAINZ RELEASE
GROUP ID"` — i.e. the legacy space form. It now asserts the canonical name via
the registry, so it cannot drift again.

## Files

- `services/metadata/tag_names.py` — **new**
- `services/metadata/tag_file_service.py` — registry-driven writers
- `services/scanning/metadata_extractor.py` — `origyear` read alias
- `tests/test_tag_name_standardisation.py` — **new**, 40 tests
- `tests/test_album_tag_sync_service.py` — canonical-name assertion

## Note for existing libraries

Nothing breaks in the meantime: the readers already accept both spellings, so
Navidrome and Popularr keep seeing the values. The duplicates are removed the
next time each file is written (album save, track edit, MBID apply, or a scan
that writes tags) — there is no migration step to run, and no file needs to be
rewritten for correctness.
