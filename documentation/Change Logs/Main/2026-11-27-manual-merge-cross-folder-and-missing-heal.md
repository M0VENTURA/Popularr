# Manual merge across folders, missing-track auto-heal, and MBIDs at import

**Date:** 2026-11-27
**Commit:** `fix(folders): let a manual merge cross folders; heal missing tracks on download; persist MBIDs at import`

## 1. "Merge track into release doesn't work if they are in different folders"

The user is right that a manual choice should win, and the reason it did not is
worth stating precisely.

**The merge button itself has no folder guard.** I verified this by exercising
`unify_album_release_identity` and `update_album_ids` against a three-row album
whose third file lives in an unrelated folder: the fan-out wrote all three
(`rows_updated: 3`). There is no path comparison anywhere in that service.

The folder refusal comes from **`guard_album_mbid`** — the pre-write gate on
*Apply MBID* (`album_service.apply_mbid_to_album`, `artist_service.apply_album_mbid`,
and the album-page lookup). `check_folder_boundary` collapses the album's file
paths to "physical roots" and refuses when there is more than one, because two
folders usually mean two albums sharing a name.

That is a **heuristic**, and a heuristic must not be able to veto a person who
has just said, explicitly, that these rows are one album. A track legitimately
downloaded into its own folder trips it.

Fix: `guard_album_mbid(..., enforce_folder_boundary=False)`, threaded through
`apply_mbid_to_album(allow_cross_folder=...)` and the two routes, and offered by
the UI. When a refusal names multiple folders, the client now asks once — listing
the folders — and retries with `allow_cross_folder: true`.

**The text check still runs.** The release must still look like this album by
artist and title; only the physical inference is skipped. The verdict records
`folder_boundary_overridden` and still reports the folders, so the response can
say what was crossed.

`merge_album_names`' `_file_belongs_to_canonical` is deliberately **unchanged**.
That guard protects against an irreversible, unrecoverable album-tag overwrite
(a foreign file would keep its path while its tag claimed another album, and the
next "Rename Files" would physically move it). It is a different operation from
"write this release id onto my album", and a click must not destroy the only
remaining evidence of a file's real album.

## 2. "When a missing item is downloaded, it doesn't auto heal"

`get_missing_tracks_from_db` returned `missing_album_tracks` **verbatim** — no
join, no anti-join. Rows are only ever deleted by `_persist_missing_tracks`,
which runs from a live recompute: a full/metadata scan of that album, or the
manual `refresh=1` "Lookup MBID". **Nothing runs either after an import.**

Worse, an `imported` queue row deliberately stops matching
`_queued_status_for_album`, so the track loses its "in queue" tick and
re-renders as a fresh **Missing** row with a live Download button. The state
visually regresses at the moment the download succeeds.

Fix: the read path now drops any row the library has since gained, using the
same identity `_persist_missing_tracks` deletes on — `_title_match_key` for the
title, with disc+track as a second chance. No MusicBrainz call, so a page load
stays a pure DB read. `__queued_for_download__` placeholder rows are excluded:
a pending download is not a satisfied one.

The anti-join is defensive on its own — a failure there degrades to "no
anti-join" rather than blanking the list. The persisted row is still removed by
the next recompute; until then the two agree, because both are filtered by the
same test.

## 3. "The Recording ID and Release Track ID aren't populating when importing from soulseek"

The import was doing the expensive work and then discarding the result.

`_apply_stored_metadata` resolves the release, matches this track's position,
fills `musicbrainz_releasetrackid` into `meta`, and **writes it into the file
tags**. It then builds `_db_payload` from an allow-list, `_mb_cols` — which did
**not contain** `musicbrainz_releasetrackid`. So:

- `tracks.musicbrainz_releasetrackid` stayed NULL;
- the value only appeared later, when a Navidrome import read the tag back off
  the file — which is exactly the "old → new" the screenshot shows.

`mbid` (the column the album review labels **"MusicBrainz Recording ID"**) was
also absent, so the review kept proposing a recording id the import had already
stored under `recording_mbid`.

Fix: both columns added to `_mb_cols`, and the two recording spellings are kept
in step so a writer setting one cannot leave the other stale.

## Tests

- `tests/test_manual_merge_cross_folder_and_missing_heal.py` — 18 cases: the
  boundary refuses by default and is overridable; the text check still applies
  under override; both routes and the client carry the flag; the read path
  anti-joins against `tracks`, uses the same normalisation, excludes
  placeholders and logs the heal; the two MBID columns reach the DB payload and
  the recording spellings stay in step.
- `tests/test_missing_tracks_heal_on_download.py` — end-to-end: a seeded missing
  row disappears once the `tracks` row exists, and a queued placeholder does
  **not** count as owned.
