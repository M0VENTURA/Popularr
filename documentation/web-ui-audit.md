# Web UI audit — all pages, mobile + desktop

Date: 2026-11-24
Scope: `test_site/templates/` (31 rendered surfaces), `test_site/static/css/` (17 files),
`test_site/static/js/` (66 files, ~25k lines in `pages/` alone), `routes/` (44 modules, 4 of which render HTML).

This is an audit plus a rebuild plan.

**Status (two passes done):** see
`Change Logs/Main/2026-11-28-config-artist-album-mobile-and-duplicate-length.md` and
`Change Logs/Main/2026-11-28-bookmarks-track-and-mobile-table-pass.md`.

First pass — §2.5 (artist `escapeHtml`), §3 config (the phone navigation rail), §6.3
(config), §4.4's artist-avatar key handler, and the album tracklist column widths and
touch targets. Also found and fixed while working the album page: the duplicate
Keep/Delete badges could name a wrong-length copy as the keeper (quality was scored
before length); the artist favourite heart was wired to an element that only exists in
the dead template, so it could never unfavourite.

Second pass — §2.1 (bookmarks), §2.2/§2.3 (track page overlay, unstyled tab bar and the
sticky tabs behind the navbar), §2.4 (duplicate analytics route), Phase 2's remaining
mobile tables (corrections, missing releases, queue events, sandbox, banned words), and
the dead files listed in §5.2. Changelog:
`Change Logs/Main/2026-11-28-bookmarks-track-and-mobile-table-pass.md`.

Third pass — §5.3 / Phase 4: the downloads trio is ONE page. `/downloads` and
`/downloads/search` (plus its three legacy sub-paths) now 302 into
`/downloads/monitor` with the query string and a tab fragment preserved, and
`monitor.js` opens the tab the fragment names. The two subset templates are
deleted. Changelog:
`Change Logs/Main/2026-11-28-downloads-trio-collapsed.md`.

Fourth pass — §6 candidate 5: `/metadata-compare` is retired. It turned out not
to be merely unbounded: it queried `beets_year`, which is not in the schema,
models or any migration (the Beets importer lives in `old_system/deprecated/`),
so it could never render; its "Beets" column was actually `musicbrainz_genres`
compared against itself; and both write endpoints targeted columns that do not
exist. It now redirects to `/correcting`, which already protects and reports
`genres` and `year` through `metadata_conflicts`. Changelog:
`Change Logs/Main/2026-11-28-metadata-compare-retired.md`.

Still open — Phase 1 (the real bundle), `/search`'s stub route, `artist_list`'s
~1000 unpaginated rows, the dashboard's five 5-second polls, and the four
oversized page modules.

---

## 1. Executive summary

The **shell is good and should not be rebuilt**. `base.html` already has the hard parts right:
a real mobile navigation model (bottom tab bar + offcanvas system sheet, no hamburger), a
declared z-index scale, safe-area insets, server-resolved active state, and a coherent CSS
token layer. That is more than most self-hosted apps ever get to.

The problems are one layer down, and they cluster into five groups:

1. **Three pages are outright broken on a phone**, and two more have a live functional bug
   independent of viewport. These are cheap fixes, not rebuilds.
2. **`/config` is not a page, it is a 4 479-line form with an 8-item vertical tab rail** that
   renders above the content at every breakpoint. It cannot be made usable on mobile by
   patching; it needs a different information architecture.
3. **There is no build step the app actually uses.** `esbuild.config.mjs` exists, builds one
   entry point, writes to a `dist/` directory that isn't in the tree, and no template
   references the output. Every page ships 17–31 unminified individual requests. This is the
   single highest-leverage structural fix.
4. **Navigation and page inventory have drifted.** Seven real pages have no nav entry, two
   templates are dead, one route redirects, and three separate URLs serve the same page with
   one tab removed each time. Three different CSV importers exist for one endpoint.
5. **Page controllers have grown past the point of maintainability**: `album.js` 4 011 lines /
   28 `window` globals, `config.js` 3 733 / 33, `playlist.js` 2 318 / 33, `download-queue.js`
   2 089 / 30. Each mixes state, polling, rendering, destructive actions and modals in one IIFE
   and publishes its internals onto `window` so inline `onclick` handlers can reach them.

**Recommended shape of the work:** fix the five real bugs first (a day, not a project), then
land the bundle, then rebuild `/config` and collapse the downloads trio, then split the four
giant page modules. Details in §8.

