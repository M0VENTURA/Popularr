# VA multi-artist imports: album identity, duplicate views and checkpoint loop

**Date:** 2026-10-31
**Area:** Navidrome import album/artist identity
**Files:** `services/scanning/navidrome_import.py`,
`services/scanning/pipelines/navidrome_pipeline.py`,
`services/scanning/reconciliation.py`,
`tests/test_navidrome_va_album_artist.py` (new)

## Reported

The same compilation was logged under several artist names:

```
Various Artists • HIM
Various Artists • Martyr AD
Various Artists • Satyricon
Alessandra • Various Artists
```

each importing the same album (`Triple J's Hottest 100 (1998)`, `Spawn: The
Album`, …) and the same tracks. The album endpoint's `artist` was a single
component (`HIM`), while the album view carried the multi-artist string
(`Various Artists • HIM`). The single component won, so a VA compilation was
treated as HIM's album; each artist view then repeated the import.

## Fixes

1. **A VA component wins album-artist resolution.**
   `album_artist_from_sources` now checks every source (`api_album_artist`,
   album-view artist, canonical name) for a `Various Artists` component using
   the `•` / `·` / `/` / `;` separators. If any source is VA, the resolved
   album artist is `Various Artists`; an existing composite VA value is
   normalised too.
2. **The canonical artist for prefetch/sanitize is VA.**
   `scan_artist_to_db("Alessandra • Various Artists")` now uses
   `canonical_artist_name = "Various Artists"` instead of `"Alessandra"`, so
   prefetch and artist-row sanitisation operate on the correct identity.
3. **One album is imported once per run.**
   The full import pipeline calls `scan_artist_to_db(..., dedupe_albums=True)`.
   Albums are claimed by Navidrome album ID and by
   `(edition-normalised album name, set of track IDs)`. The second and third
   VA views are skipped, avoiding repeated writes, fingerprint churn and
   racing album-artist values. The claim set is reset at the start of each
   import run.
4. **Existing composite album_artist rows are repaired.**
   The normal upsert preserves dirty local-authority fields, so a row carrying
   `Various Artists • HIM` would keep it forever. After a successful batch,
   `_normalise_va_album_artist` updates the album's dirty track IDs to plain
   `Various Artists` without clearing the dirty flag.
5. **Logs show the resolved identity.**
   Import/conflict lines now log `artist=<resolved album artist>` plus
   `view_artist=<Navidrome view>` when they differ. Conflict warnings include a
   `values=...` field with the local and remote values for up to three fields.
6. **A preserved local correction no longer blocks the checkpoint.**
   Previously any `metadata_conflicts > 0` set the artist import to failed, so
   `last_successful_import_ts` never advanced and every later run re-read the
   same artists. The verified Navidrome state was already read and the dirty
   rows remain protected, so the checkpoint now advances; the warning says so.

## Conflict comparison additions

`local_authority_conflicts` also now treats these as equivalent:

* `album_artist`: `Various Artists • HIM` vs `Various Artists` (VA identity),
  case-insensitive otherwise;
* `composer`/`lyricist`/`recordlabel`: delimiters and ordering;
* `releasestatus` and other case-only fields;
* `originaldate`/`releasedate` at the precision both sides carry.

Remaining `year`/`originalyear` conflicts may be semantic (local album year vs
Navidrome's per-track original year) rather than formatting; the new
`values=local=… remote=…` field shows which.

## What to do after deploying

1. Restart the WebUI and queue worker.
2. Run one Navidrome import. The VA albums should appear under **Various
   Artists**, once each, and the composite album_artist values should be
   repaired.
3. Check any remaining conflict lines' `values=`. If a field is a real local
   correction, it is intentionally preserved; if it is another format-only
   difference, it needs a normalisation rule.
4. Duplicate track rows created by previous runs (distinct Navidrome IDs) may
   need the normal rescan/cleanup; the import only dedupes within a run.
