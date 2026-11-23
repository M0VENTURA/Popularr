# The Preview buttons were dead: a `dataset` key with a hyphen throws

**Date:** 2026-11-23
**Area:** `test_site/static/js/pages/config.js` · `static/js/config.js` ·
`services/scanning/reconciliation.py` · `tests/test_config_dataset_attribute_names.py` (new)
**Commit:** `fix(config): camelCase the dataset keys that killed every Preview button`

## Reported / observed

> "None of the preview changes on the updated Playlists or Star Ratings pages seem
> to work, but they worked in the old version."

Both Preview buttons, plus Apply, were inert — no request, no error, no output.

## Root cause: `dataset['data-config-loaded']` is not a DOM property name

The dirty-tracking / "changed from default" work added two attribute constants:

```js
const CFG_DEFAULT_ATTR = 'data-config-default';
const CFG_LOADED_ATTR  = 'data-config-loaded';
…
el.dataset[CFG_LOADED_ATTR] = configFieldCurrent(el);
```

`dataset` is a `DOMStringMap`, not a plain object: its keys must be the
**camelCased** attribute name. Assigning a key containing `-` does not
silently create a property — it throws:

```
Uncaught SyntaxError: Failed to set a named property 'data-config-loaded' on
'DOMStringMap': 'data-config-loaded' is not a valid property name.
```

The throw happened inside `snapshotConfigLoadedValues()`, called from
`initConfigDirtyTracking()`, called from `initConfigPageRefresh()` — which is the
**second statement of the `DOMContentLoaded` listener**. Every preview binding sits
below it, so nothing that follows ever ran. The buttons were not broken; they were
never bound.

The same file already used the correct form in one place
(`delete el.dataset.configLoaded`), which is what made the discrepancy easy to miss.

### Why every static check passed

The failure is only reachable by executing the page:

| Check | Result |
| --- | --- |
| ids present in the rendered HTML | pass |
| no duplicate ids | pass |
| preview containers not inside `d-none` / closed `<details>` | pass |
| collector script fetched (200, non-empty) | pass |
| config layout CSS defined in the served sheet | pass |
| endpoints return JSON | pass |
| `dataset` key validity | **fails only at runtime** |

Confirmed by driving headless Edge with `--dump-dom` over a page built from the
real `/config` HTML and the real scripts inlined. Before the fix:

```
ERRORS(1): Uncaught SyntaxError: Failed to set a named property 'data-config-loaded'
           on 'DOMStringMap': 'data-config-loaded' is not a valid property name.
star_resync_preview_btn   | outBefore=0 | outAfter=0
genre_resync_preview_btn  | outBefore=0 | outAfter=0
genre_playlist_resync_preview_btn | outBefore=0 | outAfter=0
```

After:

```
ERRORS(0): none
star_resync_preview_btn   | outAfter=2533
genre_resync_preview_btn  | outAfter=485
genre_playlist_resync_preview_btn | outAfter=129
```

Fixed in both trees. `tests/test_config_dataset_attribute_names.py` pins the
camelCase constants, forbids `dataset['data-…']`, and asserts the preview
bindings still come after the init call — the ordering is what made the throw
fatal rather than cosmetic.

## Also in this change

**`Ambiguous Navidrome ID adoption … candidates=1`** — the MBID-adoption path
logged "ambiguous" whenever the compatible set was not exactly one, so a *single*
candidate that simply failed corroboration was reported as ambiguity. It now logs
a rejection at info level when nothing corroborated, and reserves the warning for
a genuine tie between several corroborating rows. The return value is unchanged —
one bad candidate was and is refused.

**The files are still not fully cleared on a copy** — deliberately left alone.
`update_file_metadata` writes a fixed key set and clears nothing else, so a
downloaded `encoder` / `comment:essentia *` / `acoustid_fingerprint` / `media`
rides through a copy. Clearing them is a real change to what an import is allowed
to destroy, not a bug fix, and it needs its own pass.