---

## 2. Verified bugs (found during this audit)

These are defects, not opinions. Each was read directly in the source.

### 2.1 `/bookmarks` always renders empty — the route passes no context

`routes/ui_routes.py:5175`

```python
@ui_bp.route("/bookmarks")
async def bookmarks() -> Any:
    return await render_template("pages/bookmarks.html")
```

The template reads `bookmarks` and `filter_type`:

- `bookmarks.html:2` — `{% if filter_type %}` (title)
- `bookmarks.html:46` — `{% if bookmarks and bookmarks|length > 0 %}` (the only list branch)
- `bookmarks.html:38-41` — four filter links driven by `filter_type`

Jinja's `Undefined` is falsy, so the list branch is never taken: the page renders its
"You don't have any bookmarks yet" empty state **unconditionally**, and all four filter
buttons render as if "All" were selected regardless of `?filter=`.

Both nav entries point at `?filter=artist` (`base.html:246`, `_system_sheet.html:66`), i.e.
straight onto the dead state. Favourites never appear anywhere in the UI.

**Fix:** either wire the route to the bookmarks repository and read `request.args.get("filter")`,
or delete the page and surface favourites on the artist/album/track pages. The second is less
work and fits the existing entity pages better.

### 2.2 The track page's quick bar covers the mobile bottom nav

`test_site/static/css/track.css:76-87`

```css
.track-quick-bar {
  position: fixed;
  bottom: 0; left: 0; right: 0;
  z-index: 1990;
  ...
}
```

The shell declares its stacking order in one place (`popularr.css:88-94`):

```css
/* Z-index scale — declared in one place so the stacking order is auditable. */
--z-player-bar: 1029;
--z-search-backdrop: 1028;
```

`.bottom-nav` uses `z-index: var(--z-player-bar)` (`popularr.css:1330`), and the global player
bar uses the same. **1990 paints over both**, so on `/track/<id>` a phone loses its primary
navigation and its player controls behind the Rescan/Save bar. `1990` also breaks the declared
scale, which is the exact thing the comment says the scale exists to prevent.

The compensating spacer is a hard-coded 68px (`track_detail.html:829`):

```html
<div class="d-lg-none" style="height: 68px;"></div>
```

against a real bar of `3.5rem + env(safe-area-inset-bottom)` = 56px + inset, plus the quick bar.

**Fix:** give the quick bar a token (`--z-quick-bar`, above the player bar but below modals),
position it `bottom: calc(var(--bottom-nav-height) + env(safe-area-inset-bottom))` so it sits
*above* the nav instead of on it, and replace the hard-coded spacer with the same `calc()`.

### 2.3 The track page's mobile tab bar has markup and a JS engine but no styling

`track_detail.html:178-184` declares the bar:

```html
<!-- Mobile Bottom Bar Trigger (driven by main.js's mobile tab engine) -->
<div class="mobile-tab-bar d-lg-none" role="tablist" aria-label="Track sections"
     data-mobile-tabs data-group-attr="data-mobile-tab"
     data-active-class="mobile-tab-active">
  <button type="button" class="tab-btn active" data-tab="details" role="tab" aria-selected="true">…
```

`main.js:201-205, 861-866` implements the engine that toggles `mobile-tab-active`. But
**`.mobile-tab-bar` and `.tab-btn` have no rule in any file under `static/css/`** — verified by
searching all 17 stylesheets for both selectors and for `[data-mobile-tabs]`. The only related
rules are the content-visibility bridge (`popularr.css:1143-1147`, `track.css:63`).

So the bar renders as four unstyled browser buttons in document flow, and its own `active` class
(styled nowhere) does nothing visually. `track_detail.html` is the only adopter of the engine.

**Fix:** style the bar consistently with `.bottom-nav` (it is the same interaction pattern), or
drop the bar and use the system sheet. Do not leave a component whose classes are inert.

### 2.4 Two handlers for `/analytics/genres-moods`; one is dead code

| Handler | Blueprint | Registered |
|---|---|---|
| `routes/analytics.py:15` — fetches `get_genre_mood_analytics(top_n=50)` and passes context | `analytics_bp` | first (`helpers/app_bootstrap.py:56`) |
| `routes/ui_routes.py:5792` — `render_template("pages/analytics.html")` with **no context** | `ui_bp` | later (`:70`) |

Flask matches the first registration, so `ui_routes.py:5792-5794` can never run. It is dead
code that would render an empty page if the registration order ever changed. Delete it.

