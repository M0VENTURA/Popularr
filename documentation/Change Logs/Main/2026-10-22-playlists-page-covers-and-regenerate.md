# Playlists page: cover art, a Gemini regenerate button, and the track list

**Date:** 2026-10-22
**Area:** `routes/playlists.py` · `templates/playlists/index.html` ·
`test_site/templates/Playlists/index.html` · `static/js/playlists_index.js` ·
`test_site/static/js/pages/playlists.js` · both `static/css/popularr.css`
**Commit:** `feat(playlists): cover art, AI cover regeneration, and the track list`

## Request

> "On the playlists page, it should show the cover art for each of the
> playlists that are imported. It should also have a button next to the
> playlist to regenerate the cover art with gemini if the setting is
> configured in the config page. Please create the layout so it fits with the
> style of the dashboard or artist page, with a nice display on mobile.
> Selecting a playlist should also show the tracks in the playlist."

## What was actually missing (measured, not assumed)

* **Navidrome already had the art.** `getPlaylists` / `getPlaylist` return
  `coverArt`, and `getCoverArt` serves the bytes — `fetch_all_playlists` passes
  the raw payload through untouched. `api_playlists_all`, however, **projects
  a new dict per playlist** and simply never named the field, so the page had
  nothing to render.
* **The browser cannot fetch it.** `getCoverArt` needs the Subsonic
  credentials, and an `<img src>` cannot carry them — so a server-side proxy is
  required, and which playlist maps to which Navidrome cover is a server
  decision anyway (a file-backed smart playlist is only a NAME until Navidrome
  is asked).
* **The track list already worked.** `selectPlaylist` → `/api/playlists/tracks`
  was implemented; it is now pinned by tests so the redesign cannot regress it.

## The changes

**Backend**

| Piece | What it does |
| --- | --- |
| `_playlist_cover_url(source, id, file_path, name)` | Builds the in-app thumbnail URL (query params, `urlencode`d so a name with `&` cannot split it) |
| `GET /api/playlist/art` | Resolves the playlist, reads its **`coverArt`** field and proxies the bytes; the placeholder SVG on any miss |
| `POST /api/playlist/regenerate-cover` | Calls the existing Gemini generator with `regenerate=True` |
| `_ai_cover_available()` | The gate, reported to the UI as `ai_cover_available` |
| `_genre_from_playlist_name(name)` | `"Metalcore - Top Tracks"` → `"Metalcore"`, using the configured template |

⭐ The proxy uses the playlist's **`coverArt`** value, not its id —
`getCoverArt` expects a coverArt ID (`pl-<hash>`), so the playlist id would 404
or return an unrelated image. Pinned by a test.

⭐ The regenerate route is async and runs the generator in a threadpool: it is a
multi-second billable call and a sync handler would stall the worker.

⭐ `_ai_cover_available()` **fails CLOSED** — an unreadable config reads as
"unavailable", because offering a billable action you cannot verify is worse
than not offering it.

**Frontend (both trees)**

* A fixed-size cover tile on every playlist card, and a larger one in the
  detail header. Both fall back to an icon tile — `onerror` swaps the `<img>`
  for one — so a playlist with no art renders a neutral tile, never a broken
  image.
* A **New Cover** button, hidden unless the server says AI covers are
  configured, disabled while generating (a double-click would spend quota
  twice), with the new art **cache-busted** on success (the thumbnail URL is
  unchanged, so the browser would keep its cached copy).
* `alt=""` on both images: the name sits beside them, so alt text would read the
  playlist name twice to a screen reader.

**Styling (both `popularr.css`)**

`.playlist-cover` (40×40) and `.playlist-detail-cover` (56×56), both
`object-fit: cover`, over the theme's `--secondary-bg` with the same hairline
`.card` uses. A `max-width: 991.98px` rule shrinks the detail cover to 44px so
the action buttons are not pushed off a phone screen.

## Verified in a browser (not just tests)

Rendered the page with stubbed data and measured the live DOM:

| Check | Result |
| --- | --- |
| Card cover tiles | 3, each **40×40**, `object-fit: cover` |
| Detail cover | **56×56** desktop → **44×44** at 390px |
| Selecting a playlist | detail opens: name, "Regular playlist · 42 tracks" |
| Track rows | **3** rendered from `/api/playlists/tracks` |
| Mobile (<768px) | stacked rows visible, scrolling table hidden |
| Actions at 390px | **no overflow** |
| Regenerate button | visible with the gate on, reads "New Cover" |

## Tests

`tests/test_playlists_page_covers.py` (new, 54): the URL builder (including the
quoting case), the AI gate (each half, plus the fail-closed config error), the
art proxy (bytes served, ⭐ the **coverArt id** is what is requested, the
placeholder paths, file-backed name resolution, async), the regenerate route
(refusal when unconfigured, the name requirement, `regenerate=True`, the
playlist id forwarded), the genre-from-name parsing, and both templates + both
JS trees + both stylesheets (cover tiles, fallback, `alt=""`, the button's
hidden default and its gate, the disable and cache-bust, and the mobile rules).

**Oracle:** 48 of the 54 fail against the pre-change commit; the 6 that pass
are the pre-existing track-list pins.

Sweep of 23 playlist/UI/route suites diffed against a pristine clone —
**identical failure sets, 0 new failures** (the 1 pre-existing failure is the
long-documented `test_static_js_is_not_jinja[static/js/artist_detail.js]`, an
untouched file). `import app` → 427 routes (+2).

## Files

* `routes/playlists.py` — cover URL, art proxy, regenerate route, the gate and
  the name→genre parser.
* `templates/playlists/index.html`, `test_site/templates/Playlists/index.html`
  — the detail cover and the regenerate button.
* `static/js/playlists_index.js`,
  `test_site/static/js/pages/playlists.js` — the cover tile, the fallback, the
  regenerate call, the gate flag.
* `static/css/popularr.css`, `test_site/static/css/popularr.css` — the cover
  rules.
* `tests/test_playlists_page_covers.py` — new.
