# Album art upload fails + corrected track titles now reach the file tags

**Date:** 2026-10-12
**Area:** album / metadata / file-tags
**Commits:** `fix(album): album art upload awaits request.files` +
`fix(metadata): the release tracklist title is promoted to the track title and written to the file tags`

## Part 1 — "Upload failed: 'coroutine' object has no attribute 'get'"

**Reported:** uploading a new album cover from a `.jpg` failed with
`Upload failed: 'coroutine' object has no attribute 'get'`.

**Root cause:** the `/album/upload-art` route read
`request.files.get("image")` — but Quart's `request.files` is an **async
attribute** (`await request.files`), exactly like `request.form`. Without the
await, `request.files` *is* the coroutine; `.get("image")` raises
`AttributeError`. Every non-empty upload failed.

**Fix:** `files = await request.files` before reading the image, matching the
playlists import route (`routes/playlists.py`) — the pattern that had it
right.

## Part 2 — "It also hadn't updated the title even though it was the title given from the release ID"

**Reported:** a track's title from the matched MusicBrainz release (e.g.
"My First and Last (Chinese version)" on an album whose local file says the
plain "My First and Last") was never written, even with "All File Metadata"
enabled.

**Root cause — three connected gaps:**
1. `_resolve_track_mb_metadata` wrote the release tracklist title only to
   `musicbrainz_title` (a review column) — never promoted it to the actual
   `title` column, so the DB kept the local spelling.
2. `sync_album_file_tags` writes TITLE **fill-if-missing** — a wrong
   existing title was never rewritten, so even a corrected DB title never
   reached the file.
3. `apply-mb-field` (the Compare/Apply UI) wrote the DB but never the file.

**Fixes:**
1. The **album-release batch identity** title — the scan's highest-confidence
   source, matched by position and duration on the album's own release, the
   SAME trust the code already places in its recording MBID — is promoted to
   `payload["title"]` when it differs from the local title. Gated on: not a
   frozen track, the title not in `mb_ignored_fields` (the Compare UI's
   Ignore), a meaningful difference, and `metadata_update.apply_during_scan`
   (recommend-only mode stashes instead). A recording SEARCH result is NOT
   the release and never promotes — only the batch identity.
2. `sync_album_file_tags(..., corrected_title_ids=...)` force-rewrites the
   TITLE tag for exactly the ids whose title the scan corrected this pass —
   a deliberately chosen local title on any other track is never clobbered
   (fill-if-missing remains).
3. `apply-mb-field` also writes the title to the track's file; a file-tag
   failure there logs a warning but never turns a successful DB write into a
   500.

**Consent model:** the `mb_ignored_fields` column is the single source of
"user ignored this field" — the scan's promotion and the Compare/Apply UI
both respect it, so a decision made in one place is honoured in the other.

## Tests

- `tests/test_album_art_upload_awaits_files.py` (4): the handler awaits
  `request.files`; `request.files.get` never appears without await;
  `request.form` still awaited; the fix is real code, not a comment.
- `tests/test_release_title_promotion_and_file_sync.py` (10): the batch
  title is promoted; frozen / ignore-list / recommend-only gates block it;
  an equal title and a search-result title do NOT promote; the tag sync
  rewrites a corrected title even when the tag exists and leaves an
  uncorrected local title alone; AST wiring guards for the runner forwarding
  `_corrected_title_ids` and the apply route writing the file.

**Oracle** at `cd94dfec`: 12 failed / 2 passed (upload route + title
promotion + tag-sync + wiring all absent at baseline).
**Sweep** (12 album/type/art/scan suites): 15 failed at base = 15 at new,
**identical failing set** → **0 regressions**. (Art-mb-tags ×3 and
album-type ×1 are the pre-existing failures.)

## Note

An existing track whose title should be corrected gets it on the next
metadata-enabled scan; or instantly via the Compare/Apply UI's "Update All"
(the file now follows). A `metadata_update.apply_during_scan: false`
config keeps the scan recommend-only — the title stays a stashed
recommendation, applied only when the user saves it.