# Unmatched paren in `popularity_math` killed the entire artist scan

**Date:** 2026-09-19
**Area:** `services/popularity` / `tests`
**Status:** fixed, guarded by `tests/test_no_python_syntax_errors.py`

---

## Symptom

```
[POPULARITY] FATAL IMPORT ERROR: SyntaxError: unmatched ')'
Artist scan failed: Could not import scanner module 'services.popularity.scan_stage_runner'
```

Every artist scan aborted before the first artist. The MusicBrainz chatter in the
same log is unrelated noise — this scan never got far enough to do real work.

## Root cause

`services/popularity/popularity_math.py:187` had **one extra closing paren**:

```python
return min(1.0, max(0.0, float(effective_median) / float(m_peak))))
#                                                                  ^ 4 closers, 3 needed
```

## How it was introduced

Commit `f324cb53` ("adjusted alrtist page") — the same commit that appended the
new album-standout ratio helpers. Its diff shows the preceding `return` being
rewritten as a *context* line:

```diff
-    return min(1.0, max(0.0, float(effective_median) / float(m_peak)))
+    return min(1.0, max(0.0, float(effective_median) / float(m_peak))))
```

An append that only needed an anchor silently corrupted the line it anchored on.

## Why the failure appeared somewhere else entirely

The typo is in `popularity_math`, but nothing reports that:

```
popularity_math.py  (unparseable)
  └── imported by scan_stage_runner.py
        └── imported by services/popularity/pipeline.py::_load_scanner_module()
              └── raises PopularityPipelineError("Could not import scanner module ...")
```

`pipeline.py` catches every exception from `importlib.import_module` and re-raises
as `PopularityPipelineError`, logging the traceback through `log_unified`. The
traceback is correct, but the top-level message names only
`services.popularity.scan_stage_runner` — three layers from the actual defect.

## Why no test caught it

Nothing in the suite imported `popularity_math` as a prerequisite in a way that
failed loudly, and **a syntax error in a dependency is invisible until something
traverses the import chain**. `get_errors`/Pylance reported no errors for the
file either (it was not the open editor).

## Fix

Remove the extra paren. One character.

Verified in a worktree checked out at the deployed commit (`f324cb53`):

```
ast.parse(popularity_math.py)        -> OK
import services.popularity.popularity_math      -> OK
import services.popularity.scan_stage_runner    -> OK  (run_scan present)
effective_album_ratio(50, 100)       -> 0.5
```

## Guard added

`tests/test_no_python_syntax_errors.py`:

- `test_no_python_file_has_a_syntax_error` — `ast.parse` every `.py` in the repo
  (skipping the frozen `old_system/`), collecting **all** failures rather than
  stopping at the first, because a batch edit can corrupt several files at once.
- `test_the_scan_finds_python_files` — self-check that the walk finds >100 files
  and specifically `popularity_math.py` + `scan_stage_runner.py`, so the guard
  can never silently go vacuous.
- `test_scanner_import_chain_parses[...]` — pins the exact chain whose break took
  the scan down.

Parsing is chosen over importing: no module side effects, no network, no
fixtures, so the WHOLE tree can be covered cheaply and deterministically —
which is the only way to catch a break high in the import graph before a scan
hits it in production.

Proven against the broken tree: **2 failed** (naming
`services/popularity/popularity_math.py:187: unmatched ')'`) → **6 passed** when
fixed.

## Lesson

When the app fails to *start* with an import/syntax error, suspect the most
recent edit to the import chain rather than the module named in the message.
And guard the class of bug (parse everything) rather than the instance.

## Files changed

- `services/popularity/popularity_math.py` — removed the extra paren
- `tests/test_no_python_syntax_errors.py` — new
