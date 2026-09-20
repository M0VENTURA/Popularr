# Album-release recording identity (the "Helden X Hymnen" mismatch)

**Date:** 2026-09-20 · **Area:** popularity / enrichment / matching

## Reported

"Still not correctly matching this album with the MusicBrainz release."
dArtagnan — *Helden X Hymnen* (2026-07-24). The album **was** correctly linked:
`musicbrainz_album_mbid = b01f7815-17fd-4239-9246-a817ef105aff`, and the log
shows the album stage reading that release for its extended metadata. The
mismatch was a level down, in the tracks.

That release's own tracklist marks FOUR of its sixteen tracks as unplugged
renditions:

| # | Release title | Recording |
|---|---|---|
| 13 | Für immer Dein (Unplugged Version) | `3c76f8e9-…` |
| 14 | Herzblut [Unplugged Version] | `2402e085-…` |
| 15 | Helden X Hymnen (Unplugged Version) | `0dfb1b40-…` |
| 16 | Farewell [Unplugged Version] | `756e6139-…` |

Three of those four library titles had lost the marker ("Für immer Dein",
"Helden X Hymnen", "Farewell (feat. Patty Gurdy)"). Two rows also share the
title "Helden X Hymnen" — the album's title track at position 1 and its
unplugged rendition at position 15.

Evidence from the scan log:

* `[MB] recording suggestion completed mbid='3b8b3a70-…' track='Für immer Dein'`
  — the **studio** recording, not `3c76f8e9-…`.
* `[TRACK] "Farewell (feat. Patty Gurdy)" | Score: 90.4 (LF: 7.5k)` — the studio
  single's catalogue-wide Last.fm listeners on an album whose other tracks sit
  at 300–500, which locked it as a global 5★.
* Two rows named "Helden X Hymnen" reported the **same** score — both had been
  resolved to the same (studio) recording.

## Cause

Three defects, all in the same chain:

1. **The resolved identity was thrown away.** `get_listenbrainz_album_tracklist_with_release`
   already resolves the album's release and matches its tracklist onto the
   library — by normalised title, or by `(disc, position)` behind a ±5 s
   duration guard. But it only emitted an entry when `total > 0`, i.e. when the
   recording had ListenBrainz listens. A recording nobody has scrobbled yet
   (exactly these unplugged takes) lost its identity, so `track_stage` fell
   through to the ambiguous title+artist SEARCH. The same discard applied to the
   runner's `recording_mbid` propagation.
2. **The per-album batch was dead.** The runner built `options["mb_batch_metadata"]`
   from `search_releases(clean_album)` — RELEASE-shaped entries keyed by ALBUM
   title — while `track_stage` looks a batch entry up by `"artist::track title"`.
   It could never match, so every track paid for a recording search, a recording
   fetch **and** a composer fetch instead of taking the batch path.
3. **A title key cannot identify a row.** The identity map was keyed by
   normalised title, so the two "Helden X Hymnen" rows collapsed onto whichever
   the loop reached last — which is why both reported an identical score.

The single-titled rows were additionally exposed to the alternate-performance
blind spot: `_ALTERNATE_PERFORMANCE_RE` only matched **parentheses**, so
MusicBrainz's own square-bracket convention (`Farewell [Unplugged Version]`)
never registered as an alternate performance.

## Fix

**The album's own MusicBrainz release is now the authority for which recording
each of its tracks is** — MBID-first, applied per track.

* `services/popularity/popularity_sources.py`
  * The position match now emits the recording **identity** regardless of the
    listen count (counts simply come out as zero), and a failed count lookup no
    longer discards it either.
  * Each matched row also publishes its own recording under a position-qualified
    alias (`track_identity_key`) carrying the release's own title. The
    title-keyed entries keep their existing meaning, so listen-count behaviour is
    unchanged for every current consumer.
  * New `album_recording_batch_key` — one definition of the batch key format,
    shared by producer and consumer so they cannot drift.
  * `_ALTERNATE_PERFORMANCE_RE` accepts square brackets as well as parentheses.
  * `get_aggregated_lastfm_popularity` / `get_search_aggregated_lastfm_popularity`
    gained `target_is_alt_rendition`: the same "do not merge a differently-typed
    candidate" guard `is_live_release` already provided, for an unplugged/acoustic
    take whose library title lost its marker. It is deliberately **not** folded
    into `is_live_release`, because liveness also drives the live weight penalty
    and the live star caps, which a studio unplugged take must not incur.
* `services/popularity/scan_stage_runner.py`
  * New `_build_album_recording_batch()` builds the per-track identity from the
    release tracklist (reusing the album-tracklist pass's position + duration
    match), fetches each recording's metadata in **bulk** so nothing the search
    would have produced is lost (writer, genres, ISRC, release identity), and keys
    each entry by row (`artist::title::disc::track`) with the legacy title-only
    key written **only when the title identifies exactly one row**.
  * The album-tracklist pass is now also demanded when a track's identity is
    unknown, so a rescan cannot leave the album on a previously searched
    recording.
* `services/popularity/stages/track_stage.py`
  * The batch is consulted **before** the "already fully resolved" short-circuit,
    so a stored MBID that is not the recording the album's release puts at that
    track is corrected (logged as `[MB] album-release recording identity applied`).
  * Row-specific batch keys are tried before the legacy title-only form.
  * A batch hit performs no MusicBrainz work at all.
  * The release/supplied title is used for the alternate-rendition flag passed to
    the Last.fm arms.

## Verification

`tests/test_album_release_track_identity.py` (new, 17 tests) builds the real
MusicBrainz release payload for `b01f7815-…` and the library as the log shows it.

| Tree | Result |
|---|---|
| Fix reverted (restore + test only) | **14 failed / 3 passed** — naming the missing release title in the index, the identity dropped at zero listens, same-titled rows collapsing onto one recording, square-bracket titles not recognised, the missing batch builder, and the missing MBID correction |
| Fix applied | **17 passed** |

Regression check — 16 related suites (`album_title_and_version_matching`,
`live_album_recording_resolution`, `compilation_tracklist_guard`,
`metadata_update_album_split`, `album_type_persistence`, `popularity_math`,
`album_genre_fallback`, `track_genre_navidrome_fallback`,
`genre_backslash_splitting`, `per_album_star_posting`,
`single_detection_title_track`, `album_musicbrainz_matching`,
`album_release_picker_contract`, `mb_batch_version_resolution`,
`scan_stall_fixes`, `track_sync_and_live_detection`):

| Tree | Result |
|---|---|
| Before | 60 failed / 225 passed |
| After | 60 failed / 225 passed |

**Identical failure lists — 0 new.** (Those 60 are the pre-existing
environment/fixture failures of this test host.)

Also removed: `MusicBrainzHttpClient` was left imported in `scan_stage_runner`
by the dead `search_releases(album)` call and is no longer used.
