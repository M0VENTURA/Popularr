# Live titles are persisted and scored as live

**Date:** 2026-10-31
**Area:** popularity scan / track stage
**Files:** `services/popularity/stages/track_stage.py`,
`services/enrichment/musicbrainz_service.py`,
`tests/test_live_alternate_title_and_mbid.py`

## Report

Explicit live titles such as `Blind (live)`, `Cocaine (Live)`,
`The Virtue Of Angels (Live)`, `I'm OK With It (Live)` and
`About A Girl - Live` were stored with `is_live = 0`, so they inherited studio
popularity without the live penalty. Other live recordings were correct.

## Causes

1. **The track stage never wrote the column.** `_resolved_is_live` was computed
   and returned in the result, but `update_payload` never received `is_live`,
   and the persist path writes only the payload. The only scan writer was the
   album stage, gated on the **album** being live — so a live bonus cut on a
   studio album was always 0.
2. **The scoring predicate was weaker than the result predicate.** The
   popularity block used a trailing **bracketed** regex, which misses the dash
   form `About A Girl - Live`. The result flag used
   `is_live_or_alternate_track_title` (brackets **and** trailing `- Live`), so
   one row could be classified live for rating and studio for scoring.
3. **A live release-group's tracks often have no "(Live)" in their title.** The
   recording lookup selected a release but never published whether its
   release-group is a live release.

## Fix

* `_recording_to_metadata` now returns `is_live` from the selected release
  group's `secondary-types` (`["Live"]`).
* `track_stage` reads it into `_mb_is_live`, uses the **same**
  `is_live_or_alternate_track_title` predicate for scoring, and persists
  `is_live = 1` when the explicit live wording, the MB live release group, the
  live album context or the live album type says so.
* Persistence uses a narrower `_LIVE_TITLE_RE` (live wording only). The
  broader helper also accepts acoustic/unplugged/orchestral/demo for the 4★ cap;
  writing those into `is_live` would mislabel a separate column. Unplugged is
  still persisted through the live album context / MusicBrainz live release
  group, and keeps its own `is_acoustic` treatment.
* The title predicate still only recognises bracketed markers and a trailing
  `- Live`/`- Acoustic`; bare-word titles (`Stayin' Alive`, `Eat You Alive`,
  `Live While You Can`, `Live and Let Die`) are not matched.

## Verification

`tests/test_live_alternate_title_and_mbid.py::TestTrackStageLiveTitleWiring`

* the existing `(Live)` case now also asserts the **persisted row** carries
  `is_live = 1`;
* new `test_dash_live_title_is_scored_and_persisted_live` covers
  `About A Girl - Live`;
* the plain-title control asserts no `is_live` is written.

`tests/test_sequel_and_version_popularity_matching.py::TestTheRecordingLookupPublishesLive`
covers the release-group → `is_live` mapping.
