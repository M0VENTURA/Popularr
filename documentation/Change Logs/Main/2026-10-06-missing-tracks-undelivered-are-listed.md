# Missing tracks: an undelivered track is now LISTED, not swallowed

**Date:** 2026-10-06 · **Area:** album / missing tracks
**Commit:** `fix(album): a track the queue has not delivered stays on the list`

## Reported

> The issue with the missing tracks is still outstanding.

Earlier (see `2026-10-05-missing-track-exclusion-counts.md`) the response
gained an `excluded` breakdown so the answer would be *computable*. It was never
**shown**, so the cause stayed invisible — and the gates themselves could still
remove a track with nothing on screen to explain it.

## What the evidence says

MusicBrainz ground truth for the reported release (`0f1ae6af`, *The Power and
the Passion: A Tribute to Midnight Oil*, 2001) is a **13-track** edition — and
it is the **only** edition of that album:

```
10  Forgotten Years        Skulker
11  Power and the Passion  David McCormack
```

Both have ordinary titles, so the `if not mb_title: continue` skip is ruled
out. The pasted table shows library files numbered **1–9, 12, 13** only, so no
library row occupies 10/11 — the `in_library` gate is ruled out too.

That leaves the two gates that hid a track **with no way back**:

1. **queue** — any row in `queued`/`searching`/`downloading` hid the track
   *permanently*. The queue is exactly what stalls here (searches of 200–350 s,
   a transfer pinned at `progress=0`), so a track the user never received simply
   stopped appearing.
2. **rejected** — `ignore_missing_track_db` sets `ignored = TRUE` and **nothing
   in the codebase ever sets it back**, so a dismissed pair disappears for good.

## The change

**Split "handled" from "not yet delivered"** (`services/metadata/album_missing_service.py`):

| queue status | before | now |
|---|---|---|
| `imported`, `completed`, `matched`, `in_collection` | hidden | **hidden** (it is in the library) |
| `queued`, `searching`, `downloading`, `processing`, `moving` | hidden | **listed**, carrying `queue_status` |

A track you do not have is missing; whether the queue is working on it is an
*attribute*, not a reason to omit it.

**Queue attribution.** The coverage query is scoped to the **artist** and the
album is matched in Python — correct for one album, catastrophic for
`Various Artists`, where it spans the whole library. A row whose `album` is
empty slipped past the `if q_album and …` guard and granted coverage to *every*
VA album at once (the reported album is VA). A row that names no album now
belongs to no album; re-queueing is harmless because
`find_blocking_queue_item` de-duplicates by identity.

Both call sites now share one `_queued_coverage(artist, album)` helper, so
`get_missing_tracks` (the Lookup refresh) and `persist_missing_from_comparison`
(the Compare path) can never disagree about what "handled" means.

**Say what was withheld** (both trees' album JS):

- a queued track renders a blue **Queued** badge instead of the amber
  *Missing* one, with the download button **disabled**;
- the header badge gains **`· N not shown`** (tooltipped as
  `already in the library / delivered / dismissed`), including on the DB-only
  page load, which now reports `excluded: {rejected: N}`.

Without that note, two withheld tracks and a release that never had them are
indistinguishable on screen — which is why this stayed unresolved.

## Known gap, now visible

There is still **no way to un-dismiss** a rejected track (`ignored` is written
by `/ignore-missing-track` and never cleared). It is now *counted on screen*,
so it can be identified rather than guessed at; restoring those rows is a
follow-up.

## Tests

- `tests/test_missing_track_queue_state_ui.py` — **14**, run against the real
  functions through node (both trees): queued badge + disabled button, the
  ordinary missing row unchanged (control), the converter forwards
  `queue_status`, and the badge reports / clears the withheld count.
- `tests/test_album_lookup_findings.py` — undelivered rows are listed with
  their status; delivered rows stay hidden (control); an album-less queue row
  cannot hide this album's track; the DB path counts dismissed rows.

**Oracle:** stashing the three source files → **12 failed, 32 passed** = exactly
the tests that depend on the change; markers restored; stash count back to 3.

## Verification

See the commit message for the suite numbers.

## Files

- `services/metadata/album_missing_service.py`
- `static/js/album_detail.js`
- `test_site/static/js/pages/album.js`
- `tests/test_missing_track_queue_state_ui.py` (new)
- `tests/test_album_lookup_findings.py`
- `tests/test_artist_page_is_db_only.py`
