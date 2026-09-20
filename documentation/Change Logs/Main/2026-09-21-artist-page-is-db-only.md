# The artist page is now a read-only view — metadata lookups moved into the scan

**Date:** 2026-09-21 · **Area:** routes / metadata / popularity scan

## Question asked

"Is the artist page doing metadata lookups? It should only be checking on the
database itself; all parts of the lookups that are happening on the page should be
done during the metadata scans in the popularity scan runners."

**Answer: yes, it was — in two places, both on page load.** The rest of the page
is already DB-only or scan-filled. An audit of every path reachable from
`routes/ui_routes.py::artist_detail` (the routed template is `pages/artist_detail_v2.html`)
and every endpoint the page's scripts call found exactly two live lookups without
a scan-side counterpart.

## 1. Artist line-up — MusicBrainz on **every** page load

`get_artist_members_cached` (`services/metadata/artist_metadata_service.py`) was
called from inside `_build_artist_detail_payload` and, whenever its 7-day cache
was cold, ran `search_artists` **and** `get_artist_members` through the shared
MusicBrainz client. That client throttles at ~1 req/s by *reserving a future
slot and sleeping*, so a page load during a scan queued behind the scan's
requests for tens of seconds — which is why the page looked frozen. It was also
the only writer of `artists.members` / `artists.members_last_updated` in the
entire live tree, so the cache it consulted was one it had to fill itself.

* **Moved to the scan**: `album_stage._fetch_artist_metadata` — the step that
  already caches `country`, `bio` and `image_url` for the same artist, once per
  artist per scan, inside the shared heartbeat/MB-accounting helpers. It now
  reads the roster, refreshes it when older than `_ARTIST_MEMBERS_TTL_DAYS`
  (default 7 — the window the page itself used), and persists it in the same
  `INSERT … ON CONFLICT` with `COALESCE(excluded.members, artists.members)`, so a
  failed lookup never erases a good roster.
* **Page path**: `get_artist_members_cached` is a pure `SELECT` and returns `[]`
  until the scan has run for that artist. It cannot reach MusicBrainz at all.

## 2. `/api/album/missing-tracks` — recomputed from MusicBrainz **per owned album**

`album_missing_service.get_missing_tracks` resolves the album's MusicBrainz
release (a `fetch_musicbrainz_release_metadata` call, plus a `search_releases`
when the album has no stored MBID) and then computes the missing set. The artist
page's badge requests this **once per owned album on load**
(`static/js/artist-releases.js::fetchMissingTrackCounts`), so opening an artist
page with 20 albums fired up to 20 MusicBrainz release fetches — the "probe
storm" that earlier fixes could only *cap* (3 workers) and *gate* (stand down
while a scan runs), because the endpoint itself always recomputed.

* **Split**: `get_missing_tracks` stays the MusicBrainz-driven recompute (it
  persists to `missing_album_tracks`), and a new
  `get_missing_tracks_from_db` reads that table only.
* **Route**: `GET /api/album/missing-tracks` now calls the DB reader. `mb_total`
  is not stored per row, so it is reported as `library_count + missing_count` —
  the UI's totals still add up without the endpoint claiming a release size it
  does not know.
* **Scan**: `scan_stage_runner` refreshes the snapshot per album in the same
  post-processing lane as the file-tag sync, behind the same
  `not popularity_only and not singles_detection_only` gate. By that point the
  album stage has resolved and persisted the release MBID, so the release-metadata
  fetch inside is normally a **cache hit** — the cost moved from *per page load
  per album* to *once per album per scan*.

A side effect worth noting: gating and capping are no longer load-bearing for the
live page, because the endpoint is cheap. The `test_site` artist page
(`test_site/static/js/pages/artist.js`), which had re-implemented those probes
with **neither** the concurrency cap nor the scan gate, is defused by the same
change.

## Audit results — what the page does now

**DB-only (already correct):** every `/api/artist/*` surface except bio/image
country fallbacks, `singles-count`, `similar` (reads `artists.similar_artists_*`),
`favourite`, `covered-by`, `update-ids`, `/api/album/tracklist`,
`/api/musicbrainz/downloads`, `/api/artists/corrections`,
`/api/correcting/albums`, `/api/queue/*`.

**Scan-filled with an external fallback (fine, but noted):**
`GET /api/artist/image` → AudioDB and `GET /api/artist/bio` → Wikidata **only
when the DB has no value**; `album_stage._fetch_artist_metadata` writes both.

**Still live on demand (deliberately not changed here):**

| Surface | Lookup | Note |
|---|---|---|
| `GET /api/artist/missing-releases` ("Check Missing") | MB release-group browse (up to 4 pages) | user-initiated; the sweep and `refresh_missing_releases_for_artist` already fill `missing_releases` |
| `GET /api/artist/release/tracklist` | MB `get_release` **on cache miss only** | cached in `missing_releases.tracklist`, backfilled by the scan |
| `GET /api/artist/genre-recommendations` ("Get Online Suggestions") | MB artist tags | **nothing persists these** — the one remaining candidate to move into `_fetch_artist_metadata` |
| `GET /api/album/title-mismatches` | MB release metadata | reached from the corrections page; same shape as missing-tracks and the next place to apply the same split |
| `POST /api/artist/lookup-ids` | MB + Discogs | the button is dead (its modal markup is not in the routed v2 template) |
| `POST /api/musicbrainz/search`, import-release, `/api/slskd/search` | MB / Discogs / slskd | user actions by definition |

## Verification

`tests/test_artist_page_is_db_only.py` (new) pins the boundary:

* the members reader's source contains **no** `search_artists`,
  `get_artist_members(` or `get_shared_mb_client`, and it parses the cached JSON,
  tolerates a missing row, unparsable JSON and a failing query;
* the scan owns the lookup: `_fetch_artist_metadata` calls
  `_lookup_artist_members`, honours the freshness window, never clobbers a roster
  with `COALESCE`, and `_lookup_artist_members` prefers a group over a person;
* `routes/album_routes.py` references `get_missing_tracks_from_db` and the
  recompute is no longer reachable from the route, while it **survives for the
  scan** (`scan_stage_runner` contains
  `get_missing_tracks(artist=artist, album=album)` and the `[MISSING_TRACKS]` log);
* the DB reader returns the persisted rows, its totals add up, it excludes
  `ignored` rows in SQL, and a failing query degrades to zeros.

⚠️ The pytest run of this new suite is pending the next workspace commit (the
virtual workspace had not committed this change set when it was written). The
change set is: `services/popularity/stages/album_stage.py`,
`services/metadata/artist_metadata_service.py`,
`services/metadata/album_missing_service.py`, `routes/album_routes.py`,
`services/popularity/scan_stage_runner.py`, plus this test file and change log.

Verified directly against the tree meanwhile: `album_stage._fetch_artist_metadata`
and the members helpers import and parse clean (`get_errors` on all five edited
files), and the previous change set's regression sweep is recorded in
`2026-09-21-scan-track-identity-and-year-tag-split.md` (0 new failures).
