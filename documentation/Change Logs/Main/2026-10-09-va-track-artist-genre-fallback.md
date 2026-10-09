# A VA track with no genres falls back to its PERFORMER's genres

**Date:** 2026-10-09 - **Area:** genre aggregation / various-artists scans
**Requested:**

> When scanning a various artists collection, if the track has no genre data
> online, can it fall back to using the genres for the track artist? If on the
> local db it can be grabbed from that artist or looked up on Musicbrainz,
> discogs and last.fm

## What happened before

`sync_various_artists_track_genres` (2026-10-05) gives each VA track the genres
its **own** source columns rate, and **skips** a track whose source map is
empty — its Navidrome value survived rather than being blanked. That skip is
right, but on a compilation the Navidrome value is often the disc tagger's
guess or nothing at all, and the one thing that IS known is the performer.

## The fallback

New in `services/enrichment/genre_aggregation_service.py`:

**`fallback_genres_for_artist(artist, *, exclude_track_id, max_genres=2)`**

| tier | source | cost |
|---|---|---|
| 1 | the artist's **other rows** in `tracks` (already-curated `genres`) | one local query |
| 1 | `artists.lastfm_artist_tags` — the per-artist Last.fm cache a scan of that artist's own music fills | one local query |
| 2 | **MusicBrainz** — `search_artists` (exact-anchored) -> `get_artist(inc="genres")` | 2 requests @ 1 req/s |
| 2 | **Discogs** — an artist-only query is an artist-level lookup | 1 search |
| 2 | **Last.fm** — `get_artist_top_tags(limit=15)` | 1 call |

**The local tier SHORT-CIRCUITS the network**: when the artist is already in the
library, MusicBrainz / Discogs / Last.fm are not contacted at all (pinned by a
test — it is the whole point of "if on the local db it can be grabbed").

**`_merge_artist_genre_sources(sources, max_genres)`** ranks the candidates
(pure, no DB, no network):

* source **priority** — library -> MusicBrainz (0.40) -> Discogs (0.25) ->
  Last.fm (0.10), i.e. the configured `genres.weights` used as an ORDER;
* junk/admin tags dropped (years, `seen live`, filter tags) with the same
  helpers the main vote uses;
* deduplicated by `normalize_genre_for_vote`, so `Hip Hop` / `hip-hop` is one
  genre;
* capped at 2.

### Why `genres.min_weight` is not applied here

That rule (0.25) exists to stop a lone weak **track** tag defining a track.
An artist's OWN top tags are the artist's genre consensus, and the fallback only
runs when the track has nothing whatever — thresholding it would mean a
Last.fm-only artist (0.10) gets no fallback, i.e. the request would fail for
exactly the compilations it was written for. The weight is the order instead.
Pinned as its own test (`test_a_lastfm_only_artist_still_gets_a_fallback`).

### Guardrails

* a **placeholder** performer (`is_track_artist_placeholder` -> "Various
  Artists", "Unknown Artist", ...) is NOT looked up;
* an empty performer is not looked up;
* the fallback result is written **per track by id** (never album-wide — the
  rule a compilation depends on), and a row already holding the value is not
  rewritten (`_same_genre_value`, so no file-tag churn);
* when the fallback finds nothing, the row keeps its Navidrome value — no track
  is ever blanked;
* the lookups are **memoised per artist** for the process lifetime (a VA album
  can hold 20 performers, each visited once per scan), and **a MISS is not
  cached** — an artist whose data appears later must be able to answer;
* Last.fm tags are written into `artists.lastfm_artist_tags` **only when that
  column is empty** (a populated column is this app's cache-hit signal), which
  makes the fallback converge: the next scan answers from the local tier.

## Tests

`tests/test_va_track_artist_genre_fallback.py` — **23**:

* ranking: library wins, MusicBrainz > Discogs > Last.fm, cap, junk/admin
  dropped, spelling variant kept once, nothing -> nothing;
* lookup order: local short-circuits the network; the three online sources run
  only when local is empty; a Last.fm-only artist still resolves; a placeholder
  / empty performer is never looked up (plus a real-artist control);
* memo: the same artist is resolved once (case-insensitively); a miss is NOT
  cached;
* the Last.fm cache: written only into an empty column, nothing written without
  tags, a DB failure cannot break the fallback;
* the sync: own-source tracks still use `aggregate_genres` (control) and never
  reach the fallback; a source-less track takes its artist's genres; the write
  is per track id; an empty fallback writes nothing; a track with no artist is
  untouched; an unchanged value is not rewritten.

**Oracle** — `genre_aggregation_service.py` stashed (symbols absent, so the new
suite errors at collection): **23 errors** -> restored **23 passed**.

**Sweep** — 51 test files touching `genre_aggregation` / `aggregate_genres` /
`sync_various_artists` / `album_tag_sync` / genre columns, run **one process per
file** (running them together hits a known Windows access violation between a
background scan worker and `conftest`'s schema recreation): baseline **44** ->
changed **44**, regressions **(none)**.

`ast.parse` clean.

## Files

- `services/enrichment/genre_aggregation_service.py`
- `tests/test_va_track_artist_genre_fallback.py` (new)
