# Per-user favourite playlists (Favourite Artists / Favourite Albums)

**Date:** 2026-10-18
**Scope:** `api_clients/`, `helpers/`, `services/`, `routes/`, `templates/`,
`test_site/` — the Python is shared by both trees; the Config page is per-tree.

## What was asked

> Add an option to sync the favourite artists, albums and songs from Navidrome
> for each user. Then have playlists created for Favourite Artists and Favourite
> Albums containing all 4 and 5 star tracks for those playlists. Each playlist
> only saves to the Navidrome Account for each profile.

## What already existed (and why this is mostly wiring)

* `user_favourites` (migration 005) — already keyed
  `(username, entity_type, entity_id)` with a `navidrome_id`, so heart state is
  **already per user**.
* `db/repositories/favourites_repository.py` — `get_favourite_navidrome_ids`.
* `services/favourites_service.py` — `sync_favourites_from_navidrome` (pull from
  `getStarred2`) and `toggle_favourite` (heart → star).
* `playlist_navidrome_service.sync_playlist_by_name(client, name, ids)` — takes a
  **client**, so "push as this user" was already possible.
* `get_navidrome_users_normalized()` — the profiles.

## What was missing, and what the fix was

### 1. The sync only ever served the ACTIVE user

`sync_favourites_from_navidrome()` read `session["username"]`. Now:
`sync_favourites_from_navidrome(username=None)` (defaults to active) plus
`sync_favourites_for_all_users()` looping every configured user.

⚠️ New `get_navidrome_user_by_name()` (EXACT match) rather than reusing
`get_active_navidrome_user()`, which **falls back to the first configured user**.
That fallback is right for a single-user install and wrong here: it would push
one account's playlists as another's. An unknown name is an error, not a
fallback.

### 2. ⚠️ Hearting an album or artist never reached Navidrome

Two stacked defects:

* `_resolve_navidrome_id()` queried `albums.navidrome_album_id` and
  `artists.navidrome_artist_id`. **Neither column exists** — there is no
  `albums` table at all and `artists` holds only `id`/`name` — so both queries
  raised, were swallowed by the `except`, and returned `None` every time.
* `toggle_favourite` then called `client.star_track(id)`, and Subsonic's `star`
  takes **`id` for a song but `albumId` / `artistId` for an album / artist**. The
  server ACCEPTS the wrong parameter and stars nothing.

So only tracks (whose `tracks.id` *is* the Navidrome song id, making the bare
`id` accidentally right) ever mirrored. Fixed with
`NavidromeClient.star_entity` / `unstar_entity` (one `_STAR_PARAM_BY_KIND` map
supplies the correct parameter name) and with `_resolve_navidrome_id` resolving
albums/artists by NAME through the Navidrome **`search3`** API.

⭐ Resolution is **exact-first, never "take the first hit"** — a search for one
artist's name routinely returns other artists' albums, and starring the wrong id
is worse than starring nothing.

### 3. The playlists themselves

New `services/playlists/favourite_playlist_service.py`. There is **no local
album→Navidrome-id or artist→Navidrome-id mapping anywhere**, so the starred
entities are expanded through the API:

```
starred album id  → getAlbum(id).song            → song ids
starred artist id → getArtist(id).album → getAlbum each → song ids
```

`tracks.id` is the only stable local↔Navidrome key, so the song ids address
local rows directly. Tracks below `favourite_min_stars` (default 4) are dropped;
the result is pushed with **that user's own client**.

* ⚠️ **An empty result is SKIPPED, never written.** Writing an empty list would
  wipe a populated playlist the moment a user un-stars one album.
* ⚠️ The star filter uses an **expanding bindparam**, never a JSON array
  deconstructor in the `IN` list (see
  `playlist_service.create_or_update_loved_tracks_playlist`).

### 4. Config + triggers

New `playlists.favourite_*` keys exposed in **both** Config pages and **both**
`config.js` collectors (§4.1): `favourite_playlists_enabled` (default on),
`favourite_min_stars` (4), `favourite_artists_name`, `favourite_albums_name`,
`sync_favourites_from_navidrome` (default off).

⚠️ `favourite_min_stars` is parsed without an `or 4` guard — a configured `0`
is falsy and `0 or 4` silently became 4, the same trap
`_resolve_genre_max_tracks` documents.

Routes: `POST /api/favourites/sync-all` and
`POST /api/favourites/favourite-playlists` (`{username?, force?}`).

Scan trigger: `finalise_stage._run_global_playlist_rebuild` now runs the sync +
playlist build (marker-guarded, once per scan) **after** the rating pass and
**before** the playlist index, so the index records them. Never fatal.

## Tests

`tests/test_favourite_playlists_per_user.py` — **20 tests**: album/artist
expansion, the star floor (drops below-floor and not-in-library ids, keeps
request order), per-user client routing (⭐ asserts aaron's client never expands
beth's album), empty→skipped, unknown-user→error, the master switch, the sync
targeting the named user, `_resolve_navidrome_id` exact-match (and its
deliberate no-match), plus the §4.1 config-surface contract.

`tests/test_favourites_sync.py`'s fake Navidrome gained
`star_entity`/`unstar_entity` — the service no longer calls `star_track`, and a
fake missing the new method made the mirror look like it failed.

`tests/test_loved_tracks_playlist.py`'s repo-wide "no JSON array deconstructor"
guard **caught this change's own docstring** naming the idiom (it strips `#`
comments but not `"""` strings) — the docstring was reworded.

### Results

| run | result |
|---|---|
| feature + favourites + playlist-sync suites | **65 passed** |
| broader sweep (navidrome/playlist/finalise/config/layout) | **340 passed**, 1 = the documented pre-existing `test_the_identity_is_marked_unambiguous` (`KeyError: 'title_is_unique'`) |
| `py_compile` (6 files), `node --check` (both config.js), Jinja parse (both config.html) | clean |
| `import app` | registers the 2 new routes |

## Files

* `api_clients/navidrome.py` — `star_entity` / `unstar_entity`
* `helpers/config_helpers.py` — `get_favourite_playlists_config`
* `services/favourites_service.py` — per-user sync, exact-match user lookup,
  API-based id resolution, kind-aware star mirror
* `services/playlists/favourite_playlist_service.py` (new)
* `routes/favourites.py` — 2 routes
* `services/popularity/stages/finalise_stage.py` — scan-tail hook
* `templates/pages/config.html`, `test_site/templates/Pages/config.html`
* `static/js/config.js`, `test_site/static/js/pages/config.js`
* `tests/test_favourite_playlists_per_user.py` (new),
  `tests/test_favourites_sync.py`

## Known limits

* Pushing a playlist requires the configured Navidrome user to OWN it (or be an
  admin) — Navidrome's `code 50`. The per-user client is what satisfies this; a
  pre-existing playlist owned by someone else will still be refused, with the
  existing `_playlist_denied_hint` guidance.
* Artist expansion costs one `getArtist` + one `getAlbum` per album, so a user
  who stars many artists makes many calls. It runs once per scan.
