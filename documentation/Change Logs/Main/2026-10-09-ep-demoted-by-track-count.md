# EPs demoted to albums by the local track count (2026-10-09)

## Reported

> During the metadata scan, albums that are EPs are not always matching
> correctly and leaving them as albums.

## Root cause

`_resolve_album_type` (`services/popularity/stages/album_stage.py`) re-bucketed
MusicBrainz's release-group `primary_type` by **track count**:

```python
if mb_type in {"single", "ep"} and track_count > 6:
    mb_type = "album"
elif mb_type == "single" and track_count > 3:
    mb_type = "ep"
```

Carried over from `old_system` (`popularity.py`: *"# Standard EP threshold is
3-6 tracks"*), and wrong here for two independent reasons:

1. **`track_count` is the LOCAL tracklist** — `len(album_tracks)`, i.e. the
   folder as stored: bonus tracks, live bonus cuts, a flattened multi-disc
   folder. It is *not* MusicBrainz's own release tracklist.
2. **An EP with 7–8 tracks is entirely normal.**

So an EP survived only while its folder held **6 or fewer** tracks and silently
became an `album` above that. That is exactly why it read as *"not always"* —
the short EPs matched and the long ones didn't, on the same scan, for the same
reason.

MusicBrainz's release-group `primary_type` is authoritative for an EP. Only a
**single** with an implausible count is genuinely ambiguous, and that half of
the rule is genuinely useful — it is kept.

```python
if mb_type == "single" and track_count > 6:
    mb_type = "album"
elif mb_type == "single" and track_count > 3:
    mb_type = "ep"
```

## Two paths I checked and ruled out

* **The corroboration guard does not touch EPs.** `_mb_type_is_corroborated`
  only looks at `_DESTRUCTIVE_SECONDARY_TYPES = ("+live", "+acoustic",
  "+remix")`, so `mb_type == "ep"` returns `True` immediately. And where the
  guard *does* downgrade (`enrich_album_extras`), it only runs on
  `_full_pass or _mode_meta` and the marker list still cannot match an EP.
* **`detected == "album"` is not the problem.** `_detect_album_type` has no EP
  rule at all (its title heuristics cover soundtrack / live / remix, then
  default to `album`) — so a plain-titled EP *relies* on MusicBrainz, and
  `if detected == "album": detected = mb_type` is what fills it in. Once
  `mb_type` was demoted to `"album"` upstream, that fill-in had nothing to
  fill with.

## Tests

`tests/test_ep_survives_track_count.py` — **20**:

* an EP stays an EP at **1, 4, 6, 7, 8, 12 and 20** local tracks (7+ are the
  ones that were broken);
* a plain local `album` is overridden by MusicBrainz's `ep`;
* **CONTROL** — the single heuristic is unchanged: 2–3 → `single`, 4–6 → `ep`,
  7+ → `album`, and a plain album at 14 tracks stays an album;
* **CONTROL** — a stored rich type (`album+soundtrack`, `album+live`,
  `album+remix`, `album+compilation`) is not clobbered by the EP, because
  `"+" in detected` blocks the override on purpose;
* the `adjusted by track count` diagnostic still exists (it is how this rule
  gets noticed at all).

## Verification

* New suite → **20 passed**.
* **Oracle** — reverting `album_stage.py` → **9 failed / 11 passed**: exactly
  the EPs at 7/8/12/20 tracks, the generic-album override, and the four
  stored-rich controls (they assert MusicBrainz still reports `ep`). The 11
  that pass either way are the short EPs that were never broken plus the
  single-heuristic controls. Restored → 20.
* **Sweep** — 51 album-type/classification/secondary/release files, clean
  `origin/develop` vs this change: **base `42 failed / 935 passed`** vs
  **new `42 failed / 935 passed`**, `Compare-Object` on the sorted `^FAILED`
  lines = **empty both ways** → 0 regressions. (The new suite is not in that
  name filter, so it does not contribute to either count.)

## Not changed

* **No local EP title heuristic was added.** `_detect_album_type` still cannot
  recognise an EP on its own — if the MusicBrainz lookup returns nothing (no
  release-group match, or the 1 req/s budget is exhausted), an EP whose title
  does not say so will still resolve to `album`. That is a separate, judgement
  call: adding `"EP"`-in-the-title detection would also claim albums that
  merely reference an EP. Say the word and I will scope it to explicit
  markers only (`(EP)`, `[EP]`, ` - EP`, trailing `EP`).