### 2.5 `escapeHtml` in `pages/artist.js` does not escape quotes

`pages/artist.js:7-16` re-implements the helper the shared one already provides:

```js
function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;      // escapes & < > — but NOT " or '
}
```

versus `utils/dom.js:59-62`:

```js
return String(value).replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch]);
```

`artist.js` then interpolates the result into attributes — `artist.js:232`
`<a … href="${escapeHtml(c.discogs_url || '#')}"` — and into a JS string inside an attribute
(`artist.js:129`, `:278`). A discogs URL or title containing `"` breaks out of the attribute.

This is the precise failure mode `dom.js` documents having fixed. Fix: delete the local copy,
use `global.escapeHtml`.

---

## 3. Mobile scorecard — every rendered surface

`GOOD` = usable at 360px as shipped. `PARTIAL` = usable but compromised.
`BROKEN` = needs repair before it is usable on a phone.

| Page | Route | Mobile verdict | Cause when not GOOD |
|---|---|---|---|
| Dashboard | `/dashboard` | **GOOD** | Reference implementation: table ↔ carousel swapped by class, `.library-bento` |
| Activity | `/activity` | **GOOD** | List rows from a shared macro |
| Artists | `/artists` | **PARTIAL** | Rows fine; actions are `btn-sm py-1 px-2` ≈ 26px, one icon-only with `title` only |
| Artist detail | `/artist/<name>` | **GOOD** | `data-mobile-cards` + `data-label` on every cell; scroll-snap jump chips |
| Album detail | `/album/<path>` | **PARTIAL** | Tracklist opts out of `data-mobile-cards`; header keeps `width:56/100/120/100px` |
| Track detail | `/track/<id>` | **BROKEN** | §2.2 overlay + §2.3 unstyled tab bar |
| Corrections | `/correcting` | **PARTIAL→BROKEN** | Non-wrapping 3-button action row; 3 tables, no `data-label`, no column hiding |
| Artist corrections | `/artist/<n>/corrections` | **GOOD** | The best mobile table in the tree — 4 tables all stacked |
| Artist genres | `/artist/<n>/genre-management` | **PARTIAL** | `.track-title { min-width: 140px }`, zero breakpoints in `artist-genres.css` |
| Config | `/config` | **BROKEN** | 8 vertical pills above content at every width; `sticky-top` pins a 300px wall |
| Sandbox | `/config/sandbox` | **PARTIAL** | `position: sticky; top:0` card eats half the phone viewport permanently |
| Missing releases | `/missing` | **BROKEN** | JS-built table with `width:3rem/5rem/7rem`, **no** `.table-responsive` at all |
| Metadata compare | `/metadata-compare` | **PARTIAL** | Scrolls, never restacks; percentage-width prose columns |
| Analytics | `/analytics/genres-moods` | **GOOD** | `col-12 col-xl-4`, one breakpoint at 640px |
| Discover | `/discover` | **GOOD** | `col-12 col-sm-6 col-lg-4 col-xl-3`, and best loading/empty/error states in the app |
| Bookmarks | `/bookmarks` | **GOOD** (visually) | `data-mobile-cards` correct — but see §2.1, always empty |
| Banned words | `/downloads/banned-words` | **PARTIAL** | Header row cannot wrap (no `flex-wrap`) |
| Downloads monitor | `/downloads/monitor` | **PARTIAL** | 9 scripts; row actions `btn-sm py-0 px-2`; queue-events table unwrapped |
| Downloads queue | `/downloads` | **PARTIAL** | Same, minus folders |
| Downloads search | `/downloads/search` | **PARTIAL** | Same, minus queue |
| Upcoming | `/downloads/discover/upcoming` | **GOOD** | `data-mobile-cards` + column hiding at 576 |
| Similar artists | `/downloads/discover/similar-artists` | **PARTIAL** | Grid is fine; no empty/error state |
| Playlists | `/playlists` | **PARTIAL** | Two independently-scrolling panes stacked, each `calc(100dvh - 320px)` |
| Playlist CSV import | `/playlist/import/csv` | **GOOD** | `col-12`, `flex-md-row` — but no `container` wrapper |
| Help | `/help` | **GOOD** | Sidebar → sticky toggle bar at ≤768 |
| Login / Setup | `/login`, `/setup` | **GOOD** | Centred single-column cards |
| `/search` | `/search` | **DEAD** | Stub that opens the flyout; "nothing links here any more" |
| `/logs` | `/logs` | **DEAD** | Route 302s to the dashboard |
| `/test-site` | preview harness | **PARTIAL** (dev only) | — |

