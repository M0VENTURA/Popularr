# Matched folder tracklist: show which tracks the folder actually has (2026-10-09)

## Reported

> When matching an album on the downloads queue, it doesn't show which tracks
> on the release have been matched from files, it just shows the tracklist of
> all tracks.

## Root cause

The expander built by `matchedReleaseHtml`
(`test_site/static/js/pages/monitor.js`) renders the **release's** tracklist
from `GET /api/downloads/folder/match-tracklist?release_mbid=…` and labels it
`Matched album tracks (N)` — where `N` is the **release's** track count.

Nothing marked a row as present or absent, so:

* a folder holding 3 of 11 tracks claimed **11** indistinguishable matches;
* the number read as a claim about the *folder* when it was a fact about the
  *release*.

The folder's file list was already on the row — `folder.files`, from
`_get_files_in_folder` → `{"name": <relative path>, "size": …}` — it was simply
never consulted. (The panel itself shipped in
`2026-10-07-matched-folder-release-details.md`; this is what it left out.)

## Fix

* **`trackListHtml(titles, keys)`** is now the single renderer for *both* the
  cached-first render and the async one — the two inline lists this replaces
  could otherwise drift apart again.
* Each row is marked: **✓ present** (`text-success`) / **– absent**
  (`text-muted`) — colour *and* icon, so it is not colour-only.
* The summary reads **`Matched album tracks (X/Y in this folder)`**, `X`
  counted against the folder's files and `Y` the release size.
* `folderTrackKeys` is keyed by **folder path**, so two folders matched to the
  same release each keep their own answer, and a re-render reuses it.

### Matching is normalised EXACT, never substring

`_normaliseTrackKey`: basename → strip extension → strip a leading track
number → strip a trailing bitrate → collapse whitespace → lowercase.

```
"01 - Enemies.flac"  →  "enemies"     == release title "Enemies"   ✔
```

A substring test would report **`Die` as present because `Dieter` contains
it** — which is worse than showing nothing, because it is confidently wrong.
Pinned by `test_the_lookup_is_an_exact_membership_test`, which forbids
`.includes(` / `.indexOf(` anywhere in the three functions that decide
presence.

The two sides are normalised in different places *by design*: the files once
per folder (`matchedTrackKeys`), the title at comparison time
(`countMatched`).

## Tests

`tests/test_matched_tracks_show_folder_presence.py` — **15**:

* one renderer serves both paths (the replaced inline lists are gone);
* present/absent rows are visually distinct, with a non-colour affordance;
* the summary counts the folder, and the old `${tracks.length}` label is gone;
* the async path updates the label **and** recovers its owning folder;
* keys are keyed by folder path;
* exact-membership matching only; the normaliser strips basename/extension/
  track number/case; each side is normalised exactly where it should be;
* **controls** — a folder with no match renders no block, the pre-load
  fallback and the `Matched to` identity block survive, the endpoint is still
  called, and a failed load is still retryable.

## Verification

* `node --check` clean on `test_site/static/js/pages/monitor.js`.
* New suite → **15 passed**.
* **Oracle** — reverting `monitor.js` → **10 failed / 5 passed**; the 5 that
  pass either way are the controls above. Restored → 15.
* **Sweep** — 31 monitor/folder/match/download files, clean `origin/develop`
  vs this change: **base `27 failed / 446 passed`** vs **new `17 failed / 456
  passed`**, `Compare-Object` on the sorted `^FAILED` lines = **empty for
  "only in CHANGED"**. The 10 that only fail at BASE are this change's own
  tests (the oracle) → **0 regressions**. `test_matched_folder_release_details`
  — the suite that owns this panel — passes unchanged.
* The pre-existing 17 are identical in both runs (they include the documented
  `test_sibling_torrents_root_is_searched` hash-order flake).

## Not changed

* The **live** `static/js/monitor.js` was left alone (test_site-only
  instruction), so the live downloads page still shows the unmarked list.
* A folder whose files are named in a way the normaliser cannot reduce will
  show **`0/Y`** rather than guessing. That is deliberate: reporting "none
  matched" when the naming simply differs is a visible signal to check, not a
  silent failure — but it does mean an unusual naming scheme shows `0/Y` until
  the normaliser learns it.
