# Album downloads queued every track as "Unknown Track" + dashboard MusicBrainz search dead

**Date:** 2026-09-23
**Area:** `services/enrichment/musicbrainz_service.py`, `services/queue/queue_processing_service.py`, `test_site/templates/Pages/dashboard.html`
**Status:** fixed, guarded by `tests/test_album_download_track_contract.py` and `tests/test_upcoming_search_handler_wiring.py`

---

## Symptom

### 1. Adding an album to the download queue from the artist page

The queue filled with anonymous rows that then backed off:

```
Unknown Track
Madball - Not Your Kingdom
Backed off        Not Your Kingdom      7caf6d6e
```

Note the giveaway: the **album** was correct, and the MBID chip was populated —
only the *title* was `Unknown Track`.

### 2. Queueing from Upcoming Releases on the dashboard

Every Search click reported:

```
MusicBrainz search is unavailable on this page.
```

---

## Root cause 1 — the release track shape had split in two

`fetch_musicbrainz_release_metadata` emitted tracks keyed for exactly ONE
consumer — the album comparison helpers:

```python
Tracks.append({
    "mb_disc_number": Disc_number,
    "mb_track_number": _as_int(Position, 0),
    "mb_title":        str(track.get("title") or Recording.get("title") or ""),
    "mb_recording_mbid": str(Recording.get("id") or ""),
    "mb_duration":     Length,
    "mb_genres":       list(dict.fromkeys(genres)),
})
```

But `add_release_tracks_to_queue` — reached from
`/api/musicbrainz/download` (artist page + release picker) and
`/api/downloads/queue-upcoming` (dashboard/upcoming Queue) — reads the **plain**
keys:

```python
track_title    = track.get("title") or "Unknown Track"
recording_mbid = track.get("recording_mbid")
```

Every field missed. Two consequences, one visible and one hidden:

| Defect | Effect |
|---|---|
| `title` missing | every row stored as `Unknown Track` (the report) |
| `recording_mbid` missing | the per-track dedupe key fell back to the *title*, so all tracks of an album hashed to `"unknown track"` and **all but the first were discarded** |

The same rewrite also dropped the enrichment the queue row persists into the
file tags: `artist` per track, `writer` / `composer` / `lyricist`, `work_mbid`,
`is_cover` and `musicbrainz_genres`. `album_missing_service`, the album-page
metadata save and the `/api/queue/*` missing-track routes read those plain keys
too, so they were silently reading blanks as well.

Confirmed before the fix:

```
PRODUCER keys : ['mb_disc_number','mb_duration','mb_genres','mb_recording_mbid',
                 'mb_title','mb_track_number']
queue reads title          -> MISS
queue reads recording_mbid -> MISS
track_title the queue would store: ['Unknown Track', 'Unknown Track']
distinct dedupe keys: 1 of 2 tracks
```

### Fix

Emit **both** key sets from one fetch, and keep defensive readers on the queue
side so the halves cannot drift apart again.

* `_fetch_raw_release()` + `_RELEASE_FETCH_INC` — one `inc` constant, so a
  future edit cannot drop `labels` (blanks the Extended Metadata panel) or
  `work-rels` (loses writer/cover enrichment).
* `_flatten_release()` — the single shape authority. Emits the plain keys
  (`title`, `artist`, `track_number`, `disc_number`, `recording_mbid`,
  `duration`, `musicbrainz_genres`, `writer`, `work_mbid`, `is_cover`, …) **and**
  the `mb_*` keys the compare/similarity helpers predicate on, plus
  `absolute_track_number` and the release-level `compilation` /
  `original_date` / `original_year` / `artist_credit` fields the scan reads.
* `artist` is the **primary** credit and `artist_credit` keeps the join — a
  joined album artist made Navidrome split a collaboration release in two.
* The recording work-relation pass is restored (`work_mbid`, `work_title`,
  `iswc`, `work_artist`, `composer`, `lyricist`, `writer`, `is_cover`,
  `original_cover_artist`, `original_title`).
* `fetch_release_metadata` resolves its client through
  `getattr(_get_service(), "http", None) or get_shared_mb_client()`, so a caller
  (or a test) that registers a service is honoured instead of bypassed.
* `add_release_tracks_to_queue` now accepts either shape
  (`title or mb_title`, `duration or mb_duration or length`, `mb_genres` list →
  comma-joined tag string).

`duration` deliberately stays in MusicBrainz **milliseconds**; the queue
normalises it with `queue_duration_seconds` on the way in.

## Root cause 2 — the dashboard never loaded the handler its own table calls

The error string is an exact locator: it is
`test_site/static/js/services/upcoming-releases.js`, whose `search()` is

```js
if (typeof global.searchMusicBrainzRelease === 'function') { … }
notifyError('MusicBrainz search is unavailable on this page.');
```

Each tree publishes that global from a different file, and
`Pages/downloads/upcoming.html` loaded its own — which is why the **dedicated
page worked and only the dashboard failed**. `Pages/dashboard.html` loaded the
status badge, the upcoming service and the page script, but not the module that
publishes the handler:

```diff
 <script src="{{ versioned_static('js/ui/status-badge.js') }}"></script>
+<script src="{{ versioned_static('js/services/musicbrainz-queue.js') }}"></script>
 <script src="{{ versioned_static('js/services/upcoming-releases.js') }}"></script>
 <script src="{{ versioned_static('js/pages/dashboard.js') }}"></script>
```

It must load **before** `upcoming-releases.js`, or the buttons wire up against
an undefined function.

*(The live tree is unaffected: `static/js/downloads.js` defines
`searchMusicBrainzRelease` and the live dashboard already loads it.)*

---

## Validation

Oracle worktree at `origin/develop` `bc11c12a`, fix replayed in the worktree.

**New suites** — `tests/test_album_download_track_contract.py` (16) +
`tests/test_upcoming_search_handler_wiring.py` (6):

| Tree | Result |
|---|---|
| unpatched | **15 failed / 7 passed** |
| patched | **22 passed** |

The failures name the defects: `queue reader 'title' is empty … would fall back
to 'Unknown Track'`, `a two-track album must create two rows`, and
`never loads js/services/musicbrainz-queue.js — the module that publishes it`.

**Regression sweep** (12 suites):

| Tree | Result |
|---|---|
| unpatched | 12 failed / 433 passed |
| patched | 9 failed / 458 passed |

`Compare-Object` of the failing sets: **3 FIXED, 0 NEW** —
`test_album_artist_multi_artist` ×2 and
`test_mb_cover_art_and_lookup_fixes::test_captures_full_mb_checklist`, all three
broken by the same shape regression. The remaining 9 fail identically on both
trees (pre-existing: an `ILIKE`-on-SQLite harness issue, a `_resolve_album_release_mbid`
signature mismatch, and 4 `mutagen`/`soundfile` environment failures).

## Files

* `services/enrichment/musicbrainz_service.py` — `_fetch_raw_release`,
  `_RELEASE_FETCH_INC`, `_flatten_release`, dual-shape tracks, restored
  work-relation enrichment, primary album artist + joined `artist_credit`.
* `services/queue/queue_processing_service.py` — defensive track readers.
* `test_site/templates/Pages/dashboard.html` — load `musicbrainz-queue.js`.
* `tests/test_album_download_track_contract.py`,
  `tests/test_upcoming_search_handler_wiring.py` — new guards.
