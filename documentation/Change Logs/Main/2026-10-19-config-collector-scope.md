# Every Config save died with "getValue is not defined" — a JS scoping bug

**Date:** 2026-10-19 · **Area:** `ui` / `config`

## Reported

> When entering the Gemini API key in the config
> **Error: Could not collect config: getValue is not defined**

The Gemini detail is incidental — **every** save of the Config page failed, on
any field.

## Root cause: pure scoping, not a missing value

`templates/pages/config.html` → `js/config.js`. `buildConfigObject()` returned the
payload and defined three field readers **inside itself**:

```js
function buildConfigObject() {
  function getValue(id, defaultValue) { … }      // ← declared HERE
  function getChecked(id, defaultValue) { … }
  function parseNumber(id, defaultValue) { … }
  …
  genres: {
    selection: collectGenreSelectionConfig()      // ← called from here
  }
}
```

and `collectGenreSelectionConfig()` — the genre-selection-engine collector — was
declared at **file scope** (live) / **IIFE scope** (rebuilt), hundreds of lines
below:

```js
function collectGenreSelectionConfig() {          // ← DEFINED out here
  return {
    enabled: getChecked('genre_selection_enabled', true),
    max_genres: parseInt(getValue('genre_selection_max', '3')) || 3,
    …
  };
}
```

JavaScript resolves a free variable against **where the function is defined**,
not where it is called. `collectGenreSelectionConfig`'s scope chain is
file → global, which never contained `buildConfigObject`'s locals. So the call
threw `ReferenceError: getValue is not defined`, and `saveConfig()`'s guard

```js
try { config = buildConfigObject(); }
catch (err) {
  showToast('Error', 'Could not collect config: ' + (err && err.message ? err.message : err), 'error');
}
```

rendered it as the reported message. Because the genre-settings block is part of
the object literal, the throw happened on **every** save — including one where
the user had only touched the Gemini key.

⭐ The comment on that guard says it exists so "the button is always restored".
It worked exactly as designed: it caught a real failure and reported it. The bug
was upstream of it.

## Fix

Hoist `getValue` / `getChecked` / `parseNumber` to **file scope** in both trees,
and delete the nested copies. `buildConfigObject` needs no other change — it now
resolves the same three names from the enclosing scope, with identical behaviour
(they are pure `document.getElementById` reads).

They are declared **once**, on purpose. A second copy nested back inside
`buildConfigObject` would shadow the outer one and reintroduce exactly this
split, so a test pins the count.

## ⭐⭐ Why the existing test did not catch it

`tests/test_genre_selection_engine.py` already asserted the file contains
`collectGenreSelectionConfig` **and** `selection: collectGenreSelectionConfig()`
— and passed the entire time the page was dead. It proves the *call* exists, not
that the call *resolves*.

Worse, the obvious harness cannot see the bug either: `tests/js/_extract.js` is
**name-based**, so it will happily pull the *nested* `getValue` out of
`buildConfigObject` and inject it into the harness — the extraction *fixes* the
scope problem silently, and the probe passes both before and after.

⇒ The only harness that observes real scope is the **whole file**, so
`tests/js/config-load-probe.js` loads the shipped script into a Node `vm` context
with a DOM stub and calls the collector. Verified as an oracle:

| tree | `buildConfigObject()` |
|---|---|
| pristine `develop` (`3ed8b16`) | ✗ `getValue is not defined` |
| fixed, live | ✓ `genres.selection` = `{enabled: true, max_genres: 3, family_slot_limit: 3}` |
| fixed, rebuilt | ✓ identical |

The probe asserts **concrete defaults** (`max_genres === 3`), not just "it
returned an object", so a stub returning `{}` cannot pass.

## Tests

`tests/test_config_collector_scope.py` — **11 passed**.

- the script loads, and `buildConfigObject()` does not throw, in both trees;
- the error is specifically *not* `… is not defined` for any of the three
  readers (a `collect_ok`-only assertion would also pass if the probe stopped
  calling the collector at all);
- `config.genres.selection` really is present and populated with its defaults —
  the control that "it did not throw" means "it ran";
- exactly one definition of each reader per file.

## Verification

Swept 103 suites (2 459 tests) including every file that references the config
page. **Zero regressions** — every failure reported was run against pristine
`develop` and reproduced identically:

| suite | pristine | fixed |
|---|---|---|
| `test_download_match_lifecycle` | 10 | 10 |
| `test_scan_stall_fixes` | 7 | 7 |
| `test_artist_tracklist_loading` | 4 | 4 |
| `test_album_save_background_phase` | 1 | 1 |
| `test_queue_failures_return_to_queue` | 1 | 1 |

## ⚠️ Two unrelated things found in this working tree (NOT changed here)

Both are **uncommitted workspace edits**, not part of this fix, and both show up
as red tests. `pristine develop` has the surfaces; this working tree has removed
them from `config.html` **and** `config.js`, in both trees:

| removed | still referenced by | tests now failing |
|---|---|---|
| `api_gemini_*` (Gemini image key/model, "AI Genre Covers") | only `tests/test_genre_ai_covers.py` — the feature itself is gone | `test_genre_ai_covers.py` (4) |
| `playlists_favourite_enabled` / `favourite_playlists_enabled` | `routes/favourites.py`, `services/playlists/favourite_playlist_service.py`, `helpers/config_helpers.py`, `finalise_stage.py` — **the backend is still there** | `test_favourite_playlists_per_user.py` (4) |

The Gemini removal is complete; the favourite-playlists one is **half done** — the
backend that reads `favourite_playlists` survives, but nothing can enable it any
more. Worth deciding deliberately: either restore the control, or remove the
backend too. This changelog does not touch either, because removing a feature (or
rewriting the tests that pin it) is a product decision, not a bug fix.

`api_integrations` as a whole is untouched — only the `gemini` sub-key is gone;
lastfm / listenbrainz / discogs / musicbrainz / audiodb still read from it
across ~20 modules.
