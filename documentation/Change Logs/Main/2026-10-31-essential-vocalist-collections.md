# Essential Vocalist Collections (cross-project)

**Date:** 2026-10-31
**Area:** playlists / popularity finalise / metadata scan
**Files:** `services/popularity/stages/finalise_stage.py`,
`services/playlists/playlist_taxonomy.py`,
`services/enrichment/musicbrainz_service.py`,
`services/popularity/stages/track_stage.py`,
`helpers/config_helpers.py`, `db/models.py`, `db/schema.py`,
`migrations/versions/034_tracks_vocalists.py`,
`templates/pages/config.html`, `static/js/config.js`,
`test_site/templates/Pages/config.html`,
`test_site/static/js/pages/config.js`,
`tests/test_essential_vocalist_collections.py` (new)

## What it is

A second Essential family: **`<Vocalist> - Essential Vocalist Collection`**.
Unlike the per-album-artist Essential Collection, this gathers the 4★/5★
recordings a vocalist sang on **across projects** — their own releases plus
guest appearances on other artists' tracks (e.g. “Chad Grey - Essential
Vocalist Collection”, “Melissa Bonny - Essential Vocalist Collection”).

Both families have separate names and ownership types, so neither the orphan
cleaner nor the per-playlist Regenerate action confuses one for the other.

## Qualifying criteria (configurable)

| Setting | Default |
|---|---|
| `essential_vocalists_min_tracks` | 15 distinct 4–5★ recordings |
| `essential_vocalists_min_release_groups` | 4 distinct release groups (compilations do not add breadth) |
| `essential_vocalists_min_projects_with_five` | 2 projects with ≥ `min_project_tracks` tracks |
| `essential_vocalists_min_project_tracks` | 5 |
| `essential_vocalists_min_projects` | 3 (alternative branch) |
| `essential_vocalists_min_outside_dominant` | 5 tracks outside the dominant project |
| `essential_vocalists_min_outside_ratio` | 0.25 of the pool |
| `essential_vocalists_max_tracks` | 50 |
| `essential_vocalists_name_template` | `{artist} - Essential Vocalist Collection` |
| `essential_vocalists_enabled` | on |
| `essential_vocalists_include_featured` | on |

A collection is built only when **both** the cross-project branch (2 deep
projects OR 3 projects) and the outside-dominant branch pass. Below any
threshold the playlist is deleted, exactly like the per-artist family.

## Confirmed recording-level vocals

`musicbrainz_service._vocalists_from_recording` reads the recording's performer
relations and keeps only roles containing `vocal` (`vocals`, `lead vocals`,
`background vocals`, …). A performer relation with a non-vocal role is NOT a
vocalist. Only when the recording has **no performer relations at all** does it
fall back to the recording's artist credit.

The list is persisted in the new `tracks.vocalists` column (migration 034) by
the metadata scan (`track_stage`). Rows scanned before this change have no
column value; the builder falls back to the guest credits in
`artist` / `album_artist` / `title` for those rows. A re-scan populates the
confirmed list.

## Projects and breadth

* **Project** = the album artist (guest credit stripped); a placeholder album
  artist falls back to the track artist.
* **Release group** = `musicbrainz_releasegroupid`, else `project::album`.
  Compilation rows are excluded from breadth so several compilation editions
  cannot manufacture it.
* **Dominant project** = the project with the most qualifying recordings;
  the remainder must still clear the outside-dominant floor.

## When it runs

* Once per real scan finalise pass (when global playlists are not deferred),
  and
* in the library-wide Essential reconciliation (daily/manual rebuild, star
  resync).

The per-artist scan path cannot build it — the pool spans artists.

## Config UI

Playlists → Essential Playlists now has the vocalist toggle, the guest
appearance toggle, every threshold above, and the name template, in **both**
the test-site and live Config trees. The existing config-surface ratchet
(`test_every_key_the_backend_reads_is_collected`) covers the new keys.

## Verification

`tests/test_essential_vocalist_collections.py`:

| Test | Pins |
|---|---|
| `test_fifteen_tracks_across_three_projects_qualify` | base case |
| `test_two_projects_with_five_each_qualify` | depth alternative |
| `test_fourteen_tracks_fail_the_track_floor` | floor |
| `test_three_release_groups_fail_breadth` | breadth |
| `test_compilations_do_not_add_release_breadth` | ⭐ compilation exclusion |
| `test_one_dominant_project_fails_cross_project_depth` | depth |
| `test_fewer_than_five_outside_the_dominant_project_fails` | ⭐ outside share |
| `test_confirmed_vocalists_are_authoritative` | vocal confirmation |
| `test_confirmed_relations_override_the_credit_string` | strictness |
| `test_a_guest_credit_is_used_when_the_row_was_never_scanned` | fallback |
| MB relation tests | vocals vs guitar, credit fallback |
| taxonomy tests | new name type + reverse value |
| config tests | defaults + partial overrides + both UI trees |
