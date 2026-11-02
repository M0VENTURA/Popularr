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

The guard now includes Peter Hollens, Lindsey Stirling, Jonathan Young, Caleb
Hyles, Leo Moracchioli, Boyce Avenue, Walk off the Earth and Scott Bradlee.
When the suffix names a cover act and there is no author hint, the title step
still runs the writer search (so `"I See Fire (Peter Hollens Cover)"` can
resolve to Ed Sheeran) instead of skipping the track entirely.

For "Hey Ya!", the work page contains a misdated **Kidz Bop** recording from
2000. The work path preferred the earliest recording and therefore reported
Kidz Bop. A writer-linked performer/band (André Benjamin → André 3000 →
OutKast) is now preferred over an arbitrary earlier title match, and known
cover acts are excluded as original candidates in the title search.



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

## Writer aliases and band identities (Hey Ya!)

The writer-candidate graph stopped at the writer's direct relations. André
Benjamin does not list OutKast directly; his "André 3000" alias does. The
candidate builder now follows one alias level to its band memberships, so the
OutKast recording can be found as the original of "Hey Ya!".

On a VA row whose artist is still the "Various Artists" placeholder, the
album-wide self-cover guard rejected the found original and wrote nothing. A
usable original is now accepted even when the track performer is unknown. A
stored suffix that names the wrong act — "(Kidz Bop Cover)" with OutKast as the
original — is rebuilt too, instead of being treated as already repaired.

The cover verdict funnel and suffix repair now compare against
`comparison_artist(track, album_artist)`: the TRACK artist first, with the album
artist only as a fallback for a single-artist release.

## Marker-only final genre (Soundtrack)

The track stage only wrote `genres` when the affiliation MARKERS changed. A row
already storing only `Soundtrack` therefore kept that list when the new
aggregation found `alternative rock`/`rock`; the markers were unchanged, so the
write was skipped. The persistence check now also compares the MUSICAL set.
If selection produced no musical genre, the stage falls back to the strongest
raw provider genres before appending markers, so a marker cannot be the whole
final genre list by itself.

## Double-click / duplicate scan starts

Hypercorn runs four workers, and the runtime registry is per process. Two clicks
routed to different workers both passed the in-process and DB checks before
either wrote the shared progress row, so both album pipelines ran (both logged
`Step 1/3: Navidrome import`).

A cross-process file lock (`services/scanning/scan_start_lock.py`) is now
acquired before a scan thread starts and released by that thread's `finally`.
It covers the popularity/full, album, artist, library, and Navidrome start
routes. A second click gets “A scan is already running”; the OS releases the
lock automatically if a worker dies.

## Remix album with a plain recording title

"Take a Look Around" on Limp Bizkit's *New Old Songs* has a plain release and
recording title, so `resolve_performance_title()` could not see a version
marker. Last.fm then matched the studio hit / the primary artist's catalogue
and reported 1.28M listeners for a remix whose recording credits E-40 and
8-Ball.

`track_stage` now raises `_alt_rendition` when the ALBUM type is
`album+remix`. Every Last.fm path uses that flag, so a plain studio recording
cannot satisfy the request and contributor catalogues cannot substitute the
original artist's track.

## Queue/search activity across worker processes

Queue events were kept in a per-process in-memory list. The standalone queue
worker wrote them in its own process, while the web worker serving
`/api/queue/events` had an empty list (and its log fallback did not include
`activity.log`). The queue and search viewers now fall back to the shared
`activity.log`, and the search fallback filters it to `[ACTIVITY] [SEARCH]` /
`[MANUAL]` lines. Scheduler startup also no longer logs a second
"Download scheduler started" when the same process calls it twice.

## Upcoming releases only showing a couple of albums

MusicBrainz release-group search returns **year precision** for most future
releases (`"2026"`). The fetcher expanded that to `"2026-01-01"` before storing
it, so the page's September→March window treated those rows as past January
releases. Only the few with exact day dates survived. The stored value now
keeps the precision MusicBrainz supplied; the route already handles a 4-digit
release date in its year branch.

## Config: forced intervals, genre scan, finalise invalidation

New Scan Behaviour settings (both Config trees):

