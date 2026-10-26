# A remix album's rows are scored as the remix, not the studio original

**Date:** 2026-10-26
**Area:** `popularity` (Last.fm / ListenBrainz identity)
**Files:** `services/popularity/popularity_sources.py` (`resolve_performance_title`),
`services/popularity/stages/track_stage.py`,
`tests/test_remix_row_scores_the_remix.py` (new)

## Reported symptom

On Limp Bizkit's *New Old Songs* the listen counts were "completely different"
between the first pass and a later one, and the rows looked like they matched
"the track release rather than the remix release on this album".

The scan log contains both halves of it:

```
Album-tracklist LB match for 'My Way' …: 10758 listens (release=10758, kept=release)
[TRACK] "My Way" … | Score: 71.0 (LF: 1212.0k, LB: 10.8k)
```

ListenBrainz is **release-scoped** and reported the remix (~10.8k). Last.fm was
**title-matched** and reported the studio hit (~1.21M). The same row was being
measured as two different recordings, a ~100× spread. The same pattern repeats
for `Take a Look Around` (LF 1283.8k / LB 459) and `Break Stuff` (LF 1988.5k /
LB 447).

`New Old Songs` is a remix album whose local file names lost the marker: the
release calls the row `My Way (DJ Premier Way remix)`, the file says `My Way`.

## Root cause

The row's **performance identity** — *which rendition is this file* — was
resolved from the album release for MusicBrainz metadata
(`_build_album_recording_batch`, which exists for exactly this reason and
documents the "Helden X Hymnen" case) but **not** for the popularity lookups.
Every provider match is a *title* match, so the title handed to Last.fm decided
which recording the row was measured as. Three separate leaks:

1. **Last.fm was queried with the retitled file name.** The artist-catalogue
   match then found the studio hit and returned it *before* the album-resolved
   `recording_mbid` was ever consulted — `recording_mbid` is only used as a
   last-resort variant, after the catalogue wins.
2. **The "alternate rendition" flag was read from the DB.**
   `_alt_rendition = _is_alternate_performance_title(effective_track["musicbrainz_title"])`
   — `musicbrainz_title` is written by the PREVIOUS scan, so it was empty on a
   first pass and populated later. `_force` scans re-read it from the track row.
   That is the first-vs-later flip: the same album, a different flag, a
   different gate, a different number.
3. **The empty-aggregate fallback re-queried the local title.**
   `lf.get_track_info(artist, title)` ran precisely when the aggregate found
   nothing, putting the studio recording straight back through the one path
   meant to catch a miss.

## Fix

`popularity_sources.resolve_performance_title(local_title, release_title)`
returns the title providers must be queried with:

* the **release** title when it corroborates the local title
  (`_position_title_corroborates`, already used for positional identity — so
  the row was position *and* duration matched upstream) **and** it carries a
  performance marker the local title lost;
* the local title otherwise, so an ordinary album's rows are untouched and a
  row whose local name is already specific keeps it.

In `track_stage` the identity is resolved **before any provider is asked**, from
`_prefetch_entry["release_track_title"]`:

* `_perf_title` is used for all three Last.fm entry points — the aggregate, the
  featured-variant search, and the per-track fallback;
* `_alt_rendition` is raised when the release title differs, so the flag is
  derived in-memory on **every** pass instead of from a column a previous pass
  wrote;
* the resolution happens **before** the `_force` block sets `_prefetch_entry =
  None`. Dropping a stale cached *count* is right; dropping the *identity* with
  it is what left a forced scan with neither.

Measured, with the reported album shape:

| queried title | Last.fm listeners |
|---|---|
| `My Way` (the file name) | 1 212 000 — the studio hit |
| `My Way (DJ Premier Way remix)` (the release name) | 6 800 — the remix |

## Verification

`tests/test_remix_row_scores_the_remix.py` (23 tests):

| Group | Pins |
|---|---|
| The resolver | retitled rows take the release name; plain rows are untouched; a non-corroborating release title is refused; live markers honoured |
| Why it matters | the local title reproduces the reported contamination; the release title scores the remix; the studio row is unreachable; plain rows still score their plain recording |
| The wiring | identity resolved from the release; **resolved before the forced entry is dropped**; all three Last.fm calls given the resolved title; the fallback no longer re-queries the local title |
| Flag stability | the flag no longer depends on a previous scan; the resolver is pure, so run order cannot change it |

- **Oracle:** with `track_stage.py` stashed, 5 wiring tests fail — the suite
  detects the ordering defect rather than describing current behaviour.
- **Regression sweep** in ten chunks (`lastfm`, `listenbrainz`, `popularity`,
  `track_stage`, `album_tracklist`, `release_credit`, `isrc`, `no_undefined`,
  `no_python_syntax`, `bootstrap`): **failure sets byte-identical** to the
  stashed baseline in every chunk. Zero regressions.

## Note on the ListenBrainz side

No ListenBrainz change was needed. Its identity already comes from the album's
own release, so with the album batch authoritative for `recording_mbid` the
`MAX(release, recording-global)` rule compares two counts of the **same**
recording — which is the assumption that rule was written on. The rule would be
wrong for counts of two *different* recordings, which is what the Last.fm side
was quietly doing.
