# Disc Number Inconsistency now names the tracks that disagree

**Date:** 2026-10-19 · **Area:** `ui` · `metadata`

## Reported

> "Under disk number inconsistency, can we add the details of the tracks that are
> showing the wrong details in the corrections page?"

## What the section showed before

Per-album **counts** only:

| Album | Total | With Disc # | Without | Disc Val |
|---|---|---|---|---|
| Dactylis Glomerata | 20 | 2 | 18 | 1 |

That says the album is inconsistent but not **which** rows disagree, so finding
the offenders meant opening the album and eyeballing every track — the manual
work the corrections page exists to remove.

## The change

Each flagged album now carries its tracks, split into the two groups that need
**opposite** treatment:

* **`tracks_with`** — rows that HAVE a disc number. On a single-disc album these
  are the odd ones out: they are exactly what *Clear Disc #* removes.
* **`tracks_without`** — rows that already match the majority case. Clearing is a
  no-op for them, so presenting them as offenders would be misleading.

A **Show N track(s)** disclosure under the album expands to both groups, showing
each track's position, title and disc value. The tracks-with group is highlighted
because that is the actionable half.

⚠️ **The split uses the SAME "absent" predicate as the SQL** — `disc_number` is
absent when it is NULL, blank, **or `'0'`** (`DISC_NUMBER_ABSENT_SQL`). A
Python-side check that only treated NULL as absent would file a track whose value
is `'0'` under "has a disc number" and offer to clear a number it does not have,
while the count printed beside it said otherwise. There is a test for exactly
that case.

⚠️ **The detail is ENHANCEMENT, never a new failure mode.** It is a second query
on a page that already worked without it, so a failure there is logged and
swallowed: the counts, the Clear button and the rest of the page all survive, and
the disclosure simply does not render. Tested by making only the detail query
raise.

## One extra query, not N+1

The detail is fetched for **all** flagged albums in a single pass with an
`IN (...)` list, rather than one query per album. The flagged set is normally
small, but per-album round trips on a corrections page is the same N+1 shape
that made the artist page slow before.

## Extracted for testability

The disc analysis moved into `fetch_disc_inconsistent_albums(session, artist,
artist_expr)`.

⚠️ **Why the extraction was necessary, not cosmetic.** The caller,
`get_artist_corrections`, also runs a **PostgreSQL-only**
`STRING_AGG(DISTINCT …, ', ')` query, so the whole function cannot execute
against SQLite at all — which is why the sibling corrections suite only ever
tested the SQL *predicate*, never this section's output. Extracting the block
lets a test run the **shipped SQL and split verbatim** through a session-scoped
helper. Rewriting the query to make it SQLite-friendly in the test would have
stopped testing production.

The extraction is behaviour-preserving: `get_artist_corrections` now calls the
helper and `extends` the same list it used to append to.

## Where the UI lives

`test_site/` only, per the standing preference — `test_site/templates/Pages/artist_corrections.html`.
The live `templates/pages/artist_corrections.html` is untouched.

## Tests

`tests/test_corrections_disc_track_detail.py` (new, **11 passed**), running the
shipped helper against a file-backed SQLite database:

- a flagged album carries `tracks_with` / `tracks_without`;
- the groups **name the right tracks** by title, not merely the right count;
- the groups **agree with the rendered counts**, and together account for every
  track in the album;
- `'0'` and `''` count as absent (the same predicate as the SQL);
- each row reports the literal disc value it will clear, and absent normalises
  to `None` rather than `''`/`'0'`;
- every row is actionable (id + title) and carries its track number;
- `has_disc_number` agrees with which group the row landed in;
- **controls:** a uniformly-untagged album and a genuine multi-disc are both
  still NOT flagged, and only the two inconsistent albums are returned;
- a failing detail query leaves the counts intact and the groups empty.

**Oracle-verified:** with the two detail keys removed again, **8 of the 11 fail**;
all 11 pass with the change.

## Verification

| sweep | result |
|---|---|
| new disc-detail suite | **11 passed** (8 fail with the feature removed) |
| sibling corrections suite + new | **50 passed**, 4 pre-existing teardown errors |

The 4 errors are `sessionmaker.close_all()` in
`test_corrections_disc_and_featured_artists.py::TestTheDuplicateArtistEndpointEndToEnd`
— **`sessionmaker` has no such method**, so those four tests error in teardown on
pristine `develop` too. Confirmed by running the same four files with the change
stashed (identical `39 passed, 4 errors`). Pre-existing, unrelated, and
deliberately **not** copied into the new suite.
