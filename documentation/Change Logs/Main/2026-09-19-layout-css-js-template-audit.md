# Layout audit — CSS / JS / template fixes

**Date:** 2026-09-19
**Area:** `ui` / `templates` / `css`

## Reported request

> Can you finish the investigation about whether there are ways to improve the
> css, javascript or templates across the new layout?

A 17-phase audit of both template trees. The findings that mattered are below;
the rest were false positives from comment text or affected dead files.

## The critical framing: **live mode is the DEFAULT**

`helpers/test_site_mode.py::config_enables_test_site` returns `False` unless
`features.use_test_site` is explicitly set:

```python
raw = features.get(CONFIG_KEY, False)
...
return bool(raw)
```

and `static_roots()` returns the live tree **only** in live mode:

```python
if config_enables_test_site() and TEST_SITE_STATIC.is_dir():
    roots.append(str(TEST_SITE_STATIC))
if LIVE_STATIC.is_dir():
    roots.append(str(LIVE_STATIC))
```

So the default user experience is the **live** tree. The audit's biggest
surprise was that several defects were *invisible in the tree everyone edits*
and *visible in the tree that actually renders*. Every fix below was therefore
applied to **both** trees.

---

## 1. `100vh` inside a viewport `calc()` — served, real clipping

**Files:** `templates/playlists/index.html`, `test_site/templates/Playlists/index.html`

Three panels sized themselves against `vh`:

```css
max-height: calc(100vh - 320px);
```

On mobile `vh` is the **largest** viewport height (the value with the URL bar
hidden), so a panel sized from it always extends behind the browser chrome. The
project already uses the correct idiom elsewhere — `popularr.css` has three
`100dvh` declarations — so these were stragglers, not a deliberate choice.

**Fix:** `100vh` → `100dvh` in all three `calc()` expressions.

A bare `min-height: 100vh` on an auth-page shell (`login.html`, `setup.html`) is
**not** flagged: those have no bottom edge to clip, and "fill the screen" is the
documented intent.

## 2. Player transport controls below the touch-target minimum

**Files:** `templates/base.html`, `test_site/templates/base.html`

```html
<button id="playerPrev"  style="width: 32px; height: 32px;">
<button id="playerPlayPause" style="width: 40px; height: 40px;">
<button id="playerNext"  style="width: 32px; height: 32px;">
```

Previous/next were **32px** — below the 44px comfortable minimum, on the two
most-tapped controls in the app, sitting in a fixed bottom bar where a miss is
most likely. Play/pause was 40px, so the row was already inconsistent.

**Fix:** all three → 44px, so the row is uniform and meets the target.

## 3. The artist page loaded **no page stylesheet at all**

**Files:** `templates/pages/artist_detail_v2.html`, `static/css/artist.css` (new),
`test_site/static/css/artist.css`

Two independent problems that hid each other:

**(a) The page carried its styles inline.** A 155-line `<style>` block. Verified
pure CSS (no Jinja), so it was safe to extract. An inline block is re-parsed
with every HTML response and cannot be cached; a stylesheet is parsed once.

**(b) `test_site/static/css/artist.css` was dead.** Its header claimed
*"Loaded by artist_detail.html AFTER popularr.css"*. No route renders
`pages/artist_detail.html` — `routes/ui_routes.py:1180` renders
`pages/artist_detail_v2.html`, which has **no** `test_site` override. Nothing in
either tree linked the file (verified: `grep versioned_static('css/artist.css')`
→ 0 hits, the only mentions were prose comments). So its rules — a 60px iOS
focus-zoom guard, a 44px touch target for the artist-ID button groups, and a
mobile table layout — **never applied to any page**.

**(c) It could never have worked anyway.** It styled
`.artist-page tr.album-row`, but the page emits album rows as
`<div class="album-row ...">` — a DIV, not a `<tr>`. Every selector in that
70-line block, including its positional `nth-child ::before { content: 'Year' }`
labels, matched nothing. That is also why nobody noticed it was broken.

**Fix:**
- Extracted the inline block to a new **`static/css/artist.css`**.
- Linked it from `artist_detail_v2.html` with `versioned_static()`.
- Kept the `test_site` copy (the loader prefers it during cutover) but corrected
  its header and **deleted the dead `tr.album-row` block**, replacing it with a
  comment recording why.
- Every selector in the extracted file was checked against the real markup
  (template + component + `artist_detail.js`) — all 18 resolve.

