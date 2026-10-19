# Gemini genre covers never appeared — the skip branch jumped over the cover pass

**Date:** 2026-10-19
**Area:** `services/popularity/stages/finalise_stage.py` · playlist AI covers
**Commit:** `fix(playlists): run the genre AI-cover check on the unchanged-playlist path`

## Report

> "I'm still not seeing any album art created by gemini. The api token has been
> added to config."

No `[GENRE_COVER]` lines, no `🎨 Playlist cover: AI artwork set` and no
`🎨 Genre playlist covers — …` summary anywhere in the scan log.

## Root cause — one real bug, plus one silent-gate trap

### 1. A `continue` above the cover block skipped it forever

In `_create_genre_top_track_playlists`, the sync result was handled with a bare
`continue` **above** the AI-cover block:

```python
keep_playlist_names.add(playlist_name)
if sync_res.get("skipped"):
    skipped_unchanged += 1
    continue                      # ← jumps over the entire cover block
log_unified(f"📄 Playlist: Synced '{playlist_name}' …")
written += 1

# ── AI cover art for the TOP GENRE playlists ──────────────────
```

`sync_res["skipped"]` is set by `_sync_playlist_to_navidrome` when
`_playlist_already_pushed(name, signature)` matches the in-memory
`_PLAYLIST_SIGNATURES` cache — i.e. *"this exact content was already pushed by
this process"*. It says nothing about the playlist's **art**.

So the block was skipped for every playlist that had ever synced, which
directly contradicts the block's own comment:

> Runs for EVERY playlist this pass synced — created, updated OR merely
> unchanged — because the point is to ensure the playlist HAS its cover

Consequences: turning the feature on, fixing a bad API key, or healing a cover
deleted by hand could not take effect until the playlist's **content** changed.

### 2. The disabled case was completely silent

`_cover_checked` is only incremented *inside* the `if cfg["enabled"]` branch, so
the guard on the summary line

```python
if _cover_checked:
    log_unified(f"🎨 Genre playlist covers — …")
```

produced **no output at all** when the toggle was off. "Feature disabled" and
"feature ran and found nothing to do" were therefore indistinguishable — which
is what makes a missing toggle so hard to spot in the log.

⚠️ Note the two independent settings, **both default `False`**:

| Setting | Where | Effect |
|---|---|---|
| `api_integrations.gemini.enabled` + `api_key` | Config ▸ API Integrations | Gemini is usable at all |
| `playlists.genre_playlist_ai_cover_enabled` | Config ▸ Playlists | covers are generated for `{Genre} - Top Tracks` |

Adding the key alone does **not** enable covers, and when
`genre_playlist_ai_cover_enabled` is on but Gemini is not, the only trace is a
`logger.warning("Genre playlist cover not applied", reason="Gemini is not
configured")`.

## Fix

`services/popularity/stages/finalise_stage.py`:

* The skip branch now counts the unchanged playlist and **falls through** to the
  cover block instead of `continue`-ing past it, matching the documented
  contract. The check stays cheap: the recorded prompt hash *and* playlist id
  come from the state file, the playlist id is passed in from the existing
  `getPlaylists` fetch, and Gemini is only called when they do not match.
* Added `_cover_flag_off`, set when the pass runs with the genre-cover toggle
  off. The summary now emits a one-line explanation **only when the Gemini key
  is actually configured**, so it points at the real misconfiguration without
  becoming noise for users who simply do not want the feature:

  ```
  🎨 Genre playlist covers: OFF — tick 'AI covers for Top Genre playlists'
  in Config ▸ Playlists (adding the Gemini key alone does not enable them)
  ```

## Tests

`tests/test_genre_cover_reached_on_unchanged_playlist.py` (new, 3 tests) —
structural assertions over the real source for the control-flow property:

* no bare `continue` statement between the skip branch and the cover block
  (checked per LINE, so the explanatory comment that mentions `continue` does
  not satisfy the assertion);
* the cover block still follows the sync result (guards the anchor);
* the disabled case is distinguished in the summary.

**Oracle:** reintroducing the `continue` makes
`test_no_continue_between_the_skip_branch_and_the_cover_block` fail with the
intended message; reverting it goes green again.

**Gate:** `test_genre_ai_covers` + `test_genre_cover_exists_check` +
`test_finalise_scan_mode` + `test_finalise_essential_refresh_and_dedup` — 88
passed, 0 new failures.
