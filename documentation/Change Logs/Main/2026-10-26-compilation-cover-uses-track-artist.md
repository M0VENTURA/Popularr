# Cover detection now measures a compilation's tracks against the TRACK artist

**Date:** 2026-10-26
**Area:** `enrichment` (cover detection)
**Files:** `services/enrichment/cover_detector_impl.py`,
`tests/test_compilation_cover_uses_track_artist.py` (new)

## Reported symptom

> Cover detection doesn't seem to be happening for various artists compilations
> based on track artist.

## What the scan log actually showed

Cover detection *did* run on *The Best 80's Car Songs In The World… Ever!* — and
most of what it found was right (`Tainted Love` → Gloria Jones, `Everything I
Own` → Bread, `They Don't Know` → Kirsty MacColl, `Don't Turn Around` → Albert
Hammond). But it also produced this:

```
Don't Dream It's Over — Crowded House → 5★  …  (labelled "Don't Dream It's Over (Denise Jannah Cover)")
```

Crowded House **wrote** that song and released it in 1986. It was attributed to a
much later cover. That is the "not based on the track artist" report: the
verdicts were being reasoned about the wrong person.

## Root cause

A cover verdict is always a statement about a **performer** — "this recording is
not by whoever released it first". On a Various Artists compilation the ALBUM
artist is the placeholder `"Various Artists"`, which matches no real credit, so
every comparison made against it is vacuous:

* `_same_artist(candidate, "Various Artists")` is never true, so the performer's
  own recording is never excluded from the candidate set, and the "the performer
  released it first" **conclusive negative can never fire**;
* `_performer_wrote_work(work, "Various Artists")` returns `False` by its own
  placeholder guard, so a track artist who wrote the song is never recognised as
  the originator.

Three of the six detection steps already resolved the track artist correctly
(work-first, ISRC, writer matching). Three passed the album artist:

| step | technique | was comparing against |
|---|---|---|
| 3 | MusicBrainz recording relations | `artist` (the album's) |
| 5 | title hints (`"(X Cover)"`) | `artist` |
| 6 | work fallback | `artist` |

Worse, `_detect_via_recording_relation`'s parameter was *named* `album_artist`,
so the defect read as correct at every call site.

## Fix

**One resolver** — `comparison_artist(track, album_artist)`:

* the **track** artist when it names a real performer;
* otherwise the album artist, for a single-artist release (where the two are the
  same, which is why this only ever broke on compilations);
* otherwise `""` — meaning *no performer to compare against*, never a match. An
  empty string is falsy, so callers' `if performer` guards **skip** the
  comparison instead of running it against a name that can never match.

Step 3, 5 and 6 now use it, and steps 1/2/4 were routed through it so all six
share one rule. `_detect_via_recording_relation` and `_check_release_for_cover`
were renamed `album_artist` → `performer`; the misleading name was load-bearing
for the bug.

A second, smaller correction in `_cover_relation_target`: a relation pointing
back at the performer is not a cover, and it was checking the album artist.

## Verification

`tests/test_compilation_cover_uses_track_artist.py` (24 tests):

| Group | Pins |
|---|---|
| The resolver | track artist wins on a compilation; a placeholder track artist falls back to the album; **two placeholders resolve to `""`, not to the placeholder**; single-artist albums unchanged; every placeholder spelling; a real "Various Hands" is kept; whitespace trimmed |
| The mechanism | a writer-performer is recognised as the originator (`{}` conclusive negative); the placeholder **can never** reach that verdict; `_performer_wrote_work` refuses a placeholder |
| Step 5 end-to-end | the guard is asked about `Crowded House`, not `Various Artists`; no cover is recorded for the performer's own song; a placeholder-only row is discarded rather than mis-attributed |
| Wiring | steps 3/5/6 take the performer and never `album_artist=artist`; step 4 does not fall back to the album artist; the parameter is *named* `performer` |

**Behavioural oracle** (`C:\Temp\oracle_cover_artist.py`) — same scenario, old
semantics vs new:

```
FIXED  (track artist) : asked=['Crowded House']     covers=[]
BROKEN (album artist) : asked=['Various Artists']   covers=['Denise Jannah']
```

That is the reported mis-attribution reproduced and closed.

- **Regression sweep** (`cover`, `no_undefined`, `no_python_syntax`,
  `bootstrap`): failure sets **identical** to the stashed baseline (28
  pre-existing in the cover suites); pass count rises by exactly the 24 new
  tests. Zero regressions.

## ⚠️ Three things in the same log that this does NOT fix

1. **A track↔file misalignment.** Row 16 of the album page shows
   `My Favourite Waste of Time (Marshall Crenshaw Cover) — Owen Paul` with the
   file `/…/CD 1/16. The Belle Stars - Sign of the Times.mp3`. The title and
   artist belong to row 4. `apply_cover_metadata_batch` updates strictly by
   `CAST(id AS TEXT)`, so cover detection cannot be the writer of that — it is a
   metadata/import pairing bug.
2. **`ISRC: PMEDIA` on all 60 tracks.** A single, non-ISRC value on every row
   means step 2 (ISRC matching) is fed garbage for the whole album.
3. **`Funky Town` (Pseudo Echo) was not detected** as a cover of Lipps Inc. Its
   MusicBrainz recording apparently carries no "cover of" work relation, so none
   of the six steps had a signal to find.
