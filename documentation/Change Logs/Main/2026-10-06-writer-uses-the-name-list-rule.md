# The writer bar compares names, not encodings

**Date:** 2026-10-06
**Reported:**

> The writer lookup is having a similar issue to the genre matching
>
> **MusicBrainz:** Writer: *`["Marianne Faithfull", "Joe Mavety", "Barry
> Reynolds", "Terry Stannard", "Steve York", "Ralph Sall"]`* → **Marianne
> Faithfull, Joe Mavety, Barry Reynolds, Terry Stannard, Steve York**

## Root cause

`tracks.writer` is a **TEXT** column that stores a **JSON array**, while
`_flatten_release` builds the proposed value as a comma string
(`", ".join(dict.fromkeys(writers))`). The field had no rule of its own, so it
was compared as a plain string:

```python
current  = '["Marianne Faithfull", "Joe Mavety", …]'   # stored
proposed = 'Marianne Faithfull, Joe Mavety, …'          # MusicBrainz
_norm(current) == _norm(proposed)  →  False, always
```

Identical names spelled two ways therefore always read as a change — the same
defect the genre rows had (`72122f90`), on the one multi-valued field that
never got the rule. It also made the bar itself unreadable: a raw JSON array
opposite a comma string.

## What the report's own example shows

Fixing the encoding does **not** make that particular bar disappear, and it
should not: the stored list has **six** names and MusicBrainz proposes
**five** — `Ralph Sall` is in one and not the other. That is a genuine
difference, which is the bar doing its job.

What changes is that both sides now render as one line each:

```
current : Marianne Faithfull, Joe Mavety, Barry Reynolds, Terry Stannard, Steve York, Ralph Sall
proposed: Marianne Faithfull, Joe Mavety, Barry Reynolds, Terry Stannard, Steve York
```

…so the name being dropped is visible instead of being buried in JSON.

## Changes

`services/metadata/metadata_proposal_service.py`:

* `current_map["writer"]` and `proposed_map["writer"]` are rendered through
  `_genres_text()` — the shared name-list parser from the genre fix (a list,
  a JSON array or a comma string all parse the same way), so the display is a
  comma line on both sides;
* a suppression rule beside the genre one:

  ```python
  if field == "writer" and _genre_sets_equal(current, proposed):
      continue
  ```

  `writer` has the same *shape* as a genre list — several names in one value,
  order not meaningful — so it shares the rule rather than growing a second
  parser. A name that genuinely appears or disappears still differs.

`_ALBUM_FIELD_SPECS` has no writer entry (the album-wide writer goes through
the save payload, not the proposal loop), so track level is the whole surface.

## Tests

`tests/test_metadata_compare_version_markers_and_genres.py` — **100** (was 93;
+7 in `TestWriterUsesTheNameListRule`):

* the same writers as a JSON array and as a comma string are **not** reported;
* order, spacing and case do not matter (parametrized);
* CONTROL: the reported pair (six stored names, five proposed) **is** still
  reported — suppressing it would hide a real edit;
* the displayed writer is a clean line on both sides, with `Ralph Sall`
  visible in `current`;
* CONTROL: an empty stored writer is still filled.

**Oracle:** with the service stashed → **5 failed / 95 passed**, the 5 being
exactly the tests that describe the new behaviour and both controls passing
(the genre tests stay green because that fix is already in HEAD).

## Verification

- new suite: **100 passed**; with `test_mb_lookup_track_artist.py` → **107 passed**
- affected set (31 files): **598 passed / 11 failed**, against a baseline
  subset of 12 for the same files → **0 new, 1 fixed**
- full suite: **225 failed / 4743 passed / 2 skipped** in 608 s against a
  baseline of 303 failures → **79 fixed, 0 real new**. The single NEW id is
  `test_download_completion_not_found_loop.py::TestDeepFileSearch::
  test_sibling_torrents_root_is_searched`, the known Windows path-case flake.

## Files

- `services/metadata/metadata_proposal_service.py`
- `tests/test_metadata_compare_version_markers_and_genres.py`
