# Soulseek matching: the new scorer rejected the most common filename layouts

**Date:** 2026-09-20
**Area:** `services/downloads/download_pipeline_service.py` / `tests`
**Status:** fixed, guarded by `tests/test_slskd_filename_layout_matching.py`

---

## Symptom

Automated Soulseek matching is markedly worse than `old_system` — correct files
that the old pipeline downloaded are now reported as `no_qualifying_result` and
retried/backed off.

## Two root causes

### 1. The filename parser lost the title (the big one)

`_parse_filename_parts` only recognised a basename split on the ASCII **`" - "`**
separator, plus a `"NN Title"` fallback. Anything else left `title = None` — and
`_score_result`'s **HARD TITLE GATE** reads exactly that field:

```python
title_score = _similarity(str(parts.get("title") or ""), expected_title)   # -> 0.0
title_ok = title_score >= 0.35                                              # -> False
if not title_ok: return 0.0
```

So every candidate in these layouts scored **0.0** and was rejected:

| Layout | Example | Parsed title (before) |
|---|---|---|
| artist + album in **folders**, bare title | `Metallica/Metallica/Enter Sandman.mp3` | `None` |
| **dot-separated** track number | `Metallica/01. Enter Sandman.mp3` | `None` |
| **en/em dash** separator | `Queen – Bohemian Rhapsody.mp3` | `None` |

The first row is *the* standard albums-on-Soulseek shape: peers share
`Artist/Album/Song.flac` with the number omitted or dot-separated, so a large
fraction of otherwise-perfect candidates were rejected outright.

Fix:
- whitespace-flanked `–`, `—`, `‐`, `‑`, `‒`, `―` are normalised to `" - "`
  (only whitespace-flanked, so a title like `1999–2000` is never split);
- a `_TRACK_NUMBER_PREFIX_RE` strips `"01. "`, `"01."`, `"01 "`, `"07 - 12 "`. It
  requires a separator or whitespace after the digits, so `Prince/1999.mp3` is
  **not** truncated to `"9"`;
- if nothing structured matched, **the basename IS the title**.

### 2. The artist gate and the artist score disagreed about who the artist is

The queue artist often carries a guest the filename never mentions
(`"KNEECAP feat. Fawzi"`). The evidence gate strips that suffix:

```python
gate_artist = _FEAT_SUFFIX_RE.sub("", expected_artist).strip()   # "KNEECAP"
```

…but the score bonus used the **raw** credit:

```python
art_score = _similarity(parsed_artist, expected_artist)          # 0.54 -> no points
elif _normalise(expected_artist) in _normalise(filename):        # full credit not in name
```

So a candidate the gate had *just accepted* earned **0 artist points**, and a
perfect title match (25) fell under the 45 floor:

```
"KNEECAP - Better Way To Live.mp3"   ->  25.0   (rejected)      old pipeline: 1.00 → accepted
```

Fix: one `credit_artist` derivation, used by **both** the bonus and the gate, so
they can never disagree.

## Measured effect

Side-by-side probe of the old pipeline scorer
(`services/queue/queue_scoring._score_soulseek_candidate`, 0–1, accept ≥ 0.45)
against the new one (`_score_result` + the 45.0 floor) over realistic peer
paths:

| | old accepts | new accepts | regressions |
|---|---|---|---|
| before | 19/19 | 11/19 | **8** |
| after | 19/19 | **19/19** | **0** |

## Verification

Worktree at `origin/develop` (`002bb848`), same edits replayed there.

| Check | Result |
|---|---|
| New guards, fix reverted | **19 failed / 11 passed** — the failures name each layout; the 11 passes are the "still rejected" safety tests |
| New guards, fixed | **30 passed** |
| Download suites (7 files) | 22 failed / 73 passed — **exactly the same 22 pre-existing failures as the unpatched baseline** (22/42), 0 new |
| `test_slskd_wrong_artist_rejection.py` | 2 pre-existing failures only (`test_unknown_artist_skips_gate`, `test_generic_various_artist_skips_gate` — fail identically on clean code) |

## Also found (reported, NOT changed — separate decisions)

1. **The top-N / early-accept pruning was never ported.**
   `helpers/config_helpers.py` carries `_AUTO_SEARCH_TOP_N_CANDIDATES = 50` and
   `_AUTO_SEARCH_EARLY_ACCEPT_LENGTH_TOLERANCE = 2`, copied from the old
   processor — and they are **referenced nowhere** in the new tree. The old
   `_run_soulseek_search` used them to (a) score only the N best
   length-prioritised candidates and (b) accept immediately on an exact
   artist+title+duration hit. The new loop scores every candidate from scratch
   and never early-accepts.
2. **`_filename_matches_queue_item` was never ported.** The old matcher's
   variant-token handling (`live`/`acoustic` always rejected; `radio`/`edit`
   tolerated), core-title exactness and orphan-token rejection live only in
   `old_system/queue_processor.py`. Some of it survives in
   `services/queue/queue_scoring.py` (which now even *adds* an
   `edition_annotations_compatible` gate the old one did not have).
3. **Banned/dismissed word filtering is gone from the automatic search.** The
   old path filtered the primary, bracket-stripped AND fallback queries through
   `_build_safe_search_query`/`_filter_banned_words`. In the new tree banned
   words are an in-memory dict used only by the banned-words page
   (`routes/download_search_routes.py`), so `process_queue_item` never applies
   them.
4. **Two different scorers now judge the same download.** Search uses
   `_score_result` (0–119, hard gates); completion verification uses
   `_score_soulseek_candidate` (0–1, `_SLSKD_MIN_ACCEPT_SCORE`) — so a file can
   be selected by one and rejected by the other. In practice the completion
   path usually wins via the exact `found_filename` match, but the divergence is
   a latent loop (deleted + rescheduled) — e.g. `"… (2011 Remaster).flac"` is
   accepted at search time (65.0) and scores **0.00** in the ported scorer's new
   edition gate.
5. **The old pipeline tried the bracket-stripped query FIRST**; the new one
   tries the raw stored query first and only reaches the stripped variants via
   the fallback list.

## Files changed

- `services/downloads/download_pipeline_service.py` — dash normalisation,
  `_TRACK_NUMBER_PREFIX_RE`, always-a-title fallback, single `credit_artist`
  derivation shared by the gate and the score
- `tests/test_slskd_filename_layout_matching.py` — new, 30 tests
