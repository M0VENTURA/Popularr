# The Playlists tab is one card per playlist, and the config page's layout CSS existed at last

**Date:** 2026-11-23
**Area:** `test_site/templates/Pages/config.html` Â·
`templates/pages/config.html` Â· `test_site/static/css/popularr.css` Â·
`static/css/popularr.css` Â· `test_site/static/js/pages/config.js` Â·
`static/js/config.js` Â· `helpers/config_helpers.py`
**Commit:** `feat(config): regroup the playlists settings and restore the missing config layout CSS`

## Reported / observed

Two separate problems, one of which made the other look worse.

**1. The served tree's stylesheet defined none of the config layout classes.**
`Pages/config.html` used `.setting-card`, `.setting-info`, `.setting-control`,
`.thresholds-grid`, `.input-card`, `.naming-card`, `.help-text` and
`.config-section` in 138 places. `test_site/static/css/popularr.css` defined
**zero** of them â€” they existed only in the live tree's
`static/css/popularr.css`. With no rules, `.setting-card` is a plain block, so
the switch wrapped *under* its description (directly above the next setting's
title), and every `.input-card` number field stretched the full width. The
definitions are now in both trees.

**2. Essential Playlists was one 584-line column of ~70 settings.** Each
playlist's toggle sat near the top, its limits in the middle and its name
template at the very bottom, so nothing could be read as a unit. Dependent
settings were editable while their parent switch was off.

## What changed

### Playlists: one card per playlist

The Playlists tab is now seven labelled sections, each keeping a playlist's
toggle, name and limits together:

| Section | Contents |
| --- | --- |
| Global Rules | Minimum Stars, Track Ordering, Max Tracks Per Artist, Spread Artists, Weekly Shuffle (master + three per-type switches), Exclude Christmas + marker, Excluded Artists |
| Genre Top Tracks | Enable, Create/Delete threshold, Max Tracks, Genres Per Track, Delete-below-threshold, Name Template |
| Artist Collections | Essential (enable, max, name, featured appearances) and Vocalist (enable, max, name, guest appearances, seven thresholds under an *Advanced* fold) |
| Library Playlists | One row each for New Music, New Releases, Hit Singles, Hidden Gems, Decades, Acoustic, High Energy, Mood Mixes |
| Covers | Great Covers, per-genre split, main-genre checkboxes, min/max, name templates |
| Per-User Favourites | Enable, min stars, two names, sync at scan end |
| Artwork | AI genre covers, regenerate, prompt |

The old **How it works** footer was cut off at the bottom of the page; it is now
a collapsed `<details>` block at the top, the same pattern the Genre Selection
Engine uses.

### Dependent settings are nested and disabled with their parent

Sixteen groups carry `data-playlists-depends-on="<parent id>"`.
`initPlaylistDependentGroups()` greys the group out and blocks interaction when
the parent is off. The group stays **visible** rather than disappearing, so a
setting never appears to vanish:

vocalist guests + thresholds Â· covers-genre split + its settings Â· the three
shuffle sub-switches Â· the Christmas marker Â· AI regenerate + prompt Â·
favourite options Â· and the limits for New Releases, Hit Singles, Hidden Gems,
Decades, Acoustic, High Energy and Mood Mixes.

### Help text that did not match the code

Fixed after reading the backend, not by guessing:

- **Vocalist Playlist Max Tracks** described *Essential* collections. It now
  describes vocalist collections.
- **Create Threshold** said "more than 100"; `finalise_stage` compares
  `< create_threshold`, so creation happens at **at least** that many.
- **Maximum Genres** claimed a default of 3 while `GENRE_TRACK_LIMIT_DEFAULT` and
  `genre_selection.py` both use 4. The stale docstring in
  `get_genre_track_limit()` said 3 too and is corrected.
- **New Music** said "created once 100 tracks qualify, capped at 100".
  `_NEW_MUSIC_MIN_TRACKS` is 25; the cap of 100 is fixed and is now labelled as
  such rather than implying it is configurable.
- **High Energy** thresholds (`danceability >= 0.65`, `bpm >= 115`) are hard
  constants in `finalise_stage` and are now labelled fixed.
- **How it works** said "more than 12 unique 4★/5★ tracks"; `_ESSENTIAL_MIN_TRACKS`
  is 12 and the comparison is `>=`, so it is **at least** 12.
- The section header's description was repeated verbatim as the first setting's
  description. The header is now **Playlists** (it covers library-wide
  playlists, not just artist collections) with its own one-line summary.

### Genre section

- **Source Weights** and **Genre Synonyms** are marked *legacy engine only*.
  Measured: `genres.weights` is read only by the legacy vote-counting path
  (`_vote_genres` â†’ `_source_weight` â†’ `get_genre_weights`);
  `services/enrichment/genre_selection.py` never imports it and uses
  `genres.selection.source_weights` (Provider Trust) instead. Two panels with
  the same provider names and different values now say which engine they drive.
- The synonym textarea's example was `r&b: rnb` â€” backwards. The canonical
  vocabulary already maps `rnb`, `r and b` and `rhythm and blues` to `R&B`.
- Provider names are spelled consistently in both panels (MusicBrainz, Last.fm,
  AudioDB, ListenBrainz) instead of `|title` producing "Musicbrainz"/"Lastfm".
- **Genre Vocabulary** gained a searchable table (id, display, parent, level,
  aliases) with the raw JSON kept as an explicit *advanced* view. Both views
  read the same textarea, so Load / Validate / Save / Reset are unchanged.

### Page-wide

- **Find a setting** in the sticky sidebar filters the settings in the active
  tab by label, help text and placeholder, hiding sections that lose every card.
  Escape clears it.
- **Changed from default** marks any field whose value differs from the
  documented default (the `placeholder`, the page's established convention) and
  adds a per-field reset arrow.
- **Unsaved changes** in the save bar, counted against the *loaded* values â€”
  deliberately a separate baseline from "differs from default", so a customised
  install is not permanently "unsaved".

## Verification

- Every one of the 74 `playlists_*` ids that existed before still exists, in
  **both** trees, with none added (`test_the_two_trees_render_the_same_playlist_settings`).
- All 16 dependent groups resolve to a parent switch that is actually rendered.
- Rendered `/config` parses with html5lib: the `#playlists` card contains all
  92 ids and 16 subgroups, with no early close and no nested duplicate.
- The vocabulary table renders all 224 entries and filters correctly ("rock" â†’
  45).
- Backend-to-collector ratchet: every key `get_playlists_config()` returns is
  emitted by both collectors; no collector read points at a missing input.
- Full suite unchanged at **403 failed / 9774 passed / 322 skipped**, byte-identical
  to the baseline at the previous HEAD. The two
  `test_favourite_playlists_per_user` failures (`dedupe_titles` TypeError) were
  confirmed pre-existing by running them against a clean stash.

## Not done, deliberately

- The seven vocalist thresholds are folded under *Advanced* rather than moved to
  another tab: they are rarely edited and all belong to one decision.
- No section was made collapsible in the page-wide sense. The Playlists tab is
  now seven short sections instead of one long column, which is what the
  collapsing was for.
