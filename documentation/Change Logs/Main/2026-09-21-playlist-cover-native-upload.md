# Playlist cover art: the artist image was never actually uploaded

**Date:** 2026-09-21 · **Area:** playlists / Navidrome integration

## Reported

"Playlist artwork isn't being added properly, it's meant to be created using the
artist image for artist playlists."

## Root cause — a false success

The cover was POSTed to the **Subsonic** `updatePlaylist` endpoint as a
`coverArt` file. That endpoint has **no cover parameter at all** — its parameters
are exactly:

```
playlistId, name, comment, public, songIdToAdd, songIndexToRemove
```

(https://opensubsonic.netlify.app/docs/endpoints/updateplaylist/)

Navidrome ignores unknown parameters, so it answered `{"status":"ok"}` and
`upload_playlist_cover` returned `True` — the log said
`[PLAYLISTS] Cover set for …` while **no artwork was ever stored**. Every artist
playlist fell through to Navidrome's 4th source (the auto-generated 4-cover
mosaic) or the placeholder.

## Fix — use the endpoint Navidrome's own UI uses

Navidrome's artwork docs list the uploaded image as the **first** source for
playlist artwork, and its web UI uploads through the **native API**:

```
POST {base}/api/playlist/{id}/image      multipart/form-data, field "image"
```

Upstream: `server/nativeapi/playlists.go` → `uploadPlaylistImage` →
`handleImageUpload`, which reads the file with `r.FormFile("image")`, validates it
by magic number (JPEG/PNG/GIF/WebP) and requires `EnableArtworkUpload` for
non-admin users. It answers `{"status":"ok"}`.

* `api_clients/navidrome.py`
  * **`native_login()`** — `POST /auth/login {username, password}` → JWT, cached
    per client instance.
  * **`upload_playlist_cover()`** now posts the image to
    `/api/playlist/{id}/image` with `Authorization: Bearer <token>` and the file
    field named **`image`** (the server 400s with "missing image file"
    otherwise). A `401` clears the cached token, re-logs in once and retries.
  * It returns **`(ok, reason)`** instead of a bare bool: `no_image`,
    `no_login`, `disabled` (403 — artwork uploads off for that user),
    `not_found` (404), `http_<code>`, `exception`. That is the guard against the
    bug recurring: the old signature could only report success.
  * The dead Subsonic upload (`/rest/updatePlaylist` + `coverArt`) is gone.
* `services/playlists/playlist_service.py` — `attach_playlist_cover()` keeps the
  `navidrome.playlist_cover_art` gate and the artist-image lookup (the
  `/artist`-page pipeline), and now reports
  `{"ok", "method", "reason"}` with an actionable reason, logging a warning —
  not "Cover set" — when the upload is rejected.
* Config page (both trees): the help text now says the cover is uploaded through
  **Navidrome's native artwork upload** and needs artwork uploads enabled there.

## Verification

A 15-check probe of the shipped request logic (fake session, transcribed method)
passed every case: the login URL/body, the native upload URL, the `Bearer`
header, **the multipart field named `image`**, the raw bytes, token caching (one
login for two uploads), the 401 re-login-and-retry, and every honest failure
reason (`403 → disabled`, `404 → not_found`, failed login → `no_login`,
missing input → `no_image`).

`tests/test_playlist_cover_native_api.py` (new) pins the same, plus the
discriminating guard that the Subsonic endpoint is **not** used for covers again
(`coverArt` / `/rest/updatePlaylist` absent from the shipped method, code-only
scan so the docstring that names them cannot fail the check).

⚠️ The pytest run follows the workspace commit, as with the previous change sets.
