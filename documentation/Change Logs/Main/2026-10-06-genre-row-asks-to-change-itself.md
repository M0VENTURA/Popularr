# A genre row no longer proposes changing a value into itself

**Date:** 2026-10-06
**Reported:**

> The genre matching here needs to be corrected as it's finding it needs to be
> updated but it's matching with the same information
>
> `MusicBrainz: Genres: ["children's music", 'classical', 'electronic',
> 'musical', 'pop', 'punk'] → children's music, classical, electronic, musical,
> pop, punk`

Every genre row on *Nightmare Revisited* offered the same "change", and both
sides were the same six genres.

## Root cause

`musicbrainz_genres` is a **JSONB** column, so the driver hands the proposal
engine a Python **list** — and the code rendered it with `_as_text()`, i.e.
`str(list)`, which is a **repr**:

```python
>>> str(["children's music", 'classical', 'electronic'])
'["children\'s music", \'classical\', \'electronic\']'
```

A repr is not JSON. `parse_json_tags` correctly rejected it and fell back to
comma-splitting, which left the decoration inside the names:

| side | parsed names |
|---|---|
| current (repr) | `["children's music"`, `'classical'`, …, `'punk]'` |
| proposed (comma text) | `children's music`, `classical`, …, `punk` |

Different sets → `_genre_sets_equal` says "not equal" → a bar asking you to
apply a change that changes nothing. The repr was also what the row *displayed*,
so the review showed a Python list against a tidy string.

**Why no test caught it:** every case in the suite feeds **strings**
(`_local(musicbrainz_genres="Rock, Metal")`), which is not what Postgres
returns. The regression test in this change feeds a list, which is the
production shape.

## Changes

`services/metadata/metadata_proposal_service.py`:

* `_genre_value()` — hands the shared parser something it can read:
  a list/tuple/set passes through unchanged (it is already the structure the
  parser wants), and a string that looks like a list is read with
  `ast.literal_eval` (literals only — safe) rather than being comma-split.
* `_genre_list()` — names from any encoding, in source order, deduplicated;
  `_genre_names()` (the comparison set) now builds from it.
* `_genres_text()` — the same names as one comma-joined string. **Both** sides
  of the comparison now go through it, so the bar displays two spellings a
  human can line up:

  ```
  MusicBrainz: Genres: children's music, classical, electronic, musical, pop, punk
                → children's music, classical, electronic, musical, pop, punk, rock
  ```

  …which is what a real difference looks like — the identical-set row is gone.

Nothing else in this module reads a JSONB field: `musicbrainz_genres` is the
only one in `_TRACK_FIELD_SPECS`, so the blast radius is that column.

## Tests

`tests/test_metadata_compare_version_markers_and_genres.py` — **93** (was 88;
+5, all in `TestAJsonbListIsNotARepr`):

* the exact reported row (JSONB list vs comma string) proposes **nothing**;
* a repr is read as the list it is;
* the displayed value is never a Python repr (either side);
* CONTROL: a real difference with a list stored is still reported;
* CONTROL: an extra genre in a list still reads as different.

**Oracle:** with the service stashed → **3 failed / 90 passed** — exactly the
3 tests that describe the new behaviour, both controls passing. The import of
`_genres_text` is deliberately *inside* one test: a module-level import would
collapse the stashed run into a single collection error and no test could say
which rule it was checking.

## Verification

- new suite: **93 passed**
- affected set (29 files — 27 by the first derivation plus
  `test_genre_consensus_aggregation.py` and
  `test_track_genre_navidrome_fallback.py`, which exercise
  `genre_aggregation_service`, itself a consumer of `_genre_names`):
  **542 passed / 17 failed**, against a baseline subset of 18 → **0 new,
  1 fixed**. All 17 are pre-existing (present in the baseline *and* in this
  run's full-suite output).
- full suite: **225 failed / 4729 passed / 2 skipped** in 663 s against a
  baseline of 303 failures → **79 fixed, 0 real new**. The single NEW id is
  `test_download_completion_not_found_loop.py::TestDeepFileSearch::
  test_sibling_torrents_root_is_searched`, the known Windows path-case flake.

## Files

- `services/metadata/metadata_proposal_service.py`
- `tests/test_metadata_compare_version_markers_and_genres.py`
