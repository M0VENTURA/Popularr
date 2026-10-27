# Weekly rotation: charts rotate, Essential collections stay in order

**Date:** 2026-10-28
**Area:** `services/popularity/stages/finalise_stage.py`, `helpers/config_helpers.py`, Config page

## Report

> I wanted the top genre tracks to be randomised, but the essential artist collections in order.

## Root cause

`_weekly_shuffle()` was gated by **one** global switch
(`playlists.weekly_shuffle_enabled`, default **on**) and called from three
builders with no way to tell them apart:

| builder | what the playlist is | rotation applied |
|---|---|---|
| `_create_genre_top_track_playlists` | a **chart** — top tracks per genre | yes |
| `_create_new_music_playlist` | a **discovery feed** | yes |
| `_sync_essential_playlist` | a **ranked best-of for one artist** | yes ← wrong |

Rotation means opposite things to those families:

- a Top Tracks chart is a *shopping list* of what is hottest right now. If it
  always reads in the same sequence it goes stale even when nothing about the
  music changed, so rotating it is right.
- an Essential Collection is a *ranked best-of*. **"Track 1 is their strongest,
  the last row is their weakest" is the entire information the list carries.**
  Shuffling it throws that away.

One global switch cannot express that: turning rotation on to keep the genre
charts fresh also scrambled every artist's best-of.

## Fix

`_weekly_shuffle()` takes a **`scope`** naming the playlist family. With
`enabled=None` (the normal call), the rotation happens only when the master
switch **and** the family switch both allow it:

```python
enabled = bool(shuffle_cfg.get("enabled", True))
if enabled and scope:
    enabled = bool((shuffle_cfg.get("scopes") or {}).get(str(scope), True))
```

An explicit `enabled=` still wins, so the rotation's own unit tests keep
forcing the decision.

`get_playlist_shuffle_config()` now returns:

```python
{
    "enabled": <master>,
    "scopes": {
        "genre_top_tracks": True,   # a chart — rotates
        "new_music":        True,   # a discovery feed — rotates
        "essential":        False,  # a ranked best-of — stays ordered
    },
}
```

Each builder declares its family:

| builder | scope |
|---|---|
| `_create_genre_top_track_playlists` | `"genre_top_tracks"` |
| `_create_new_music_playlist` | `"new_music"` |
| `_sync_essential_playlist` | `"essential"` |

The `_weekly_shuffle()` call is **kept** in `_sync_essential_playlist` even
though it is now a no-op by default. Two reasons: the family switch lets it be
turned back on, and the pinned "rotation runs AFTER the track cap" contract
holds for every builder.

## Config page (both trees)

`Shuffle The Order Weekly` is now the master switch, and three family switches
sit beneath it:

| element id | config key | default |
|---|---|---|
| `playlists_weekly_shuffle` | `playlists.weekly_shuffle_enabled` | on |
| `playlists_shuffle_genre` | `playlists.weekly_shuffle_genre_top_tracks` | on |
| `playlists_shuffle_new_music` | `playlists.weekly_shuffle_new_music` | on |
| `playlists_shuffle_essential` | `playlists.weekly_shuffle_essential` | **off** |

The markup default and the Python default are asserted to agree.

## ⚠️ One consequence to be aware of

An existing `config.yaml` written before this change has **no**
`weekly_shuffle_essential` key, so it reads as `False` — Essential collections
stop rotating on the next scan without any user action. That is the intended
behaviour change, not a side effect: the rotation was never meant to apply to a
ranked best-of, and the switch is there to restore it.

`_sync_single_curated_playlist` (decade / hit-singles / hidden-gems /
new-releases) is **not** rotated and was not before; it is untouched.

## Verification

- `tests/test_genre_pooling_and_weekly_rotation.py` +
  `TestRotationIsPerPlaylistFamily` (16 tests): defaults per family, a genre
  playlist is reordered, an Essential collection keeps its order byte-for-byte,
  master switch still governs all, each family can be flipped, explicit
  `enabled=` wins, unreadable config fails closed, every builder declares its
  family, and every switch is rendered + collected in both Config trees.
- **Oracle:** reverting the two Python files fails **9 of the 16**; the 7 that
  still pass are the Config-page markup assertions, which do not depend on the
  Python.
- Regression sweep, 65 playlist / finalise / essential / genre / config /
  rotation / resync suites: **64 failures before, 55 after, 0 NEW**. The 9 that
  went away are exactly the new tests. The 55 remaining are pre-existing in
  this worktree and unrelated.
