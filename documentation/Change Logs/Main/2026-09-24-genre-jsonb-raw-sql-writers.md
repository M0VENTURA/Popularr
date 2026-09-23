# Raw-SQL genre writers still handed JSONB a CSV string — completing the fix

**Date:** 2026-09-24
**Area:** `db/repositories/metadata.py`, `db/repositories/popularity_repository.py`,
`routes/misc_routes.py`, `services/metadata/album_service.py`

## Why this exists — an earlier fix was **overclaimed**

`0a66dfa9` added JSON coercion to
`db/repositories/popularity_repository.coerce_track_value_for_pg_type` and its
message claimed it "fixes every writer at once". **That was wrong.**

That coercion only runs inside `save_to_db`, i.e. on the
`insert_or_update_track` path. The sites below do their own hand-written
`UPDATE`, so they **bypass it entirely**:

| Site | Writer |
|---|---|
| `db/repositories/metadata.py::update_track_genres` | the album-genres box |
| `routes/misc_routes.py` | genre add / remove / apply-to-artist |
| `routes/misc_routes.py::api_track_tags` | the per-track tag merge |
| `services/metadata/album_service.py` | bulk tag write |

All four pass the same comma-separated string to **both** columns, and
`manual_genres` is JSONB. So the reported symptom ("Save Metadata does not
update the files and database") was still reproducible via the album-genres box
even after `0a66dfa9`.

⚠️ **The read side was broken too.** psycopg returns a JSONB column as a Python
**list**, not a string, so:

- `raw.replace("\\", ",").split(",")` → `AttributeError: 'list' object has no
  attribute 'replace'`
- `str(row.get("manual_genres"))` → the Python repr `"['a', 'b']"` — braces,
  quotes, commas and all — which was then split into junk genre names and
  written **back** into the column.

## Fix

Two shared helpers in `popularity_repository`, now public because raw-SQL
callers must use them:

- **`coerce_json_value(value)`** — the ONE place a value becomes JSON text.
  `None → None`; `''` → `'[]'`; list/dict → JSON; a JSON literal → normalised;
  any other string → split then serialised.
- **`parse_genre_value(value)`** — accepts a list/tuple, a JSON literal, a CSV
  string, and the legacy backslash-separated form; always returns `list[str]`.
  Handles the nested case (a list containing a CSV string) and never returns
  empty entries.

Call sites updated:

- `update_track_genres` coerces the JSONB column at the call site.
- The three `misc_routes` raw UPDATEs coerce, and their `_strip_genres` /
  `_merge_genres` / `_merge` helpers go through `parse_genre_value`, so a JSONB
  list no longer raises or round-trips a repr.
- `album_service` drops `str(row.get("manual_genres"))` and its local
  `_split_genres` in favour of `parse_genre_value`, and coerces on write.

⚠️ `coerce_json_value` always returns **JSON text**, never a Python list:
`services/scanning/payload_builder.py` already writes these columns as
`json.dumps([...])` and that path works, whereas a list is adapted by the driver
to a Postgres ARRAY literal, which a `jsonb` column rejects. `_coerce_json_value`
is kept as an alias for the old private name.

## Tests

`tests/test_genre_jsonb_write_contract.py` (new, 8):

- `coerce_json_value` ↔ `coerce_track_value_for_pg_type` must agree — one
  splitter, not two.
- `parse_genre_value` accepts list / CSV / JSON / backslash / `None` / `''`.
- A **source-contract sweep over the three raw-write files**: every
  `UPDATE tracks` that mentions `manual_genres` must coerce it, so a future
  writer cannot quietly bypass the helper again.
- A guard that `album_service` never stringifies the JSONB column.

## Verification

| Check | Result |
|---|---|
| New + existing suites (3) | **51 passed** |
| Regression sweep, 20 suites | pre-existing failing set **IDENTICAL** (37 → 37); owned suites 8 → 0 |
| `from app import app` | imports cleanly |

⚠️ `import routes.misc_routes` **directly** raises a circular import between
`db.repositories.tag_repository` and `services.metadata.tag_file_service`. That
is **pre-existing** — verified by stashing these changes and reproducing it
identically — and does not affect the real entrypoint, which imports fine. It is
a latent landmine worth a separate fix.

## Lesson

⚠️ **"Fixes every writer at once" is a claim about a CALL GRAPH, not about a
function.** The coercion was correct and unit-tested, and the tests passed
because they exercised the coerced path. A fix placed on one path protects that
path only; when the same data is written from several places, the fix belongs in
a helper **every** writer calls — and the test must assert that they *do* call
it, which is why the contract test greps the raw SQL rather than just testing
the helper.
