# Genre playlists now follow the track's real genres, three are written, order rotates weekly

**Date:** 2026-10-26
**Area:** `services/popularity/stages/finalise_stage.py` ·
`services/enrichment/genre_aggregation_service.py` ·
`services/popularity/stages/track_stage.py` · `helpers/config_helpers.py` ·
both `config.html` trees · both `config.js`
**Commit:** `fix(genre): pool playlists from the final genres, write three, rotate weekly`

## Requests

> "I had an offspring one that was originally set as Alternative Metal, but it
> was adjusted to remove Alternative Metal, Navidrome doesn't show Alternative
> Metal, but it's still on the Alternative Metal Playlist."
>
> "The finalisation scan should select the top 3 genres from the most used
> across sources, then apply those to the metadata for the tracks. The genre
> playlists should be loaded from those 3 genres. Cover, Live, Christmas or
> Holiday Genres should be allowed on top of the 3 as extra genres."
>
> "it could be useful to shuffle them once a week too so it's not always the
> same order of tracks."

## 1. The playlist pooled by provider columns, not by the track's genres

`_resolve_playlist_genres` built the pool from `lastfm_tags`,
`musicbrainz_genres`, `discogs_genres`, … — columns that keep whatever the
provider originally returned. A genre removed from a track's metadata (by the
selection engine, a manual correction or a re-scan) therefore lived on in those
columns and kept the track in a playlist the displayed `genres` — and Navidrome —
no longer claimed.

Now the pool comes from the track's FINAL `genres` value, falling back to the
provider columns only when that value is empty (so a never-aggregated track is
not silently dropped from every playlist). **One value, one answer**: the
playlist agrees with what the metadata and Navidrome show.

New `genre_aggregation_service.playlist_pool_genres()` / `is_playlist_pool_genre()`.

## 2. Three musical genres, with the markers on top

The write paths passed `max_genres=2` — **overriding the configured ceiling**.
`genres.selection.max_genres` is already on the Config page ("Maximum Genres",
default 3); nothing honoured it. `track_stage` (the per-track write),
`get_track_recommendations` (the album blend) and
`sync_various_artists_track_genres` (the VA path) now all use the configured
value via the new `get_genre_track_limit()`, defaulting to 3.

`_append_extra_genres` already appends Cover / Live / Acoustic / Remaster /
Christmas / Holiday **after** the selected genres, so those markers are extras
on top of the three and never consume a slot. Verified by test.

⚠️ The pooling helper deliberately excludes only PERFORMANCE markers
(Live / Acoustic / Unplugged / Cover / Remix / Remaster / Instrumental) plus the
holiday family. It is narrower than `_TRACK_AFFILIATION_FILTER_TAGS`, which is
derived from `_FILTER_TAGS` and also contains Comedy / Orchestral / Symphonic /
Soundtrack — those are real enough to keep their own playlists.

⭐ A purely seasonal track has no musical pool key (the holiday token is dropped
as an extra), so the builder now seeds it into the Holiday pool when
`is_christmas_track` is true. Without that, a track whose only genre was
"Christmas" would have vanished from every playlist.

## 3. Weekly rotation

`_weekly_shuffle(rows, playlist_key, enabled, when)` reorders a finished
playlist once per ISO week, seeded on `"<year>-W<week>::<playlist name>"`.
Applied AFTER ranking, the per-artist quota, interleaving and the `max_tracks`
slice — so **membership never changes, only the sequence**:

* the content still only changes when the library does, so the zero-request sync
  still skips an unchanged week;
* the order changes once a week, costing one rewrite a week rather than one per
  scan.

Shuffling before the slice would let the cap drop genuinely top tracks at
random; a test asserts the call sits after it in all three builders. The seed is
a STRING handed to `random.Random`, which derives its state from a SHA-512 of the
text — identical in every process and after every restart, and no two playlists
share an order by accident.

Config: `playlists.weekly_shuffle_enabled` (default **true**), on the Config page
and in the page script in BOTH trees.

## 4. Tree parity

`test_site` had been the source of the migrated UI, so the two Config pages were
diffed id-for-id. Two remaining structural differences, both reported rather
than changed:

* `searchExperienceBody` — live wraps the Search Experience card in a collapse.
* `feature_use_test_site` — the Live/Rebuilt selector exists only in
  `test_site`, so from the live Config page there is no switch back.

## Verification

`tests/test_genre_pooling_and_weekly_rotation.py` (41), plus
`tests/test_genre_family_tiebreak.py` (rewritten control).

Oracle — source reverted with the tests present: **32 failed / 9 passed**; the 9
that pass are guards that must hold either way. Restored → 41 passed.

`test_genre_family_tiebreak.py::TestTheRefreshAgreesWithTheBuilder` held a
control asserting the two resolvers **disagreed** — i.e. it encoded the defect as
a property. Inverted with the reversal documented, and the one remaining
legitimate asymmetry (empty `genres` → provider fallback) is now its own test.

Regression sweep, 97 genre/playlist/tag/config/template suites, one process per
file: baseline **91** failing ids vs patched **90**, `Compare-Object` NEW
**empty**, and one pre-existing failure FIXED (a stale
`_genre_playlist_track_genres(row` assertion). `import app` → 425 routes; both
page scripts pass `node --check`.
