# `raw_score`: the pre-remap popularity blend (2026-10-09)

## Reported

> When just running a finalize scan from the dashboard, no tracks are getting
> 5 stars.

## Root cause

`_apply_album_relative_normalization` rewrites `popularity_score` / `final_score`
from `_raw_combined` as

```
z = (score − median) / mad ;   score = sigmoid(z)
```

Monotonic but **saturating** — applied to an already-remapped value it pulls the
album's extremes back toward the middle.

`_raw_combined` was **never persisted**: it is an underscore-prefixed key, and
`_execute_save` filters to `if k in columns`. So the only copy lived in the
process, and the next run rebuilt it from `final_score` — the *remapped* value —
remapped it again, and `_persist_album_relative_scores` wrote the result back:

```sql
UPDATE tracks SET final_score = :s, popularity = :s WHERE id = :id
```

### Measured, with the shipped functions

An 8-track album (`[88, 71, 64, 55, 47, 39, 31, 24]`):

```
pass |  top album_z | clears 5* | top score
   0 |       1.512 |      True |      88.0   ← raw
   1 |       1.422 |      True |      73.3   ← first scan
  11 |       1.001 |      True |      66.5
  12 |       0.977 |     False |      66.1   ← 5★ LOST
  20 |       0.837 |     False |      63.8
```

`album_z` falls monotonically and crosses below `star5_album_z` (1.0) after
**12** further passes — and never recovers. 4/3/2/1 have lower bounds, so they
keep working. A repeated Finalise pass *is* that loop: every run re-normalises
already-normalised scores and persists them.

The **12** is specific to that distribution; real data crosses sooner or later
depending on its spread. What is proven is the **direction and the permanence**.

## Fix — store the blend the remap is defined against

New column **`tracks.raw_score`** (migration `016`), written wherever a genuinely
raw blend is computed and read back wherever a stored score was being used as
one.

| Path | Before | After |
|---|---|---|
| fresh scoring | `raw_score` not written — copy died with the process | written, under the `not _cached` guard |
| log-ratio audit re-blend | same | written (recomputed from raw components) |
| interlude re-blend | same | written (recomputed from raw Last.fm) |
| singles pass (stored score) | `_raw_combined = final_score` ← **the bug** | `_raw_combined = raw_score`, falling back to `final_score` for rows written before 016 |
| `_cached` branch (Finalise's path) | `_raw_combined = _stored_score` ← **the bug** | `_raw_combined = raw_score`, falling back |

`_apply_album_relative_normalization` is unchanged — it was always correct; it
was being handed the wrong input. With the raw blend restored the operation is
**idempotent by construction**: `remap(raw) == stored`, so a second pass changes
nothing and nothing is persisted.

`raw_score` was also added to `_POPULARITY_PROTECTED_COLUMNS` — a Navidrome
metadata sync must never replace it with a tag-derived value.

## Why a column and not a flag

A stopgap (skip re-normalising cached tracks) would have stopped the erosion
but left the remap reading a pool that mixes two scales. Storing the raw blend
makes the operation correct rather than merely non-destructive — which is why
this is the column and not the guard.

## Tests

`tests/test_raw_score_preserves_the_pre_remap_blend.py` — **12**:

* **The column**: declared in `db/schema.py` (the DDL registry the bootstrap
  reads) and `db/models.py`, and protected from Navidrome syncs.
* **The wiring**: written at all three raw-computation sites (with the fresh one
  pinned *under* the `not _cached` guard, or a cached track would overwrite a
  good `raw_score` with a remapped value), and read first in both stored paths
  with a fallback for pre-016 rows.
* **Why**: `_apply_album_relative_normalization` reports **0 changed** when
  `_raw_combined` is genuinely raw (the contract), **>0** when fed the
  already-remapped value (a control that proves the defect exists), and the
  measured 12-pass erosion crossing `star5_album_z`.
* **The migration** chains from `015` and is inspector-guarded — no
  `ADD COLUMN IF NOT EXISTS`, which is PostgreSQL-only and breaks the SQLite leg
  of the chain.

> ⚠️ Two traps hit while writing them, both previously recorded:
> * **A guard must not match its own explanation** — the migration docstring
>   *names* `ADD COLUMN IF NOT EXISTS` to say why it is PostgreSQL-only, and the
>   substring check reported the file as using it. `_strip_python()` now removes
>   docstrings and comments before matching.
> * **`\n` inside a raw string in a JSON-escaped edit becomes a real newline**,
>   producing a syntax error. `re.S` with `.*?` avoids needing it.

## Verification

* New suite → **12 passed**.
* **Oracle** — reverting the source + migration → **9 failed / 3 passed**;
  exactly the wiring and migration guards fail, and the three "why" tests pass
  both ways (they document the hazard rather than the wiring). Restored → 12.
* **Sweep** — 35 popularity/star/rating/migration/track files, clean
  `origin/develop` vs this change: **base `61 failed / 551 passed`** vs
  **new `52 failed / 560 passed`**, `Compare-Object` on the sorted `^FAILED`
  lines = **empty for "only in CHANGED"**. The 9 that only fail at BASE are this
  change's own tests (the oracle) → **0 regressions**; the other 52 are
  identical pre-existing failures.
* `tests/test_migrations_idempotent.py` passes with the new revision in the
  chain (its first run caught the PostgreSQL-only DDL above).

## Not done

* **Existing damage is not repaired.** The fix stops further erosion; scores
  already pushed down stay down. A forced popularity re-score rebuilds raw
  scores from live Last.fm/ListenBrainz data and would restore them, at the cost
  of re-fetching the whole library.
* Rows written before migration 016 have `raw_score IS NULL` and still fall back
  to `final_score` — they correct themselves on their next fresh score.