## 4. Add-to-Queue form unusable at `xs`

**Files:** `templates/components/search/_queue_status.html`, `test_site/.../same`

```html
<div class="col-md-1"><select id="queueSource">…</select></div>
<div class="col-md-1"><input id="queuePriority" …></div>
<div class="col-md-1"><button>Add</button></div>
```

Below `md` a bare `col-md-*` is full width, so `col-md-1` collapsed the select,
the priority box and the Add button to roughly 30–40px.

**Fix:** explicit xs widths — `col-12 col-md-3` for the text inputs,
`col-6 col-md-1` for the select/priority, `col-12 col-md-1` for the button.

## 5. CSS gaps that only bit live mode

**Files:** `static/css/popularr.css`

`.btn-outline-secondary` (**140 uses** in live templates) and
`.btn-outline-info` (**59 uses**) had **no rule at all** in the live
stylesheet, so both fell through to Bootstrap's grey and cyan — foreign colours
in a green/neutral theme. The rebuilt stylesheet had them; the live one did not.
Also added:

- `--accent-info` (`#22d3ee`) and `--status-warning` (`#ff6b00`) tokens, which
  test_site defined and live did not.
- `.badge-source-listenbrainz` — the only brand badge with no rule, so
  ListenBrainz alone did not read as a source badge.
- `.source-key-badge` — used by `dashboard.js` and `upcoming_releases.js`
  (3 sites), unstyled.
- Re-pointed the MusicBrainz focus ring at `var(--accent-info)` instead of a
  hardcoded `#22d3ee`.

## 6. `data-mobile-cards` on the bookmarks table

**Files:** `templates/pages/bookmarks.html`, `test_site/templates/Pages/bookmarks.html`

The track bookmarks table had no mobile treatment, unlike the rest of the app.
The attribute hides `<thead>` below 768px and rebuilds each column name from
`td::before { content: attr(data-label) }`, so the `data-label` attributes were
added to every cell **in the same change** — opting in without them would have
*removed* the headers.

**Deliberately NOT applied to `album_detail.html`.** An intermediate version of
this change did add it there, and validation caught the problem:
`album_detail.js` injects missing/update/extra rows whose cells are
non-columnar (an empty cell plus a `colspan=3` cell). The stacked layout labels
each cell individually, so those rows would have rendered unlabelled. Leaving
that table on horizontal scroll is correct.

---

## Verified

| Check | Result |
|---|---|
| New guard suite, pre-fix tree | **8 failed**, 231 passed |
| New guard suite, post-fix tree | **240 passed**, 1 skipped |
| Existing layout guards (`tag_attribute_integrity`, `test_site_shadowing`) | 193 passed |
| Live templates referencing assets absent from `static/` | 0 |
| Extracted `artist.css` selectors with no matching markup | 0 of 18 |

## New guard tests

`tests/test_layout_css_js_template_integrity.py` — each guard pins a failure
mode that produces **no error, no warning and no failing test**:

1. **Orphaned stylesheet.** Every `.css` in either tree must be linked by some
   template. `search.css` is recorded as a known documented orphan (its own
   header says "Nothing loads this file").
2. **Dead selectors.** No `artist.css` may style `tr.album-row`, and the guard
   also pins the `<div class="album-row">` markup that makes it dead.
3. **`data-mobile-cards` without `data-label`.** Covers server-rendered cells
   and, separately, JS-injected rows — with `colspan` annotations exempt, since
   they are row notes rather than columns.
4. **`100vh` inside `calc()`** and **sub-44px player controls.**

## Not changed (audit findings, deliberately deferred)

- **`test_site/templates/Pages/artist_detail.html`** (1428 lines) is unreachable
  — no route renders it. It also uses `d-md-table-header-group`, which is **not
  a Bootstrap 5 utility** and is defined in no stylesheet, so its paired
  `d-none` never lifts. Safe to delete once the vfs permits.
- **`test_site/templates/Pages/downloads/monitor.html`** is a stray artist-page
  snapshot, already pinned in `_SHADOWED_TEMPLATES`, so it can never render.
- **`dashboard.html`'s `min-width: 560px`** is on a `d-none d-lg-block` table,
  so it only applies at ≥1200px where there is room. Not the defect it appeared
  to be in the earlier pass.
- **`popularr.css` cross-tree drift** remains the largest latent risk: the two
  copies have diverged in both directions and only the loader decides which
  wins. Reconciling them is a separate, larger piece of work.