**Count: 3 broken, 12 partial, 13 good, 3 dead.**

Every `GOOD` table shares one mechanism (`popularr.css:720-768`, `[data-mobile-cards]`), and
every `PARTIAL`/`BROKEN` table is a table that either didn't adopt it or is generated in JS that
never learned about it. **The fix already exists in the codebase; it just isn't applied
uniformly.** That is a strong argument against a from-scratch table rewrite.

---

## 4. Structural findings

### 4.1 The bundle is decorative

`esbuild.config.mjs:19-22` builds exactly one entry:

```js
const entryPoints = {
  main: join(__dirname, "test_site", "static", "js", "main.js"),
};
```

Three things make it inert:

1. **No template references the output.** `base.html:647` loads `js/main.js` (the raw source),
   not `dist/main.js`. `{{ dist }}/…` (`base.html:23`) is used only for vendor CSS/JS.
2. **`test_site/static/dist/` does not exist** in the tree.
3. **`copyVendorAssets()` never copies** — `esbuild.config.mjs:66-78` imports `copyFileSync` but
   only reads and logs `✓ ${src} → ${dest}`. So `features.use_local_assets` (`base.html:24`)
   would 404 on every vendor path; the app is on the CDN branch in practice.

`package.json` has no build gate — `"build": "node esbuild.config.mjs"` and nothing invokes it.

**Request budget per page:**

| Page | JS requests | CSS requests |
|---|---|---|
| any page (shell) | **17** | 4 (+2 icon fonts) |
| `/dashboard` | **25** | 5 |
| `/artist/<name>` | **24** (7 page scripts, incl. the downloads-search controller) | 5 |
| `/track/<id>` | **22** | 5 |
| `/downloads/*` | 20–25 | 5–6 |

All unminified, all with comment headers.

### 4.2 Shared modules exist and are good; adoption is the gap

`utils/api.js`, `utils/poller.js`, `utils/dom.js`, `ui/toast.js`, `ui/confirm.js`,
`ui/button-state.js`, `ui/status-badge.js`, `services/item-groups.js` are genuinely well built —
several carry headers documenting the duplication they replaced.

But:

- **`esc()` is copy-pasted 9×** — character-for-character identical wrapper in `main.js:54`,
  `analytics.js:46`, `dashboard.js:86`, `monitor.js:79`, `upcoming.js:85`, `item-groups.js:57`,
  `upcoming-releases.js:92`, `status-badge.js:44`, `confirm.js:66`.
- **Status vocabulary is forked.** `ui/status-badge.js` owns the mapping (with aliases for
  `downloading`/`in_progress`/`active`), yet `soulseek-search.js:464-471` re-derives it by
  substring and renders the **raw DB string** (`initiati` — truncated to 8 chars at `:494`).
- **`formatScanPercent` is duplicated** between `main.js:300` and `dashboard.js:296`, with a
  comment in each saying "kept identical to the copy in the other file — do not let them drift".
- **`pages/artist.js` uses raw `fetch(...).then(r => r.json())`** throughout — no `r.ok` check,
  no `.catch`, spinner left spinning on failure (`artist.js:287-316`).

### 4.3 No single error/empty/loading contract

| Contract | Example |
|---|---|
| All three states, mutually exclusive | `pages/analytics.js:118-143` — the model |
| Deliberately silent on error | `dashboard.js:393-399` (`return` on catch) — leaves stale data, no signal |
| Unhandled rejection, spinner forever | `artist.js:287-316` |
| Static "Loading…" text, no error branch | `_queue_status.html:57` |
| No loading, no error, no empty | `downloads/search.html`, `metadata_compare.html` |

### 4.4 Accessibility: three patterns re-invented per page

The shell gets it right: every icon-only nav link has `title` + `aria-label` (`base.html:220-229`,
with a comment explaining why), the player buttons are 44px with `aria-label`, `toast`/`busy-popup`
set `role="status" aria-live="polite"`.

Failures, all instances of a pattern not being shared:

1. **`<div role="button" tabindex="0">` with no key handler** — focusable, announced as a button,
   Enter/Space does nothing. `artist_detail_v2.html:100-103` (artist image), and the same shape on
   every release row (`_release_section.html`, `.release-summary`), which also nests a real
   `<button>` inside the fake one → two tab stops for one action.
