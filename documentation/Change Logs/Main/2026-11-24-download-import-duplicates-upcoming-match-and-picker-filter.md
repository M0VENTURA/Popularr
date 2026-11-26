# Duplicate files import again; upcoming Queue matches first; the folder picker hides imported rows

**Date:** 2026-11-24
**Area:** `services/scanning/reconciliation.py` ·
`services/scanning/navidrome_import.py` ·
`routes/downloads.py` ·
`services/queue/queue_constraints.py` ·
`test_site/static/js/services/upcoming-releases.js`
**Commit:** `fix(downloads): import live duplicates, match before queueing, hide imported rows`

## Reported

> "During the Navidrome import, the imports don't seem to be happening if a track
> with the same artist, album and track name exist. Even though the filename is
> different.
>
> Also, for the upcoming releases, I added Tyr by pressing the queue button, but
> it just added the album name, not each track. When I used the search, I was able
> to find it with the same release date in musicbrainz. … if no release is found,
> then it should bring up the manual search option.
>
> Also when using the Matched and unmatched folders, When searching for a
> release, it shows all files in the download queue including items already
> imported. Should tracks be removed from the database of the queue once they
> have been completed?"

## 1. A live duplicate was "adopted" instead of imported

The Navidrome import keys writes on `tracks.id` (the Navidrome song id) and never
on title. But before writing, it runs **ID adoption**: when a file moves or is
renamed, Navidrome gives it a new song id, and adopting the old row preserves
every Popularr-owned column (stars, genres, manual edits).

`find_adoptable_track_id` accepted adoption on **recording MBID + title +
artist + album + duration (±2 s)** — and a *duplicate* file (same tags, different
name) satisfies all of that. Adopting renamed the original row to the duplicate's
id, and the import payload then overwrote its `file_path`. Result: one row where
there were two files, and the "new" file appeared not to import at all. Order-
dependent, too: if the original came first it was skipped, then the duplicate
adopted its row.

**Fix — adoption must not fire while the old file is still served.** The import
already has the album's live Navidrome songs, so it passes their ids as
`live_ids`. An old row whose id is in that set belongs to a file that still
exists; the new file is a duplicate, not a successor, and it is inserted as a new
row. Path-based adoption is guarded the same way. `live_ids=None` (the default)
keeps the old behaviour for callers with no live set.

## 2. Pressing Queue on an unmatched release queued a dead album row

`/api/downloads/queue-upcoming` only ran the per-track path when
`release_group_mbid` was set. An unmatched (or merely *candidate*) row skipped it
and fell through to a single album-typed row — which `_process_release_request`
parks as `pending_release` forever, because it has no release id to expand.

**Fix — match on demand, then queue; only fall back to manual search.**

```
no release_group_mbid
  → match_to_musicbrainz(artist, album, release_date)   (same matcher /match uses)
  → status == "matched"  → persist, resolve the group, queue per track
  → anything else        → { success: false, needs_manual_search: true }
```

Only a **high-confidence** match is auto-queued; a `candidate` is left for the
user to confirm. A match failure (or an exception) returns the same manual-search
signal rather than a dead row. The album-row fallback is kept ONLY when a release
id exists but immediate expansion failed — that row carries the release id, so
the queue can expand it later.

Front end: the Queue button shows a **"Matching…"** busy state while the server
runs the match, opens the MusicBrainz picker pre-filled when the answer is
`needs_manual_search`, and warns when the shape is a single album row rather than
a per-track queue (previously `queue_shape` was ignored, so an album-only result
looked like a full per-track queue).

## 3. The folder picker listed already-imported queue rows

`GET /api/downloads/queue/albums` feeds the "In your download queue" block on the
folder-match screen. Its hand-written status list **included `imported` and
`in_collection`** — the rows that are already in the library.

**Fix — filter, do not delete.** A new `QUEUE_ALBUM_PICKER_STATUSES` excludes
those and the tombstones (`removed`/`cancelled`/`deleted`). `completed` is kept:
it means "downloaded, awaiting organization", which is still queue work.

### Why imported rows must NOT be deleted on completion

The report asked whether completed rows should be removed from the database. They
are deliberately kept (`status='imported'`), and deleting them would regress
several live features:

* **Matched & Unmatched folders** derive every "imported" flag and the folder
  `matched` status from imported rows (`_imported_source_paths`); deleting them
  would make already-imported folders reappear as Unmatched and drop the guard
  that refuses to delete imported files.
* The **"Imported" counter** (`3012 Imported`) counts them.
* **Metadata heal** on already-imported downloads (`HEALABLE_STATUSES`).
* **Missing-track queue badges** annotate from them.
* **Claimed-file / file-deletion protection** during completion.
* **Discovery dedupe** (`find_existing_discovered_file`).
* The explicit **"Clear Imported"** action and "clear queue keeps imported".

If growth is a concern, the right follow-up is an explicit retention policy for
imported rows — not deletion on import.

## Tests

* `tests/test_navidrome_reconciliation_freshness.py::TestALiveDuplicateIsNotAdopted`
  — MBID and path adoption refused while the old id is live; a genuine move still
  adopts; no live set keeps the old behaviour.
* `tests/test_upcoming_queue_matches_on_demand.py` — a high-confidence match
  queues every track and is persisted; unmatched/candidate/exception open the
  manual search and insert no album row; a pre-matched release is not re-searched.
* `tests/test_monitor_folder_match_ui.py` — imported/in_collection/tombstones are
  not offered; `completed` still is.
