# MIGRATION.md — finishing the `New/` rebuild

Everything in `New/` is complete and internally consistent. This file lists the
operations that **cannot be performed from the editor's virtual workspace**
(renames and deletions) plus the bugs found in the live tree that the migration
surfaced.

Nothing here is speculative — every claim was verified against git.

---

## 1. Renames required before `New/` can replace `static/` + `templates/`

### 1.1 `New/static/CSS/` → `New/static/css/` — REQUIRED, currently broken

Every template asks for the lowercase path:

```jinja
<link rel="stylesheet" href="{{ versioned_static('css/popularr.css') }}">
```

`New/static/CSS/` is capitalised. This works on Windows and **404s on Linux /
Docker**, so the container build is the first place it will fail. Rename the
directory — do not change the templates, since lowercase is the existing
convention (`static/css/` in the live tree).

Contains: `artist.css`, `dashboard.css`, `downloads.css`, `popularr.css`,
`track.css`.

### 1.2 `New/templates/Pages/` → `New/templates/pages/`

The live tree uses `templates/pages/`, `templates/playlists/`,
`templates/auth/` — all lowercase. Lowercasing `Pages/` (and `Playlists/`) keeps
the cutover diff to file *contents* rather than a rename of every path.

```
New/templates/Pages/            → New/templates/pages/
New/templates/Playlists/        → New/templates/playlists/
```

Jinja include/extends paths are unaffected: they are resolved by the loader, not
by the filesystem case, and `base.html` is referenced as `"base.html"`.

---

## 2. Deletions

### 2.1 Stray/mis-created files in `New/`

| File | Why it goes |
|---|---|
| `New/templates/Pages/downloads/monitor.html` | Contains a ~1,345-line **copy of an older artist page**, not the monitor page. Cannot be overwritten from the editor (create_file refuses to replace) and cannot be deleted from a virtual workspace. |
| `New/templates/Pages/downloads/upcoming.txt` | A superseded draft. The real file is `upcoming.html`. |
| `New/templates/components/modals/_musicbrainz_release_match.html` | Dead in **both** trees — see §3.2. |

### 2.2 Dead templates in the live tree (their own headers say so)

| File | Evidence |
|---|---|
| `templates/playlists/browse.html` | Line 1: "DEPRECATED — unreachable. /playlists/browse redirects to the /playlists hub" |
| `templates/playlists/create.html` | Line 1: "DEPRECATED — unreachable… Safe to delete" |
| `templates/playlists/importer.html` | Line 1: "DEPRECATED — unreachable… Safe to delete" |
| `templates/components/_musicbrainz_search_functions.html` | "DEPRECATED — backward-compatibility shim. Renders nothing." |
| `templates/components/modals/_album_lookup.html` | "DEPRECATED — this wrapper no longer renders a search form of its own." |

### 2.3 Unreachable templates (routes redirect or render something else)

| File | Evidence |
|---|---|
| `templates/pages/downloads/search_soulseek.html` | `ui_routes.py` → `downloads_search_soulseek()` redirects to `downloads_search` |
| `templates/pages/downloads/search_musicbrainz.html` | same route pattern |
| `templates/pages/downloads/search_playlists.html` | same route pattern |
| `templates/pages/dashboard_external.html` | no literal `render_template(...)` anywhere |
| `templates/pages/beets_integration.html` | same (beets integration removed) |
| `templates/pages/smart_playlists.html` | route `/smart-playlists` REMOVED 2026-09-18; no link to it anywhere, and it was a second smart-playlist builder duplicating `pages/playlist.js`'s |
| `templates/components/_hero.html` | only referenced by itself (`{% from 'components/_hero.html' import hero_cta %}`) |

### 2.4 Orphaned partials (no template includes them)

```
templates/components/search/_monitor_upcoming.html
templates/components/search/_monitor_discovery.html
templates/components/search/_monitor_queue_section.html
templates/components/search/_queue_add_form.html
templates/components/search/_qbittorrent.html
templates/components/search/_musicbrainz.html
```

`_monitor_upcoming.html` additionally calls three globals that no longer exist
(`scrapeUpcomingReleasesMonitor`, `clearUpcomingReleasesMonitor`,
`autoMatchAllUpcomingReleasesMonitor`), so it could not work even if included.

### 2.5 After the artist-page cutover (§4.1)

```
templates/pages/artist_detail_v2.html
```

