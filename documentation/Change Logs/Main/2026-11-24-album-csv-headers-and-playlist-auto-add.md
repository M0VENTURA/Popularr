# Album CSV tracklist: correct headers + Exportify support; imported tracks auto-add to their playlist

**Date:** 2026-11-24
**Area:** `services/metadata/album_missing_service.py` ·
`services/playlists/playlist_pending_additions_service.py` ·
`services/playlists/playlist_navidrome_service.py` ·
`services/queue/queue_orchestrator.py` · `db/repositories/queue.py` ·
`test_site/templates/Pages/album_detail.html` ·
`test_site/templates/components/search/_playlists.html` ·
`test_site/static/js/features/csv-import.js`
**Commit:** `fix(album): correct CSV tracklist headers; auto-add imported tracks to their playlist`

## Reported

> "Can you confirm the best layout for the csv here? And update the album page to show the
> required csv headers and the csv items that can be added. Such as Track Artist, Album
> Artist, Track Name, Length, etc. …
>
> Could this be added to a column on the download queue so when the file is matched, it's
> added to that playlist?"

## 1. The best CSV layout (and the bug behind the ask)

`_parse_csv_tracklist` maps columns by NAME (order-free) or accepts a positional
row. The header aliases were narrow, and critically **`Track Name` — Exportify's
own title header, and the one a user is most likely to have — mapped to nothing**.
The header row was still detected (another cell matched), so every row lost its
title, was skipped, and the import returned `success: true, imported: 0` with **no
error**. An Exportify CSV silently did nothing on the album page.

**Best layout — a short header row, any column order:**

```csv
Disc Number,Track Number,Title,Artist,Length,Year
1,3,Never Tear Us Apart,INXS,3:52,1987
```

**Required:** `Title` (also `Track Name`, `Name`, `Song`, `Song Name`, `Track Title`).
**Optional:** `Artist`/`Track Artist`/`Artist Name(s)` (track artist), `Track Number`,
`Disc Number`, `Length`/`Duration`/`Time` (`3:52` or seconds), `Year`.
**Ignored on purpose:** `Album Name` / `Album Artist` / `ISRC` — the album identity is
the page's artist+album, not a per-row value.

Fixes:
* added the missing aliases (`Track Name`, `Song Name`, `Track`, `Artist Name(s)`,
  `Disc No`, …), so an Exportify CSV now imports its rows.
* `Duration (ms)` is deliberately **not** mapped: the album tracklist stores
  SECONDS, and mapping ms would store 184586 s. A null length is honest.
* the album page hint now lists the required header and every supported column,
  and says album/album-artist/ISRC are ignored.

## 2. Imported tracks auto-add to the playlist they were queued for

The queue row cannot carry the song id: at import time a brand-new track's
`tracks.id` is a placeholder (`dlimport-…`) until the **Navidrome import adopts
it** to the real Subsonic id. And a playlist write is DEFERRED while a Navidrome
import runs (it would silently no-op). So the addition is applied later, by a
maintenance sweep.

Flow:
1. **Download Missing** (Exportify panel) queues each missing track with
   `metadata.playlist_target` (+ `playlist_user`).
2. New maintenance hook `playlist_pending_additions_service.apply_pending_playlist_additions`
   (registered in `MAINTENANCE_CANDIDATES`):
   * returns early if a Navidrome import is running (retry next cycle);
   * selects queue rows that landed (`imported`/`completed`/`in_collection`) and
     still carry a playlist intent;
   * resolves the REAL `tracks.id` by `file_path`; skips `dlimport-…` (not adopted yet);
   * appends the id to the named playlist **idempotently** and clears the intent.

`playlist_navidrome_service.add_track_to_playlist_by_name(client, name, song_id)`
reads the current song ids first and appends — it never passes `[song_id]` alone,
because `sync_playlist_by_name` REPLACES the list (a single-id call would wipe the
playlist down to one track).

Net user behaviour: create the playlist, press Download Missing, and the tracks
join it automatically once they download, import, and are indexed by Navidrome.
If the playlist does not exist yet, the intent stays pending until it does.

`metadata` was added to `UPDATE_ALLOWED_COLUMNS` so the intent can be cleared.

## Tests

* `tests/test_album_tracklist_csv_headers.py` — `Track Name` and the other title
  aliases; Artist/Number/Disc/Length/Year land in the track list; `Duration (ms)`
  is not misread as seconds; album/album-artist tolerated; positional still works.
* `tests/test_playlist_pending_additions.py` — the append helper never duplicates
  and never replaces the list; the sweep adds a landed track and clears the
  intent, retries a provisional `dlimport-` id, skips during an import, and keeps
  the intent when the playlist does not exist yet.
