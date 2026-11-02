# Scan consistency fixes

**Date:** 2026-10-31
**Area:** popularity scan / rating passes / scan log
**Files:** `services/popularity/scan_stage_runner.py`,
`services/popularity/stages/finalise_stage.py`,
`services/popularity/stages/track_stage.py`,
`services/popularity/stages/album_stage.py`,
`services/scanning/pipelines/popularity_pipeline.py`,
`helpers/scan_log.py`,
`services/metadata/album_missing_service.py`,
`services/metadata/pending_update_service.py`

## 5★ global pre-pass and compilations

The global catalogue-top pre-pass now excludes compilation rows and does not
run for a pending VA/compilation album. A compilation is rated against each
track's resolved artist, so a lock derived from the album artist ("Various
Artists") can never be honoured by the finalise stage.

The runner also refuses to set `_global_5star_locked` when the lock cannot be
applied (compilation or popularity-only scan). If a legacy lock is present it
logs `catalog top (5★ lock NOT applied: ...)` instead of `GLOBAL 5★ LOCKED`.
This removes the "LOCKED then 3★" contradiction.

## Score columns agree

The SCAN RESULTS table now prints both scores explicitly:

```
RATING  TRACK TITLE  FINAL  POP  CAT-Z/Z-SCORE  LF LISTENS  SINGLE CONF
FINAL = persisted final_score | POP = album-relative popularity_score
```

Previously the table printed `popularity_score` under the generic `SCORE`
heading, while `[TRACK_RESULT]` printed `final_score`, so the same track
appeared to have two different values (for example 80.6 vs 71.1).

## Rating passes are named

Two-phase rating is intentional, but the log now says so:

* `[RATING_PASS_1] Provisional ratings ...`
* `[GLOBAL_RECONCILIATION] Cross-album caps and locks ...`
* `[RATING_PASS_2] Final reconciled ratings ...`

Provisional `Compilation detected`, album-era, and weights lines are DEBUG;
the final pass emits the user-facing compilation and weights lines once.
The provisional pass's album model and per-track-artist catalogue evidence are
cached in `options` and reused by reconciliation, avoiding duplicate catalogue
lookups for the same album.

## Forced ISRC recalculation

`_sync_isrc_popularity()` now returns `(updated_count, affected_albums)` using
`RETURNING`. On a forced scan, the affected albums are re-rated immediately via
`resync_star_ratings` and the log reports the recalculation. A non-forced scan
keeps the previous "next rating pass" message.

## Heartbeat wording

`ScanHeartbeat` now reports both clocks:

```
Still checking MusicBrainz... · no new result for 15s · total elapsed 1m 24s
```

`quiet_seconds` is no longer labelled `elapsed`. The structured record carries
`quiet_seconds` and `elapsed_seconds`.

## Genre fallback

If a track's genre aggregation produces only affiliation markers
(`Cover`, `Remaster`, …), the track stage now inherits the resolved track
artist's dominant musical genres. This prevents an album default of
`Remaster` and leaves Leper Messiah with a real musical genre alongside
`Cover`/`Remaster`.

## Log noise

These moved from INFO/unified to DEBUG:

* `[ENRICH] MusicBrainz secondary type accepted ...`
* `[ENRICH] album-art pipeline started`
* `Compilation track rated on artist's online catalogue ...`

Background metadata maintenance lines are now prefixed
`[BACKGROUND_METADATA]`, so a separate DragonForce job is visibly not part of
the current album scan.

## Not changed in this pass

* Cover detection pipeline/duplicate lookups (the derivative-work original
  lookup was fixed below).
* Navidrome's initial scan + tag-change follow-up ordering.
* Single-detection source identity for tribute covers (the detector still
  records `MusicBrainz`/`Discogs` without the release artist identity in the
  unified line).

## Follow-up: derivative-work original artist

The derivative-work path was returning Elton John Band for Fall Out Boy's
"We Didn't Start the Fire" because a title search accepted a later cover that
carried a writer relation. It now checks the **base work's own recordings**
first and prefers a recording by one of the base work's writers (Billy Joel).
A title-search result is accepted only when the candidate is actually a base
work writer. See `_earliest_writer_recording` in
`services/enrichment/cover_detector_impl.py`.

The title-hint parser also now ignores cover-act names in `(X Cover)`
annotations. `"Hey Ya! (Kidz Bop Cover)"` names the Kidz Bop performer, not a
cover OF Kidz Bop, so it is deferred to the work/recording evidence rather than
becoming `originally by Kidz Bop`.

## Remix popularity identity

On Limp Bizkit's *New Old Songs* the release track titles for "Break Stuff" and
"My Way" are plain, but their MusicBrainz **recordings** are
"Break Stuff (DJ Lethal remix)" and "My Way (DJ Lethal remix)". The album
tracklist payload used the release title, so `resolve_performance_title()` saw
no marker and Last.fm was queried with the studio title; the title-keyed
prefetch also returned the studio hit's ~1.2M/2.0M listeners.

The tracklist payload now prefers the recording title when it adds a
performance marker, version compatibility compares recording titles, and
`track_stage` no longer reuses a title-keyed Last.fm prefetch once the row is
known to be an alternate rendition. Forced/rescans query the remix title
instead.

## Cover/Remaster markers and source refresh

A source column containing only `Cover`/`Remaster` was treated as already
fetched, so the provider genre call was skipped and a cover could end up with
only markers. Marker-only lists now count as missing evidence and are
re-fetched. Navidrome is now a FALLBACK source: online sources decide whenever
they produce a musical genre; Navidrome is consulted only when they produce
none. Its affiliation markers are stripped before any vote (Navidrome echoes
the tags this pipeline wrote), and a stale `Remaster` in the stored `genres`
display list is not reused as fresh marker evidence. `Cover` remains an
intercepted marker appended outside the genre vote.

## VA albums with no MusicBrainz match

ListenBrainz was only retried by title/artist when a recording MBID existed but
returned zero. A VA compilation with no MusicBrainz release match has no
recording MBID at all, so every track stayed at `LB: 0`. The title/artist
fallback now runs for those rows too. The candidate search was also raised from
20 to 100 recordings: MusicBrainz ranks duplicate/compilation recordings above
the canonical one, and the recording carrying the real ListenBrainz audience
can sit past the first page (measured on "Scar Tissue": the 2.6M-listen
recording was outside the first 20).

## Reciprocal derivative work (Scar Tissue)

MusicBrainz has both work-to-work directions for RHCP's "Scar Tissue" and NTO's
later remix. The derivative path followed RHCP's relation to NTO and named NTO
as the original. It now compares the earliest recording date of both works: a
derived work cannot predate its base, so a relation pointing at a later work is
ignored and the track stays an original.

## My Heart Will Go On

The title search preferred a writer-verified recording over an earlier
performed one. Céline Dion's 1997 recording often has no writer relation on the
recording, while James Horner's 2004 score recording does, so the composer won.
`_find_original_recording` now compares years first and uses writer
verification only as a tie-break at the same date.



