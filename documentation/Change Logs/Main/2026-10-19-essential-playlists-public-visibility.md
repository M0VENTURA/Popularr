# Essential Playlists are created private because `createPlaylist` cannot set visibility

**Date:** 2026-10-19 · **Area:** `playlists` · `api`

> **Reported:** *"Essential Playlists are no longer creating as public."*

## Root cause — the `public` parameter is a lie

`NavidromeClient.create_playlist` sent `public` on the **create** request:

```python
def create_playlist(self, name, song_ids, public=True):
    params = {"name": ..., "public": "true" if public else "false"}
    return self._post_subsonic_response("createPlaylist", timeout=120, **params)
```

**Navidrome ignores that parameter completely.** Its Subsonic handler reads only
`songId`, `playlistId` and `name`, and calls `playlists.Create(...)` — whose
signature has **no visibility argument at all**:

```go
// server/subsonic/playlists.go
songIds := p.Strings("songId")
playlistId, _ := p.String("playlistId")
name, _ := p.String("name")
id, err := api.playlists.Create(ctx, playlistId, name, songIds)   // ← no public
```

```go
// core/playlists/playlists.go
Create(ctx context.Context, playlistID string, name string, ids []string) (string, error)
```

So **every Essential Collection was born PRIVATE**, while the code, the default
argument and the success log all said "public". Nothing in the create path ever
corrected it.

Navidrome's own Jellyfin adapter states the rule outright — *"Create takes no
visibility, so a requested one costs a second write."*

## Why it surfaced now

Visibility is settable **only** through `updatePlaylist`, where Navidrome takes
`public` as a *nullable pointer* and applies it only when present:

```go
public := p.BoolPtr("public")                 // nil when absent
...
if public != nil { pls.Public = *public }     // core updateMetadata
```

The only place that ever exploited this was `sync_playlists_public()` — a
post-library-sync pass that walks every playlist and flips it. So the playlists
were public **by accident of timing**, and only when that pass ran.

⚠️ That pass is exactly what the four-hourly Navidrome import broke. Both
`getPlaylists` and `updatePlaylist` are in the import gate's deferred set, and a
deferred read is answered with `{}` — which `fetch_all_playlists()` turns into
`[]`. The pass then found nothing, `checked` stayed `0`, and it returned
`{"enabled": True, "checked": 0, ...}`: **indistinguishable from "every playlist
was already public"**. A silent no-op, logged as a clean run.

*(Navidrome itself is not at fault for the ordering: `updatePlaylist` without
`public` leaves the flag untouched, so `update_playlist_songs` was never the
cause. Only the CREATE silently dropped it.)*

---

## 1. The fix: perform the second write

`create_playlist` now applies visibility itself, using the only mechanism
Navidrome honours:

```python
data = self._post_subsonic_response("createPlaylist", timeout=120, **params)

_pid = str(((data or {}).get("playlist") or {}).get("id") or "").strip()
if _pid and (data or {}).get("status") == "ok":
    if public and self.update_playlist_public(_pid, True):
        data["public"] = True
    else:
        data["public"] = False
        ...
```

Three things this gets right:

| detail | why |
|---|---|
| the id comes from the **create response** | it is the only place the new playlist's id appears |
| **no id ⇒ no second write** | a deferred/failed create answers `{"status": "ok"}` with no `playlist` object; aiming a write at `playlistId=""` is not a harmless no-op |
| the result reports the **outcome** | a caller can no longer mistake a playlist that stayed private for a public one |

`public` is still *sent* on the create, because other Subsonic servers may honour
it; Navidrome ignoring it is harmless. This also repairs
`create_navidrome_playlist()`, which forwards to the same method, so **generated
playlists are public from the moment they are created** — no second pass, no
race, no dependency on the `auto_public_playlists` opt-in.

## 2. A deferred visibility write must not report success

```python
# _get_subsonic_response answers a skipped request with {}
# the empty-body-success rule turns that into True
if self._defer_for_import("updatePlaylist"):
    return False
```

⚠️ This mattered because fix 1 now *depends* on `update_playlist_public`. Without
it, a create overlapping an import would report `public: True` while sending
nothing — worse than a failure, because nothing would ever retry. (`updatePlaylist`
is deferred; `createPlaylist` is too, so an import genuinely owns the server here.)

## 3. The fallback pass reports a deferral instead of a clean zero

`sync_playlists_public()` now checks the gate up front and returns
`"deferred": True` — and `library_sync_service` logs that case separately:

```
Playlist visibility sync deferred (Navidrome import running)
```

No client is built, because every request it would make is deferred anyway.
This is what turns the silent no-op above into a visible, honest state.

---

## Tests

`tests/test_playlist_public_visibility.py` — **11 passed**, and
**oracle-verified**: with the three guards removed again, **5 of them fail**
(the second write, the private control, the failed-visibility report, the
deferred-update report, and the deferred-pass report).

Covered:

- a public create issues **exactly one** extra `updatePlaylist` write carrying
  `public=true` **and the id from the create response**;
- `public=False` issues **no** second write (control);
- a create with no `playlist` id performs **no** second write;
- visibility failing leaves the create a success but reports `public: False`;
- the `updatePlaylist` request is skipped entirely when deferred, and
  `update_playlist_public` returns **False** (not the empty body's `True`);
- the import's own client is exempt from its own gate;
- `sync_playlists_public` reports `deferred: True` **without constructing a
  client**, still runs when no import is active, and still short-circuits on the
  opt-in flag.

⚠️ The harness deliberately models `response.content` and `response.text`, both of
which the client reads to detect the empty-body-success case.
`tests/test_navidrome_playlist_post_body.py` omits `content`, which is why 5 of
its 7 tests fail — **identically on pristine `develop`**, unrelated to this work.

## Verification

| sweep | result |
|---|---|
| new visibility suite | **11 passed** (5 fail without the fix) |
| playlist / navidrome / library suites | 119 passed, 9 pre-existing failures |
| full suite | see below |

The 9 failures in the focused run are all pre-existing and unchanged by this
fix: 5 × `test_navidrome_playlist_post_body.py` (the missing `.content` above) and
4 × `test_favourite_playlists_per_user.py::TestTheConfigSurfaceExists` (the
workspace's own in-progress removal of the `playlists_favourite_enabled`
controls).

## Note for the config contract

`navidrome.auto_public_playlists` remains the switch for *retro-fitting*
visibility onto playlists that are already private (including any created before
this fix). It is **no longer required for newly generated playlists**, which are
now public at creation.
