# The resync did rebuild covers — a running Navidrome import silently voided it

**Date:** 2026-11-28
**Commit:** `fix(playlists): wait out a running Navidrome import before rebuilding playlists`

## Question: does "Apply & Rebuild Playlists" rebuild covers and Essential Collections?

Yes — the apply rebuilds **three** families:

| Family | Builder | Gate |
|---|---|---|
| genre top-tracks | `_create_genre_top_track_playlists(cleanup_orphans=True)` | always |
| covers | `_create_covers_playlist` (+ `_create_genre_covers_playlists`) | `playlists.covers_playlist_enabled` |
| Essential Collections | `_refresh_all_essential_collections` | `playlists.essential_playlists_enabled` (default on) |

Both curated families are wrapped in `try/except` so a failure there cannot fail
a job whose genres were already written — which also means a failure is only a
log line.

## Why the covers collection still had 3★ tracks

The floor is real and configurable — `covers_min_stars`, default **4** — and
`_create_covers_playlist` applies it **twice**: in the row fetch and again as a
post-filter (that second one exists precisely because the scan path passes one
shared 3★ superset pool to every curated builder, and the audit found 148 cover
memberships below four stars as a result).

So the 3★ tracks were not a floor failure. The playlists were never written.

**`sync_playlist_by_name` defers every playlist write while a Navidrome import is
running** and returns `skipped` (see `services/navidrome/import_gate`). That
deferral is correct — a deferred `getPlaylists` returns an empty list, which is
indistinguishable from "not there yet", so proceeding would CREATE a second copy
of the playlist.

The defect is one line up:

```python
def _playlist_sync_succeeded(result):
    return bool(result.get("created")
                or result.get("updated")
                or result.get("unchanged")
                or result.get("skipped"))     # ← deferred counts as SUCCESS
```

So a rebuild started during an import reported *"17 playlist(s) synced"* having
written nothing at all, and every playlist — covers included — kept its old
contents. Imports run on a schedule and take minutes (the log shows ~10 min for
206 albums), so this is easy to hit.

## The fix

`apply_genre_playlist_resync` now **waits (bounded, 15 min) for a running
Navidrome import to finish** before the playlist phase:

```python
import_deferred = not _wait_for_navidrome_import()
```

Waiting is safe: this is a background job with no request timeout, and the
alternative is a guaranteed no-op. It fails **open** when the gate cannot be
read, matching the gate's own policy. The progress row says what it is waiting
for, and a unified log line explains the pause.

If the import still has not finished at the deadline the job proceeds (the
genres are already written, and blocking longer is worse than a partial rebuild),
but it now says so instead of claiming success:

- `result["playlists_deferred"] = True` plus a human `warning`;
- the progress detail ends `· PLAYLISTS DEFERRED (import running)`;
- the Config card renders an amber banner — *"playlist updates were deferred and
  may not have been applied. Run this again once the import has finished."*

## Two things worth checking in your config

If a rebuild still leaves 3★ covers, these are the remaining explanations, both
configuration rather than code:

1. **`playlists.covers_playlist_enabled` is off.** The covers family is skipped
   entirely, and the old playlist stays exactly as it is.
2. **`playlists.covers_min_stars` is set below 4** (0 disables the floor). The
   Config page shows "Minimum Stars" for covers; it defaults to 4.

## Tests

`tests/test_resync_rebuilds_covers_and_essentials.py` — 15 cases:

- all three families are rebuilt, with their gates;
- the covers floor defaults to 4 and is applied even when rows are supplied;
- a deferred sync counts as success (the trap), the sync defers during an
  import, and the wait happens BEFORE the rebuild;
- the wait helper: returns at once when idle, waits out a running import, gives
  up at the deadline, and fails open on a broken gate;
- the deferral reaches the result, the progress line and the UI.