2. **`role="progressbar"` with no `aria-valuenow`/`min`/`max`** — `base.html:506`, the primary media
   progress bar. Two other progress bars in the app *do* set all three
   (`dashboard.js:495-500`, `soulseek-search.js:497-500`).
3. **Icon-only buttons with `title` only, no `aria-label`** — `album_detail.html` art button,
   `base.html:499-516` player controls. Inconsistent with the rule the navbar follows.
4. **No `<h1>` on the dashboard** — first heading is `<h6>` (`dashboard.html:83`). Album page goes
   `<h1 class="h5">` → `<h5>` (1 → 5 jump).
5. **Star ratings emitted as glyph runs** (`{% for i in range(track.stars) %}★{% endfor %}`) — a
   screen reader reads "black star black star black star", never "3 of 5".

### 4.5 Performance

1. **Dashboard polls 5 endpoints every 5 s** (`dashboard.js:1230-1240` + `:1291-1296`, plus
   `main.js:907-913`). 3 600 requests/hour per open tab. `poller.js` pauses on `document.hidden`,
   which is the only thing making it tolerable.
2. **A 1 s timer exists only to redraw a countdown** on top of two other 5 s pollers in
   `soulseek-search.js` (`COUNTDOWN_INTERVAL_MS`).
3. **`/artists` server-renders ~1000 rows**, each with an `<img>` to a per-artist render endpoint,
   no intrinsic `width`/`height` → layout shift (`artist_list.html:161-166`).
4. **Full-page reloads for one row of state**: `artist.js:303-306` (favourite toggle → `location.reload()`),
   `:185` (IDs), `bookmarks.js:70-72`. Each reload re-fetches 17–25 scripts and restarts every poller.

---

## 5. Page inventory: reachability and duplication

### 5.1 Seven live pages have no navigation entry

Determined against the complete nav vocabulary in `base.html:232-339`, `base.html:376-400` and
`_system_sheet.html:65-134`.

| Page | Only way in |
|---|---|
| `/search` | none — self-documented dead end |
| `/metadata-compare` | none |
| `/config/sandbox` | none |
| `/downloads/discover/similar-artists` | none |
| `/playlist/import/csv` | none — Playlists uses its own modal instead |
| `/artist/<n>/corrections` | one row button in `/artists` |
| `/artist/<n>/genre-management` | one chip on the album hero |
| `/downloads` (queue) | one link from `/playlists` |
| `/downloads/search` | deep-linked with `?q=` from every "Search Soulseek" button |
| `/activity` | one link from the dashboard |

**Recommendation:** delete `/search`, `/metadata-compare`, `/config/sandbox`,
`/downloads/discover/similar-artists`, and `/playlist/import/csv`. Promote
`/artist/<n>/corrections` and `/artist/<n>/genre-management` onto the artist hero, where the
sibling actions already live. Promote `/activity` next to the dashboard card that summarises it.

### 5.2 Dead templates and dead code

| Item | Evidence |
|---|---|
| `Pages/logs.html` | Never rendered; `/logs` 302s (`ui_routes.py:5085`); self-declares "⛔ DEAD — DO NOT PORT" |
| `Pages/artist_detail.html` | Pure alias: `{% extends "Pages/artist_detail_v2.html" %}` with a dead body in `{% raw %}`; never rendered |
| `ui_routes.py:5792` | Duplicate route, unreachable (§2.4) |
| `js/pages/library-search.js` | Orphan (the page that loaded it is a stub) |
| `css/search.css` | Self-declares "⛔ DEAD FILE — DELETE ME" |
| `js/pages/artist_releases.js` | Underscore twin of `artist-releases.js`; not referenced by any template |
| `js/pages/folder-groups.js` | `base.html:584-593` says it was removed; it is still on disk |
| `soulseek-search.js:483-518` | Builds a 7-column monitor table whose host ids exist in no template |
| `downloads.css:217-237` | `.upcoming-monitor-table` — `monitor.html` removed that section |

### 5.3 Three URLs, one page, sliced three ways

| URL | Contains |
|---|---|
| `/downloads/monitor` | folders + Soulseek + playlists + queue |
| `/downloads` | Soulseek + playlists + queue |
| `/downloads/search` | Soulseek + playlists |

