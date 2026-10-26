# Clearing the Release Name now sticks

**Date:** 2026-10-26
**Area:** `ui` / `metadata` (album page save)
**Files:** `routes/ui_routes.py` (`album_detail` POST, `_write_album_track_file_tags`),
`tests/test_album_release_name_can_be_cleared.py` (new)

## Reported symptom

"Deleting the release name from a release and selecting save doesn't remove it.
It puts the same one back."

## Root cause — the box was write-only in one direction

`#album_release_title` is documented on the page as *"Leave blank when the
collection holds the original release"* — so blank is a **meaningful** input,
not a missing one. The handler nonetheless read it with a truthiness test:

```python
if release_title:
    payload["release_title"] = release_title
```

An emptied box contributed **no key** to the payload. `_execute_save` filters by
column *name*, so it never touched `release_title`, and the value the user had
just deleted was still stored and re-rendered. The one direction a user actually
needs — removing an edition name the collection no longer holds — silently did
nothing while the save reported success.

A **second** leak then made it survive a rescan, which is why it looked like the
app was putting the name back deliberately:

`release_title` is not in `build_tag_updates`' column map at all (the *file's*
name for it is `albumversion`), and that function deliberately **drops empty
values**. So the audio file kept its old `ALBUMVERSION` frame. Navidrome reads
that frame and appends it to the album name as a subtitle
(`strip_appended_album_version` exists precisely because of this), so a stale
frame re-clips an edition the album page no longer shows.

## Fix

**Database — presence-gated, not truthiness-gated.** The question is not "is the
value empty?" but "did the form **carry** the field?":

```python
if "album_release_title" in form:
    payload["release_title"] = release_title
```

The same convention `album_lookup_skip` already uses on this route. A caller that
omits the input leaves the column alone; a submitted blank clears it.

The identical defect is fixed for its neighbours, which had the same write-only
behaviour:

| field | why |
|---|---|
| `release_year` | an emptied edition-year box means "no edition year" — `""` is coerced to `NULL` for the INTEGER column |
| `release_values` (`recordlabel`, `catalognumber`, `barcode`, `releasedate`, `media`, `releasecountry`, `originalyear`) | gated **per field** — these are written only when the form had a box for them, so metadata with no box on the page is never blanked |

**File.** `_write_album_track_file_tags` now pushes the release name to the
file's `albumversion` frame explicitly, including the clear. `build_tag_updates`
cannot do it: the column is unmapped and it drops empty values. An explicit
`albumversion` in the payload wins, so the artist page's direct field still works.

### ⚠️ One box deliberately left truthiness-gated: `album_type`

Its select cannot render every type the scan stores (`album+acoustic`,
`album+dj-mix`, a mixtape…). When no option matches, the browser falls back to the
**first** option — the blank "Unknown" one — so a blank submission does *not*
reliably mean "the user cleared it". Presence-gating it would wipe a type the page
never showed. Clearing a type needs an option for every value first; until then it
stays as it was. A test pins this so a later sweep cannot "fix" it into a bug.

## Why the artist page's Edit modal is unaffected

The live `#editReleaseModal` renders only Title / Year / MBID and those inputs
carry **no `name` attributes**, so its generic `#editReleaseModal [name]` sweep
never posts `album_release_title`. It posts `release_year` only when non-empty.
`/api/album/metadata` pre-fills the field where a name box exists.

## Verification

`tests/test_album_release_name_can_be_cleared.py` (18 tests):

| Group | Pins |
|---|---|
| **The file, driven for real** | a set name becomes `albumversion`; `""` and whitespace-only clear the frame; an absent field writes nothing; an explicit `albumversion` wins; the `disc_number` clear this was modelled on still works |
| The database gate | the truthiness test is gone; an absent field is not a clear; `release_year` and the release-detail fields gated too; `album_type` deliberately not gated |
| Column types | `""` → `NULL` for INTEGER, `""` for TEXT; `_execute_save` filters by name so a blank value still reaches `UPDATE SET` |
| The read-back | the template fills the box from `album_data.release_title`, and the album view reads the column rather than an `albumversion` fallback that would re-show what the file write just cleared |

- **Oracle:** with `routes/ui_routes.py` stashed, **9** of the new tests fail.
- **Regression sweep** (`album`, `tag_file`, `no_undefined`, `no_python_syntax`,
  `bootstrap`, `metadata_update`, `genre`): failure sets **identical** to the
  stashed baseline in every chunk (82 / 1 / 12 / 6 / 84 / 5 / 44 — all
  pre-existing). Zero regressions.
