# Album page: tracks appear in listening order (2026-10-05)

**Report:**

> The ordering on the album page is sometimes off. For instance Track 13 is
> showing before track 1.

## Root cause

`tracks.disc_number` and `tracks.track_number` are **TEXT**, and there were
three different orderings in the code, each wrong in its own way.

The album page (`routes/ui_routes.py::album_detail`) ordered with:

```sql
ORDER BY
    COALESCE(disc_number, '1'),
    NULLIF(regexp_replace(COALESCE(track_number::text, ''), '[^0-9].*$', ''), '')::int NULLS LAST,
    track_number,
    title
```

`COALESCE` replaces **NULL only** — a blank disc tag stays `''`, and `''`
sorts *before* `'1'` in a text column. So a single track whose `DISC` tag was
empty (the common case for a file where only `TRACKNUMBER` was written) jumped
to the top of the album while every other row sorted perfectly.

That is exactly the reported symptom, and exactly why it was *sometimes*: the
tracks whose tags were complete were fine, and one blank tag was enough to
pull its row above track 1.

The numeric key was correct, but the `track_number` fallback after it was raw
text — `1, 10, 11, …, 2, 3` for any row where the cast produced NULL.

The **artist page** had the mirror-image bug in Python:

```python
key=lambda t: (
    safe_int(t.get("disc_number")) or 1,
    safe_int(t.get("track_number")) or 0,   # unnumbered → 0 → FIRST
    str(t.get("title") or "").lower(),
)
```

`or 0` sends every track with no number to the **front** of its album.

## Changes

New `helpers/track_ordering.py` — one definition both pages share:

- `leading_int(value)` — the integer a tag *starts* with, mirroring the SQL
  `regexp_replace(x, '[^0-9].*$', '')`: `'13'` → 13, `'1/2'` → 1, `'01'` → 1,
  `'A1'`/`''`/`None` → `None`.
- `album_track_sort_key(track)` → `(disc, track, title)`:
  - **disc** numeric, and blank / zero / absent is **disc 1** ← the fix;
  - **track** numeric, and unnumbered tracks sort **after** numbered ones;
  - title breaks ties (duplicate or missing numbers).
- `sort_album_tracks(tracks)` applies it.

Applied in:

- `album_detail` — immediately after the query. The SQL `ORDER BY` stays as a
  pre-sort, but the order the user sees is now decided in Python, where it is
  testable without a database (and where `''` can no longer beat `'1'`).
- the artist page's inline lambda, replaced by the same call — which also
  stops unnumbered tracks from sorting first there.

## Tests

`tests/test_album_track_ordering.py` — 15 tests:

- the reported case, fed in the **wrong** input order so the assertion proves
  the sort moved the row: track 13 with a blank disc lands back at position 13;
- blank, `" "`, `"0"` and `None` discs all behave as disc 1;
- numeric (`1, 2, 3, 10, …`) rather than lexicographic (`1, 10, 11, …, 2`);
- disc 2 after disc 1, discs compared numerically (`1, 2, 10`);
- unnumbered tracks last within their disc — never ahead of numbered ones;
- the `leading_int` shapes real tags carry;
- source pins that `album_detail` applies `sort_album_tracks` and that the
  artist page's `safe_int(...) or 0` key is gone.

Oracle: reverting `routes/ui_routes.py` fails **2** (the wiring pins); breaking
the disc guard in `album_track_sort_key` fails **5** (the blank-disc cases).