The nav points only at the largest. `monitor` loads **9 scripts**, `queue` 9, `search` 6 — and
`search` pulls the 2 318-line `playlist.js` for one function (`downloads/search.html:80-83`
says as much: "splitting createPlaylist out is worth doing").

**Recommendation:** one route, real tab router, `#folders`/`#soulseek`/`#queue` as the tabs,
legacy URLs 302 into it.

### 5.4 Three CSV importers for one endpoint

`/downloads/search` Playlists tab, `Playlists/index.html:180-231` modal, and
`Playlists/importer_csv.html` all POST `/api/playlist/import/csv`. Keep one.

### 5.5 Inconsistent vocabulary for the same concept

"Missing" is solid amber in `_release_section.html:162`, a raw inline `rgba(100,116,139,0.2)` for
upcoming in the same file, `bg-danger` in `corrections.html:57`, and a hard-coded `#fd7e14` in
`artist_corrections.html:50`. Green means "owned" on the artist page and "Ready" on the queue page.
`utils/missing-tracks.js` exists precisely because the album page said "3 bonus" while the artist
page said "3 missing" for the same album.

Four confirmation mechanisms coexist (`ui/confirm.js`, per-page Bootstrap modals, decorative
post-hoc progress modals, and **no confirmation at all** for `Purge All`, `Clear Queue`,
`Clear Imported`, `runDbCleanup` — all inline `onclick`).

---

## 6. Rebuild candidates, ranked

| # | Candidate | Size | Why it can't be patched |
|---|---|---|---|
| 1 | **`config.html` + `config.js`** | 4 479 + 3 733 | 8 vertical tabs over ~50 cards; **no stylesheet of its own, no breakpoint**; 14 mixed concerns; 33 window globals; a 410-line hand-transcribed YAML schema that must be edited in lockstep with the backend; 3 competing pollers; its own duplicate toast. A schema-driven renderer (server emits field descriptors, one form) deletes most of both files. |
| 2 | **Downloads trio** (`monitor`/`queue`/`search`) | 9 scripts each | One page sliced three ways, nav points at one, destructive actions unconfirmed, `downloads/search` drags in `playlist.js` wholesale. |
| 3 | **`album.js` + `album_detail.html`** | 4 011 + 1 162 | 10 concerns, 28 globals: playback, an entire track-edit modal, genre staging, MB compare/apply, MBID lookup, two delete modes, release merge, tab restore. One `try` chain's worth of compat shims because the shared modules are optional. Paired POST handler is 1 200 lines across 5 phases. |
| 4 | **`/bookmarks`** | 60-line route | §2.1 — the route passes no context. Rebuild (or delete) is the fix. |
| 5 | **`/artist/<n>/corrections`** | 643 + 956 | 19 handlers, deletes DB row **and file**, a 5-step merge that rewrites audio on disk, `jsonify` on error so a failure renders JSON not a page, no nav entry, 4 hard-coded hex colours. Most dangerous, least discoverable. |
| 6 | **`playlist.js`** | 2 318 | 8 concerns incl. a ~700-line smart-playlist builder, the downloader, two recommendation feeds, and its own `formatDuration`. |
| 7 | **`artist_list`** | 1 000 rows SSR | Unbounded, no search/filter, per-row image requests, 26px actions. |
| 8 | **`download-queue.js`** | 2 089 | 9 concerns, 30 globals, 10 s poll, 6 destructive actions in one module. |

**Runner-up:** `missing_releases.html` + JS — small page, but the generated table is the only one
in the app with no responsive wrapper at all, so it's a rewrite of ~30 lines either way.

---

## 7. What not to touch

- **The shell** (`base.html:93-400`, `popularr.css:125-135, 229-287, 1296-1382`). Bottom nav +
  system sheet + app header, declared z-index scale, safe-area handling, server-resolved active
  state. This is the strongest part of the UI and a rebuild would regress it.
- **`[data-mobile-cards]` table stacking** (`popularr.css:720-768`). Every good mobile table uses
  it; extend adoption, don't replace the mechanism.
- **`utils/api.js`, `utils/poller.js`, `ui/confirm.js`, `ui/button-state.js`.** Good APIs — the
  problem is adoption, not design.
- **`services/item-groups.js`** — the shared row shell that already wraps its action cluster
  instead of squeezing the title (`:216-237`, with the bug documented in its header).
- **`pages/analytics.js`** — the reference for loading/empty/error.
- **Dashboard table↔carousel duality**, `.library-bento`, help/analytics/bookmarks layout,
  login/setup cards.

