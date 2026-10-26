# New Music playlist stopped generating — a NameError my own guard could not see

**Date:** 2026-10-26
**Area:** `services/popularity/stages/finalise_stage.py` · `services/downloads/match_engine.py`
**Commit:** `fix(popularity): the New Music builder referenced an undefined name`

## Reported

```
WARNING services.popularity.stages.finalise_stage
  New Music playlist generation failed error="name 'playlist_name' is not defined"
```

## Cause — mine, from `9d714d4`

The weekly-rotation change added

```python
winners = _weekly_shuffle(winners, playlist_key=playlist_name)
```

to `_create_new_music_playlist`. That function never defines `playlist_name` —
its two sibling builders do, which is exactly why the line looked right when
copied. Every New Music run raised `NameError` at that point, the caller's
`except Exception` logged it at WARNING, and the playlist simply stopped being
generated. Nothing else failed, so nothing else pointed at it.

Fixed by giving the function a module constant `_NEW_MUSIC_PLAYLIST_NAME` and
using it for the Christmas filter, the delete path, the sync call AND the
rotation seed — four literals that could otherwise drift apart.

## ⚠️ Why my tests passed while the code was broken

`test_the_rotation_runs_after_the_track_cap` reads the function SOURCE and
asserts the shuffle call sits after the `[:max_tracks]` slice. It passed:

* the call WAS there,
* it WAS after the slice,
* the text was correct.

**A source-text assertion can only prove a string is present — never that the
code executes.** That is the same class of blind spot already recorded for
"a source assertion cannot detect a neutered branch". A NameError is precisely
the defect a text guard cannot see.

## The guard that DID catch it

`tests/test_no_undefined_names_in_production.py` (pyflakes) reported it:

```
finalise_stage.py:2124:53: undefined name 'playlist_name'
```

⚠️ It was reporting the failure and I missed it, because my regression-sweep
FILTER did not include that filename (`no_undefined`). **A sweep whose file
filter omits the global guard suites is not a regression check.** The filter now
includes `no_undefined`, `no_python_syntax` and `bootstrap`.

## Second, pre-existing defect found by the same guard

```
services/downloads/match_engine.py:251:23: undefined name 're'
```

`match_engine.py` calls `re.compile(...)` for contraction folding but never
imports `re`. Present at `origin/develop` (`a9076aa`), unaffected by any recent
work — a latent `NameError` on the download-matching path. `import re` added.

With both fixed, the whole-tree undefined-name guard passes for the first time.

## New tests

`tests/test_playlist_builders_execute.py` (7) — these **execute** the builders
with their collaborators stubbed rather than reading their source:

* `_create_new_music_playlist()` returns > 0 and pushes `"New Music"`;
* the synced name equals the rotation key (so a week's seed cannot name a
  different playlist than the one synced);
* a short pool deletes instead of syncing;
* the rotation is actually invoked and preserves membership;
* `_sync_essential_playlist()` and `_create_genre_top_track_playlists()` run;
* a pyflakes check scoped to this module, so a future NameError names the file.

## Verification

Oracle — source reverted with the tests present: **5 failed / 2 passed**, and the
failure is the production error verbatim
(`finalise_stage.py:2124: NameError: name 'playlist_name' is not defined`).
Restored → 7 passed. `test_no_undefined_names_in_production` → 7 passed.

## Logged but NOT changed

* `Navidrome stored a different number of tracks than requested … dropped=1
  tracks=['Blind Channel - Diana']` — a stale/not-yet-indexed Navidrome song id.
  The warning already states the remedy (re-import, or shorten Navidrome's
  `ScanSchedule`). Not a playlist bug.
* `[HTTP-TRACE] Internal Network I/O attempt failed … ReadTimeout` on
  `/rest/getPlaylist` — a slow Navidrome on the LAN, retried per policy.
* `Navidrome user skipped — same server as an earlier entry` — deliberate: the
  playlists are server-scoped, so one account owns them.
