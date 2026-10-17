# Non-Latin titles kept colliding on one aggregation key

**Date:** 2026-10-17
**Scope:** `services/popularity/popularity_matching.py` — shared by BOTH trees.

## The report

From a scoring review:

> **Arkona — Во славу Великим!:** Every track has 109,751 LB listens; highest
> score is 51.3 (zero 5★ tracks). … All 14 tracks inherited the exact same LB
> count … flattening variance.

The review's hypothesis was that a **single-track release** (`count=1`) had been
matched to the 14-track album. That was probed and **disproved** (a one-track
release cannot produce a title-keyed entry for rows whose titles differ).

## The actual root cause

`normalize_for_aggregation` ended with:

```python
value = re.sub(r"[^a-z0-9]+", " ", value)
```

`[^a-z0-9]` deletes **every non-Latin character**, so a Cyrillic (or CJK) title
normalised to the **empty string**:

```
'Во славу Великим!'  ->  ''
'Другая песня'       ->  ''
'Третья вещь'        ->  ''
```

Every non-Latin title on the album therefore shared ONE key — `""` — in the
prefetched popularity map (`prefetched_popularity[_key]`) and in the title-keyed
album-tracklist map. The first count resolved was read by all fourteen rows, so
one number (109,751) landed on every track and the album's distribution
flattened. It was never a release-matching problem; it was a **key collision**.

⚠️ **A normaliser that can return `""` turns a per-title map into a single
slot.** The empty key is not "no key" to the callers — they skip rows with a
*blank raw title*, not a blank *normalised* key — so the collision is silent.

## The fix

```python
value = value.replace("_", " ")
value = re.sub(r"[^\w]+", " ", value)
```

`\w` is **Unicode-aware**, so letters and digits from any script survive the
sweep, while punctuation still becomes a separator. `_` is folded first so its
handling is byte-identical to before.

Verified: `'Во славу Великим!'` → `'во славу великим'`, `'彼女の歌'` →
`'彼女の歌'`, and the ASCII/Latin output is **unchanged** (`'Café'` → `'cafe'`,
`'Hoppípolla'` → `'hoppipolla'`, `'Foo - Bar'` → `'foo bar'`, `'Track_One'` →
`'track one'`, `'Gangnam Style (PSY Cover)'` → `'gangnam style'`, `'Song feat.
X'` → `'song'`, `'Song (Remastered)'` → `'song'`).

## Tests

`tests/test_aggregation_key_non_latin.py` — **13 tests**: the non-Latin keys are
non-empty and distinct (including the Arkona 14-distinct-keys property), CJK is
preserved, and a parametrized CONTROL pins the unchanged ASCII/Latin shape.

### Oracle

| | result |
|---|---|
| without the fix | **4 failed** / 9 passed |
| with the fix | **13 passed** |

The 4 failures are exactly the non-Latin assertions; the 9 controls pass on
**both** sides, so the ASCII contract provably did not move.

Regression sweep of the 10 suites that touch this normaliser: **200 passed**,
1 failure = the documented pre-existing `test_the_identity_is_marked_unambiguous`
(`KeyError: 'title_is_unique'`), **proven identical with the fix stashed**.

## Files

* `services/popularity/popularity_matching.py`
* `tests/test_aggregation_key_non_latin.py` (new)
