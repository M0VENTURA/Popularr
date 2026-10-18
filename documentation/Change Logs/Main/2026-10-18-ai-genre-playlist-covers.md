# AI cover art for the Top Genre playlists (Google Gemini)

**Date:** 2026-10-18
**Scope:** `api_clients/`, `helpers/`, `services/`, `templates/`, `test_site/` —
the Python is shared by both trees; the Config page is per-tree.

## What was asked

> Could we look at having Gemini api option in the config and use it to create
> custom album art for the Genre Playlists. Similar to one attached. For the top
> genre playlists, it would generate one for each, artist playlists stay as they
> are now.

## Scope, deliberately narrow

Only the ``{Genre} - Top Tracks`` playlists get AI art. The Essential
Collections (artist playlists) keep the artist-image cover they already get via
``attach_playlist_cover``, and no other generated playlist is touched. A test
pins the hook to ``_create_genre_top_track_playlists``.

## What was built

**`api_clients/gemini.py`** (new) — one call: ``generateContent`` on an
image-capable model, reading the inline base64 image out of the response.

* ⚠️ Image output is requested with ``generationConfig.responseModalities =
  ["IMAGE"]``, and the response **parts are SCANNED** for ``inlineData`` rather
  than indexed: a TEXT+IMAGE reply carries a text part too, and indexing
  ``parts[0]`` would report a perfectly good response as "no image".
* ⚠️ The key travels in the **``x-goog-api-key`` header**, never a ``?key=``
  query string — a URL is copied into logs and screenshots far more often than a
  header is.
* Failures return a **reason**, never raise: ``no_api_key``, ``blocked: …``,
  ``no_image``, ``http_<code>`` (401 bad key, 429 quota), ``exception``. The
  error BODY is logged, because Google puts the actionable sentence there and a
  bare ``http_400`` sends the operator to the wrong place.

**`services/playlists/genre_cover_service.py`** (new) — per genre playlist:
build the prompt → skip when this exact prompt was already applied → generate →
upload via the existing ``NavidromeClient.upload_playlist_cover`` → record a
prompt hash.

* ⭐ **The playlist is resolved BEFORE generating.** An image that cannot be
  attached is quota spent for nothing.
* ⭐ **Cost guard:** a scan generates only for a playlist it **created or
  updated**, and the state file
  (``<state>/genre_playlist_covers.json``) then suppresses regenerating an
  identical prompt — so a nightly scan does not re-bill for the same art.
* ⚠️ A **rejected upload is NOT recorded as done** (recording it would make the
  next scan skip a cover that is not there).
* ⚠️ ``regenerate`` is honoured from the **config as well as the argument** — the
  setting is what the user turns on, and a caller that forgets the parameter
  must not silently make the option a no-op.
* ⚠️ The prompt template is substituted with ``str.replace``, not
  ``str.format``: a stray brace is a legitimate prompt character and
  ``format`` would raise ``KeyError`` over a typo in a text box.

**Config** (both trees, §4.1): ``api_integrations.gemini`` = ``{enabled,
api_key, model}`` (defaults **off** — it is a billable third-party API, and the
key is read from ONE place only, never a legacy top-level ``gemini_api_key``),
and ``playlists.genre_playlist_ai_cover_{enabled,regenerate,prompt}``. New
getters ``get_gemini_config()`` / ``get_genre_cover_config()``.

A **Google Gemini card** was added to the Integrations tab and an **AI Genre
Covers** group to the Playlists section, in both trees, with collectors in both
``config.js`` files.

## Tests

`tests/test_genre_ai_covers.py` — **29 tests**: prompt substitution (including a
stray brace), the client (header-not-URL, text-part-before-image, 429, transport
error, safety block, no image), the service (disabled paths, generate+upload+
record, skip-on-same-prompt, regenerate, changed-prompt, no-Navidrome-match
spending no quota, rejected upload not recorded), and the §4.1 config surface.

### ⭐ The tests caught a real bug in this very change

Both new modules used the **stdlib** ``logging.getLogger`` with structlog-style
``key=value`` kwargs. A stdlib logger raises
``TypeError: Logger._log() got an unexpected keyword argument`` on those — and
every one of those calls sits in a **failure handler**, so the warning *and* the
scan's log line would have been lost. Both modules now use ``structlog``, the
house style. (This is the 6th occurrence of this trap in this codebase; it is
recorded in memory.)

Two further failures were my own test's fault / a design gap, both fixed: the
blank-template fallback returns the builtin **with ``{genre}`` substituted**, and
``regenerate`` now also comes from config.

### Results

| run | result |
|---|---|
| `tests/test_genre_ai_covers.py` | **29 passed** |
| broad sweep (genre/playlist/config/finalise/layout) | **316 passed** |
| `py_compile`, `node --check` ×2, Jinja parse ×2 | clean |
| `import app` | **421 routes** |

## Files

* `api_clients/gemini.py` (new)
* `services/playlists/genre_cover_service.py` (new)
* `helpers/config_helpers.py` — `get_gemini_config`, `get_genre_cover_config`
* `services/popularity/stages/finalise_stage.py` — cover hook after a successful
  genre-playlist sync
* `templates/pages/config.html`, `test_site/templates/Pages/config.html`
* `static/js/config.js`, `test_site/static/js/pages/config.js`
* `tests/test_genre_ai_covers.py` (new)

## Notes / limits

* Requires Navidrome ``EnableArtworkUpload`` for non-admin users, and the
  configured Navidrome user must own the playlist (Subsonic ``code 50``) — the
  same constraints the existing artist-cover path documents.
* Each cover is one image-generation call. The state file makes that **once per
  genre per prompt**; turning on *Regenerate* re-runs them all.
