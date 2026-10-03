# Navidrome import now pulls MusicBrainz IDs — and stops wiping them (2026-10-04)

**Report:** *"I don't think the Navidrome import is pulling in the mbid for
all the tracks, which is what is causing the issues."*

## Root cause — two halves, both proven against Navidrome's own source

1. **Never pulled in.** Navidrome sends the **recording** MBID as
   `musicBrainzId` on every song child
   (`server/subsonic/helpers.go::osChildFromMediaFile` →
   `child.MusicBrainzId = mf.MbzRecordingID`) and the **release** MBID as
   `musicBrainzId` on the *album* (`AlbumID3` → `album.MbzAlbumID`). The old
   system read exactly that key —
   `t.get("musicBrainzId", "") or t.get("mbid", "")  # Navidrome uses musicBrainzId field`
   (`old_system/deprecated/navidrome_import.py`) — and the new-system port
   **lost the line**, so `extract_track_metadata` matched key names Navidrome
   never sends (`musicbrainz_recordingid`, …) and always returned `""`.
   On top of that, `build_track_payload` never mapped anything to the
   canonical **`recording_mbid`** column — the column every consumer queries
   (queue dedupe, download-import track resolution, popularity, love sync).

2. **Actively wiped.** The tracks upsert writes `col=EXCLUDED.col` for every
   payload key (`_execute_save`), and the payload carried `""` for every MBID
   identity field whenever extraction came up empty — so **every import
   overwrote the MBIDs the popularity scan / download import had stored**
   (`mbid`, `musicbrainz_trackid`, `musicbrainz_albumid`,
   `musicbrainz_album_mbid`, `musicbrainz_releasegroupid`, artist ids…).
   The old upsert used `COALESCE(EXCLUDED.…, tracks.…)` for these columns;
   that preserve semantics was lost in the port. The same trap class is
   already documented for scoring columns in `_POPULARITY_PROTECTED_COLUMNS`
   ("a Navidrome sync must never clobber them").

## Changes

* `services/scanning/metadata_extractor.py` —
  `extract_track_metadata` now reads `musicBrainzId`/`musicbrainzId` (first
  aliases) for the recording MBID and feeds **both** `mbid` and
  `musicbrainz_trackid` from it; legacy tag-key aliases kept as fallbacks.
* `services/scanning/payload_builder.py` —
  * new `MBID_IDENTITY_FIELDS`: an empty MBID is **omitted** from the
    payload instead of sent as `""`, so the upsert leaves the stored value
    alone (old `COALESCE` semantics);
  * `build_track_payload` gains `album_mbid=` (release MBID from the album
    object) and writes it to `musicbrainz_album_mbid` + `musicbrainz_albumid`;
  * the canonical **`recording_mbid`** column is written whenever Navidrome
    provides a recording MBID (only when non-empty);
  * artist MBIDs keep their validated form when present, omitted when not.
* `services/scanning/navidrome_import.py` — passes
  `album_mbid=album.get("musicBrainzId")` per album, so every track row of
  the album receives the release MBID.

## Verification

* `tests/test_navidrome_import_pulls_mbids.py` (15): extractor key reading
  (incl. legacy-alias controls and the "song's musicBrainzId must not leak
  into the album column" guard), payload emit/omit contract, **real-upsert
  preserve test** (stored MBIDs survive a sync without MBIDs), real-upsert
  write test, and two **end-to-end `scan_artist_to_db` runs** — one proving
  `musicBrainzId` lands in `recording_mbid`/`musicbrainz_album_mbid`, one
  proving a tagless response does NOT erase previously stored IDs.
* **Oracle:** 11 failed / 4 controls with the three source files stashed →
  **15 passed** restored. The wipe e2e was redesigned after the oracle
  caught it passing vacuously (`should_skip_cached_album` skipped the
  unchanged album) — it now reports one extra song so the upsert really
  runs, and fails unpatched with `'' == 'stored-alb'`.
* **Regression sweep:** all seven import/extractor suites — **67 passed,
  0 failed** (no baseline needed: nothing fails).
* `import app` OK (389 routes).

## Effect on the affected instance

After the next import scan, tracks whose files carry `MUSICBRAINZ_TRACKID`
tags (including everything Popularr's own download import wrote) gain
`recording_mbid`, and album rows gain `musicbrainz_album_mbid` from the
album's `musicBrainzId` — while IDs resolved earlier by the popularity scan
are no longer blanked by a later import.
