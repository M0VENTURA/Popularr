# The AI cover prompt: one concept, random direction, real typography

**Date:** 2026-10-21
**Area:** `services/playlists/genre_cover_service.py`
**Commit:** `fix(playlists): give the AI cover prompt variety, one concept, and a real title`

## Report

> "Please adjust the prompt for the AI cover generation to the following. It's
> currently giving very similar art for each cover. The cover will also need
> to show the playlist name. Try to make them not look 'AI' like."

## Root cause — one formula for every genre

The old template asked for the same thing every time:

> "Square album cover artwork for a {genre} music playlist. Cinematic,
> atmospheric scene evoking {genre}, rich colour, dramatic lighting, highly
> detailed digital painting. Large bold ornamental title text "{genre}"
> centred at the top, weathered metal lettering. No band names, no artist
> names, no faces, no logos, no watermark."

Every genre got the same glowing, misty, fantasy-landscape render with the
same ornamental metal lettering. The model had nothing to vary, so it
reproduced one look — which is exactly the "very similar art" and the
recognisable AI sheen.

## The fix — six changes, all in the prompt

### 1. A RANDOM CREATIVE DIRECTION per cover

Six axes — medium, composition, lighting, texture, palette strategy and
conceptual approach — are each drawn from a curated list on every call
(28 × 16 × 14 × 16 × 14 × 15 possible combinations). A House cover and a
Metalcore cover no longer share a formula, and two House covers made months
apart do not either.

The prompt states plainly that these are **suggestions**: *"adapt,
reinterpret, or reject any that conflict with the musical identity —
musical authenticity wins."* Randomness must not become another rigid
template.

### 2. ONE CENTRAL VISUAL CONCEPT

> "DEVELOP ONE CENTRAL VISUAL CONCEPT. The cover must have one dominant idea,
> a clear focal point, and a coherent composition. Do NOT combine multiple
> unrelated genre symbols into a collage or montage."

This is the single biggest change for the "not AI" feel — the model
otherwise defaults to *guitar + skull + forest + city + lightning + smoke*.

### 3. ANTI-CLICHÉ blocklists

* Universal: instruments, microphones, headphones, speakers, vinyl records,
  equalizer graphics, waveforms, streaming UI elements, concert photography,
  neon music symbols, guitars, drums, DJ equipment.
* Heavy music: castles, warriors, skulls, graveyards, dead trees, swamps,
  fire, lightning, medieval scenery, fantasy typography.

The wording is "do not automatically use" / "do not automatically resort to",
not "never" — for Power Metal a castle might genuinely be right, and the
point is to stop the word "Metal" from summoning one.

### 4. The model INVENTS the palette

Colour is derived from the genre's character, the chosen medium, the subject
and the lighting. The random input is a *strategy* (monochromatic, muted +
accent, warm dominant, complementary clash, duotone, …), never a hard-coded
genre→colour map.

### 5. TYPOGRAPHY as a clean wordmark

The playlist name is rendered as *"a clean, geometric, sans-serif wordmark in
the lower third"*, single-colour, legible at thumbnail size, with *"no
ornamental fonts, no drop shadows, no bevels, no outlines, no distressed
metal text, no fake 3D."* The old prompt asked for "large bold ornamental
title text … weathered metal lettering" — one of the loudest AI tells.

### 6. "Make it look like a record sleeve"

> "The artwork must look like a real record sleeve — not like a rendered
> search query, a stock image, or a playlist thumbnail."

## The playlist name

The old prompt put the **genre** on the art. The cover now carries the
**playlist name** (`{playlist}` placeholder, e.g. "Metalcore - Top Tracks"),
which is what the user asked for. The genre is still in the prompt — as the
musical brief, not as the title.

A user-configured template still wins: when `genre_playlist_ai_cover_prompt`
is set, it is used as-is (with `{genre}` and `{playlist}` substituted), so
anyone who has tuned their own prompt keeps it.

## Note on the cost guard

The prompt hash decides whether a cover is regenerated. With the random
stimulus, two consecutive calls for the same genre produce DIFFERENT
prompts — and therefore different art, which is the point. The
"don't re-bill for identical art" guard still works exactly as before for
a deterministic (user-configured) template; the test that pinned it now uses
one.

## Tests

* `tests/test_genre_ai_covers.py` — the prompt class rewritten: the dynamic
  prompt carries the playlist name, asks for ONE central concept, includes
  the anti-cliché blocklists, and is **randomised** (20 calls must produce
  more than one distinct prompt).
* `tests/test_genre_cover_exists_check.py` /
  `tests/test_genre_cover_per_artist_full_scan.py` — the fixtures now
  configure a deterministic template, because those tests assert
  deterministic skip/upgrade behaviour.

Sweep of 27 genre/cover/album suites diffed against a pristine `develop`
clone — **identical failure sets, 0 new failures.** `import app` → 425
routes.

## Files

* `services/playlists/genre_cover_service.py` — the random stimulus axes,
  `_random_creative_stimulus`, and the rewritten `build_cover_prompt`.
* `tests/test_genre_ai_covers.py`,
  `tests/test_genre_cover_exists_check.py`,
  `tests/test_genre_cover_per_artist_full_scan.py` — updated.
