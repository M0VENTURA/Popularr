# Album metadata save wrote nothing — JSONB columns rejected the genre writes

**Date:** 2026-09-24
**Area:** `db/repositories/popularity_repository.py`, `routes/ui_routes.py`

## Reported

> "After doing lookup MBID on the album page, all the changes get shown on the
> UI. But after selecting Save Metadata, the changes don't update on the files
> and database correctly"

## Root cause — a JSONB column refusing a CSV string, with the error swallowed

`tracks.manual_genres` is declared **JSONB**, but `update_track_genres`
(`db/repositories/metadata.py`) writes a comma-separated **string**:

```python
UPDATE tracks SET genres = :genres, manual_genres = :genres
```

`"Hardcore, Punk"` is not valid JSON, so PostgreSQL rejects the whole statement
with `invalid input syntax for type json`.

⚠️ **The persistence layer had NO JSON branch at all.** Every value that was not
boolean/int/float fell through `coerce_track_value_for_pg_type` unchanged, so
nothing ever converted the CSV form. The write failed, the caller logged it at
**DEBUG** and moved on, and the page then flashed **"No changes were made."** —
telling the user their edit was a no-op when the database had actually refused
it.

### Why it worked before and broke now

⚠️ **This is a regression introduced by `292e4996`** (the JSONB drift repair,
earlier in this same session). Those genre columns were TEXT *because of schema
drift* — which is exactly why the CSV round-trip appeared to work. Converging
them to their declared JSONB made the writes start failing. The drift repair
fixed the schema and exposed a latent writer bug that had been masked by the
drift itself.

**My own commit is the cause**, and the verification I did at the time (24 tests,
4 mutations, a 248-test sweep) could not catch it because the SWEEP only checked
that the failing set did not grow — and these writes fail **silently**, at DEBUG,
with the UI reporting success. Nothing failed. Nothing had ever failed.

## Fixes

### 1. Coerce JSON/JSONB values (`db/repositories/popularity_repository.py`)

New `PG_JSON_TYPES` branch plus `_coerce_json_value`:

| Input | Output |
|---|---|
| `None` | `None` |
| list / dict | `json.dumps(...)` |
| `'["a","b"]'` | normalised, re-serialised |
| `''` / whitespace | `'[]'` (never NULL — matches schema.py's `'' -> '[]'::jsonb`) |
| `"Hardcore, Punk"` | `'["Hardcore", "Punk"]'` |

⚠️ **The return type is always `str` (JSON text), never a Python list.** This is
load-bearing: `services/scanning/payload_builder.py` already writes these columns
as `json.dumps([...])` strings and that path demonstrably works. A Python list
would be adapted by the driver to a Postgres ARRAY literal, which a `jsonb`
column rejects (`column is of type jsonb but expression is of type text[]`) —
so returning a list would have fixed the album page and **broken the scanner**.
Pinned by a test.

Fixing it here rather than at the one call site repairs every writer at once
(`routes/misc_routes.py`, `services/metadata/album_service.py`, the scan
payloads).

### 2. A staged disc number wins over the single-disc strip (`routes/ui_routes.py`)

The album-level "single disc ⇒ clear each track's disc_number" heuristic ran
**after** the staged review had been applied and unconditionally overwrote it —
in the DB, and again on the file via a hard-coded `_file_tags["disc_number"] = ""`.

That strip is an inference about the library's existing tags; a staged value is
the user having just confirmed the correct position against MusicBrainz.
`disc_number` is in `_STAGED_WRITABLE`, so honouring it is what that whitelist
already promises. Both sites are now gated on `_disc_staged`.

### 3. A refused write is REPORTED, not presented as "no changes"

- `db_failures` / `genre_write_failures` counters; the DB failure is logged at
  **WARNING** with the track id, not DEBUG.
- New flash: *"⚠️ N track(s) could NOT be saved to the database."* (danger).
- The zero-counters branch now also requires `db_failures == 0` and
  `genre_write_failures == 0`, so a **failed** save can never fall through to
  "No changes were made."

## Verification

| Check | Result |
|---|---|
| `tests/test_album_review_save_persists.py` (new, 27) | **27 passed** |
| Oracle (source fix stashed) | **8 failed / 19 passed** |
| Regression sweep, 19 suites | differing set = **one entry, `[<=]` (baseline-only)** — my new test correctly failing without the fix; **0 regressions** |

The distinguishing entry in the sweep is the new
`test_the_zero_condition_consults_every_failure_counter`, which fails on the
baseline by design (the counter did not exist yet).

⚠️ `tests/test_album_save_reports_changes.py` anchored on the literal one-line
text `"if updated_count == 0 and reverted_live_count == 0"`. The condition
legitimately gained a line per counter, so the anchor broke **without any
behaviour changing**. It now anchors on the comparison alone, with a context
window, and gained a test asserting the condition consults *every* failure
counter — the property the branch order depends on.

## Notes

- ⚠️ Reproducing this required care: the first harness used `client.post(data=…)`
  instead of `form=…`, so `request.form` came back empty and there was no way to
  tell "the handler dropped the payload" from "the payload never arrived".
  `helpers.app_hooks.needs_setup()` also redirects every request to `/setup`
  while Navidrome is unconfigured. Both are documented in the test module.
- A staged **genre** correctly reaches the file as the `musicbrainz_genres`
  frame; it is not expected to populate the separate `genres` frame, which the
  album-genres box owns via `update_track_genres`.
