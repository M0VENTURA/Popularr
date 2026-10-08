# `is_cover IS NOT TRUE` on a BIGINT column — the reported type mismatch (2026-10-08)

## Reported

> **Type Mismatch Query Error**: At 22:22:18 AEDT, an `UPDATE tracks` statement
> fails with the error "argument of IS NOT TRUE must be type boolean, not type
> bigint at character 406".

## Root cause

`services/enrichment/cover_verdict_repair_service.py`:

```sql
UPDATE tracks
SET is_cover = 1, is_cover_reason = :repaired_reason, cover_last_checked = NULL
WHERE is_cover_reason = :damaged_reason
  AND is_cover IS NOT TRUE              -- ← is_cover is BIGINT
  AND cover_manual_override IS NOT TRUE -- ← BOOLEAN, correct
```

| column | declared as | `db/schema.py` | `db/models.py` |
|---|---|---|---|
| `is_cover` | **BIGINT** | `"is_cover": "BIGINT DEFAULT 0"` | `mapped_column(BigInteger, …)` |
| `cover_manual_override` | **BOOLEAN** | `"cover_manual_override": "BOOLEAN DEFAULT FALSE"` | `mapped_column(server_default="FALSE")` |

PostgreSQL's `IS [NOT] TRUE` requires a boolean operand, so the first predicate
is rejected outright. The statement is swallowed by `except Exception` and
logged as `Cover verdict repair skipped`, so the repair has silently not been
running — and the error is exactly what arrived in the log.

**Character 406 lands inside `is_cover`** once psycopg2 has interpolated the two
long bound strings (`Re-flagged for deep detection: …` and `cover attribution
removed from title`) into the statement the server actually receives.

## How it got in — a test pinned the wrong column's rule

This is the *second* time this one statement has been "fixed" by the wrong
rule:

1. **Original bug:** both predicates used `COALESCE(<col>, 0) = 0`. Legal for
   `is_cover` (BIGINT); illegal for `cover_manual_override` (BOOLEAN) →
   `COALESCE types boolean and integer cannot be matched`. The repair never ran
   while the test stayed green.
2. **The fix (2026-09-24):** both were switched to `IS NOT TRUE`, and
   `tests/test_cover_verdict_repair.py` was rewritten to **require** it:

   > "the unflagged-row predicate must use boolean syntax (`IS NOT TRUE`);
   > **the integer form fails on PostgreSQL**"

   That is true *only of the BOOLEAN sibling*. Applied to `is_cover` it is a
   type error of the opposite sign — and the new assertion locked it in, so CI
   was green while the statement stopped working again.

⭐ **The lesson: a type rule belongs to the COLUMN, not to the statement.** Two
columns in one `WHERE` clause can need opposite spellings; asserting one syntax
for both guarantees one of them is wrong.

## Fix

```sql
WHERE is_cover_reason = :damaged_reason
  AND COALESCE(is_cover, 0) = 0              -- BIGINT: the integer form
  AND cover_manual_override IS NOT TRUE      -- BOOLEAN: the boolean form
```

`COALESCE(is_cover, 0) = 0` is the form the codebase already documents as legal
for this column (the guard's own docstring calls it "fine"), and it also
selects `NULL` rows — which `is_cover = 0` would not.

`tests/test_cover_verdict_repair.py` now asserts **each column with the syntax
its own declared type requires**, including a negative assertion that
`is_cover IS NOT TRUE` is absent.

## The guard learned the mirror rule

`tests/test_no_boolean_int_sql_mixups.py` already existed to stop
`COALESCE(<boolean>, 0)` — but it only knew which columns are BOOLEAN, so it
was blind to the inverse mistake. Added:

* `_column_types()` — `{column: declared type}` read from `db/schema.py` (the
  DDL registry, which is what the live column actually is; `models.py` is
  deliberately not consulted, because "declared `Boolean` in the ORM but
  `BIGINT` in the registry" is how this defect arose);
* `_IS_TRUE` — `<col> IS [NOT] TRUE`, **deliberately case-sensitive** so Python
  prose (`if x is True:`) and docstrings cannot match;
* a third enforced rule: **`IS [NOT] TRUE` on a column that is not declared
  BOOLEAN**, reported as `IS TRUE on a non-boolean column`;
* four tests: the guard discovers `is_cover` as BIGINT / `cover_manual_override`
  as BOOLEAN, the reported shape is detected (while the BOOLEAN sibling stays
  unflagged), the fix passes, and Python prose is not flagged.

> ⚠️ The pre-filter list had to grow too (`IS TRUE`, `NOT TRUE`). The comment
> above it already warns it must list EVERY construct the scan looks for — the
> earlier omission of ` = 0` was how `boolean_col = 0` escaped for weeks.

## Verification

* `tests/test_no_boolean_int_sql_mixups.py` + `tests/test_cover_verdict_repair.py`
  + `tests/test_cover_verdict_cleared_only_after_deep_detection.py` → **43 passed**.
* **Oracle** — reverting ONLY the production SQL → **2 failed**:
  `test_no_boolean_column_is_combined_with_an_integer` (the guard reports it)
  and `test_it_only_touches_rows_that_are_currently_unflagged` (the contract
  test). Restored → 24 passed. **The extended guard would have caught the
  original defect.**
* **Sweep** — 60 cover/verdict/bool/sql/scan/metadata/album files, clean
  `origin/develop` vs this change: **base `64 failed / 1740 passed`** vs
  **new `64 failed / 1744 passed`**, `Compare-Object` on the sorted `^FAILED`
  lines = **empty both ways** → 0 regressions, +4 tests.

## Not reachable from CI

The suite runs against SQLite, which is loosely typed and accepts *both*
`COALESCE(boolean, 0)` and `bigint IS NOT TRUE`. Neither form can fail a test
run, which is precisely why this class of defect keeps reaching production.
The source-level guard is the only thing that can catch it without a
PostgreSQL-backed test job.
