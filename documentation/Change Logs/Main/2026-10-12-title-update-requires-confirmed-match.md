# Track titles auto-update only on confirmed matches; everything else goes to "metadata requiring approval"

**Date:** 2026-10-12
**Area:** metadata / scan / approval
**Commit:** `fix(metadata): a track title auto-updates only on a verified track#+length match; unconfirmed matches go to the approval panel`

## Reported

> When updating a track title, it should only update if the track number and
> track length are matched. Anything that is flagged as potentially wrong with
> the match should leave it only as a recommendation and post to "metadata
> requiring approval" on the dashboard.

## The three confidence levels

The scan's release-tracklist identity now carries a confidence marker end to
end, from the position/duration matching through the per-album batch to the
track-stage promotion:

| match | produced by | title behaviour |
|---|---|---|
| **CONFIRMED** | position-qualified alias (the local track NUMBER matched a release position) AND the ±5 s length guard VERIFIED both durations | **auto-applied** |
| **POTENTIALLY WRONG** | a position match whose length was never verified (either side's duration missing), OR the title-keyed fallback (no track-number guarantee) | **recommendation only** → `tracks.pending_mb_updates` → the dashboard's "metadata requiring approval" panel |
| **SEARCH** | no batch identity at all (`_from_batch` False) | unchanged — never auto-applied, never recommended (a title-search result is too unstable to offer) |

## Changes

1. **`services/popularity/popularity_sources.py`** — the position-matched
   identities (both the title-key fill and the per-row position-qualified
   alias) now carry `length_verified: True` ONLY when both durations were
   known and within the 5 s guard. The guard is skipped when either side's
   length is missing, so absent lengths must not read as "verified".

2. **`services/popularity/scan_stage_runner.py`** —
   `_build_album_recording_batch` records whether an identity came from the
   position alias (`by_position`) and whether its length was verified, then
   exposes `entry["_title_match_confirmed"] = by_position AND length_verified`
   on every batch entry.

3. **`services/popularity/stages/track_stage.py`** —
   `_resolve_track_mb_metadata` promotes the release title to the track title
   ONLY when `_title_match_confirmed` is true (plus the existing gates: not
   frozen, not ignore-listed, differs, apply-mode). When the batch supplied a
   release title but the match is NOT confirmed, it returns a
   `title_recommendation` instead of applying. `process_track` stashes that
   onto `tracks.pending_mb_updates` via the new merge helper.

4. **`services/metadata/pending_update_service.py`** — new
   `stash_track_title_recommendation`: writes ONE track's title proposal onto
   its `pending_mb_updates`, MERGING into any existing envelope (never clears
   the album — that lifecycle belongs to `stash_album_recommendations`),
   respecting `mb_ignored_fields`, and declining to touch an envelope of an
   unknown version. This is what feeds the dashboard's "needs metadata
   approval" panel (`/api/metadata/pending-recommendations` →
   `fetch_albums_pending_review`), which already groups by album and counts
   changes per track.

   In recommend-only mode (`metadata_update.apply_during_scan: false`) the
   branch stays silent — the album-level stash already owns recommendations
   there, so nothing is double-posted.

## Tests

`tests/test_title_update_requires_confirmed_match.py` (12):

- confirmed (position + verified length) → promoted, no recommendation;
- title-keyed fallback → NOT promoted, recommendation present;
- position alias without verified length → NOT promoted, recommendation
  present;
- ignored title → neither promoted nor recommended;
- recommend-only mode → nothing stashed per-track (album stash owns it);
- search result → no promotion, no recommendation;
- `_build_album_recording_batch` marks confirmed only for position+verified
  length, and unconfirmed for the title-keyed fallback and for
  position-without-verified-length;
- the stash helper writes a title change, merges with an existing envelope,
  and respects ignored fields.

Existing `test_release_title_promotion_and_file_sync.py` updated to the
confirmed gate (batch fixtures gain `_title_match_confirmed: True` where they
assert promotion).

**Oracle** at `252c3d0d`: 8 failed / 4 passed (the 8 are exactly the new
behaviour tests; the 4 control passers are the recommend-only, search-result,
ignored, and confirmed-promote paths). **Sweep** (10 pending/title/identity
suites): base 9 failed / new 1 failed — the only-in-BASE set is exactly my 8
new oracle tests; the 1 shared failure
(`test_album_release_track_identity … test_the_identity_is_marked_unambiguous`)
fails at baseline in isolation → **0 regressions**.

## Note on the dashboard flow

An album whose unconfirmed title was stashed appears on the dashboard's
"metadata requiring approval" panel immediately. Saving or discarding the
review clears the stash (the existing save path already calls
`discard_album_recommendations`), so the panel empties without a rescan.