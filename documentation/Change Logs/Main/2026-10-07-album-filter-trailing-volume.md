# Album filter: a trailing "Volume 2" suffix is a different album

**Date:** 2026-10-07
**Area:** scan (album filter matching)
**Commit:** `fix(scan): album filter no longer matches trailing-volume siblings`

## Report

> I'm doing an album scan for Various Artists - MTV Headbangers Ball, but
> it's also scanning MTV Headbangers Ball volume 2, even though they are
> both different albums.

## Root cause

`album_names_match` tolerated a candidate that was **the requested name
plus a separator-introduced trailing suffix** (`have = want + sep + …`).
The volume's name is exactly that shape:

```
MTV Headbangers Ball - Volume 2   → True   (dashed separator)
MTV Headbangers Ball, Volume 2    → True   (comma)
MTV Headbangers Ball: Volume 2    → True   (colon)
MTV Headbangers Ball (Volume 2)   → True   (bracket)
```

so `should_skip_album` (Navidrome import, step 1) and `load_stage`
(popularity, step 2) both admitted the volume whenever the base album was
requested. The same rule ran in mirror: requesting the *volume* also
admitted the *base* album (rule 2, wordy prefix).

None of the pinned behaviours in `tests/test_album_name_filter_matches.py`
depend on that direction — the module's own doctrine says *"the dangerous
direction is OVER-MATCHING"*.

## Fix

`helpers/normalization_service.py` — `album_names_match`:

- **Removed** the trailing-suffix rule entirely: a separator after the
  requested name introduces a sequel/sibling, never a spelling of the same
  album. Edition markers are still equal because `album_name_key` strips
  them before comparison.
- **Restricted** the reverse rule (`requested = candidate + sep + …`) to
  **bare-number** short forms, so the pinned Ricky Martin case
  (`Navidrome "17"` ↔ requested `"17: Greatest Hits"`) survives while
  wordy prefixes no longer pair two real albums (the volume never pulls
  the base album either).
- Kept: exact/edition-stripped equality, core-title suffixes
  (`"Greatest Hits"` ↔ `"17: Greatest Hits"`), numbered-prefix siblings
  (`"Greatest Hits"` → `"17: Greatest Hits"` / `"18: Greatest Hits"`).

Both filter sites (`services/scanning/filters.py`,
`services/popularity/stages/load_stage.py`) share this one function, so
step 1 and step 2 can never disagree.

## Tests

`tests/test_album_name_filter_matches.py` (+8,
`TestSequelVolumesAreDifferentAlbums`): all four separator spellings of
the volume are skipped on a base request; the mirror direction (volume
request vs base album) is skipped too; the base album still matches
itself; the bare-number short form still matches.

**Gate:** targeted 39/39 · affected set (6 files) — the single failure is
pre-existing baseline · oracle (exactly the 8 new tests fail on revert) ·
full suite 222 failed / 4945 passed → **0 NEW**, 81 fixed vs baseline.
