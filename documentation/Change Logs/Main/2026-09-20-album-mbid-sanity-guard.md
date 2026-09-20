# Album MBID sanity guard: never force an ID onto files that disagree with it

**Date:** 2026-09-20
**Area:** `services/metadata/album_mbid_guard.py` (new) / album page lookup / album MBID fan-out / Config
**Status:** implemented, guarded by `tests/test_album_mbid_guard.py` (39 tests)

---

## The problem

A stored album MusicBrainz ID was treated as **proof of identity** in two places,
and neither of them looked at the album:

| Step | Code | What it did |
|---|---|---|
| Album page lookup | `musicbrainz_service._lookup_existing_mbid` | resolved the stored ID and returned it with `confidence: 1.0`, so the UI **auto-selected** it ahead of every text-search candidate |
| Apply | `album_service.apply_mbid_to_album` | wrote the ID (plus the MB album-artist/type/country/year tags) onto **every** track **and every audio file** matching `(artist, album)` — with no comparison at all |

So one errant batch-tagging run that stamped a wrong ID onto a folder was enough
to merge unrelated albums. That is the reported Metallica / d'Artagnan case,
where a poisoned ID grew a 66-track "super album".

`artist_service.apply_album_mbid` (the corrections page) is the same fan-out and
had the same hole.

## The guard

New `services/metadata/album_mbid_guard.py`, called from all three points.

### 1. Text check — local artist/album vs the ID's own artist/title

Normalisation is deliberately **generous**, because a guard that cries wolf gets
switched off, which is worse than not having one:

- edition markers stripped (`Prokopton (Deluxe Edition)` → `Prokopton`,
  `(2011 Remaster)`, …) via `strip_album_edition_marker`
- a leading release year dropped (`2011 - A Night At The Opera`)
- featured / `&` guest credits stripped on both sides — the user's own example,
  `dArtagnan & The Dark Tenor` vs `dArtagnan`
- `Vol. 1` ↔ `Volume 1`, `Pt.` ↔ `Part`
- punctuation + case folded, then a token-set similarity (RapidFuzz
  `token_set_ratio` when available, Jaccard-with-containment fallback otherwise —
  the same fallback the old `queue_processor` used)
- the **artist** comparison is skipped when either side is a placeholder
  ("Various Artists", "Unknown"), because a compilation's per-track artists
  legitimately differ from the release-group credit

Threshold: **0.65**, configurable. Both album *and* artist must clear it.

### 2. Physical boundary — one folder = one album

Per the stated rule, *"the only way an album should be spread across two folders
would be if they are multi-disk of the same album"*:

```
Album/*.flac + Album/Disc 1/*.flac   -> ONE root (Album)        accepted
Album/CD1 + Album/CD2                -> ONE root (Album)        accepted
A/Greatest Hits + B/Greatest Hits    -> TWO roots               REFUSED
```

Disc subfolders are folded into their parent before counting roots, so a genuine
multi-disc release is never flagged. Paths may be stored RELATIVE (Navidrome
imports `Artist/Album/01 - x.mp3`) — the check only compares folders with each
other, so it never needs `MUSIC_ROOT`.

### 3. Unverifiable ≠ mismatched (the important distinction)

If the ID cannot be looked up — MusicBrainz unreachable, or a release that
answers without a title — there is **no measurement to act on**, so the update
proceeds and logs `[MB-GUARD] album MBID could NOT be verified - applying anyway`.

This was a deliberate correction during implementation, found by an existing
test (`test_album_mb_tags_to_files.py::test_file_tags_written_with_album_mbid`)
failing. Blocking on "no data" would have turned every legitimate
*Use This Album* into a silent no-op during a MusicBrainz outage — replacing a
rare corruption bug with a frequent one. The guard only has standing to refuse
when it has actually measured a contradiction, which is exactly the poisoning
case: a bad ID is usually a **real** MB release, so it resolves and compares.

## The fallback (silent, as requested)

On refusal **nothing is written** — not the DB columns, not the file tags, not
the cover art:

- **Album page lookup** — the conflicting stored ID is simply not offered; the
  text search that already runs supplies the candidates, so the picker shows the
  right album instead of the poisoned one.
- **Apply / corrections apply** — the response carries
  `{success: false, rejected: true, reason, album_similarity, artist_similarity,
  threshold, folders, refused_mbid, candidates[]}` where `candidates` is a
  **text search** for the album, so the next step is one click away.

Nothing is queued for review (per the chosen "auto-fallback, no review queue"),
but every refusal and every unverified apply is logged with
`artist/album/mbid/reason/similarities/threshold` under the `[MB-GUARD]` prefix.

## Config (Config page is the source of truth)

New fields in the **Updating Metadata** card, in BOTH trees, saved through the
same `metadata_update` block:

```yaml
metadata_update:
  album_mbid_guard:
    enabled: true
    min_similarity: 0.65
    allow_disc_folders: true
```

- `helpers/config_helpers.get_album_mbid_guard_config()` — defaults always
  present, partial user block merges (the recurring "value saves then reverts"
  bug class), `min_similarity` clamped to `0.05..1.0` so `0` cannot silently
  disable the guard.
- `routes/ui_routes.py::_sanitize_config_sections` gained `album_mbid_guard` in
  `metadata_update`'s nested-children tuple — without it the nested dict is
  coerced to `{}` on save.
- `static/js/config.js` + `test_site/static/js/pages/config.js` collect the three
  ids; `metadata_update_mbid_guard_enabled` / `_min_similarity` /
  `_allow_disc_folders`.

## Verification

Worktree at `origin/develop`, same edits replayed there (`album_mbid_guard.py` +
5 patches, code identical, docstrings trimmed in the harness).

| Check | Result |
|---|---|
| New suite, fix reverted (guard module + tests kept) | **9 failed** — the album-page auto-select, all three `apply_mbid_to_album` cases, and the 5 config tests |
| New suite, fixed | **39 passed** |
| `test_album_mb_tags_to_files` + `test_album_musicbrainz_matching` + `test_mb_cover_art_and_lookup_fixes` | **6 failed / 48 passed** = the **identical 6 pre-existing failures** as the unpatched baseline, **0 new** |
| `get_errors` on all 9 changed/added files | clean |

`test_legitimate_variation_is_accepted` (9 parametrised cases) is the guard
against over-strictness: identical, case/punctuation, deluxe/remaster edition
markers, leading year, `&`-credit, `feat.` credit, `Vol.`/`Volume`, and a
compilation credit all must PASS.

## Files

- `services/metadata/album_mbid_guard.py` — **new**: text comparison, folder
  boundary, verdict/logging, MB-text resolution, text-search fallback
- `services/enrichment/musicbrainz_service.py` — `lookup_musicbrainz_album` no
  longer auto-selects a stored ID that contradicts the album
- `services/metadata/album_service.py` — guard before the fan-out; `_album_file_paths`
- `services/metadata/artist_service.py` — same guard on the corrections action
- `helpers/config_helpers.py` — `get_album_mbid_guard_config()`
- `routes/ui_routes.py` — nested-children tuple
- `templates/pages/config.html`, `test_site/templates/Pages/config.html`,
  `static/js/config.js`, `test_site/static/js/pages/config.js` — Config UI
- `tests/test_album_mbid_guard.py` — **new**, 39 tests

## Not done (deliberately)

- No review/quarantine queue — per the chosen "auto-fallback only".
- The scan path (`album_stage._lookup_musicbrainz_album_type` →
  `retain_release_group_id`-style persistence) still resolves album types from
  text searches rather than a stored ID, so it is not a trust-the-ID site; it
  was left alone.
