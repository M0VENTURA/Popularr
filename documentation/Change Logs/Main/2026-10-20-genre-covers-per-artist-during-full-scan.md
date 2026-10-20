# Genre playlist AI covers never ran during a full library scan

**Date:** 2026-10-20
**Area:** `services/popularity/stages/finalise_stage.py` ·
`services/popularity/scan_stage_runner.py` · `helpers/config_helpers.py` ·
`templates/pages/config.html` · `static/js/config.js` (both trees)
**Commit:** `fix(playlists): check the genre AI covers per artist, not only at the end of a scan`

## Report

> "Gemini is still not creating the covers for the genre playlists"

> "It should list any playlist where the tracks are updated after an artist
> scan and then update the cover art via gemini if it's set in the config."

> "The album scan seems to show the playlists, but they dont show during a
> full library scan that does each artist one by one."

> "This is the result from an album scan, still doesn't show gemini output
> though" — a log with the genre playlists syncing and **no cover line at
> all**, not even the "OFF" explanation.

## Root cause — the cover check rides on a rebuild the full scan DEFERS

The AI-cover check lives inside `_create_genre_top_track_playlists`, because
that is where the genre playlists are built and their ids are known.

A full library scan does **not** call that function per artist. The global
genre rebuild is deliberately deferred to the END of the scan
(`services/popularity/scan_scope`): the genre playlists aggregate the WHOLE
library, so rebuilding them after every artist is slow (the measured cost was
5–8 minutes per artist) and semantically wrong.

So during a 940-artist scan the cover check ran **ONCE, at the very end** —
and if the scan was stopped, not even that. Artist by artist nothing
genre-shaped happened at all, which is exactly the reported symptom.

The per-ALBUM refresh could not fill the gap either:

* it only ran when `total_albums <= 1` — a **single-album** scan — so an
  artist scan with several albums never reached any genre work; and
* it re-pools the whole library, which is the expensive half.

⭐ The evidence in the log: the "🎨 Genre playlist covers" line appears
**14 times** in `unified_scan (7).log` — once per TARGETED scan — and
**zero times** across the entire full-scan window (06:14 → 08:05, artist 42
→ 46). The one genre rebuild inside that window is a targeted scan that ran
beside the full scan.

## The fix — the CHEAP half, per artist

`refresh_genre_playlist_covers_for_artist(artist, albums)`:

* asks which genre playlists **this artist's updated tracks** feed (an
  ALBUM-SCOPED query — cheap), and
* ensures each of those playlists carries its AI cover.

It deliberately does **NOT** re-pool the library and does **NOT** re-sync
playlist CONTENT. That stays with the end-of-scan rebuild, which is where it
belongs. This is the same split the deferral was built on — the missing piece
was that the *cover* half had no per-artist path at all.

It is called from `_flush_artist_star_ratings`, which is the **artist
boundary** — so it runs once per artist during a full scan, and once per
artist for a targeted artist scan too.

The pass also **lists** the playlists it touched (the report's explicit ask),
whether or not they exist on the server yet.

## Second defect — a second opt-in for a billable API that has ONE consumer

`api_clients/gemini.py` is imported by **nothing else** in the app. So the
AI-covers toggle in `playlists:` asked the user to opt in **twice**, in two
different config sections, for the same billable API — and the second opt-in
was the easy one to miss. The log said so plainly:

```
🎨 Genre playlist covers: OFF — tick 'AI covers for Top Genre playlists'
in Config ▸ Playlists (adding the Gemini key alone does not enable them)
```

⭐ **`playlists.genre_playlist_ai_cover_enabled` now DEFAULTS TO TRUE.** The
billing guard is the Gemini integration itself (`api_integrations.gemini.enabled`
plus a key), which is off, with no key, until the user supplies one. Turning
the covers off is still possible — the Config page's checkbox writes `false`.

The Config page checkbox and the JS collector in **both** trees were updated
to match, so the page does not silently write `false` back on an ordinary
save.

## Third defect — "enabled but nothing checked" was silent

The end-of-scan summary only printed when a playlist was actually checked, so
a pass that never got that far produced **no line at all** — indistinguishable
from the feature not running. The pass now records that the feature was
enabled (`_cover_enabled`) and says so when it checked nothing.

## Tests

`tests/test_genre_cover_per_artist_full_scan.py` (new, 13 tests) — drives the
real service functions with the database, the Navidrome client and the image
API stubbed:

* the artist boundary reaches the cover pass, and is **not** gated on
  `total_albums <= 1`;
* the affected playlists are LISTED (and a playlist that does not exist yet
  is listed without spending an image);
* a playlist without our art gets it; an already-covered one is not re-billed;
  the playlist id is passed in so no extra Navidrome round trip happens;
* the "toggle off" and "no Gemini key" misconfigurations are REPORTED, and
  the pass never raises;
* an enabled cover pass that checked nothing says so.

`tests/test_genre_ai_covers.py` — the defaults test was **REWRITTEN** with
the reversal documented (the old assertion pinned the contract that caused the
report), and a new test pins that an explicit `false` still wins.

**Oracle:** with the source reverted, **12 of the 13 new tests fail** (the
13th is a control that must hold either way); restored → 13 passed. The
rewritten defaults test fails on the old code too.

**Sweep:** 90 scan/genre/playlist suites + 73 config suites, one process per
file, failing sets diffed against the unpatched baseline — **0 new failures**
in both sweeps. `import app` → **423 routes**. `node --check` clean on both
config JS files.

## Files

* `services/popularity/stages/finalise_stage.py` —
  `refresh_genre_playlist_covers_for_artist`, `_genre_affected_genres_for_albums`,
  `_genre_playlist_ids_by_name`, `_genre_playlist_rows_for_album`, the
  `_cover_enabled` summary branch
* `services/popularity/scan_stage_runner.py` — the artist-boundary call
* `helpers/config_helpers.py` — `get_genre_cover_config` default
* `templates/pages/config.html`, `test_site/templates/Pages/config.html`
* `static/js/config.js`, `test_site/static/js/pages/config.js`
* `tests/test_genre_cover_per_artist_full_scan.py` (new)
* `tests/test_genre_ai_covers.py` (defaults rewritten)

## Notes / limits

* Existing installs that already have `genre_playlist_ai_cover_enabled: false`
  in `config.yaml` keep that value — the reversal only changes the DEFAULT.
  Delete the line to pick up the new default.
* The per-artist pass only covers a genre playlist that **already exists** on
  the server. A playlist created by the end-of-scan rebuild gets its cover
  there, exactly as before.
* Navidrome still needs `EnableArtworkUpload` for non-admin users, and the
  configured user must own the playlist.
