# Navidrome code 50 on updatePlaylist: the hint blamed permissions when it was a read-only playlist (2026-10-05)

**Report:** scan log showed
`updatePlaylist songs rejected playlist_id='4R9djxgoqULKXGklNurGc1' … hint='not
authorized: the configured Navidrome user does not own this playlist and is not
an admin — make this user an admin in Navidrome…'` — with the operator's reply:
**"The user is the admin."**

## Why the old hint was wrong

Navidrome answers Subsonic **code 50** for **two different failures**.
`server/subsonic/api.go`:

```go
case errors.Is(err, model.ErrNotAuthorized), errors.Is(err, model.ErrPlaylistNotEditable):
    // Subsonic has no code for "read-only resource"
    err = newError(responses.ErrorAuthorizationFail)   // → code 50
```

1. **`ErrNotAuthorized`** — `checkWritable`: `if !usr.IsAdmin && pls.OwnerID != usr.ID`
2. **`ErrPlaylistNotEditable`** — `checkTracksEditable`: `if !pls.TracksEditable()`

Our track replace takes the `hasTrackChanges` branch, so **both** are reachable:

```go
if hasTrackChanges {
    pls, err = s.checkTracksEditable(ctx, playlistID)   // ownership AND editability
}
```

For an **admin**, branch 1 cannot fire — `!usr.IsAdmin` is false — so an admin who
still gets code 50 is hitting branch 2: **the playlist's tracks are not editable**
(a smart `.nsp` playlist, or otherwise non-editable type). Told to "make yourself
an admin", the operator could do nothing: the advice could never be right.

## The detection gap behind it

Popularr *tries* to skip smart playlists before writing
(`playlist_navidrome_service._is_smart` → `NavidromeClient._is_smart_playlist`),
which looks for `smart`, `isSmart`, `criteria` and `type == "smart"`.

**Navidrome's `getPlaylists` response sends none of them.** The playlist object is:

```go
type Playlist struct {
    Id, Name, Comment, SongCount, Duration, Public, Owner, Created, Changed, CoverArt
    *OpenSubsonicPlaylist   // → "readonly", "validUntil"
}
```

So `_is_smart_playlist()` returns `False` for every playlist, the filter excludes
nothing, and a non-editable playlist is selected and written to every scan.

## Changes

- New pure helper `_playlist_denied_hint(playlist_name, owner, configured_user)`
  in `api_clients/navidrome.py` picks the wording from what we actually know:
  - **owner == the configured user** → definitive: this is a *read-only*
    playlist (smart/.nsp or synced). Navidrome reports that as "not authorized"
    even for the owner or an admin. Fix: delete the playlist — or the `.nsp`
    that defines it — so Popularr recreates a regular one.
  - **owner differs** → name the owner and give both branches (not admin, or
    not editable), so the operator can tell which applies.
  - **owner unknown** → both branches, keeping the original "make the user an
    admin, or delete the playlist" wording.
- `update_playlist_songs(..., owner=, playlist_name=)` passes what the caller
  already fetched from `getPlaylists` (the match is done on that same payload,
  so the owner is on hand and the id is known-current — it is not a stale id).
- The warning now carries `owner`, so a pasted log line is self-diagnosing.

`delete_playlist`'s hint is unchanged: `Delete` only calls `checkWritable`, so
code 50 there genuinely *is* the ownership rule.

## Not changed (deliberate)

The selection logic still writes to the matched playlist. Auto-deleting a
playlist Popularr did not create is too destructive to do on a guess — the
accurate message tells the operator what to remove instead.