* `genre_skip_days` (default 7) — the Genres scan window.
* `forced_finalise_skip_days` (default 0) — a forced finalise still skips an
  album finalised within this many days.
* `forced_singles_skip_days` (default 0) — the same protection for a forced
  singles pass.

A new **Genres** scan type refreshes provider genre sources without running
popularity/singles/finalise. It retries any online source column that is still
empty (MusicBrainz, Discogs, ListenBrainz, Last.fm) and forces such albums
through the completeness gate.

When the track stage changes a genre, popularity count/score, cover/live or
release-year field, it now persists `stars_reconciled = FALSE`. The next
finalise/rating pass cannot skip that album as unchanged, and finalise sets the
flag back to TRUE once it has written the resolved ratings/tags.

## Advanced Full Scan stages

The scanner sheet now exposes per-run stage tickboxes for **Popularity**,
**Singles**, **Covers**, **Genres**, and **Essentia**. A ticked stage is fully
FORCED; an unticked stage still fills missing data but reuses anything already
stored. `stage_selection` marks that per-stage intent, so an older client that
sends no such marker keeps the previous force-everything behaviour. The flags
flow through library, artist, album, and targeted full scans.

The live `static/js/ui/scan-modal.js` was missing the `stageOptions()` helper
that the rebuilt copy already had, so `run()` threw a `ReferenceError` before
sending the request and the tickboxes never reached the server. The helper is
now present in the live tree and sends `force_*` flags instead of hard `skip_*`
flags.

With Singles unticked, Force no longer bypasses the singles freshness cache:
a track with a stored verdict reuses it, while a track with no verdict still
gets detected. "Stored" here means presence of a verdict, not merely a
still-fresh timestamp, so an old verdict is not re-fetched either. The same
fill-only presence rule applies to Popularity, Covers, Genres, and Essentia.

## Finalise fills missing popularity

A Finalise pass normally reuses stored scores. If either provider count is zero
(Last.fm or ListenBrainz), the album is no longer skipped: `track_stage` runs
the popularity lookup for the missing source while still reusing any present
stored score. Albums with both counts present skip the scoring step as before.

## Genre list order and family outliers

`Baby One More Time` was finalising as `Alternative Rock, Ambient, Contemporary
R&B` from provider lists that also contained pop, dance-pop, synth-pop, electro,
europop and teen pop.

Two causes:

- MusicBrainz/ListenBrainz return genre arrays in **alphabetical** order, and the
  scorer treated that position as a popularity ranking. `alternative rock` and
  `ambient` therefore received rank-1/rank-2 weight solely because they sort
  before `pop`.
- A lone family could still take a slot on specificity even when the rest of the
  evidence was a large pop/electronic cluster.
- Worse, `pop` was the highest-scoring candidate but ancestor condensation
  deleted it because a weaker `synthpop` (76% of Pop's score) also existed.
  That promoted `alternative rock`, `ambient` and `contemporary R&B` into the
  three slots.

Alphabetically sorted lists now contribute every tag at equal rank weight,
family outliers are removed before diversity is enforced, and only the
**selected** set is condensed. A descendant must reach 80% of its ancestor's
score before replacing it, so a weaker `synthpop` no longer deletes `pop`.

MusicBrainz and ListenBrainz genre/tag vote counts are now pulled through
(`pop 14`, `dance-pop 4`, `alternative rock 2`). The provider names are sorted
by count before storage, so the count drives the rank instead of the API's
alphabetical order. If a release group has no `genres` array, its scored
`tags` array is used as the fallback.

## Release format preference when linking an album

When several releases in a group have the same tracklist, the best-release
scorer and the release picker now prefer:

1. Digital Media
2. CD
3. Vinyl
4. other formats

Track count still dominates the score, so a complete edition always beats an
incomplete one. The preference is only a tie-break between equivalent
tracklists.

## My Heart Will Go On

The title search preferred a writer-verified recording over an earlier
performed one. Céline Dion's 1997 recording often has no writer relation on the
recording, while James Horner's 2004 score recording does, so the composer won.
`_find_original_recording` now compares years first and uses writer
verification only as a tie-break at the same date.



