# Album save stripped live / unplugged / acoustic from track titles

**Date:** 2026-09-25
**Area:** `routes/ui_routes.py` (`album_detail`)
**Reported:**

> Saving on an album page removes live, unplugged or acoustic from a track even
> if the metadata had updated to include them.

## Root cause

The album save called `revert_track_live_state` for **every track** whenever the
*submitted* album type was not live:

```python
if album_type and "+live" not in album_type.lower() and "(live)" not in album_type.lower():
    if track_carries_live_state(track):
        revert_track_live_state(track_id)
```

`revert_track_live_state` does two things, and the second is the destructive one:
it clears the `is_live` / `is_acoustic` / `album_context_live` flags **and it
rewrites the title** through `strip_live_acoustic_suffix`.

Proven end to end before the fix:

```
before save   title='Song (Unplugged)'  is_acoustic=1
after save    title='Song'              is_acoustic=0
```

### Why that condition could not work

It asks **"is the SAVED type non-live?"** — which is true for *every ordinary
album*. `album_type` is read straight from the Album Type `<select>`
(`form.get("album_type")`), whose normal value is `album`, so the revert fired
essentially always.

The right question is **"did the album just stop being live?"**. The revert
exists to undo tagging the pipeline applied because it *believed the album was
live*; that is only meaningful when the belief has just changed.

This save path cannot express that change either: it never writes `is_live` /
`is_acoustic` / `album_context_live` — none appears in the payload it builds,
and none is in `_STAGED_WRITABLE` (both now pinned by tests). So nothing about
the live state was cleared there, and there was nothing for the revert to undo.

### The acoustic half of the same bug

The old guard tested only `+live` / `(live)`, so an `album+acoustic` release was
treated as an ordinary album and lost its "(Acoustic)" titles the same way —
even though `_apply_live_remix_album_tagging` applies the Acoustic label on
exactly that type.

## The change

The gate is now a real **reclassification test**, comparing the STORED album type
(read from the track rows the page already loaded, so no extra query) against the
saved one:

```python
if _stored_type_was_live and not _saved_type_is_live:
```

New module-level `_album_type_is_live_state()`, which recognises live, acoustic
and unplugged.

⚠️ **It uses WORD BOUNDARIES, not a substring test.** A substring check
classified `Delivery` / `Deliverance` / `Alive` / `Outlive` as live releases —
each merely *contains* `"live"` — which would route every ordinary save of such
an album through the revert. Measured: the substring matcher got **4 of 18**
cases wrong, the boundary matcher **0 of 18**.

The per-track `track_carries_live_state` guard is retained, so the common case
still costs no extra query.

The sibling call site in the **track** detail (`if any(f in update_values for f
in ("is_live", "is_acoustic"))`) was inspected and is **correct** — it checks
that the submitted values actually CLEARED the flags, i.e. a real user action —
so it is deliberately left alone.

## Verification

**NEW `tests/test_album_save_keeps_live_markers.py` (51 tests)**, covering:

* every live-state spelling is recognised (live / acoustic / unplugged, bare and
  parenthesised and `+`-composed);
* ordinary types are not (`album`, `ep`, `single`, `album+remix`,
  `album+compilation`, `album+soundtrack`, empty);
* words merely *containing* `live` are not (`Delivery`, `Alive`, …);
* the revert fires **only** on a genuine reclassification and never on an
  ordinary save;
* the handler no longer keys on the saved type alone, does still compare stored
  vs saved, and still keeps the cheap per-track guard;
* the assumption the gate rests on — that this save never writes the live flags —
  plus their absence from `_STAGED_WRITABLE`;
* `revert_track_live_state` still rewrites the title, so the guard's premise
  cannot silently change.

**End-to-end proof** (real `revert_track_live_state` against a seeded row):

| scenario | before | after |
|---|---|---|
| ordinary save, `Song (Unplugged)` | `Song` ❌ | `Song (Unplugged)` ✅ |
| ordinary save, `Song (Live)` / `Song (Acoustic)` | stripped ❌ | kept ✅ |
| reclassified `album+live` → `album` | — | `Song` ✅ (still reverts) |
| still typed `album+live` | — | kept ✅ |

**Mutation-verified 5/5 CAUGHT**: restoring the old saved-type-only guard, a
matcher that is always True, one that is always False, the substring regression,
and dropping the cheap per-track guard.

**Oracle** (worktree at `73d48d4e`): sweep over
`album/live/acoustic/title/metadata/review/track/normali`. One new failure —
`test_year_prefixed_album_matches` — verified **pre-existing and
order-dependent** (passes 3/3 in isolation in BOTH trees) and in a module this
diff does not touch.

## Note on what this does NOT change

The two legitimate reverts are untouched:

* **track** detail, when the user clears the live/acoustic flags on a track;
* the album save, when the album is genuinely reclassified away from live.

A track that arrives from the metadata update carrying `(Unplugged)` and then has
an ordinary album save run over it now keeps its marker.
