# Navidrome import: stop format-only metadata conflicts

**Date:** 2026-10-31
**Area:** Navidrome import authority comparison
**Files:** `services/scanning/reconciliation.py`,
`tests/test_navidrome_conflict_normalisation.py` (new)

## Reported

Every import logged:

```
[NAVIDROME_IMPORT] metadata conflict — local correction preserved
artist='A Perfect Circle' album='Mer de noms' track='The Hollow'
fields='composer,releasestatus'
```

and, for Aerosmith, `fields='composer,originaldate'`.

These are **not** stale file tags and do not need a DB→file re-save. They are
different representations of the same values:

| Field | Popularr/file value | Navidrome value |
|---|---|---|
| `composer` | `Billy Howerdel, Maynard James Keenan` (writer names joined with `", "`) | `Billy Howerdel; Maynard James Keenan` (contributors joined with `"; "`) |
| `releasestatus` | `Official` | `official` (or the field is absent/empty) |
| `originaldate` | `2000` (file tag) | `2000-05-23` (album `originalReleaseDate`) |

`local_authority_conflicts` compared these as raw strings, so every dirty row
looked permanently conflicting; the successful-import checkpoint then could not
advance.

## Fix

`local_authority_conflicts` now compares values semantically:

* **Composer / lyricist / record label** (`_PEOPLE_FIELDS`, `_LIST_FIELDS`):
  split on `;`, `,`, `/`, `|`, `&` and "and"; compare the deduplicated,
  accent/case-folded **set**, so delimiter and ordering no longer matter.
* **Release status / country / media / script / language / copyright**:
  case-insensitive comparison.
* **Original date / release date**: parse `YYYY[-MM[-DD]]` and compare only the
  precision present on **both** sides. `2000` vs `2000-05-23` is compatible;
  `2000-04-23` vs `2000-05-23` is a real conflict.
* **Empty incoming values are not assertions**: if Navidrome omits a field or
  echoes `""`, it no longer conflicts with a local value. This removes the
  permanent `releasestatus` noise when the album object does not expose it.

Genuine differences still conflict: a different composer set, a different
release status (`Official` vs `Bootleg`), or a different month/day/year.

## Do not re-save files for this

Re-saving the DB values to the audio file would not change Navidrome's indexed
view, and it would re-stamp `local_tags_written_at` / mark another rescan. The
file already contains the local value (that is what made the row dirty). The
correct path is:

1. Compare equivalent formats semantically (this change).
2. Keep `metadata_dirty_for_navidrome` until a verified import agrees.
3. Only write DB→file when the file is actually stale; that is a separate
   conflict-only repair, not a blanket post-import pass.

Run one Navidrome import after deploying: the `composer`/`releasestatus`/
`originaldate` warnings should disappear and the affected rows should be
confirmed and cleared.
