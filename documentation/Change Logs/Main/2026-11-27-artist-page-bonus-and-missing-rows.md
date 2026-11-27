# Artist page: bonus tracks called "missing", and missing rows not listed

**Date:** 2026-11-27
**Commit:** `fix(artist): stop counting bonus cuts as missing; list them in the tracklist`

## Reported

Two symptoms on the artist page, with screenshots.

1. Album row "Black Sails at Midnight" (Alestorm, 2009) showed a yellow
   **"3 tracks missing"** badge — while the album page for the same album showed
   **"3 Bonus to download"** with `#11 P is for Pirate`, `#12 Weber Und Weinen`,
   `#13 Heavy Metal Pirates` each badged **Bonus**.
2. Expanding the album listed 10 tracks (the collection's rows) and nothing
   else — so a badge claiming three were missing had nowhere to show them.

## Root causes

Both client-side. The server was already right in both cases.

### 1. The artist page never subtracted bonus cuts

`/api/album/missing-tracks` returns `missing_count`, `bonus_count` **and**
`missing_core_count`, plus each row with `is_bonus`.

- `album.js:updateMissingHeaderBadge` — `core = count - bonus`. Correct.
- `artist-releases.js:fetchMissingTrackCounts` — `var count = data.missing_count`.
  Raw. This produced "3 missing".
- `artist.js:initCorrectionsAndMissingTracks` — the same raw read, with
  different wording ("3 tracks missing"), on **the same span**. Two writers
  racing one element.

### 2. The artist tracklist had no missing-row concept at all

`artist-releases.js:loadTracklist` rendered only
`/api/album/tracklist` — the rows in `tracks`. There was no call to the missing
endpoint for content, no `buildMissingRow` equivalent, no badge. The album page
has done this inline-by-position since `placeMissingRow` was added.

## Fix

**One rule, one file.** New `test_site/static/js/utils/missing-tracks.js`
(loaded from `base.html`) exposes `splitMissingCounts()` and
`missingBadgeText()`. It prefers the server's authoritative
`missing_core_count`/`bonus_count` and derives them from the rows only for an
older payload.

- **`artist-releases.js`** — the badge now comes from the helper. A bonus-only
  album gets "3 bonus" in **green** with
  `title="Bonus tracks to download — not an album gap"`, matching the artist
  *list* page's vocabulary; a real gap stays yellow with
  "N tracks missing · M bonus".
- **`artist.js`** — the duplicate writer is removed. It refetched every album's
  missing tracks for a badge that another module already owned.
- **`artist-releases.js:appendMissingTracks`** — the expanded tracklist now
  appends the album's missing rows and re-sorts by leading position, so a
  missing track sits where it belongs. Bonus rows get a grey **Bonus** badge, a
  queued row shows its queue status, and — on a compilation — the track's own
  performer is shown next to the title, since the album artist is
  "Various Artists".
- **`album.js`** — reads the same helper, so the two pages cannot drift again.

Owned albums only: a missing or upcoming release has no collection rows to be
missing *from*, and its tracklist already comes from MusicBrainz.

## Not changed

`GET /api/album/missing-tracks` still returns every non-ignored row in
`missing_count` with the split alongside. Changing the server would silently
move the decision away from the only two places that render it; the split is
already correct and already sent. The artist **list** page
(`ARTIST_MISSING_MB_SQL` in `routes/artist_routes.py`) was already splitting at
the SQL level and needed nothing.

## Tests

`tests/test_artist_page_bonus_tracks_and_missing_rows.py`:

- the split rule (bonus-only → "3 bonus", mixed → "3 tracks missing · 2 bonus",
  singular, empty, and an older payload with no `missing_core_count`);
- the artist page reads the helper, not `missing_count` raw;
- bonus-only albums get the green class;
- `artist.js` no longer writes or refetches the badge;
- the tracklist fetches and renders missing rows, labels bonus rows, shows
  queue status and the compilation performer;
- the album page reads the same helper.

Node-backed cases skip when `node` is not on PATH; the arithmetic was verified
against the file separately.
