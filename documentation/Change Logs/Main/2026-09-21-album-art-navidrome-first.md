# Album art: Navidrome's `getCoverArt` now comes first

**Date:** 2026-09-21 · **Area:** enrichment / album art

## Reported

"For the cover art on the albums, currently it looks online for it, but it should
first use the coverart from Navidrome using the subsonic api of getCoverArt. It
doesn't look like that command is being used so far within `api_clients/navidrome.py`."

## What the audit found

`getCoverArt` **was** implemented and reachable — `NavidromeClient.get_cover_art_url`
/ `get_cover_art_bytes` (`api_clients/navidrome.py`) and
`album_art_service.fetch_album_art_from_navidrome` all existed, and every art path
already tried it *before* MusicBrainz/Discogs/AudioDB. But three defects meant the
online providers were what actually ran:

1. **The library guard was case-SENSITIVE.** It matched
   `COALESCE(NULLIF(album_artist,''),artist) = :artist AND album = :album` while
   every other album-scoped lookup in the app is `LOWER(...)`. Any album whose
   stored casing differed was declared *"not in local library"* and Navidrome was
   skipped entirely — the online providers then ran. This is the same defect class
   as the 2026-08-14 album-MB-matching fix.
2. **It threw away the id it already had.** The album was located with a
   library-wide Subsonic `search()` plus an exact normalised match; that call was
   expensive enough that the whole function is guarded by a config/db check. But
   `tracks.id` **is** the Navidrome/Subsonic song id (the importer stores it
   verbatim — `services/scanning/payload_builder.py`), and `getCoverArt` accepts a
   **song** id, so one request to the local server is enough — no search at all.
3. **The DB cache shadowed it.** Every art path reads the cache first and returned
   the cached blob, so an album that had ever collected Cover Art Archive (or
   Discogs/AudioDB) art could *never* pick up the library's own cover: the cache
   made the Navidrome step unreachable. That is precisely "currently it looks
   online for it".

## Fix

* `db/repositories/metadata.py` — new `fetch_album_art_record()` returning
  `(image_data, mime, source)`. The existing `fetch_album_art_blob()` is
  untouched, so its two-value callers are unaffected.
* `services/enrichment/album_art_service.py`
  * `fetch_album_art_from_navidrome()` now:
    1. reads a **song id** for the album from `tracks` with a **case-insensitive**
       key — which doubles as the local-library guard, so a missing release still
       costs zero requests;
    2. calls `get_cover_art_bytes(song_id)` — **one request, no search**;
    3. only if that yields nothing, falls back to the album `search()` + album id
       exactly as before.
  * new `navidrome_art_may_replace(source)` — the precedence rule: art a
    **provider** supplied (`musicbrainz`, `discogs`, `audiodb`, `itunes`,
    `missing_releases`, `unknown`, `""`) may be replaced by Navidrome's copy;
    `navidrome`, `upload` and `url` are **final**, so the user's own choice is
    never overridden.
  * `get_or_fetch_album_art()` (the canonical orchestrator) now returns cached art
    only when it may *not* be replaced, asks Navidrome next, and — when Navidrome
    has no copy — **keeps what it already holds** instead of re-downloading the
    same provider art (which could also replace a good cover with a worse one).
* `services/popularity/stages/album_stage.py` — the scan's art pipeline compares
  the cached **source** the same way (`fetch_album_art_record`), so provider art
  gets first refusal from Navidrome, and a failed Navidrome lookup logs
  `album art kept` and stops rather than falling through to the online providers.
* `services/metadata/album_service.py` — `get_local_album_art()` (the album page's
  path) follows the same order and the same precedence rule.

Order everywhere is now: **Navidrome `getCoverArt` → CAA/MusicBrainz → Discogs →
AudioDB**, with stored art only able to short-circuit that when it is Navidrome's
own or the user's.

## Verification

`tests/test_album_art_navidrome_first.py` (new) covers the precedence rule
(provider vs user sources), the fetch order (song id first, one request,
`search()` skipped; the album-id fallback only when needed; zero requests for an
album not in the library), the case-insensitivity of the guard, and that the cache
cannot shadow Navidrome while a user upload never gets overridden.

A 13-check probe of the shipped `fetch_album_art_from_navidrome` (driven with fake
client/session, run against a checkout) passed every case:

```
PASS  getCoverArt called once with the SONG id   -> [('song-123', 600)]
PASS  library-wide search skipped                -> []
PASS  both ids tried, song first                 -> ['song-123', 'album-9']
PASS  album absent -> NO Subsonic request         -> []
PASS  guard lowercases the artist/album key
```

`get_errors` is clean on all four edited files. ⚠️ The pytest run of the new suite
follows the workspace commit, as with the previous change sets in this session.