### 2.6 `New/static/js/pages/folder-groups.js` — already unloaded, file to remove
Removed from `base.html` on 2026-09-18 (the script tag is gone, and the
load-order comment explains why). The file itself still needs deleting.

It was the legacy "green folders" view for MusicBrainz releases and could never
do anything useful:

* it rendered into `#folderGroupsList` — the same element
  `pages/download-queue.js::renderQueueSection()` owns;
* it stood down whenever `global.loadQueueStatus` existed, which is true only
  on the queue/monitor pages;
* on **every other page** `loadQueueStatus` is undefined, so nothing made it
  stand down and its `DOMContentLoaded` handler started a **5-second poller
  ticking a no-op `load()` forever**;
* its only unique capability (`/api/downloads/folder-groups`) is superseded by
  `pages/monitor.js`'s Matched & Unmatched Folders.

---

## 3. Bugs found in the LIVE tree (not migration artefacts)

### 3.1 `templates/pages/downloads/manager.html` is missing but still routed

`routes/ui_routes.py` renders `pages/downloads/manager.html`. Git history:

```
a75ce08c  A  templates/pages/downloads/manager.html   "updated files"
9454d21b  D  templates/pages/downloads/manager.html   "updated page layouts"
```

The file was added then deleted, and the route was never updated — so
`/downloads/manager` raises `TemplateNotFound` today. It still exists in a clone
at commit `9423790b` if the page is wanted back; otherwise point the route at
`pages/downloads/queue.html` (the current downloads hub) or drop it.

### 3.2 A broken macro import that may explain the artist-page situation

`templates/pages/artist_detail.html` does:

```jinja
{% from "_album_category_section.html" import render_album_row %}
```

That is a **template-root** name. It resolved while the macro lived at
`templates/_album_category_section.html`, but commit `1a33be0b`
("removed orphaned files", 2026-07-12) deleted that copy after `a75ce08c` had
already added `templates/components/_album_category_section.html`. **No Jinja
loader search path is configured anywhere in the app** (no `ChoiceLoader`,
`FileSystemLoader`, `PackageLoader` or `jinja_loader` — grepped `app.py`,
`helpers/`, `routes/`, `db/`, `services/`), so the import raises
`TemplateNotFound`.

Fixed in `New/` (`components/_album_category_section.html`).

### 3.3 The artist page: `artist_detail.html` is canonical, `_v2` is a workaround

The timeline is decisive:

| Date | Commit | Effect |
|---|---|---|
| 2026-07-12 | `1a33be0b` | root macro deleted → `artist_detail.html` starts throwing `TemplateNotFound` |
| 2026-08-12 | `80e0f7fd` "Corrected the artist layout" | route switched to `artist_detail_v2.html`, **one month later** |

`artist_detail.html` is 216,376 bytes and was edited as recently as 2026-09-16;
`artist_detail_v2.html` is 32,492 bytes and untouched since 2026-09-04. `_v2`'s
only distinctive feature (`render_release_category`) duplicates what
`render_album_row` plus the per-category sections already do.

**Recommendation: retire `_v2`, don't merge.** Cutover is two steps:

1. `routes/ui_routes.py` — render `pages/artist_detail.html`
2. delete `templates/pages/artist_detail_v2.html`

Do this *after* a visual check: the file has not been served in its current form.

---

## 4. Load-order and asset rules the ported pages depend on

1. **`base.html` owns the global scripts.** `utils/*` → `ui/*` → `services/musicbrainz-picker.js`
   → `ui/player.js` → `pages/folder-groups.js` → `main.js` → `ui/search-flyout.js`.
   Page templates add only page-specific scripts in `{% block scripts %}`.

2. **`components/search/_soulseek.html` needs its controller on every page that
   includes it** (`downloads/{search,queue,monitor}.html`):
   `css/downloads.css` + `js/services/slskd.js` + `js/pages/soulseek-search.js`.
   The old design worked because all three pages loaded `downloads.js`, which
   carried `searchSoulseek`. That flow now lives in `soulseek-search.js`.

