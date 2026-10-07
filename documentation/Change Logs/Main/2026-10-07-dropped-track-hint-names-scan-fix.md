# Dropped-track hint names the concrete Navidrome fix

**Date:** 2026-10-07
**Area:** scan / playlists (drop reporting)
**Commit:** `fix(scan): dropped-track hint names the concrete Navidrome fix`

## Report

```
Navidrome did not store these track(s) in the playlist
playlist='Rock - Top Tracks' dropped=1 tracks=['Ivri - NOISE']
hint='stale ids are refreshed by a Navidrome import; ids for files
Navidrome has not indexed yet need a Navidrome scan first'
```

The hint said a scan was needed but not how to arrange one.

## Fix

`services/popularity/stages/finalise_stage.py` — extend the hint with the
actionable configuration: run a scan from Navidrome's admin UI, or shorten
its `ScanSchedule` setting (e.g. `@every 1m`) so newly downloaded files are
indexed before the next playlist sync.

No behavioural change; log text only. The playlist-sync verification,
`dropped_ids` reporting, and import-triggered id refresh are unchanged.