---

## 8. Recommended sequence

### Phase 0 — fix the five verified bugs (do this first, regardless of rebuild plans)

1. Wire or delete `/bookmarks` (§2.1).
2. Tokenise and reposition `.track-quick-bar`; correct the spacer (§2.2).
3. Style or remove `.mobile-tab-bar` / `.tab-btn` (§2.3).
4. Delete `ui_routes.py:5792` (§2.4).
5. Replace `artist.js`'s local `escapeHtml` with `global.escapeHtml` (§2.5).

All five are small and independent. None of them is invalidated by anything later in the plan.

### Phase 1 — make the bundle real

Highest leverage single change. Add `utils/*`, `ui/*`, `services/*` as one `shell` entry; emit to
`dist/`; add CSS entry points so the 17 stylesheets collapse to two with content hashes; point
`base.html:611-664` at the built files; fix `copyVendorAssets()` so `use_local_assets` works.

Payoff: 17–31 requests → 2, minification over ~66 files, and the duplicate `esc()` wrappers
disappear for free once modules can `import` from `dom.js` instead of reaching for `window`.

**Prerequisite for everything modular.** Do not do Phase 3 before this.

### Phase 2 — apply the mobile fixes that already have a mechanism

Cheap, no new architecture:

- `missing_releases.js:206` → wrap in `.table-responsive` + `data-mobile-cards` + `data-label`.
- `corrections.html` → `data-mobile-cards` on all three tables; `flex-wrap` on the action row.
- `_queue_status.html:321` → responsive wrapper.
- `album_detail.html` → decide: stacked cards on mobile (consistent with the rest) or keep the
  scroller and remove the hard `width` from `<th>`.
- `artist_list.html:194-199` → 44px actions, matching what `artist.css:202-230` already does for
  release rows.
- `sandbox.html:43` → unstick on mobile / collapse to a summary row.
- `banned_words.html:29` → `flex-wrap`.
- Extend the `@media (max-width: 575.98px) { .btn-sm { min-height: 44px } }` rule to rows — the
  current blanket `min-height: 0` exemption (`popularr.css:1129-1131`) is what makes every row
  action ~26px.

### Phase 3 — rebuild `/config`

The only page that genuinely needs a new information architecture:

- section switcher as horizontal scroll chips (mobile) / horizontal tabs (desktop), not a vertical rail;
- server-emitted field descriptors replacing the 410-line hand-copied YAML schema;
- split the 3 pollers into one poller service;
- delete `#configToast` and the `showToast` shim.

### Phase 4 — collapse the downloads trio into one route with tabs

302 the legacy URLs. Split `createPlaylist` out of `playlist.js` first. Add `ui.confirm` to
`Purge All` / `Clear Queue` / `Clear Imported` / `runDbCleanup`.

### Phase 5 — split the four giant page modules

In order of payoff: `album.js`, `config.js` (if not already rebuilt), `playlist.js`,
`download-queue.js`. Target: one module per tab/section, no `window` globals, inline handlers
replaced by `data-action` delegation (the stated convention that most of the tree already follows).

### Phase 6 — delete the dead surface

`/search` + `library-search.js` + `search.css`, `/metadata-compare`, `/config/sandbox`,
`/downloads/discover/similar-artists`, `/playlist/import/csv`, `logs.html`, `artist_detail.html`,
`ui_routes.py:5792`, the stray `artist_releases.js` / `folder-groups.js`, and the unused
`soulseek-search` monitor table.

---

## 9. Method and limits

- `grep` is non-functional in this workspace (returns zero matches for strings that exist), so
  every keyword search was done via directory listing + direct file reads. Call counts and
  cross-file JS call graphs were therefore **not** exhaustively traced; claims are grounded in the
  markup and the module headers, which is what a UI audit can act on.
- The four large controllers (`album.js`, `config.js`, `playlist.js`, `download-queue.js`) were
  read at their heads and exported-API tails, not end to end. Concern counts may undercount.
- Page inventory was derived by enumerating every `render_template(...)` call across all 44 route
  modules; only `ui_routes.py`, `playlists.py`, `analytics.py` and `preview_routes.py` render HTML.
- The `/test-site` preview harness has its own inventory defects (wrong slugs for
  `playlist_import_csv` and `sandbox`, `discovered` de-duplicates by template stem), so it is not
  a reliable page list.
- Bugs in §2 were each confirmed by reading the cited source line.
