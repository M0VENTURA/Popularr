# Sequel, short-title and version collisions in provider matching

**Date:** 2026-10-31
**Area:** popularity matching / Last.fm + ListenBrainz aggregation
**Files:** `services/popularity/popularity_matching.py`,
`tests/test_sequel_and_version_popularity_matching.py` (new)

## Reports

* Metallica **"The Unforgiven II"** shared exact Last.fm totals with
  **"The Unforgiven"** (2,368,661 listeners / 20,978,370 plays).
* Pearl Jam **"Spin the Black Circle"** shared exact totals with **"Black"**
  (1,923,662 / 19,879,527).
* Stray Kids **`MANIAC -Japanese ver.-`** (and VENOM, Chk Chk Boom, Silent Cry)
  shared totals with the Korean originals.

## Cause

The aggregation gate used `fuzzy_match_score`, which is
`rapidfuzz.token_set_ratio`. That metric returns **1.0 whenever one token set is
a strict subset of the other** — by design. So every shorter title "matched" a
longer one that merely contained its words:

    "the unforgiven"  ⊂ "the unforgiven ii"
    "black"           ⊂ "spin the black circle"
    "maniac"          ⊂ "maniac japanese ver"

The gate's novelty-phrase check only covered gimmick suffixes ("for dogs").

## Fix

`aggregation_keys_compatible` now handles a strict subset explicitly:

* a **sequel token** (`ii`, `iii`, `iv`, `pt`, `part`, `2`…) ⇒ **not the same**;
* an extra that is only a **version marker** (`live`, `acoustic`, `unplugged`,
  `remix`, `radio edit`, `version`…) or framing (`the`, `deluxe edition`) ⇒
  still the same at THIS gate; marker agreement stays with the context-aware
  `title_variants_compatible` / `_alt_status_matches_ctx`, which know the
  release context (a plainly titled track on an unplugged release must match);
* a **provenance/session** anchor (`from`, `session(s)`, `outtake(s)`, `take(s)`)
  ⇒ still the same recording — Last.fm files
  "100 Words (from Sing the Sorrow sessions 2003)" separately and those rows
  must sum;
* otherwise a strict subset is rejected. In particular, `japanese`/`ver` are
  not recognised markers, so a Japanese version is a **different recording**;
  and a one-word title only matches exactly: "Black" can no longer consume
  "Spin the Black Circle".

Roman numerals are **not removed** by normalisation; they are now enforced as
meaningful tokens. The same gate is used by the ListenBrainz candidate search,
so both providers separate the versions.

Japanese version counts are now separate. The user-suggested "base-song
popularity as a capped inherited fallback" is not implemented in this change —
nothing is inherited rather than inheriting the wrong number.

## Verification

`tests/test_sequel_and_version_popularity_matching.py`:

| Test | Pins |
|---|---|
| `test_the_unforgiven_and_its_sequel_do_not_match` | ⭐ II ≠ original |
| `test_part_two_is_a_different_song` | pt/part/2 |
| `test_black_does_not_match_spin_the_black_circle` | ⭐ one-word containment |
| `test_one_word_soft_variant_still_aggregates` | radio edit still sums |
| `test_a_session_annotation_still_aggregates` | provenance still sums |
| `test_japanese_versions_do_not_inherit_the_original` | ⭐ Japanese ver. |
| `test_live_and_acoustic_stay_separate_upstream` | marker handling stays upstream |
