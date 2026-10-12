# Album page: "Do not search album release" + CSV tracklist import (2026-10-13)

## Request

> Can we add an option under the edit album area on the album page for "Do
> not search album release"? This will skip the album lookup for albums that
> are custom and not found on MusicBrainz or Discogs? Or better yet, when
> this is selected show an option to upload csv for tracklist. This can match
> the tracklist to import missing tracks. — **test_site only.**

## What it does

**Edit Album Metadata** (test_site album page) now has a **Custom Album**
section:

* **"Do not search album release"** checkbox — persisted album-wide
  (`tracks.album_lookup_skip = '1'` on EVERY track, written with the rest of
  the save). For a custom album that isn't on MusicBrainz/Discogs, the
  scan then skips the **album-release** lookups:

  * the MusicBrainz album-type lookup (falls back to the local-title
    heuristic),
  * the Discogs release ID lookup,
  * online album art (CAA / AudioDB / Discogs — Navidrome + the stored cover
    still win),
  * the per-album missing-track refresh against a MusicBrainz release.

  Per-track metadata (recording MBID, genres, scans) is untouched — this only
  stops the ALBUM release lookup.

* **Upload a CSV tracklist** (shown when the checkbox is on) — the CSV stands
  in for the MusicBrainz release: tracks the library doesn't have are
  persisted to `missing_album_tracks` (the SAME table the MB comparison
  writes), so the album's existing missing list + **Download Missing Tracks**
  flow just works. Uploading also sets the flag — and the scan's
  missing-track refresh is skipped for flagged albums so it never DELETES the
  CSV rows as "not in the MB release".

  CSV format (liberal): a header row (`Title,Artist,Duration,…`) or
  positional `title` / `track number,title` / `disc,track number,title`, and
  Exportify-style `#5 - Song` cells; `3:45` durations parse to seconds.

## Implementation

* **Column** `tracks.album_lookup_skip` (TEXT, '' = search, '1' = skip) —
  migration `021_album_lookup_skip` (inspector-guarded, house pattern) +
  `db/schema.py` registry + ORM `db/models.py`.
* **`services/metadata/album_lookup_skip_service.py`** — `album_lookup_skip`
  (album-wide read, fails OPEN to searching) + `set_album_lookup_skip`
  (album→tracks fan-out write; also clears).
* **Album save** (`routes/ui_routes.py`) — writes the flag album-wide after
  the rename; `album_data["album_lookup_skip"]` renders the checkbox.
* **`POST /api/album/tracklist-csv`** (`routes/album_routes.py`) →
  **`import_csv_tracklist`** (`services/metadata/album_missing_service.py`):
  liberal CSV parser → compute missing vs library (title + position + queue
  coverage gates — same semantics as `get_missing_tracks`) → set the flag →
  `_persist_missing_tracks` (reused, so the album page's existing list and
  queue path need no change).
* **Scan gates** — `scan_stage_runner` missing-track refresh skips flagged
  albums; `album_stage._resolve_album_type` / `_resolve_discogs_album_id` /
  `_fetch_album_art_with_fallback` return early for flagged albums.
* **test_site UI** — `Pages/album_detail.html` checkbox + CSV card;
  `pages/album.js` `toggleAlbumLookupSkip` / `uploadAlbumCsvTracklist`
  (FileReader → POST, refreshes the missing list).

## Tests — `tests/test_album_lookup_skip.py` (22)

helper read/clear + fan-out (incl. other-album isolation); CSV parser (header,
positional, disc,number,title, `#N - Title`, blank, `3:45`); the full import
persists the missing set + sets the flag (and an owned title is not missing);
scan gates skip flagged albums (`pytest.fail` probes) while unflagged albums
still search; wiring (runner refresh gated, save + album_data carry the flag,
endpoint, template + JS + global handlers).

**Pinned-house-test updates (deliberate, documented in each):**
`test_album_scan_budget` "exactly two writers" → **three** (the CSV upload is
the deliberate new `missing_album_tracks` writer) and the scan-type gate
window widened (the flag guard sits inside the mode-gated block);
`test_finalise_scan_mode` "network metadata gated off" asserts the mode gate
+ the recompute still exist with the new inner guard (finalise stays DB-only).

**Sweep** — 111 missing/album/scan/metadata suites: **failure set identical
to pristine `bd5f4de8`** (59 pre-existing; none of the 4 new) → 0 regressions.
`import app` OK (399 → 400 routes, the new endpoint); `node --check` on the
JS clean.

## Note for the user

* The CSV upload takes effect immediately (no Save click needed) — it sets
  the flag AND persists the missing rows; the flag also saves with the form.
* Unchecking "Do not search album release" clears the flag (lookups resume);
  the CSV rows remain in the missing list until downloaded/dismissed, then a
  normal scan's MB refresh takes over.
* test_site only, as requested — the live album page is unchanged.