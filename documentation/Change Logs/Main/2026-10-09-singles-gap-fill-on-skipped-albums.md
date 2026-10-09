# `run_singles_on_skipped_albums` was a dead option — now it fills the gaps

**Date:** 2026-10-09 · **Area:** `scan` / `config`

## Reported (answering the question the previous entry left open)

> The idea behind it was that if it had scanned an album but somehow missed a
> track it wouldn't keep skipping that track.

That intent was already written on the Config page, and nothing implemented it:

> Backfill single verdicts on skipped albums … so never-assessed tracks get one
> without a full rescan. Makes Discogs/MusicBrainz lookups for tracks without a
> stored verdict — off by default for quiet, fast skips.

## The distinction the window cannot make

The album window answers **"has this album been scanned?"**. The question that
matters here is **"did every track get a verdict?"** — and a track the earlier
pass never reached leaves the album looking finished:

- its worker was abandoned on the per-album budget,
- the track was added to the album after the scan,
- the detection **ERRORED**, which stamps no timestamp.

In every one of those cases `was_album_scanned` skips the album again on the
next pass, and the next, and the gap is never filled.

## Fix — three pieces, all decided in one place each

**`_tracks_without_singles_verdict(tracks)`** — the ONE definition of
"assessed". `single_detection_last_updated` is stamped only when detection ran
to completion (`track_stage`'s singles block is its sole writer, and a raised
exception leaves it untouched), so a row without one is a track the pass
genuinely missed. **A negative verdict still has a timestamp, which is what
makes "assessed" safe to skip on** — otherwise every ordinary track would keep
its album un-skipped for ever.

**`_pass_runs_singles(options)`** — mirrors `track_stage`'s own gate on its
singles section (`if not metadata_only and not popularity_only and not
_sd_fresh:`), so "a pass that can fill a singles gap" has one definition. A
metadata-only or popularity-only pass runs no singles detection, so the option
cannot claim to fill a phase the pass does not run.

**`_singles_gap_fill(tracks, runs_singles, enabled)`** — returns the unassessed
tracks when the opt-in applies, else an empty list. The album loop un-skips the
album when it is non-empty:

```
Popularity Scan - Album "Birds of Tokyo" has 2 of 11 track(s) with no singles
verdict — running it instead of skipping (Run Singles Detection on Skipped Albums)
```

### What it costs

Only the tracks **without** a verdict do any work: every other track answers
`singles_detection_is_fresh` and returns `(cached)` at 0.0s. So the price is one
assessment per genuinely-missed track, **once** — the verdict is then cached for
its TTL and the window takes over again. That is what makes it safe to leave on.

### What it deliberately does not do

It does **not** set `force_metadata_for_this_album` (that is the completeness
override's job, and setting it here would turn a one-verdict backfill into a
full metadata re-run per missed track), and it does not touch a track that
already has a verdict — including a stale one, which the TTL handles.

## The gate and the fill can no longer disagree

`skip_unchanged_albums` already refused to skip when a verdict was missing —
but it re-derived that inline:

```python
all_done = all(t.get("single_detection_last_updated") for t in tracks)
```

Two definitions of "assessed" is how a gate ends up unable to satisfy itself
(the exact failure the singles-freshness fix had to undo last commit), so both
call sites now go through `_tracks_without_singles_verdict`. A test pins it.

## Tests

`tests/test_run_singles_on_skipped_albums_gap_fill.py` — 27 tests:

- the predicate: unassessed, **assessed-as-negative** (the load-bearing case),
  assessed-as-single, a manual override with no timestamp, a mixed album, a
  blank/`None` timestamp, no tracks, a `None` entry
- `_pass_runs_singles`: metadata-only and popularity-only say no; combined,
  singles, singles-missing-popularity, finalise and singles-detection-only say
  yes; and it matches `track_stage`'s literal gate
- `_singles_gap_fill`: returns the unassessed; off by default; no fill when the
  pass has no singles phase; a complete album is still skipped (control); no
  tracks means no fill
- wiring: both decisions share the predicate, the option is read, an unassessed
  track un-skips the album, the gap fill does not force metadata, and it only
  runs when something else already decided to skip

**Oracle:** reverting `scan_stage_runner.py` → **26 failed / 1 passed** (the one
pass is the `track_stage` contract guard, which holds either way); restored →
**27 passed**.

**Sweep:** 37 test files grepping the changed modules, one pytest process each,
baseline (all four source files reverted) vs changed → **0 new failures**; the
26 baseline-only entries are the oracle's own discriminators.

Docs updated in both trees (`templates/pages/config.html` and
`test_site/templates/Pages/config.html`) plus `documentation/USER_GUIDE.md` —
the Config page is the source of truth for what a setting means, and its help
text now names the trigger (`no singles verdict`) rather than the old, vaguer
"never-assessed tracks". The previous changelog entry that reported this option
as dead now points here.
