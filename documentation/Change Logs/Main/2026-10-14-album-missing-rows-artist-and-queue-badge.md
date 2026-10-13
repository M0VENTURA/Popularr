# Album missing rows: show the track artist, and label the queue state clearly (2026-10-14)

## Two reported requests
> Can missing release items also show the track artist in the list (if
> different than the album artist)? Also, instead of showing imported, can it
> make it obvious that it's in the download queue.

The album page's tracklist merges the library rows with the **missing** rows
built by `buildMissingRow` / `_buildMissingTrackRow`. The library rows render
`Title — Artist` (the artist only when it differs from the album artist:
`album_detail.html` `{% if track.artist and track.artist != artist_name %}`),
but the missing rows rendered only the title plus a status badge.

## 1. Show the track artist on missing rows
Both trees now render a muted `— <track artist>` after the missing track's
title, using the SAME rule as the library rows: shown only when the performer
differs from the album artist. Source is `track_artist || mb_artist` (the
track's own credit — never the album artist).

Why it matters most on compilations: the album artist is "Various Artists", so
the missing rows previously gave the user no idea who performs a track before
queuing it. Now the row reads `Forgotten Years — Midnight Oil` beside library
rows that already show their performer.

## 2. The badge says the DOWNLOAD QUEUE owns the track
The badge echoed the raw DB status title-cased, so a queue row with status
`imported` rendered as **"Imported"** — which on a *missing* row reads as "this
track is already in the library, why is it listed as missing?" and hides the
fact the download queue is what is handling it.

`queueStatusLabel` / `_queueStatusLabel` now maps each queue stage to a
queue-oriented phrase, and the badge carries a `bi-download` icon:

| queue status | badge label |
|---|---|
| `downloading` / `processing` / `moving` | Downloading |
| `searching` | Searching |
| `failed` / `backed_off` | Retrying |
| `queued` / `pending` / `pending_match` / `matched` | Queued |
| everything else incl. **`imported`** / `completed` | **In download queue** |

The exact raw status stays in the badge's `title` tooltip, so no information is
lost. (The album's missing list is a snapshot, so a track the queue has already
fetched can legitimately still appear here — the badge's job is to say the
queue owns it.)

## Applied to both trees
`test_site/static/js/pages/album.js` (`buildMissingRow`) and
`static/js/album_detail.js` (`_buildMissingTrackRow`).

## Tests
`tests/test_missing_track_queue_state_ui.py` — the existing node harness gained
three discriminating cases (an `imported` row must NOT show "Imported" but
reveal "In download queue"; a differing track artist is shown; an equal one is
suppressed) and the stub now sets `window._pageData` so the album artist is
resolvable (the modules read `_pageData`, not the standalone `pageArtist`
global, so the "differs from the album artist" branch was previously
unreachable in the harness).

60 missing-row tests pass. Album/missing suite sweep: 42 failures — **all
pre-existing** (verified against a pristine `origin/develop` worktree: 27 fail
at base in the same 4 files; they are backend star-posting / release-category
tests untouched by this change). `node --check` clean on both JS files.