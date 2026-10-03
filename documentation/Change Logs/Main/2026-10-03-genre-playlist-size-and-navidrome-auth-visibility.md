# Genre playlists: visible Navidrome write failures + a real size knob (2026-10-03)

**Report:** *"The top tracks playlists for genres are only showing maximum of
50 tracks"* (clarified: *"less than 50 on each playlist"*), reported right
after **Navidrome was upgraded to the newest release** (0.64.x).

## Investigation

The generator itself runs (`Genre playlists: 69 playlist(s) synced` in the
user's log) and the current tree has **no 50-track cap anywhere**: row fetch
has no `LIMIT`, the only cap is `playlists.genre_playlists_max_tracks`
(default **300**), the sync pushes every id, and neither UI slices the list.
The user's log exposed the real defect:

```
updatePlaylist songs rejected … response={'status': 'failed', …
 'error': {'code': 50, 'message': 'User is not authorized for the given operation'}}
```

* **Navidrome's rule** (verified in `core/playlists/playlists.go`,
  `checkWritable`): a non-admin user may only modify playlists **it owns**
  (`!usr.IsAdmin && pls.OwnerID != usr.ID → ErrNotAuthorized` → Subsonic
  **code 50**). A rejected update leaves the playlist **frozen at its old
  contents** forever; a rejected delete leaves stale playlists behind.
* **Our client swallowed this class of failure**:
  * `update_playlist_songs` logged only the raw response — no hint what to do;
  * `delete_playlist` returned `False` with **no log at all** — the genre
    sweep treats `False` as "keep", so auth-rejected deletes were invisible;
  * `_sync_playlist_to_navidrome` counted only **exceptions** as `failed`;
    a returned `success: False` incremented nothing;
  * the genre loop's `if not _playlist_sync_succeeded: continue` was silent,
    so the scan summary reported only successes while playlists stayed stale.
* **Navidrome 0.64 re-encoded all internal IDs** ("canonical 128-bit
  base62… clients that cache item IDs may need to re-sync"). Song IDs cached
  in `tracks.id` may no longer resolve, so an ACCEPTED write can store fewer
  tracks than requested — previously undetectable.
* **Config contract gap (§4.1):** `genre_playlists_max_tracks` had **no
  input** on the Config page (in neither tree), the section description
  promised *"(no top-N cap)"* while the code capped at 300, and a configured
  `0` was silently coerced back to `300` (`int(cfg.get(...) or 300)`), so the
  documented "no cap" option was inexpressible.

## Changes

* `api_clients/navidrome.py`
  * `update_playlist_songs`: on failure, log a `hint=` for code 50 — *"the
    configured Navidrome user does not own this playlist and is not an admin —
    make this user an admin in Navidrome, or delete the playlist there so it
    is recreated under this user"*.
  * `delete_playlist`: a `status: failed` response is now logged at WARNING
    (with the same code-50 hint) — previously completely silent.
* `services/playlists/playlist_navidrome_service.py` — **post-write
  verification**: after a successful update, re-fetch the stored song ids and
  warn when `stored != requested` (*"stale song IDs are the usual cause
  (Navidrome re-encoded all IDs in 0.64); re-run the Navidrome import scan to
  refresh them"*). The create path verifies from the response's own
  `songCount` (no extra fetch).
* `services/popularity/stages/finalise_stage.py`
  * `_sync_playlist_to_navidrome` now counts a returned failure as `failed`;
  * a fully failed genre sync logs *"Genre playlist sync failed — Navidrome
    keeps its previous contents"* (playlist + requested count) instead of a
    bare `continue`;
  * new `_resolve_genre_max_tracks(cfg)` — `0` (or negative) = **unlimited**,
    invalid/missing = 300, mirroring `_resolve_max_per_artist`.
* `helpers/config_helpers.py` — `get_playlists_config()` preserves `0`
  instead of coercing it to 300; docstring documents "0 = no cap".
* Config page (BOTH trees) + BOTH config.js collectors: new **Max Tracks Per
  Playlist** input (`playlists_genre_max_tracks`, min 0, default 300,
  "0 = no cap"), and the description now says "…every qualifying track is
  included **up to Max Tracks Per Playlist** (0 = no cap)" instead of the
  false "(no top-N cap)".

## Verification

* `tests/test_playlist_sync_auth_and_cap.py` (28): code-50 guidance on update
  and delete, post-write count verification (update + create), failure
  counting, the genre-loop failure warning, cap behaviour incl. a **305-row
  pool proving 0 = unlimited** (fails at `300 == 305` pre-fix), the
  `_resolve_genre_max_tracks` table, and the Config-page contract in both
  trees.
* **Oracle:** 21 failed / 7 controls passed with the eight source files
  stashed → **28 passed** restored.
* **Regression sweep** (9 playlist/navidrome suites): failing set
  **identical** to the clean-tree baseline (`Compare-Object` empty,
  base=53 / patched=53 = 0 regressions; those 53 are pre-existing).
* `node --check` both config.js, Jinja parse both config.html, `import app`
  OK (389 routes).

## Operator actions for the affected instance

1. In Navidrome, make the user Popularr connects with an **admin** (or delete
   the stuck playlists so they are recreated under that user) — code-50
   rejections now say this explicitly in `error.log`.
2. **Re-run the Navidrome import scan** after the 0.64 upgrade so cached
   song IDs in `tracks.id` are refreshed; the new post-write verification
   will warn if pushes still store fewer tracks than requested.
3. Use the new **Max Tracks Per Playlist** field (0 = no cap) if the 300 cap
   itself is ever the limit.
