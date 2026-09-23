# Refused track writes were silent — bulk upsert, bulk tag, and tag application

**Date:** 2026-09-24
**Area:** `db/repositories/popularity_repository.py`,
`services/popularity/scan_stage_runner.py`,
`services/scanning/navidrome_import.py`,
`services/metadata/album_service.py`

## Reported (Postgres log while saving an album)

```
ERROR:  invalid input syntax for type json at character 756
DETAIL:  Token "alternative" is invalid.
CONTEXT:  JSON data, line 1: alternative...
STATEMENT:  INSERT INTO tracks (…, musicbrainz_genres, …)
            VALUES (…, 'alternative rock, britpop, rock', …)
            ON CONFLICT (id) DO UPDATE SET …
```

**Yes — same root cause** as the album-save bug. `musicbrainz_genres` is JSONB
and was handed a CSV string, so PostgreSQL refused the whole statement.

Confirmed it predates the fixes: at `7094cfc5` (the HEAD before them)
`popularity_repository.py` contained **no JSON handling at all**, so the code
that produced this log had no coercion on any path.

⚠️ **Why it surfaced in the ERROR log and nowhere else**: `upsert_tracks_bulk`
caught every row in `except Exception` and logged at **DEBUG**:

```python
logger.debug("Bulk track upsert skipped for %s: %s", payload.get("id"), exc)
```

so a wholly discarded album returned `False` — **which no caller checked** — and
a normal log tail showed nothing. The scan reported success.

## Three separate silence defects

### 1. `upsert_tracks_bulk` — DEBUG, and the reason was hidden

Now logs at **WARNING**, naming the track and carrying the database's own
reason. `False` is still returned, and that value is now **checked** (below).

⚠️ This module uses the **stdlib** logger (`logging.getLogger`), not structlog,
so keyword arguments raise `Logger._log() got an unexpected keyword argument`.
The call uses `%`-style formatting, as the rest of the file does. A structlog
convention copy-pasted here would have crashed inside the error handler.

### 2. Two callers discarded the `False` return

`scan_stage_runner.py` and `navidrome_import.py` both called
`upsert_tracks_bulk(...)` as a statement. A completely refused batch was
therefore indistinguishable from a successful one. Both now inspect the result
and log a WARNING naming the artist/album and the row count.

### 3. `bulk_tag_tracks` returned `success: True` unconditionally

Its per-track `except` logged an ERROR and `continue`d — but the function then
returned

```python
{"success": True, "updated_count": updated_count, …}, 200
```

so the endpoint answered **200** and the UI reported the tags applied while
every row had been skipped. It now:

- counts `failed_count` (including a track row that no longer exists, which
  previously `continue`d silently without counting as anything),
- returns `success: updated_count > 0` and **HTTP 500** when nothing was
  updated, with an explanatory `error`.

## Verification

| Check | Result |
|---|---|
| `tests/test_track_write_failures_are_loud.py` (new, 13) | **13 passed** |
| Oracle (source fix stashed) | **5 failed / 8 passed** |
| Regression sweep, 17 suites | pre-existing failing set **IDENTICAL** (48 → 48); owned suite 5 → 0 |

The reported values now round-trip through the real coercer:

```
'rock, singer-songwriter'        -> ["rock", "singer-songwriter"]
'alternative rock, britpop, rock' -> ["alternative rock", "britpop", "rock"]
'alternative rock, glam rock, rock' -> ["alternative rock", "glam rock", "rock"]
```

## Notes

⚠️ `test_a_failed_row_is_not_logged_only_at_debug` asserts on **comment-stripped**
source. The replacement code explains the old `logger.debug` in a comment, so a
naive substring check matched its own documentation and reported the opposite of
the truth. This is the third time in this session that an assertion has matched
prose instead of code — the helper is now local to the module.

⚠️ **Still silent elsewhere** (not changed here, worth a follow-up): many other
`except Exception` blocks in `misc_routes.py` and `album_service.py` log at
ERROR and continue without surfacing the failure to the caller. This change
covers the track-write paths that produced the report; it is not an audit of
every handler.