2b. **`js/services/item-groups.js` is required by `pages/download-queue.js` AND
    `pages/monitor.js`** — load it BEFORE either, on any page that loads one:
    `downloads/queue.html` and `downloads/monitor.html`.
    It is the shared renderer for the "folder" rows both pages show (Matched &
    Unmatched Folders and the queue's Active / Completed / Failed groups), and
    it was extracted because the two had drifted: the completed list had ended
    up repeating its album actions on every track row beneath it. It owns the
    row shell, the action-button markup, the delegated `bindActions` table, and
    the collapsible-group toggle + expansion memory.
    `queue.html` loads it; `monitor.html` still has to.

3. **`features/csv-import.js` requires `state/import-state.js` first.**
   Both these files used to declare `currentImportData` and
   `missingTracksForSearch` with `let` at top level; the second one loaded threw
   `SyntaxError: Identifier has already been declared` **at parse time**, so none
   of its functions were ever defined.

4. **`components/_musicbrainz_search_modal.html` and `_unified_search_modal.html`
   are included once, globally, by `base.html`.** Never include either from a
   page — their ids are fixed and `getElementById` returns the first match in DOM
   order, so a second copy makes the search write into the hidden modal.

---

## 5. Handlers that exist nowhere

Worth knowing these were already broken **before** the refactor — they are not
regressions from it.

| Symbol | Referenced by | Status |
|---|---|---|
| `quickQueueRelease` | `_album_category_section.html` "Quick Queue" | defined nowhere in the repo |
| `searchAgainSlskd` | `_slskd_results.html` "Search Again" | defined nowhere |
| `searchMatchTargets` | `_manual_match.html` search box | defined nowhere |
| `openAddTrackModal` | `_playlists.html` (removed in `New/`) | defined nowhere |
| `searchMissingTracks` | `_playlists.html` (removed in `New/`) | defined nowhere |
| `saveEditedTrack` | `_track_edit.html` simple modal | defined nowhere — **migration gap**, see below |
| `organizeSelected`, `batchOrganizeSelected` | `_queue_status.html` | deliberate stubs; `download-queue.js` says to remove the buttons (done in `New/`) |
| `searchSoulseek`, `runSoulseekManualSearch` | legacy `static/js/downloads.js` | **un-migrated** — superseded by `global.slskd.*`; the manual-search modal has since been IMPLEMENTED (see §5b) |

`saveEditedTrack` is the one to fix rather than remove: `album.js`'s own header
documents the simple modal as live ("saved via `saveEditedTrack()`"), so it needs
a ~15-line implementation that reads `#simpleEditTrackId`,
`#editTrackCurrentField`, `#editTrackValue` and posts to
`/api/track/update-metadata`.

Also fix: `services/genres.js::editTrackArtist()` checks for and shows
`#editTrackModal` (the comprehensive modal) while writing the **simple** modal's
fields (`#editTrackCurrentField`, `#editTrackLabel`, `#editTrackValue`). **FIXED
2026-09-18** — it now prefers `#simpleEditTrackModal` (+ `#simpleEditTrackId`)
and falls back to the artist page's `#editTrackModal` + `#editTrackId` pairing.
See the header of `New/templates/components/modals/_track_edit.html`.

---

## 5b. The three Soulseek modals — one implemented, two dead

All three had a template, a broken handler and a half-migrated flow. They are
NOT the same problem:

### `_soulseek_manual_search.html` — UN-MIGRATED → now IMPLEMENTED

Its `runSoulseekManualSearch()` existed only in the legacy
`static/js/downloads.js` (along with `ensureSoulseekManualSearchModal` and
`openSoulseekManualSearchModal`), and was never carried across. Meanwhile two
refactored files already called the opener, both behind `typeof` guards so they
silently did nothing:

```
pages/download-queue.js  manualQueueSearch(query, queueId)   line 755
pages/search.js          the per-track search on the search tab  line 53
```

Implemented in `services/slskd.js` (the module `download-queue.js`'s header
already claimed owned it) and exported under the names the call sites use:
`global.openSoulseekManualSearchModal` and `global.runSoulseekManualSearch`. The
poll loop / slot-busy retry / terminal grace come from the module's `search()`
rather than the legacy hand-rolled `waitingForSlot` flag and window timer.

It reuses `renderTable`, which now forwards `opts.queueId` through to
`download()` — that is what routes the pick to `/api/slskd/queue-download` and
attaches the file to the EXISTING queue row instead of creating a second one.
Without that pass-through the modal would have orphaned the row it was fixing.

`queueId` is reset when the modal closes, so a later open cannot attach a file
to a row the user has moved on from.

### `_slskd_results.html` — DEAD, do not port

Every dependency is gone from the current tree:

| Needed | Status |
|---|---|
| opener `showSlskdResults(id)` | only ever in `old_system/templates/downloads.html:2883` |
| `GET /api/slskd/search-results/<downloadId>` | **no longer a route** — the slskd blueprint has search / search-slot / search/<id> / download / cancel / status / retry / queue-download / events, and nothing else |
| `selectSlskdResult(result_id)` | gone with it |

The one live reference is a guarded call in `download-queue.js` (an "awaiting
selection" row), which no-ops. Reviving this feature means re-adding the
endpoint, not porting the template.

### `_manual_match.html` — UNFINISHED, do not port

The markup is real and its endpoints both exist:

* `GET /api/queue/<id>/match-targets?q=` → library tracks to match against
* `POST /api/queue/<id>/link-track` `{track_id}` → copies the track's metadata
  onto the orphan queue row and moves it to `completed`

but `searchMatchTargets()` and `openManualMatchModal()` were **never defined in
any tree** — not `static/js/downloads.js`, not `old_system/`, nowhere. So the
modal has no JavaScript and, more importantly, **no caller**: nothing in the app
offers a way to open it.

This is an unfinished feature rather than a migration casualty. If it is wanted,
the work is: add a "Match to library track" action to the orphan rows in
`components/search/_queue_status.html`, then implement `openManualMatchModal` +
`searchMatchTargets` + the link call in `pages/download-queue.js`. The endpoints
are already there to build on.

---

## 6. Live templates still to port into `New/`

Taken from every literal `render_template(...)` call — the authoritative set.

### Ported 2026-09-18

| Template | New files |
|---|---|
| `pages/artist_list.html` | `Pages/artist_list.html` + `static/CSS/artists.css` |
| `auth/login.html` | `auth/login.html` + `static/CSS/login.css` |
| `pages/downloads/search.html` | `Pages/downloads/search.html` |
| `pages/bookmarks.html` | `Pages/bookmarks.html` + `js/pages/bookmarks.js` |
| `pages/analytics.html` | `Pages/analytics.html` + `js/pages/analytics.js` + `static/CSS/analytics.css` |
| `pages/banned_words.html` | `Pages/banned_words.html` + `js/pages/banned-words.js` |
| `pages/artist_corrections.html` | `Pages/artist_corrections.html` + `js/pages/artist-corrections.js` |
| `pages/corrections.html` | `Pages/corrections.html` + `js/pages/corrections.js` + `static/CSS/corrections.css` |
| `pages/missing_releases.html` | `Pages/missing_releases.html` + `js/pages/missing-releases.js` |

Every one of these was verified with `node --check` (JS) and a Jinja
`Environment().parse()` (templates) — see the verification recipe in repo memory.

### THE REMAINING PAGES EACH NEED A NEW JS MODULE — they are not markup-only

This is the important point for planning. Every remaining page carries an inline
`<script>` whose functions exist **nowhere** in the New tree — there is no module
to port the markup against. Verified by grepping the New tree for each page's
distinctive function names: only `importPlaylistFromCSV` and one unrelated
`displaySearchResults` matched, everything else returned nothing.

| Template | Inline JS to extract | Notes |
|---|---|---|
| `playlists/importer_csv.html` | 260 lines | see the collision note below |
| `pages/logs.html` | 241 lines | `loadLog`, `renderLineHtml`, `startStream/stopStream`, filters. NOT the same viewer as `main.js`'s unified scan-log modal |
| `pages/sandbox.html` | 233 lines | `loadMetrics`, `recalculate`, `zscoreToPopularity`, `starsFromZ`, `liveDistribution`, slider sync |
| `pages/discover.html` | 233 lines | `loadRecommendations`, `renderSection`, `buildCard`, `createPlaylist` |
| `auth/setup.html` | 159 lines | first-run wizard; `goToStep`, `testConnection`, `saveSetup`, Essentia downloads. 479 lines, 3 steps |
| `pages/metadata_compare.html` | 141 lines | `searchMusicBrainz`, `applyMusicBrainzData`, `acceptNavidromeData` |
| `pages/artist_genres.html` | 134 lines | `saveChanges`, `setSpinner`, `showStatus` — likely belongs in `services/genres.js` |
| `downloads/similar_artists.html` | 90 lines | `loadSimilarArtistsDiscover` |
| `pages/help.html` | 41 lines | doc page; 243 lines of inline CSS to extract |
| `pages/downloads/monitor.html` | — | blocked on §2.1, and its `slskdMon*` / `monitorSlskd*` tables must be built to `pages/soulseek-search.js`'s contract |

`pages/search.html` is NOT on this list — see §6b. It is superseded, not pending.

Line counts here are the CRLF-affected `Measure-Object -Line` values and run
~10% low: `artist_corrections` measured 1,049 and is actually 1,106;
`corrections` measured 751 and is actually 819; `missing_releases` measured 472
and is actually 523. Treat them as a ranking, not a budget.

### `playlists/importer_csv.html` — NAME COLLISION, do not reuse `features/csv-import.js`

The page defines its own `importPlaylistFromCSV` (via `#csvFile` +
`#csvPlaylistName` + modal progress ids), and it is a DIFFERENT flow from the
global of the same name in `features/csv-import.js`:

* **importer_csv.html** — queues every track from the CSV for download.
  "matching & tagging happens in the queue".
* **features/csv-import.js** — matches against the library and produces a
  playlist, feeding `_playlists.html`'s matched/missing panels.

They only avoid clashing because no page loads both. Extracting this page's
script must create a new module (e.g. `pages/playlist-import-csv.js`) — pointing
it at `csv-import.js` would silently swap a queueing import for a matching one.

---

## 6b. `pages/search.html` — SUPERSEDED by `ui/search-flyout.js`

`/search` was a standalone library search page. **All of it now lives in
`ui/search-flyout.js`**, the global navbar search that `base.html` loads on
every page:

| | `pages/search.html` | `ui/search-flyout.js` |
|---|---|---|
| Endpoint | `/api/search` | `/api/search` (line 163) |
| Buckets | albums, compilations, live_albums, eps, singles, tracks | same keys (lines 76-77) |
| Rendering | its own cards + table | `buildBuckets` (410) + `renderReleaseSections` (467/493) |
| Release-type filters | ✗ | ✓ (mapping at 263-267) |
| MusicBrainz merge, queue buttons | ✗ | ✓ |

**And nothing links to it.** The only references to `ui.search` anywhere in the
repo are in `templates/pages/downloads/search_playlists.html` — itself dead,
since its route redirects to `/downloads/search`.

Status:

* `New/templates/Pages/search.html` is now a **thin entry point** — a card with
  an "Open search" button that calls
  `global.openUnifiedSearch('all', initialQuery)` (signature confirmed at
  `search-flyout.js:707`), and auto-opens when `?q=` is present. So a bookmarked
  `/search?q=…` still works, against the one implementation.
* **DELETE these two** — written in an earlier pass before this page was
  identified as superseded, and now marked with a "SUPERSEDED — DELETE" banner.
  The template no longer loads either:
  - `New/static/js/pages/library-search.js`
  - `New/static/CSS/search.css`
* Alternative if `/search` is not wanted at all: remove the route
  (`routes/ui_routes.py` → `search()`) and delete the template, the same
  treatment `/smart-playlists` and `/downloads/manager` received.

**Lesson for the remaining pages:** check whether a page's functionality already
lives in a global module (`ui/*`, `main.js`) *before* extracting its inline
script. The tell here was an inline script that duplicated an endpoint and
wrapper shape a global module already consumed.

---

## 6c. Previewing the New tree before cutover — `/new`

A temporary blueprint serves the rebuilt templates so they can be clicked
through while the live routes stay untouched.

**Use it:** open `/new` (there is a **New UI preview** button on the dashboard).
It lists every ported page, links the ones that work standalone, and explains why
the others need their live route's context.

| | |
|---|---|
| `routes/preview_routes.py` | the blueprint — `preview_bp` at `/new` |
| `helpers/app_bootstrap.py` | registers it immediately after `ui_bp` |
| `templates/pages/dashboard.html` | the entry-point button |
| `New/templates/_preview_index.html` | the index page itself |

**How it works, and the one non-obvious part.** The blueprint carries its own
`template_folder` (`New/templates`), so the New pages resolve the *new*
`base.html` and partials rather than the old ones. But those templates call
`versioned_static('js/pages/…')`, and the app's global implementation points at
the LIVE `static/` folder — which still holds the legacy scripts. The preview
therefore passes its own `versioned_static` in the render context: Jinja resolves
context names before globals, so this shadows the global for that render and
points every asset at `/new/static/<path>` (an explicit `send_from_directory`
route, so the URL does not depend on how the blueprint prefix and static path are
joined).

**What is previewable (12):** analytics, banned_words, sandbox, discover,
missing_releases, similar_artists, search, logs, corrections, queue, monitor,
upcoming.

**Not previewable, deliberately:** dashboard, album_detail, artist_detail,
artist_list, track_detail, config, setup, metadata_compare, artist_genres,
artist_corrections, help. Each needs the context its live route assembles;
rendering them empty fails on the first unguarded Jinja expression and proves
nothing. The index names the reason for each. To preview one anyway, add a
context builder to `_preview_context()`.

The index also lists any page file that is in *neither* list, so newly ported
pages show up as "unclassified" rather than being quietly untested.

**Docker:** `Dockerfile` does `COPY . /app` and `New/` is committed, so the
container already has it.

**Delete at cutover:** `routes/preview_routes.py`, its registration in
`helpers/app_bootstrap.py`, the dashboard button block, and
`New/templates/_preview_index.html`.

---

## 7. The two "corrections" pages are NOT duplicates — keep both

Both are live and both are linked, but they serve different APIs and different
jobs. There is **no shared endpoint at all**:

| | `pages/corrections.html` | `pages/artist_corrections.html` |
|---|---|---|
| Route | `ui.correcting` → `/correcting` | `ui.artist_corrections` → `/artist/<name>/corrections` |
| Scope | GLOBAL — field-level tag conflicts across the library | PER-ARTIST — data-quality pass over one artist |
| APIs | `/api/conflicts/{pending,stats,resolve,resolve-batch,ignore}` + `/api/correcting/{fix-album-field,ignore,ignores,unignore,mb-suggestions}` | `/api/artist/corrections/{apply-album-mbid,clear-disc-number,delete-track,merge-albums}` + `/api/duplicate-artists/merge` + `/api/queue/add` + `/api/track/match-missing` |
| Capabilities | conflict resolution, per-field ignore lists, MB suggestions | duplicate tracks, duplicate artists + merge, album MBID apply, disc-number cleanup, title mismatches vs MB, missing MB tracks, match-missing-to-existing |
| Size | 751 lines (493 inline JS) | 1,049 lines (684 inline JS) |
| Linked from | `base.html` navbar → Artists ▸ Tag Corrections | `artist_list.html`, `album_detail.html`, `artist_detail.html`, `artist_detail_v2.html`, `track_detail.html` |

**`artist_corrections.html` is the more robust of the two** by every measure —
size, number of distinct capability areas (7 vs 2), number of endpoints, and
number of call sites. So it is the one to port first.

**But it cannot replace `corrections.html`.** That page is the ONLY UI for
`/api/conflicts/*` (served by `routes/metadata.py`) and for `/api/correcting/*`.
Dropping it would orphan eight live endpoints and remove the navbar's Tag
Corrections entry, i.e. it would delete a feature rather than de-duplicate one.

Decision RESOLVED 2026-09-18: **both were ported.** `corrections.html` is now
the single TAG-CORRECTIONS page, holding both halves — pending metadata
conflicts (`/api/conflicts/*`) and album tag inconsistencies
(`/api/correcting/*`) — because those two are the same job seen from two
angles: a conflict is one field on one track that was blocked from being
overwritten; an inconsistency is an album whose tracks disagree on one field.
The page now presents them as two peer sections with their own headers instead
of a conflicts card followed by a bare `<hr>`. See `Pages/corrections.html`'s
header comment for the layout changes.

`artist_corrections.html` remains a separate, per-artist page because it does
different work: duplicate tracks and artists, merging, album MBID application,
disc-number cleanup, missing MusicBrainz tracks. Nothing in it overlaps the
conflicts API.

So the correction surface is three levels, not three duplicates:

| Level | Page | Route |
|---|---|---|
| WHICH artists are dirty | `artist_list.html` | `/artists` (badges from `/api/artists/corrections`) |
| Fix ONE artist's data | `artist_corrections.html` | `/artist/<name>/corrections` |
| Fix ALBUM tags / conflicts | `corrections.html` | `/correcting` |

---

## 8. The remaining components

None. §5b records the last three (one implemented, two dead/unfinished).
