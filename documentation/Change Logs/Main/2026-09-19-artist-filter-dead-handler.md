# Artist page: In Library / Missing filter did nothing

**Date:** 2026-09-19
**Area:** `ui` (templates / static JS)

## Reported symptom

> The filter between In Library and Missing on the artist page still doesn't
> work. I can't hide the missing releases that are populated.

The **All / In Library / Missing** buttons on the artist page produced no
change: no rows hid, no rows appeared, nothing happened at all.

## Root cause

`static/js/artist_detail.js` **is not JavaScript.** It is a 5346-line **Jinja
page template**:

```
line 1    {% extends "base.html" %}
line 3    {% block title %}{{ artist_name }} · Popularr{% endblock %}
line 5    {% block content %}
line 5346 {% endblock %}
```

Yet `templates/pages/artist_detail_v2.html` loads it as code:

```jinja
<script src="{{ versioned_static('js/artist_detail.js') }}"></script>
```

The browser parses the Jinja as JavaScript, throws

```
SyntaxError: Unexpected token '%'
```

on line 1, and **discards the entire file**. Not one of its functions was ever
defined.

The filter bar calls its handler from inline `onclick` attributes:

```html
<button ... onclick="setArtistFilter('all')">All (…)</button>
<button ... onclick="setArtistFilter('library')">In Library (…)</button>
<button ... onclick="setArtistFilter('missing')">🟡 Missing (…)</button>
```

`setArtistFilter` was defined **only** in that file:

```
git grep -rn "function setArtistFilter" origin/develop
→ static/js/artist_detail.js:3910   (sole hit)
```

so every click threw

```
ReferenceError: setArtistFilter is not defined
```

and nothing filtered. Confirmed directly:

```
node --check static/js/artist_detail.js
SyntaxError: Unexpected token '%'   at 1:1
```

### How the file got this way

Commit `c228b35d` ("Update artist_detail.js") replaced a working ~3000-line JS
module with this Jinja page template. The genuinely-working copy still exists in
an older revision, which is how the regression is confirmed below.

## Why this was invisible

This class of defect produces **no server error, no app-level console warning,
and no failing Python test**. The asset serves fine (200, correct MIME), the
page renders, and only the JavaScript silently dies. The pre-existing
"artist-filter-toggles" changelog even recorded the symptom — *"This function
did not exist ANYWHERE in the codebase"* — without identifying that the whole
file was unparseable, so the fix was attempted in the wrong place.

## Fix

The filter is restored as its own module, `static/js/artist-album-filter.js`,
loaded **before** the broken file:

```jinja
<script src="{{ versioned_static('js/artist-album-filter.js') }}"></script>
<script src="{{ versioned_static('js/artist_detail.js') }}"></script>
```

A `SyntaxError` in one `<script src>` does **not** stop later scripts from
running, so this restores the filter immediately without depending on the
larger repair.

The module carries the original filter's semantics verbatim, which matter and
are preserved deliberately:

- **Status sources:** `.category-section .album-row[data-status="library"|"missing"]`
  — matching the markup `render_release_category` produces in
  `artist_detail_v2.html`.
- **Asymmetric matching:** only an explicit `missing` counts as missing. An
  unexpected or absent status leans to *Library*, because wrongly hiding an
  owned album is worse than wrongly showing a missing one.
- **Empty sections are hidden:** a category card left with zero visible rows is
  hidden, so it cannot keep its header and a stale "N / M in Library" badge
  above an empty body.
- **Toggle-off:** re-clicking the active filter returns to **All**, so the
  buttons behave as on/off switches rather than a sticky 3-way radio.
- **Persistence:** the choice is remembered in `localStorage`, inside a
  `try/catch` so private mode cannot break filtering.
- **`window.setArtistFilter`** is published explicitly, because inline `onclick`
  resolves by global name.

## Tests

`tests/test_static_js_is_not_jinja.py` — 75 cases.

- `test_static_js_contains_no_jinja` — parametrised over **every** `.js` file in
  `static/` and `test_site/static/`, asserting no Jinja token appears in
  uncommented code and that no file begins with a Jinja tag. Comments are
  stripped first, because documentation legitimately quotes Jinja.
- `test_artist_filter_module_is_loaded_and_publishes_globals` — pins the
  specific regression: the module must exist, define `setArtistFilter`, publish
  it on `window`, and be loaded by `artist_detail_v2.html`.
- `test_js_files_are_discovered` — stops the scan becoming vacuous.

**Oracle verified:** against the current (broken) `artist_detail.js` the suite
fails with `…contains Jinja template syntax in code (314 token(s)…)`; the scan
found this as the **only** affected file out of 73.

Functional verification (`node`, simulated DOM with library-only, missing-only
and mixed sections): 10/10 assertions pass, including *"In Library hides EVERY
missing row"* — the reported case. The module also parses cleanly under
`node --check`.

## Related defect found — NOT fixed here (needs a decision)

Repairing the larger file is a separate change. Extracted verbatim, its two real
`<script>` bodies give 66 functions and parse cleanly (only
`{{ artist_name|tojson }}` → `window.artistName` is needed, which the template
already publishes). That would revive ~63 currently-dead handlers such as
`checkMissingReleases`, `loadArtistBio`, `editArtistCountry`,
`openArtistImageModal` and `toggleTracklist`.

Comparing that extraction against the older working copy shows the clobbered
version also **lost 21 functions** the previous module had. Four are still
called by the current template and are defined **nowhere** in the live tree:

| Handler | Called from |
|---|---|
| `goToArtistAbout` | `artist_detail_v2.html:342` |
| `forceArtistMetadataRefresh` | `artist_detail_v2.html:402` |
| `playArtistTopTracks` | `artist_detail_v2.html:410` |
| `toggleArtistBio` | `artist_detail_v2.html:607` |

Each currently throws `ReferenceError`. They need either implementations or the
buttons removing — that is a product decision, not a mechanical fix, so it is
recorded rather than guessed at.

### Two smaller issues in the same file

- Line 542 uses the **old** import path `_album_category_section.html`; the file
  now lives at `components/_album_category_section.html`.
- `downloadSlskdFile` is declared twice with different arities (a pre-existing
  collision, unaffected here since the declarations were copied verbatim).
