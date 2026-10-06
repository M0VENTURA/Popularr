# A compilation's ListenBrainz count is no longer replaced by a fragment

**Date:** 2026-10-06 · **Area:** popularity / scan
**Commit:** `fix(popularity): keep the higher ListenBrainz count for a track`

## Reported

> ListenBrainz is returning the listen count *only* for that specific
> soundtrack release, which is why "Burn" only registered 577 listens instead
> of its true global count.

## Where the bug actually was

The plan pointed at a "release vs recording" split, and that turned out to be
right in substance — but the fix belongs at the **consumer**, not the fetcher.

`popularity_sources.get_listenbrainz_album_tracklist_with_release` fetches the
release's tracklist (that's the `Preloaded ListenBrainz album tracklist …
release_mbid=…` log line) and `scan_stage_runner` then wrote it straight over
the row:

```python
_cur["listenbrainz_listens"] = int(_entry["listenbrainz_listens"] or 0)
```

An **assignment**. `prefetched_popularity` may already hold the recording's
*global* count (fetched by recording MBID via `/popularity/recording`), and a
release-bound count is a **subset** of it. On a compilation the two differ by
orders of magnitude — 577 versus ~150k — so the larger, already-known number
was thrown away, and the track scored far too low against its true
popularity.

## The rule now

**The higher of the two wins**, and the `(listens, users)` pair is kept
*together* from whichever source won — pairing the release's listens with the
global's user count would produce a number that exists in neither source.

The scan line now says which won, so the expectation is checkable from a tail:

```
[scan_runner] Album-tracklist LB match for 'Burn' (…): 151234 listens (release=577, kept=recording-global)
```

Identity resolution is untouched: `recording_mbid` and `release_track_title`
still come from the album's own release (position + duration matched) *before*
any count is considered.

## What from the plan was deliberately NOT done

* **Step 1** (`if va_compilation: mode = 'album_only'` in `finalise_stage.py`)
  does not exist in this codebase — there is no `va_compilation` variable and
  no `mode = …` assignment there. VA scoring actually goes through
  `_apply_album_relative_normalization(..., is_compilation=is_compilation or
  is_va_compilation)` plus `_mark_track_artist_top_band` in
  `scan_stage_runner`, and it already applies album-relative normalisation to
  VA albums. Changing that means changing star ratings, so it is not done on
  the strength of a snippet that describes different code.
* **Step 3** ("validate the `album_only` z-score") has no `album_only` mode to
  validate for the same reason.
* **Step 4** runs a scan against a deployed container, which is not reachable
  from here — but the log line added above is exactly what to look for.

## Tests

`tests/test_compilation_lb_release_vs_recording.py` — **4**:

* the reconciliation is a max, not an assignment (the raw release count is not
  written straight through);
* listens and users are written by the same branch, and the other branch
  (`kept=recording-global`) exists — otherwise the "max" is unreachable;
* the log states which source won;
* CONTROL: identity (`recording_mbid` / `release_track_title`) untouched.

**Oracle:** stashing `scan_stage_runner.py` → **3 failed, 1 control passed**;
with it, 4/4.

## Verification

- new suite: **4 passed**
- affected set (60 files): 935 passed / 69 failed → **0 new vs baseline**
- full suite: **225 failed / 4691 passed / 2 skipped** in 635 s against a
  baseline of 303 failures → **79 fixed, 0 real new**. The single NEW id is
  `test_download_completion_not_found_loop.py::TestDeepFileSearch::
  test_sibling_torrents_root_is_searched`, a known flake in this suite (it was
  the only new id in the previous run too).

## Files

- `services/popularity/scan_stage_runner.py`
- `tests/test_compilation_lb_release_vs_recording.py` (new)
