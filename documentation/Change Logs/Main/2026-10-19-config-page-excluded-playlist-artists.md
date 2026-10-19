# Config page now manages excluded playlist artists

**Date:** 2026-10-19 · **Area:** `ui` / `config` / `playlists`

## Reported

> "The excluded artist list is configurable from `config.yaml`, but there's no UI
> for it — and because `/config/save-json` replaces the whole `playlists` block,
> any save from the Config page would wipe the list."

## What was actually happening

The playlist generator already respects `playlists.excluded_artists` (via
`helpers/config_helpers.get_excluded_playlist_artists()` and the
`/api/artist/playlist-exclusion` endpoint), but the Config page had **no input
for it**. Worse, the `buildConfigObject()` collector only emitted the fields it
knew about, so saving the Config page produced a `playlists` dict without
`excluded_artists`. The server-side save route then overwrote the YAML with that
slim payload, silently deleting the user's exclusions.

## The change

### UI

A new **Excluded Playlist Artists** card is added to the Playlists tab in both
templates (`templates/pages/config.html` and
`test_site/templates/Pages/config.html`):

* an input + Add button (Enter key also submits);
* rendered artist badges with a remove button;
* case-insensitive duplicate detection.

### JS manager (both trees)

`static/js/config.js` and `test_site/static/js/pages/config.js` now:

1. read `pageConfig.playlists.excluded_artists` on load;
2. maintain an local `configExcludedArtists` array;
3. render/add/remove badges;
4. emit `playlists.excluded_artists: [...configExcludedArtists]` in
   `buildConfigObject()`.

This is the same pattern already used for strip-keywords and upcoming-release
sources, so the list survives a save unchanged unless the user edits it.

### Safeguard test

`tests/js/config-load-probe.js` now seeds `pageConfig.playlists.excluded_artists`
and the rebuilt/live scripts expose `loadExcludedArtists()` so the probe can
initialise the list. The probe output includes the round-tripped value.

`tests/test_config_collector_scope.py` gained `TestExcludedArtistsRoundTrip`,
which asserts the collector emits exactly the seeded list.

## Tests

* `tests/test_config_collector_scope.py` — **12 passed** (was 11).
* Probe directly verifies `excluded_artists: ["Artist A", "Artist B"]` in both
  trees.
