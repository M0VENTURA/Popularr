# The release group now resolves even when the album type is already known (2026-10-09)

**Area:** popularity / enrichment (album stage)
**Commit:** `fix(album-type): resolve the release group even when the album type is cached`

## Reported

> The release group ID doesn't seem to be populating during the metadata scan.

## Two exits that both ran before the persistence call

`_persist_album_type_to_tracks` is the only thing that writes
`musicbrainz_releasegroupid`, and **both** of its callers sat behind a gate that
could return first:

### 1. `ensure_album_type` returned on a type cache hit

```python
if len(stored) == 1 and not options.get("force"):
    value = next(iter(stored))
    Logger.info("[ENRICH] ensure album type cache hit", detected_type=value, **context)
    return value          # ← _resolve_album_type never runs
```

The type and the release group are written by the **same** call, so an album
whose type had already been set — by the popularity pass, by an earlier
metadata run, or by the album page's Save — never had its release group looked
for at all. That is Step 3/3 of the album pipeline
(`album_pipeline._maybe_auto_detect_album_type`, the log's
`Step 3/3: Auto-detecting album type`), described in its own docstring as
*"a lightweight safety net for albums that were skipped"*. The more scans that
run, the more certain the type is to be cached, and the less likely the release
group ever is to appear — which is why it read as "doesn't populate during the
metadata scan" rather than "never populates".

**Fix:** the cache hit now short-circuits only when the row **also** carries a
release group. Otherwise it resolves, and then keeps the ROW's type —
`detected = cached_type` — because the resolve exists for the id, not to
re-decide the type (`_persist_album_type_to_tracks` is fill-only for the type
anyway, so a cache hit cannot turn into a re-detection).

### 2. The only source of a release group was a text search

`_lookup_musicbrainz_album_type` ran a release-group **search** with a `0.6`
score gate and returned `(None, None)` when it found nothing or scored low. The
album's own `musicbrainz_album_mbid` — a concrete MusicBrainz **release** id,
arriving from the file's tag, from Navidrome's `musicBrainzId`, or from a manual
match — was never consulted.

**Fix —** new `_release_group_from_stored_release(tracks, context)`: one
`fetch_musicbrainz_release_metadata(release_mbid)` call (HTTP-layer cached, **no
text matching, so no score to fail**) returns the release group. It is used at
the two "the search produced nothing" exits (`no matches`, `score < 0.6`).

It returns `(None, rg)` — **the type is deliberately `None`**: a release id says
which release group the album belongs to, not what the album is *called*, so the
local detection still decides the type and the caller persists both through the
same call.

Not touched: the VA tracklist gate (a deliberate rejection of a *text* match —
a stored binding is not a text match, but that gate guards a different risk) and
the exception path.

## Tests

`tests/test_release_group_populates_during_metadata_scan.py` — **12**:

* `TestTheTypeCacheHitStillResolvesTheReleaseGroup` — a cached type with no
  release group now reaches `_resolve_album_type` **and**
  `_persist_album_type_to_tracks` with the id; the row's own type survives the
  resolve; **controls**: a row that already has both never searches, a row with
  no type still resolves, and `force` still bypasses the cache;
* `TestTheStoredReleaseMBIDAnswersTheReleaseGroup` — resolves the bound
  release's group, returns `(None, None)` with no release MBID, degrades when
  the service is unreachable, and both search rejections fall back to it;
* `TestTheGatesAreStillIntact` — controls: the VA gate still precedes taking the
  id, and the binding never fabricates a type.

## Verification

* New suite → **12 passed**.
* **Oracle** — reverting `album_stage.py` → **8 failed / 4 passed**: the four
  cache-hit cases, the three stored-release cases and the source assertion on
  the new helper. The 4 that pass either way are the controls. Restored → 12.
* **Sweep** — the 16 test files referencing the album stage, clean
  `origin/develop` vs this change: **baseline 12 failures, changed 12**,
  `Compare-Object` on the sorted `^(FAILED|ERROR) tests/` lines = **identical,
  0 regressions**.
* `import app` → **392 routes**.

## Pre-existing (unchanged)

`tests/test_album_type_persistence.py::TestEnsureAlbumTypeReturnsVerdict::test_detects_when_missing`
fails **identically on both sides** — it stubs `_lookup_musicbrainz_album_type`
with a 2-argument lambda, while the function has taken optional `album_artist`
and `tracks` since the VA tracklist gate was added. Present in the baseline and
the changed sweep set.

## Not changed

* **The popularity pass still does not resolve release groups** — it takes the
  `popularity_pass or finalise_pass` branch of `enrich_album_extras`, which is
  deliberately DB-only (`_detect_album_type`, no writes). Adding an MB call
  there would put a throttled request on every popularity scan.
* `musicbrainz_releasegroupid` stays **fill-only**: a wrong-but-present value is
  never overwritten by a scan.
* The 0.6 threshold itself and the Tier-1/Tier-2 MBID confirmation added in
  `2e5aa4dd`.
