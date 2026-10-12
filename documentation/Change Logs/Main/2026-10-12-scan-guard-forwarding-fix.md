# The shared scan guard no longer forwards `scan_type` to runners that reject it (2026-10-12)

## Report

After the album/artist "full" scan was moved behind the shared guard
(`21f792b4`), the album page's Full scan died on the first click:

```
[POPULARITY] Targeted worker failed: run_album_pipeline() got an unexpected keyword argument 'scan_type'
ERROR routes.scan_routes._common [POPULARITY] Targeted worker failed error="run_album_pipeline() got an unexpected keyword argument 'scan_type'"
```

(Identical for `run_artist_pipeline`; a satellite "Worker starting mode=all …
resume_from=Nervosa" ran right after, so the failure was visible mid-deploy.)

## Root cause — the guard forwarded bookkeeping kwargs to the runner

`guard_and_launch_popularity_scan` blindly forwarded its `**kwargs` to the
runner. The new "full" branches pass `scan_type="full"` for the RUNTIME
REGISTRY label (and failure record), but:

* `run_popularity_artist_scan` / `run_popularity_album_scan` **accept**
  `scan_type` (it is their mode selector) — the targeted branches were fine;
* `run_artist_pipeline` / `run_album_pipeline` **do NOT** take a `scan_type`
  kwarg. The worker then died inside its daemon thread with the TypeError —
  and `run_async`'s signature check could not catch it because it validates
  the WRAPPER (`_worker(*args, **kwargs)`), not the runner.

## Fix — `routes/scan_routes/_common.py`

`guard_and_launch_popularity_scan` now inspects the runner's signature once
and forwards ONLY the kwargs it accepts:

* `scan_type` is resolved once and used only for the registry entry
  (`set_runtime(..., type=_scan_type)`) and the failure `record_scan` — it is
  dropped from the runner's kwargs when the runner lacks it (the pipelines),
  preserved when it has it (the targeted runners).
* Any OTHER unexpected kwarg is dropped with a WARNING (a mismatched
  signature must surface in the log, not kill the worker silently).

This is the exact pattern `assert_callable_accepts_kwargs` documents, applied
to the runner before the thread starts.

## Tests — `test_album_full_scan_concurrency_guard.py` (14 → 17)

Added `TestGuardFiltersKwargsForTheRunner`:

* a pipeline-style runner (artist, album, force-only) is called WITHOUT
  `scan_type` while still starting (THE regression — oracle runs it against
  blind forwarding and it fails with the exact reported TypeError);
* a targeted-style runner (accepts `scan_type`) still receives it;
* the runtime registry still records `type="full"` even though the runner
  did not receive the kwarg.

**Oracle**: restore blind forwarding → `test_pipeline_runner_does_not_receive
_scan_type` FAILS with `...got an unexpected keyword argument 'scan_type'`;
the 2 controls pass. Restored → 17 passed.

**Sweep**: scan/route/async suites (88 passed incl. `test_scan_async_signature_
contract`, `test_finalise_scan_mode`, `test_scan_never_moves_files`,
`test_scan_continuation_guard`) — and a 15-fail widened sweep is **identical
at pristine `f494c049`** → 0 regressions. `import app` OK (399 routes).

## Note for the user

No config change. This fixes the album/artist-page **Full** scan trigger that
`21f792b4` (the concurrency guard) introduced; a rebuild is all it needs.