# Bookmarks, track page and the mobile table pass

Date: 2026-11-28
Scope: `routes/ui_routes.py`, `routes/preview_routes.py`,
`test_site/templates/Pages/{bookmarks,track_detail,corrections,sandbox,banned_words,search}.html`,
`test_site/templates/components/search/_queue_status.html`,
`test_site/static/js/pages/{corrections,missing-releases,search-entry}.js`,
`test_site/static/css/{popularr,track,sandbox}.css`,
`tests/test_layout_css_js_template_integrity.py`, plus four deleted dead files.

Second batch from `documentation/web-ui-audit.md`, after the config/artist/album pass.

---

## 1. `/bookmarks` could only ever render "you have no bookmarks"

**Reported by the audit:** the page is always empty.

```python
@ui_bp.route("/bookmarks")
async def bookmarks() -> Any:
    return await render_template("pages/bookmarks.html")      # ← no context
```

The template reads `bookmarks` and `filter_type` (`bookmarks.html:2`, `:38-41`,
`:46`). Jinja's `Undefined` is falsy, so `{% if bookmarks and bookmarks|length > 0 %}`
was never true: the empty state rendered unconditionally and all four filter links
rendered as "All" whatever `?filter=` said. Both nav entries point at
`?filter=artist`, i.e. straight onto the dead state.

### The table has two column sets

The **current** schema (`db/schema.py`, `db/models.py`) is:

```
id, bookmark_type, type, entity_id, name, artist_name, album_name, title, notes, created_at
```

Databases created by the older app also carry `artist`, `album`, `track_id`, `url`.
`CREATE TABLE IF NOT EXISTS` means a long-lived database keeps whichever it was born
with, so a query naming either set's columns is a bet.

The route therefore reads `SELECT *` and normalises in Python
(`_normalise_bookmark_row`):

* `favourite` spellings collapse to the three kinds the page groups
  (`artist_favourite` → artist, `track_favourite` → track, `album_favourite` → album);
* `name`/`title`, `artist`/`artist_name`, `album`/`album_name`,
  `track_id`/`entity_id` are read with fallbacks;
* artist favourites store their name in `name` only, and track favourites store the
  **track id** in `name` — both are handled.

Track favourites are then enriched from `tracks` (`_fill_track_bookmark_details`), so
the Tracks table lists a title instead of a bare UUID. The IN list is built from bound
placeholders, and the enrichment is optional — a failure degrades to the raw id.

A read failure now renders an alert instead of an empty list (`bookmarks.html`).

`routes/preview_routes.py` claimed the rows "load client-side"; they do not. Corrected.

---

## 2. Track page: the quick bar covered the navigation

### 2.1 `z-index: 1990` over a 1029 shell

`track.css` positioned `.track-quick-bar` at `bottom: 0; z-index: 1990`. The shell
declares its stack in one place (`popularr.css`): `--z-player-bar: 1029`, used by both
the player bar and the mobile bottom nav. **1990 painted the Rescan/Save bar over the
bottom navigation and the player**, so a phone lost its primary navigation on
`/track/<id>`. 1990 also broke the declared scale — the exact thing the scale's comment
says it exists to prevent.

Now: `bottom: var(--footer-stack-height)` (the token that already accounts for the
bottom nav, the player bar when visible, and the safe-area inset) and
`z-index: var(--z-quick-bar)`, a new token at 1030 — above the player/nav, below
offcanvas (1045), backdrop (1050) and modal (1055).

The spacer that keeps the last card clear of the bar was a hard-coded `68px`; it is now
`.track-quick-bar-spacer` with `calc(var(--footer-stack-height) + 4.5rem)`.

### 2.2 The mobile tab bar had no CSS at all

`track_detail.html:179-184` renders `.mobile-tab-bar` / `.tab-btn` and `main.js`'s
engine toggles `active` on them — but **no stylesheet under `static/css/` defined either
class**. The bar rendered as four raw browser buttons and the active state styled
nothing.

Styled like the `.nav-segmented` control in the same card: a four-column grid, 44px
minimum height per button, green active fill. The engine's `active` / `aria-selected`
handling needed no change.

### 2.3 The sticky tab card parked behind the navbar

```html
<div class="card mb-4 sticky-top shadow-sm" style="top: 0; z-index: 1000;">
```

`sticky-top` is `top: 0`, and `main` is the scrollport whose top edge sits under the
fixed navbar — so the tab strip scrolled behind it. Replaced with
`.track-tabs-card` (`position: sticky; top: var(--navbar-height); z-index: 20`).

---

## 3. Mobile tables

### 3.1 Corrections (`/correcting`)

* The three-button action row (`Fix All`, `Lookup MusicBrainz`, `Ignored`) had no
  `flex-wrap` and pushed the card wider than a phone.
