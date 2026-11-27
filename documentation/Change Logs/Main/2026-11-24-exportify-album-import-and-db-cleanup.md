# Exportify: import as album, download missing, columns guide; safe DB cleanup

**Date:** 2026-11-24
**Area:** `routes/playlists.py` · `routes/maintenance_routes.py` ·
`services/maintenance/db_cleanup_service.py` ·
`services/queue/queue_processing_service.py` · `db/repositories/queue.py` ·
`services/downloads/download_completion_service.py` ·
`test_site/templates/components/search/_playlists.html` ·
`test_site/static/js/features/csv-import.js` ·
`test_site/static/js/pages/monitor.js` · `test_site/templates/pages/downloads/*`
**Commit:** `feat(downloads): Exportify album import + download missing; safe DB cleanup`

## Reported

> "Should we have an option to clear the database of rows no longer in use. Such
> as albums that have no items in the download queue or in Navidrome. Or downloads
> that have completed.
>
> Can it have the option to create a playlist or import as an album? … add them as
> a new album under either an artist … or Various Artists … The setting to not
> search album release should automatically be ticked … The download queue should
> be able to do the current 'Copy if it already exists'. … It may need to pull
> more information from it for the page such as duration … I would also like to
> pull in manual csv's … the ideal items … should be listed on the exportify page
> to show how the csv needs to be ordered. …
> With the Playlist option … for items missing, could it have a download missing,
> then the playlist could be updated as the missing items are added."

## 1. Playlist vs Album mode

The Playlist Downloads panel now has a mode selector:

* **Playlist** — the existing behaviour: match against the library, show
  Matched/Missing, create a Navidrome playlist.
* **Album** — queue every CSV track into the download queue as **one** new album.

`POST /api/playlist/import/csv` gained `mode=album`. `_queue_csv_tracks_as_album`
decides the album artist ONCE for the group:

| CSV credits | album artist |
|---|---|
| every track credits the same non-generic artist | that artist |
| mixed artists, or a "Various Artists" credit | **Various Artists** |

Each row keeps its own `artist` (performer) and forces `album`/`album_artist` to
the one album, adds a positional `track_number` and the CSV `duration`, and sets
`import_type="song"` (NOT `"album"` — that would make the pipeline treat each row
as a MusicBrainz release request to expand). All rows share one `import_group`, so
the queue groups them as an album.

### "Do not search album release" is applied automatically

`album_lookup_skip` lives on `tracks`, so it cannot be set before the album's
tracks rows exist. Each queue row therefore carries the intent in
`download_queue.metadata`, and `persist_import_metadata` (the single hook shared by
the automatic and manual import paths) calls `set_album_lookup_skip(...)` right
after the tracks row is written.

This required `insert_queue_item` to persist `metadata` (it previously dropped
it), and `add_to_queue` / `queue_add_batch` to forward it.

### "Copy if it already exists"

Already automatic (`try_library_copy` runs before the Soulseek gate and copies a
track you own). It needs a known length, which the CSV duration supplies — so
album imports and missing-track downloads benefit with no extra flag.

## 2. Download Missing (playlist mode)

A **Download Missing** button queues the missing tracks. Recommendation (asked
which is cleaner): for a PLAYLIST, each missing track is queued as a **normal
single under its own artist and album from the CSV**, not forced into one
artificial album — a playlist spans many artists and releases, and this keeps each
track on its real album (where its metadata and art belong). Album mode is the
opposite by design.

The UI states that the playlist should be re-created (`Create Playlist`) once the
missing tracks have imported. Making it fully automatic needs a stored, managed
playlist that re-syncs on import — flagged as the follow-up rather than guessed at
here.

## 3. Duration, columns guide, manual CSVs

* **Duration** is now displayed per track (`m:ss`), from the library for matched
  tracks and from the CSV for missing ones.
* A collapsible **column guide** lists the two required columns and the optional
  ones that improve matching/album import, and says other Exportify columns
  (Danceability, Tempo…) are ignored. Manual CSVs are accepted through the same
  case-insensitive aliases.
* The parser already read Duration/Album/Album Artist/ISRC/Genres; nothing new was
  needed there beyond surfacing it.

## 4. Create Playlist was a dead button

`playlist.js`'s `createPlaylist()` posted to `/api/playlist/create-custom`, which
**did not exist** — the button always 404'd. It is now implemented: it creates or
updates (in place) a real Navidrome playlist from the matched library track ids via
`sync_playlist_by_name`.

`queue.html` also included `_playlists.html` without loading its controller, so
the whole panel was inert there; it now loads `import-state.js`, `csv-import.js`
and `playlist.js` like the monitor and search pages do.

## 5. Safe "clear unused rows" cleanup

New `GET/POST /api/maintenance/cleanup` plus a **Database Cleanup** card (Preview +
Clean up) on the monitor's Folders & Queue tab.

**What it prunes (safe):**

1. `album_art` rows whose album is no longer in `tracks`. That table is keyed by
   (artist_name, album_name) with no FK and had no delete path anywhere, so every
   removed/re-keyed album leaked its artwork forever.
2. `download_queue` **tombstones** — `removed` / `cancelled` / `deleted` older than
   30 days. Their `musicbrainz_release_tracks` children are deleted first (that FK
   has no ON DELETE).

**What it NEVER touches:** `imported`, `completed`, `in_collection`. Those drive
the Matched/Unmatched folder flags, the Imported counter, metadata heal,
missing-track badges, the claimed-file and file-deletion guards, discovery dedupe,
and folder auto-deletion. Deleting them is the thing the report asked about, and
the answer is no — filter the UI instead of deleting the history (see the previous
commit for the picker filter).

Every call is a dry-run by default (`GET`, or `POST {dry_run: true}`); the card
previews before it offers to run.

## Tests

`tests/test_exportify_album_and_cleanup.py`: single-artist vs Various Artists,
per-row performer preserved, one forced album, `import_type='song'`, the skip-lookup
intent, one import group, an end-to-end multipart album import (duration parsed to
185 s), and the cleanup service + route (orphan art and tombstones removed;
imported rows and live art preserved; preview writes nothing).

Updated `tests/test_api_paths_are_served.py` (the now-served `create-custom` left
the broken-pin register) and `tests/test_rebuilt_pages_have_no_dead_buttons.py`
(queue.html now resolves the panel handlers).
