# Lookup MBID now populates missing tracks on test_site (2026-10-05)

**Report:** *"Missing tracks are still not populating on test_site after the
lookup_mbid."*

## Two ways it silently produced nothing

Both were in `test_site/static/js/pages/album.js`, and both failed **quietly** —
the tracklist simply looked complete.

**1. `applyAlbumMbid()` never refreshed the findings.**

Live's `applyAlbumMbid` has always fired `_refreshAlbumTrackFindings(mbid)`
right after switching to the Edit tab. test_site's copy did
field → preview → notify and stopped:

```
setFieldValue('album_mbid', mbid)  →  markDirty()  →  tab  →  applyProposal  →  notify
                                                                          ✗ nothing
```

So every caller that arrives with a concrete release id — the release picker's
release path, an inline handler — produced **no missing rows at all**. The
renderers were fine; nothing ever asked the server.

**2. `refreshAlbumTrackFindings()` refused to run without a release id.**

Its body was `if (releaseMbid) { … }`, and `applyAlbumMatch` passes
`resolvedReleaseId`, which stays `''` whenever the best-release probe fails
(throttled MusicBrainz, an offline group browse). The proposal in the *same
function* falls back to `release.id` in that situation — so the orange bars
appeared while the tracklist stayed blank — and the skip was swallowed by an
empty `catch (_e) {}`.

## Changes (test_site only — live already behaved correctly)

- `applyAlbumMbid()` now fires `refreshAlbumTrackFindings(mbid)` immediately
  after the tab switch, exactly as live does, and before the (optional)
  metadata preview so a failing preview cannot take the findings with it.
- `refreshAlbumTrackFindings()` **always** fetches. With an id it sends
  `&refresh=1&release_mbid=<id>`; without one it sends `&refresh=1` and lets
  the server fall back (stored album MBID → name search — the same computation
  the page-load path runs). Showing the old release's list beats showing
  nothing, and the id'd shape is unchanged for the normal path.
- The empty `catch` became `console.warn(...)` — a silent swallow here is
  exactly what hid the blank tracklist.
- `refreshAlbumTrackFindings` is now exported (`global.…`), so both entry
  points and tests can call it.

## Tests

`tests/test_album_lookup_missing_tracks_populate.py` — **8 tests**, driving the
REAL module in node (harness modelled on `test_findings_persist_behaviourally`):

- `applyAlbumMbid('release-abc')` requests `/api/album/missing-tracks` scoped
  to that release;
- it still fetches when `applyProposal` throws (preview is optional);
- `refreshAlbumTrackFindings('')` fetches **without** a `release_mbid` param;
- a non-empty id is still passed through;
- a failed request is attempted **and reported** (the warn is asserted, not
  `or True`);
- parity guards: live calls `_refreshAlbumTrackFindings(mbid)`, test_site's
  `applyAlbumMbid` calls `refreshAlbumTrackFindings(mbid)`, and the function
  is exported.

Oracle: stashing only `test_site/static/js/pages/album.js` → **7 of 8 fail**
(the live-parity check still passes, as it must).

The pre-existing wiring contract in `test_album_lookup_findings.py` (which
pins the literal `refresh=1&release_mbid=` URL shape) is preserved rather than
relaxed — the URL is now built conditionally but the id'd form is byte-identical.
