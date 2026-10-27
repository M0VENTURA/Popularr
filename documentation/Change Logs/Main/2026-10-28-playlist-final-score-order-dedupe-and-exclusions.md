# Playlist ordering by final score, artist-aware dedupe, and track-artist exclusion

**Date:** 2026-10-28
**Area:** `services/popularity/stages/finalise_stage.py`,
`helpers/config_helpers.py`, Config page, generated playlists

## Report

> The playlist creation for Essential Artist playlists, should be ordered by 5
> star first, followed by 4* and the ordering of each of the 5 and 4 star
> sections should be done via the final score rating and left in score order.
>
> For the Genre playlists, they should be organised in a similar way. They
> should be ordered by 5*, then 4* first to get the top 300, then randomised
> when creating the playlist. The ordering is mostly needed to make sure that
> the top songs are selected for the top 300.
>
> Both playlists should have deduplication so the same track isnt displayed
> twice, making sure to check track artist as well as album artist for the dedup.
>
> Also make sure that if an artist is listed in the config to ignore from
> playlist generation, that it also takes into account the track artist. I've
> seen some tracks from compilations making it through.

## 1. Ordering: star tier, then FINAL SCORE

`_playlist_order_key` already led with the star tier; the second key came from
`_effective_order_score`, which under the previous default (`prominence`) was
the raw Last.fm/ListenBrainz listener count.

A new `final_score` mode now reads the track's stored `final_score` — the
number the track page displays as **Score** — and is the **new default**:

| mode | second sort key |
|---|---|
| `final_score` (default) | `COALESCE(final_score, popularity)` |
| `popularity` | raw listener counts (the old `prominence`) |
| `stored` | album/artist-relative `popularity_score` |

`prominence` remains accepted as a legacy value and resolves to
`final_score`: it was the value the *previous default* wrote to `config.yaml`,
so an existing config would otherwise silently keep the listener ordering the
change replaces. Anyone who deliberately wants listener ordering now selects
"Global popularity", which persists `popularity`.

All playlist SELECTs now fetch `final_score` explicitly
(`_GENRE_ROWS_SQL`, `_GENRE_ALBUM_ROWS_SQL`, `_CURATED_ROWS_SQL`, the Essential
and featured-track queries), and the genre pool dict carries it through.

**Genre Top Tracks:** the ranking (5★ → 4★ → final score) now selects with
`interleave=False` so the `max_tracks` slice (the default 300) is taken from
the PURE ranking. The artist spread runs afterwards, so it can only reorder the
survivors, never displace a stronger track from the top 300. The weekly
rotation still runs last (after the slice), so the playlist order is randomised
without changing membership.

**Essential Collection:** unchanged pipeline, now ranking each star section by
final score. Its family rotation switch remains off by default, so the list is
left in score order.

## 2. Dedupe checks BOTH artist fields

The Essential builder grouped on the normalised **title only**; the genre
builder grouped on `(track artist, title)`. Neither could recognise every copy
of a recording, and the title-only key could merge unrelated songs.

`_group_duplicate_tracks()` is now the single grouping helper. Two rows are the
same track when the titles match AND **either** the track artist or the album
artist matches, or when they share a track id. Guest credits are stripped from
the track artist before keying, and a placeholder album artist ("Various
Artists", "Soundtrack", …) is ignored as an identity — otherwise every
compilation row would collapse into one.

Grouping is transitive (union-find), so an original, a compilation copy and a
feature-credit variant all resolve to one entry.

## 3. Exclusion checks the TRACK artist too

The genre, New Music and curated filters compared only
`COALESCE(album_artist, artist)` — so a compilation row carrying
"Various Artists" in `album_artist` slipped past a user exclusion for the
performer in `artist`.

`_row_artist_is_excluded()` checks `artist`, `track_artist` and
`album_artist`, plus the guest-stripped form of each, and is used by:

- `_create_genre_top_track_playlists`
- `_sync_essential_playlist` (new — featured tracks could otherwise pull an
  excluded guest into another artist's collection)
- `_create_new_music_playlist`
- `_fetch_curated_playlist_rows`

The New Music query now selects `artist AS track_artist` alongside the album
artist it already used, so the check sees both.

## Config page

Both template trees now offer:

| value | label |
|---|---|
| `final_score` | Final score (recommended) |
| `popularity` | Global popularity |
| `stored` | Stored score (album-relative) |

The markup default, the page-script default and
`config_helpers.get_playlists_config()` all agree on `final_score`.

## Tests

- `tests/test_playlist_ordering.py` — final-score ranking inside a star tier,
  listener counts ignored in that mode, the default/legacy mapping, and the
  Config-page surface.
- `tests/test_playlist_dedupe_and_exclusions.py` (new) — compilation copies,
  guest credits, track-id duplicates, placeholder album artists, unrelated
  same-title songs, and exclusion via every artist field.
