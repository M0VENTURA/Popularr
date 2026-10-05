# Various Artists tracks take their genres from MusicBrainz/Last.fm, not Navidrome (2026-10-05)

**Report:**

> I want the tracks on Various Artists collections to get the genres from
> Musicbrainz, Last.fm, etc for the tracks rather than using the Navidrome
> genres.

## What was actually happening on a VA album

1. **The import writes Navidrome's value.** `payload_builder.py:260` sets
   `genres = navidrome_genres` — the audio file's own genre tag, per track.
2. **The album-level overwrite is deliberately skipped** for VA albums
   (`sync_album_file_tags`) — correctly, because it writes *one* blended value
   to every track of the album (`WHERE … AND album = :alb`), which on a
   compilation destroys each performer's genre. That skip is itself a previous
   fix; its docstring records the reported symptom ("every track on a VA album
   ended up carrying the same value").
3. **Nothing replaced it.** Grepping every `UPDATE tracks SET genres` in the
   repo finds only: the album-wide one (skipped for VA), a manual artist-page
   action, the album-save path, and two functions — `sync_confident_genres()`
   and `enrich_genres_aggressively()` — with **zero callers**.

So a VA track's `genres` was frozen at whatever Navidrome had, while the scan
happily populated `musicbrainz_genres`, `lastfm_genres`, `discogs_genres`,
`listenbrainz_genres`, `spotify_genres`, `essentia_genres`, `audiodb_genres`
and `wikidata_genres` **right next to it**.

## Changes

New in `services/enrichment/genre_aggregation_service.py`:

- **`va_track_source_map(track)`** (pure) — the track's own source columns,
  **excluding `navidrome`**, parsed into the vocabulary
  `aggregate_genres()` weighs (`musicbrainz`, `discogs`, `audiodb`, `essentia`,
  `listenbrainz`, `lastfm`, `spotify`, `wikidata`, `manual`).
- **`sync_various_artists_track_genres(tracks, album)`** — for **each track
  individually**:
  - `aggregate_genres(source_map, max_genres=2, context_title=…, context_album=…, nav_genres=None)`;
  - `nav_genres=None` on purpose: Navidrome normally acts as the tie-breaker,
    and the request excludes it from this album entirely;
  - writes `UPDATE tracks SET genres = … WHERE id = :track_id` — by id, never
    album-wide;
  - **skips** a track with no online sources (keeps its Navidrome value rather
    than wiping it to blank) and **skips** a track whose value already matches
    (no pointless write, no file-tag churn).

Called from the VA branch of `sync_album_file_tags`, which previously only
logged "Skipped". The non-VA branch is untouched — a single-artist album still
shares one genre list.

### A limit worth knowing

`aggregate_genres` rejects anything below `genres.min_weight` (default **0.25**):

| source | weight | clears alone? |
|---|---|---|
| musicbrainz | 0.40 | ✅ |
| manual | 0.30 | ✅ |
| discogs | 0.25 | ✅ |
| essentia / audiodb | 0.20 | ❌ |
| listenbrainz | 0.15 | ❌ |
| lastfm | 0.10 | ❌ |
| spotify | 0.05 | ❌ |

So a track whose **only** online source is Last.fm, ListenBrainz or Spotify
gets no verdict and keeps its Navidrome genres — deliberately, because one weak
signal should not define a track's genre, and that rule is shared with every
other genre path in the app. The common case is unaffected: the scan writes
`musicbrainz_genres` for every recording, and MusicBrainz alone clears the bar.
**Combined** sources always clear it (Last.fm + MusicBrainz, Last.fm + Discogs,
etc.).

### Spelling variants are not changes

The "already matches" skip uses `_genre_sets_equal`, which ignores case and
whitespace but not punctuation — so MusicBrainz's `hip-hop` read as a change
against a stored `Hip Hop` and the track was written (database row *and*
physical file tags, per the fan-out rule). The comparison now falls back to
names reduced to bare letters and digits, so `hip hop`, `Hip-Hop` and `hiphop`
are one genre. The genres still have to **match**: a genuinely different genre
still writes, which a control test pins so the tolerance cannot quietly
disable the sync.

## Tests

`tests/test_va_track_genres.py` — 21 tests:

- `va_track_source_map` excludes Navidrome, carries every online source, drops
  blanks, returns `{}` for a Navidrome-only track, and parses both JSONB and
  CSV values;
- the write is per track (`WHERE id = :track_id`, never `album = :alb`), the
  aggregator is called with `nav_genres=None` and the track's own title as
  context, Navidrome-only tracks are left alone, unchanged tracks are not
  rewritten, and an empty aggregation writes nothing;
- the min-weight limit above is pinned so a future change to it is visible;
- the scan actually calls it (the VA branch previously only logged) and the
  non-VA album-wide write still exists (control).
