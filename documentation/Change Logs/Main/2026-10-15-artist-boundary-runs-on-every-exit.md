# The artist boundary now runs on every exit from the album loop

**Date:** 2026-10-15
**Files:** `services/popularity/scan_stage_runner.py`

## The follow-up

> There's still no try/finally around the album loop, so an exception escaping
> an album skips the boundary flush — phase 1 + the heal already recover that
> on the next scan (and deferred album renames still need the boundary).

Verified before changing anything, and confirmed on all three parts:

**1. No `try/finally` around the loop.** Each album sits in its own `try`
islands — a *prep* island that logs and `continue`s, and a *main* island that
logs and continues — so a normal album failure is absorbed. What had no
protection is a statement at **loop level, outside both islands**. There are
several, and they are realistic rather than theoretical:

* `update(stage="album", ...)` — a progress write, i.e. a DB round-trip;
* `log_unified(...)` — structlog has raised `UnicodeEncodeError` in this
  file's history (emoji in a scan log on a cp1252 console);
* `_load_mb_single_titles` / `_load_discogs_single_titles` /
  `_load_discogs_promo_titles` — DB-backed, called unguarded per album;
* `was_album_scanned(...)` — the skip gate's freshness query;
* a `KeyboardInterrupt`, which no `except Exception` can see.

Only the two `return False` paths — stop, and the abandoned-artist cancel —
closed the boundary for themselves.

**2. Ratings recover.** Phase 1 already persists stars per album, and
`_album_needs_star_reconcile` force-runs a provisional album on the next scan.

**3. Deferred album renames do NOT recover.** They live in `run_scan`'s
in-memory `_deferred_album_renames` and are `.pop()`ed at the boundary, so a
skipped boundary dropped them until the album was resolved by hand or by a
forced scan. That is the real cost, and it is silent.

## The change

`try/finally` around the album loop (through the zero-track summary, so the
log order on the normal path is unchanged), with the boundary close moved into
the `finally`:

```python
finally:
    try:
        _close_artist_section(_section_artist)
    except Exception as exc:
        logger.warning("Artist boundary unwind failed", artist=..., error=str(exc))
```

Three properties, each deliberate:

* **`finally`, not `except`.** It runs for `BaseException` too — a Ctrl-C
  during a scan is exactly when queued renames must not be lost.
* **Guarded.** An exception raised in a `finally` *replaces* the one in flight,
  so an unwinding failure would turn a diagnosable crash into a confusing one.
* **The in-loop stop/cancel closes stay.** They unwind *before* `finish()`
  reports the scan as stopped, and the `finally`'s second call is a documented
  no-op (the flush and the rename batch both `.pop()` their bucket; the
  essential section is keyed by a `done` set).

## The 1395-line diff is whitespace

Python has no way to attach `finally` without indenting the block it covers, so
the loop body (lines 2201–3595) moved +4. Applied by script, not by hand:
`git diff -w --stat` reports **37 insertions, 0 deletions** — the whole change
is additive, and `ast.parse` was run on the result *before* it was written, so
a malformed re-indent could never reach the file. The script also asserts there
are no triple-quoted strings in the region (indenting one changes its *value*,
not just its appearance) and leaves blank lines alone so no trailing whitespace
is introduced — the file still has exactly the 4 trailing-space lines it had
before. `git blame` on that region will attribute it to this commit; `git blame
-w` shows the original authors.

## Tests

**`tests/test_scan_boundary_unwinds.py` — 11 tests.**

* **Behavioural (the important one):** drives a *real* `run_scan` through a
  patched collaborator set, raises from the loop-level `update(stage="album")`,
  and asserts the boundary still ran. `BaseException`, not `Exception`, so the
  sentinel cannot be absorbed by the very islands this change works around. The
  oracle is the essential-playlist sync — the last thing
  `_close_artist_section` does, and only it calls it.
* **Controls:** the exception still propagates (a `finally` must not swallow
  the fault), and an escape *before* the loop records nothing (the fix did not
  turn the boundary into an unconditional sync).
* **Structure, via `ast`:** the album loop is inside a `Try` with a
  `finalbody` that calls `_close_artist_section`; that call is itself guarded;
  and the old function-level close is gone.
* **Idempotency contracts:** the three parts of the boundary are re-entrant.

Oracle (source reverted, tests kept): **5 failed / 6 passed** — the six are the
controls. `import app` clean on both sides (407 routes).

Sweeps: 27 scan/star suites, all green except the three that were already red
before this change and are red on the unmodified baseline too —
`test_scan_stall_fixes` 7F, `test_popularity_marking` 2F,
`test_old_album_windows` 1F (ID-level comparison with the source stashed —
identical). Plus `test_no_python_syntax_errors` and `import app`.

## ⚠️ The virtual workspace copy of this file is stale

`scan_stage_runner.py` is 3665 lines and this change re-indents 1395 of them,
so it cannot be replayed into the `vscode-vfs` workspace the way ordinary edits
are. The workspace still holds the pre-fix file, and **an auto-commit from
there would revert this change on `develop`**. Refresh it before committing
anything from the workspace:

```
git checkout origin/develop -- services/popularity/scan_stage_runner.py
```
