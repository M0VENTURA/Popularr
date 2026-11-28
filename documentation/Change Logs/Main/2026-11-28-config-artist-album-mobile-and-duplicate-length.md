# Config, artist and album pages: mobile navigation, real error states, and duplicate keep/delete by length

Date: 2026-11-28
Scope: `test_site/templates/Pages/{config,artist_detail_v2,artist_list,album_detail}.html`,
`test_site/static/js/pages/{artist,album}.js`, `test_site/static/js/ui/toast.js`,
`test_site/static/css/{popularr,artists}.css`,
`services/metadata/duplicate_version_service.py`,
`tests/test_duplicate_length_recommendation.py`.

Follow-up to `documentation/web-ui-audit.md`. This is the first three pages of that
audit worked end to end: **config**, **artist** and **album**. Nothing here touches
the shell (`base.html`, navigation, tokens) — that part of the audit said not to.

---

## 1. Album: the Keep/Delete badges named the wrong-length copy

**Reported:** *"The tags to show which tracks should be kept and deleted in the album
pages are falsely showing to keep tracks with the wrong song length."*

### Cause

`recommend_duplicate_keeper()` consulted **audio quality first** and returned the
moment one copy scored higher:

```python
best_quality = max(c["quality"] for c in copies)
if len(quality_winners) == 1:
    return { ... "confidence": "high" }     # ← returned before length was ever read
```

`_quality_score()` reads the file from disk, so a higher-bitrate copy of a truncated
download beat a correct-length copy with a lower bitrate every time. The length rule
only ran **when quality tied**, which is exactly the case where the wrong copy is
usually the better encode — the one the user is complaining about.

The length rule also ran against `quality_winners` only, so a correct-length copy in a
lower quality tier could never win even when the release's own length proved it right.

And with two copies, no reference and differing lengths, control fell through to
**metadata** — a tag-based coin flip on a question that has nothing to do with tags.

### Fix

1. **A proven length now outranks quality.** `_length_outlier_winner(copies,
   expected_duration)` runs over **every** copy before the quality comparison. When it
   names one copy, that copy is the keeper — no bitrate argument.
2. **An unprovable length disagreement names nobody.** New
   `_copies_disagree_on_length()`: if the copies are more than `_LENGTH_OUTLIER_GAP`
   (5 s) apart and nothing proves which length is right, the function returns a **tie**
   with an explanation, instead of letting quality or metadata invent a verdict. The
   page then shows a plain `Duplicate ×N` badge, not Keep/Delete.
3. **The explanation now reaches the badge in every case.** `album.js` only recorded
   `recommendation.reason` when there was a keeper, so the one case that actually
   needs explaining — no verdict — dropped it. It is now always recorded and shown as
   the badge's tooltip.

The tie reason names both lengths:

> These copies run to different lengths (3:12, 4:05) and nothing in their tags or
> encoding says which is the correct one — so neither is marked Keep or Delete.
> Compare the lengths before removing either.

5 s is still "the same length" (the approved tolerance); 6 s is a conflict.

### Tests

`tests/test_duplicate_length_recommendation.py` gained:

* `TestQualityCannotOutvoteAProvenLength` — a better-encoded wrong-length copy is no
  longer the keeper; quality still decides when the lengths agree (control).
* `TestAnUnprovableLengthDisagreementNamesNobody` — no keeper is invented, both
  lengths appear in the reason, 5 s passes and 6 s conflicts.

The eight pre-existing tests in that file are unchanged and still describe the rule.

---

## 2. Config: the section rail filled a phone screen

**Reported by the audit:** config was *broken* on a phone.

The section navigation was `nav flex-column nav-pills sticky-top` at **every** width:
eight stacked links plus the search box — about 300 px — rendered above the first
setting, and `sticky-top` then pinned that wall while a 4 400-line form scrolled
behind it.

* `config.html` — the rail is now a `.config-nav` wrapper holding `#configTabs`
  (buttons only) and a separate `.config-nav-tools` block (divider + "Find a setting").
  `flex-column` is gone from the markup: Bootstrap's utility is `!important`, so no
  media query could have undone it.
* `popularr.css` — `.config-tabs` is a **vertical sticky rail at ≥768px** and a
  **single horizontally-scrolling chip strip below it**, sticky at
  `top: var(--navbar-height)` (plain `top: 0` would park it behind the fixed navbar,
  because `main` is the scrollport and its box starts under the navbar). Only the
  chips stick; the search box scrolls away with the page.
* The page-local `#configToast` markup is deleted. `showToast()` calls in `config.js`
  already resolve to `ui/toast.js`'s global; this copy was dead markup with
  `z-index: 11`, under both the navbar and the bottom nav.

`ui/toast.js`'s stale note claiming config still defines its own `showToast` was
corrected — it does not, and has not since the module migration.

---

## 3. Artist page: three real bugs, not just polish

### 3.1 The favourite heart was wired to an element that does not exist

```js
if (data.is_favourite) document.getElementById('artistFavouriteBtn')?.classList.add('btn-danger');
```

