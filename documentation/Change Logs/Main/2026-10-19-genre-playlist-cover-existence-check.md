# Genre playlist covers are now VERIFIED against the playlist, not the prompt hash

**Date:** 2026-10-19 · **Area:** `playlists` · `ui`

## Reported

> "The log does not mention Gemini or any genre playlist cover
> generation/updates during this pass… If your Gemini genre cover generator is
> only triggered when a genre playlist is newly created (or when its cover is
> missing/forced), it wouldn't fire here since all 75 genre playlists were
> already present. **Can the cover be checked if it exists when updating a
> playlist with new tracks?**"

## What was actually happening

The cover step *does* run for created/updated/unchanged playlists — but it was
deciding "already done" from the **state file alone**:

```python
if not (regenerate or cfg["regenerate"]) and state.get(genre_key) == digest:
    return {"status": "skipped", "reason": "cover already generated for this prompt"}
```

That hash answers *"did we ever make this image for this genre?"* — **not**
*"does this playlist currently carry it?"*. Consequences:

* a playlist that was **deleted and recreated** gets a **new id and no image**,
  but its genre is the same, so the hash still matched and the cover was never
  re-applied;
* a cover **removed by hand** left the hash intact — also skipped forever;
* and a `skipped` verdict produced **no log line at all**, so an enabled
  feature that checked 75 covers looked identical to one that never ran.

## The change

The state entry now records **which playlist** the art was applied to:

```json
{ "nu metal": { "hash": "…", "playlist_id": "7fNI…" } }
```

and the skip requires **both** to match the playlist being synced:

```python
if not _forced and recorded_hash == digest:
    if recorded_pid and recorded_pid == resolved_id:
        return {"status": "skipped", "reason": "cover already applied to this playlist"}
    # recorded but for a DIFFERENT playlist / no id on record → re-apply
```

* The playlist is resolved **before** the decision, and `finalise_stage` passes
  the id **in from the `getPlaylists` fetch it already performs** — so verifying
  a cover costs **no extra Navidrome round trip**.
* `_load_state` no longer runs `str(v)` over its values (that coercion would
  flatten the new dict entry and lose the id); legacy bare-hash entries are
  still read via `_state_entry`.

### ⚠️ Legacy state costs ONE extra image per genre

An entry with **no recorded playlist id** is treated as *unconfirmed* — the
cover is applied once and the entry is upgraded. That is deliberate: a missing
id cannot prove the cover is on the playlist, and this is what makes an install
whose covers are missing heal on the next scan. After that first pass every
genre is confirmed and later scans skip as before.

### The pass is now legible

One line per pass, only when covers are enabled:

```
🎨 Genre playlist covers — 0 applied, 75 already present, 0 failed (75 playlist(s) checked)
```

so "enabled but nothing logged" can no longer hide a pass that ran.

## A bug the existing suite caught

The internal rename (parameter `playlist_id` vs resolved `resolved_id`) initially
left the upload passing the **parameter**, which is `None` on the default call
path — i.e. `POST /api/playlist/None/image` → 404 → the cover would silently
never apply. `test_genre_ai_covers.py::test_it_generates_uploads_and_records`
failed on exactly that, and the upload now uses `resolved_id`.

## Tests

* `tests/test_genre_cover_exists_check.py` (**new**) — state-entry reading
  (`_state_entry`, dict vs legacy), legacy re-apply + upgrade (and that the
  upgrade costs only one image), recreated-playlist re-cover, confirmed-cover
  skip, id plumbing (supplied id avoids a lookup), no-match never spends quota,
  and the genre-pass wiring (`existing_playlist_ids`, summary line).
* `tests/test_genre_ai_covers.py` — unchanged and still green (44 cover tests pass).
* Genre/playlist suites: 322 pass.
