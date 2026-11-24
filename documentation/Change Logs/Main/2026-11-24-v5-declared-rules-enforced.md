# The v5 vocabulary's declared rules are now enforced

**Date:** 2026-11-24
**Area:** `services/enrichment/genre_selection.py` ·
`services/enrichment/genre_vocabulary.py` · `services/enrichment/genre_vocabulary.json` ·
`services/enrichment/genre_aggregation_service.py` ·
`services/enrichment/genre_playlist_resync_service.py` ·
`services/popularity/stages/track_stage.py` · `services/popularity/stages/finalise_stage.py` ·
`helpers/config_helpers.py`
**Commit:** `fix(genres): enforce the v5 declared rules; rebuild Essentials on resync`

---

## The root cause of almost all of it

**The loader discarded `evidence_policy` entirely.** The vocabulary file documented
a dozen selection policies — per-position evidence levels, artist-only limits,
corroboration weights, dilution — and `_build` never read the block, so every one
of them was inert. Editing the file changed nothing.

`Vocabulary` now carries the block and `_settings_from_config` reads the keys it
implements. This is why several of the reported symptoms were "the code isn't
following what the JSON already says".

## JSON edits (the two you asked for)

1. `root_fallback_only` became a policy object with `same_family_only: true`.
2. `indie_rock` lost the bare `"indie"` alias. "Indie Rock" still matches through
   its display name.

⚠️ **The object form exposed a silent bug.** `bool({"enabled": false})` is `True`,
so the old `bool(...)` coercion left the rule ON for *any* non-empty dict. Both
readers now read `enabled` explicitly. Two tests pin it.

## Code fixes

| Problem | Fix |
| --- | --- |
| Hard Rock / Post-Rock / Alt Rock on every track | `positions[].requires_levels_any` is now enforced: positions 2+ need TRACK or ALBUM evidence. Artist tags fill slot 1 only. |
| Root rule | `same_family_only` is read. `Rock + Hard Rock` → Hard Rock; **`Punk + Hard Rock` keeps Punk**. |
| Live/Acoustic/Cover stored as genres | A marker is written only when its own `is_*` flag agrees (`marker_flag_allows`). Christmas/Soundtrack/Comedy/Remaster have no flag and are unaffected. |
| Genre order changes between tracks | One `_rank_key` with a display-name tiebreak, replacing four near-identical sorts that fell back to evidence-map iteration order. |
| Playlists shrinking to 48–66 | Two things: the creation gate now uses the **final** track count (it used the pool size, measured before the artist cap and slice), and the preview reports `shrinking_playlists` split into `genre_driven` / `builder_driven` with the knobs named. |

## Your Essential question: yes, and it was missing

`apply_genre_playlist_resync` rebuilt the genre top-tracks and the covers family.
**An Essential Collection only ever moved on a full scan**, so changing the
Essential rules and pressing "Apply & Rebuild" left every artist playlist
untouched.

Rather than add a second button, the existing resync now runs the Essential pass
at the end — the *same* `_refresh_all_essential_collections` the scan uses, so a
preview and a scan cannot disagree about what an Essential Collection is. It is
gated by `essential_playlists_enabled` and a failure cannot fail the job, because
the genres are already written by then.

⚠️ **I did NOT add a separate Config button.** You asked whether there *should*
be one; folding it into the existing "Apply & Rebuild" means one action has one
meaning. If you want them separable (rebuild Essentials without rewriting
genres), say so — that is a UI decision, not a correctness one.

## The shrink decision is yours

`shrinking_playlists` now tells you which of the two causes applies per playlist.
Genre-driven means the rules did their job. Builder-driven points at
`genre_playlists_max_tracks`, `genre_playlists_max_per_artist` and
`genre_playlists_create_threshold`, all named in the report. I have not changed
any of those values.

## Tests

* `tests/test_genre_v5_declared_rules_are_enforced.py` — 20
* `tests/test_essential_rebuilt_by_playlist_resync.py` — 8

Full suite: **447** against a true baseline of **451** (re-measured at HEAD with
this work stashed). Nothing added by me.