* All three tables now opt into `data-mobile-cards`:
  * the server-rendered inconsistencies table got `data-label` on its three cells;
  * the conflicts table and the MusicBrainz suggestions table are filled by
    `corrections.js`, so their labels are written there — the `cell()` helper gained a
    `label` argument, and the MB rows carry them inline.
* Per-row actions now get the 44px touch minimum from the shared dense-table rule below.

### 3.2 Missing releases

`missing-releases.js` built the only generated table in the app with **no**
`.table-responsive` wrapper, so its fixed `# / Duration / Action` columns pushed the
title out of the card. Wrapped. (The stacked-card layout is deliberately not used: the
injected queued row has an empty last cell and would render unlabelled.)

### 3.3 Queue events

`_queue_status.html`'s events table sat in a container with `overflow-y` only — no
horizontal escape at all. Wrapped in `.table-responsive`.

### 3.4 Row actions in dense tables

The `btn-sm { min-height: 0 }` exemption is deliberate (it fixed the "stretched
buttons" reports) and stays. But a table's per-row action is the one `btn-sm` that is
**tapped** rather than read — corrections' Fix/Ignore, missing-releases' queue buttons,
the queue log. Below 768px `.table .btn-sm` now gets 44px. This is the "dense context
opts back in" pattern the exemption's own comment describes, and
`test_mobile_chrome_layout_fixes.py` still passes because it inspects the ≤575.98
block, which is unchanged.

---

## 4. Sandbox and banned words

* `sandbox.html` pinned its whole control panel (`position: sticky; top: 0`) at every
  width — a scope row, artist field, three sliders and a 2×2 summary, roughly half a
  phone viewport, permanently fixed. Now `.sandbox-controls`: sticky on desktop
  (offset below the navbar), static on a phone.
* `banned_words.html`'s title + Back button overflowed 360px; `flex-wrap` added.

---

## 5. Dead code removed

| Removed | Why |
|---|---|
| `static/js/pages/library-search.js` | A second full search implementation; superseded by `ui/search-flyout.js`. Its own page said "delete both". |
| `static/css/search.css` | Styles only that implementation used. It was the last entry in the stylesheet orphan allow-list, which is now empty. |
| `static/js/pages/artist_releases.js` | Underscore twin of `artist-releases.js`; zero references. |
| `static/js/pages/folder-groups.js` | `base.html` already documented its removal from the shell block; still on disk. |
| `ui_routes.py` — duplicate `/analytics/genres-moods` | `analytics_bp` registers first and passes the data; the copy here had no context and could never run. |
| `tests/…integrity.py` — `_KNOWN_ORPHANS` | Emptied, since `search.css` no longer exists. |

Comments in `search.html`, `search-entry.js` and four page templates that described the
deleted files as "delete these" / "loaded globally" were corrected.

---

## Verification

* LSP diagnostics clean on every touched Python/JS file.
* **The suite was not executed here** — no `python` on PATH and no Docker in this
  environment. Run before merging:
  `pytest tests/test_layout_css_js_template_integrity.py tests/test_mobile_chrome_layout_fixes.py tests/test_downloads_monitor_mobile.py`.

### Test contracts checked by reading

* `test_layout_css_js_template_integrity.py::test_mobile_cards_tables_label_every_cell`
  — every newly opted-in table either has server-rendered `data-label`s or an empty
  tbody (skipped by the guard).
* `…::test_js_filling_mobile_cards_tables_sets_data_labels` — `corrections.js` writes
  into `conflicts-tbody` (now inside a `data-mobile-cards` table) and contains a
  literal `data-label=`, which is what the guard looks for.
* `test_mobile_chrome_layout_fixes.py::test_btn_sm_is_exempt` — reads the ≤575.98 block;
  the exemption is unchanged and the new rule lives in a separate ≤767.98 block.
* `test_downloads_monitor_mobile.py` — its queue-events assertions are on the
  stylesheet, not the markup, so the wrapper is invisible to it.

---

## Still open from the audit

* Phase 1, the real bundle (`esbuild` builds one entry no template references).
* Collapsing `/downloads`, `/downloads/monitor` and `/downloads/search`.
* `/metadata-compare` — unbounded `GROUP BY`, mislabelled "Beets" column,
  `metadata_locked` written without a confirm, no nav entry.
* `/search` still exists as a stub that opens the flyout; removing the route is a
  product decision, not a bug fix.
* `/activity` and `/downloads/search` have no error/empty states.
* `artist_list` server-renders ~1000 rows with no search or pagination.
* Dashboard polls five endpoints every 5 seconds.
* Splitting `album.js` (4 011 lines), `config.js` (3 733), `playlist.js` (2 318),
  `download-queue.js` (2 089).
