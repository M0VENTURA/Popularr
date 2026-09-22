# A cleared cover verdict left the "(Artist Cover)" title and the "Cover" genre behind

**Date:** 2026-09-23
**Area:** `services/popularity/stages/track_stage.py`
**Status:** fixed, guarded by `tests/test_cover_clear_all_artifacts.py`

---

## Symptom

A track falsely flagged as a cover had its flag removed, but the other two
artifacts of that verdict stayed:

> When tracks are incorrectly marked as covers, it's removing the flag from them
> being a cover, but the track name is still showing `TrackName (Artist Cover)`
> and it's keeping the genre of cover assigned to the track.

The cover verdict is stored as **three** separate things — `is_cover`, the
`"Title (X Cover)"` rename, and the `Cover` genre in `genres` /
`musicbrainz_genres` — and only the first (plus one copy of the third) was
cleared.

## Root cause A — the cleaned title was dropped from the persist payload

`normalise_scan_track_identity` (via `prepare_track_context`) **does** strip the
`(X Cover)` wording, and it writes the clean title back onto the loaded row in
place. But `title` lives in `_STALE_PROTECTED_COLUMNS`:

```python
_STALE_PROTECTED_COLUMNS = frozenset({"title", "album_artist"}) | _ALBUM_TYPE_COLUMNS | _ALBUM_MBID_COLUMNS

def _strip_album_type_columns(track, update_payload):
    result = dict(track)
    result.update(update_payload)
    for col in _STALE_PROTECTED_COLUMNS:
        if col not in update_payload:
            result.pop(col, None)      # <-- title thrown away
    return result
```

That guard is **correct and deliberate**: the album stage renames *genuine*
covers (`"Song"` → `"Song (Artist Cover)"`) after track contexts are prepared, so
the loaded title is stale and an upsert would clobber the rename.

The clear branch, however, never claimed `title`, so the cleaned value was
dropped and the DB kept the suffixed one forever — while `is_cover` read `False`.
That mismatch is exactly what was reported.

```
# probe on the unpatched tree
identity title           : 'TrackName'      <- the normaliser IS correct
title_had_cover_wording  : True
PERSISTED title          : None             <- ⭐ title absent from the payload
PERSISTED is_cover       : False
title present in payload?: False            <- ⭐ THE BUG
```

## Root cause B — the `Cover` genre was re-voted back in

Filtering `musicbrainz_genres` was not enough, for two independent reasons:

1. **step 5 re-votes from `effective_track`** (= raw row + `update_payload`),
   which still carried the stale `genres` CSV `"Cover, Rock"`, so the
   aggregation re-added it through the *other* column;
2. `genre_aggregation_service._append_extra_genres` re-adds an intercepted
   `Cover` filter tag whenever the **title** contains the word — and the stale
   title was still feeding it.

With `Cover` as the only genre source, the filtered list was empty and the stale
CSV was written straight back:

```
input  genres : 'Cover'
OUT    genres : 'Cover'     <- unchanged
```

## Fix

In the clear branch (which only runs when detection has just decided the track
is **not** a cover, and never under `cover_manual_override`):

* **claim the cleaned title** — `update_payload["title"] = title`. Claiming it is
  the correct narrow fix; the `_STALE_PROTECTED_COLUMNS` guard must stay, because
  it protects the genuine-cover rename. ⚠️ The claim must not be conditional on
  `title != track["title"]`: the loaded dict was already mutated to the clean
  value, so that comparison can never differ and the claim would silently never
  fire.
* **seed both genre columns from the filtered lists** — `musicbrainz_genres` and
  the aggregated `genres` CSV, so the aggregation has nothing left to re-vote.

Claiming the title also removes the title-driven re-add (reason 2), because
step 5 reads its `context_title` from `effective_track`.

## Validation

Oracle worktree at `origin/develop` `bc11c12a`.

| Suite | Unpatched | Patched |
|---|---|---|
| `tests/test_cover_clear_all_artifacts.py` (new, 10) | **4 failed / 6 passed** | **10 passed** |

The four unpatched failures name the defects:
`the cleaned title was dropped from the persist payload`,
`With no other source, "Cover" must not be re-derived from the title`, and the
two source guards.

**Regression sweep** (10 cover/genre/identity/tag suites):

| Tree | Result |
|---|---|
| unpatched | 10 failed / 142 passed |
| patched | 10 failed / 152 passed |

`Compare-Object` of the failing sets: **0 NEW, 0 changed** — the same 10
pre-existing failures (`mutagen` `delall` env issues, genre-config harness
mismatches) on both trees. The 10 extra passes are the new suite.

## Files

* `services/popularity/stages/track_stage.py` — the clear branch now claims the
  title and seeds both genre columns.
* `tests/test_cover_clear_all_artifacts.py` — new guard, driving the real
  `prepare_track_context` → `process_track` path.