`#artistFavouriteBtn` exists only in `Pages/artist_detail.html` — the **dead alias**
template nothing renders. The live page renders `[data-artist-fav-btn]` with a
`[data-artist-fav-icon]` heart. Consequences:

* the saved state never painted — a favourited artist always showed an unfilled heart;
* `toggleArtistFavourite()` POSTed unconditionally, so it could add a favourite but
  **never remove one**;
* and it finished with `location.reload()`, re-fetching ~24 scripts and restarting
  every poller to repaint one icon.

Now: state is painted from `GET /api/artist/favourite`, kept on the button's
`aria-pressed`, and the toggle sends `DELETE` when already favourited and `POST` when
not. No reload; failures go to a toast.

### 3.2 The shared escaper was being overwritten page-wide

`artist.js` defined its own `escapeHtml` with `createTextNode` + `innerHTML`, which
escapes `& < >` but **not** `"` or `'` — and its output is interpolated into
attributes here (the Discogs "Open" link, the qBittorrent Add button). A quote in a
URL or title broke out of the attribute.

In a classic script a top-level `function escapeHtml` also **replaces
`window.escapeHtml`**, so every other module on the artist page silently inherited the
weaker escaper. The local `escapeHtml`/`formatBytes` are deleted; `utils/dom.js` is
loaded by the shell before any page script and provides both. `escapeJsString` stays —
it is a JS-string escaper, not an HTML one.

### 3.3 Five loaders could fail silently forever

`loadArtistBio`, `loadSinglesCount`, `loadSimilarArtists`, `loadArtistCoveredBy` and
`loadArtistFavouriteState` all used bare `fetch(...).then(r => r.json())`: no `r.ok`
check, no `.catch`. A 500 or an expired session returns HTML, `r.json()` throws, the
rejection is unhandled, and the server-rendered spinner stayed on screen — identical
to "nothing to show". `loadSimilarArtists` additionally threw a `TypeError` when
`similar_artists` or `.lastfm` was absent.

New `artistGetJson()` rejects on any non-2xx; each loader has an explicit empty state
and an explicit failure state.

### 3.4 Album tracklist link

`artist_detail_v2.html` built the track link with
`url_for('ui.track_detail', artist=…, album=…, track_id=…)`. The route takes only
`track_id`, so the other two became a query string nothing reads. Dropped.

---

## 4. Mobile touch targets

* **Artist list** — the corrections icon and View were `btn-sm py-1 px-2`, about 26 px,
  on the page every mobile user reaches through Downloads → Artists. New
  `.artist-row-actions` rule in `artists.css` holds both at 44 px below 768px, matching
  what `artist.css` already does for release rows. The icon-only button also gained an
  `aria-label` (`title` alone is invisible on touch).
* **Album tracklist** — the `#` and Actions columns carried inline
  `style="width: …"`, which outranks every media query, so they could not shrink. They
  are now `.col-track-num` / `.col-track-actions` classes. On a phone the `#` column is
  3rem, Actions 6.5rem, and the four row buttons wrap into a **2×2 cluster** instead of
  demanding 4 × 44 px = 176 px of a 100 px column and pushing the Title off-screen.
* **Album card header** — Link / Align and the mixed-release Merge button were ~24 px
  tall; both are now 44 px on phones.
* **Artist hero avatar** — `role="button" tabindex="0"` had no key handler, so it was
  focusable, announced as a button, and did nothing on Enter/Space. Added `onkeydown`
  and `aria-label`.

---

## 5. Dead page-local styling removed

`album_detail.html` carried an inline `<style>` block on every response (four rules,
one of them a duplicate of the global `.min-w-0`). The three unique rules moved to
`popularr.css`'s "Album page" section; the duplicate was dropped. A page-level
stylesheet cannot be cached across pages and cannot be bundled.

---

## Verification

* `services/metadata/duplicate_version_service.py` and the test file report clean in the
  IDE's language servers; no syntax or type diagnostics on any touched JS or CSS file.
* **The Python test suite could not be executed in this environment** — there is no
  `python` on PATH (only the Windows Store stub) and no Docker. Every claim above is
  from reading the code and the existing tests, not from a green run. Run
  `pytest tests/test_duplicate_length_recommendation.py tests/test_mobile_layout_density.py`
  before merging.

### Existing test contracts preserved

* `tests/test_mobile_layout_density.py` pins the album CSS: `.tracklist-table
  .col-track-duration` appears exactly once (my new rules use different class names),
  the phone block's exact strings are untouched, and the table's class attribute is
  unchanged.
* The eight original duplicate-recommendation tests keep their expectations; the new
  tie path still reports `confidence: "tie"`, which is what
  `test_the_majority_cluster_wins` and `test_two_copies_without_a_reference_do_not_guess`
  assert.

---

## Not done here (still open from the audit)

* Splitting `config.html` (4 479 lines) / `config.js` (3 733) into a schema-driven
  renderer. The mobile navigation is fixed; the page is still one enormous form.
* Collapsing `/downloads`, `/downloads/monitor` and `/downloads/search` into one route.
* `album.js` is still ~4 000 lines with 28 `window` globals.
* The bundle (`esbuild` builds one entry that no template references).
