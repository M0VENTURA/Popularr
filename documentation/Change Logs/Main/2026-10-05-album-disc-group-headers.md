# Disc group headers on the album page (2026-10-05)

**Request:** group a multi-disc album under "Disc N" rows (only when the album
actually has more than one disc).

## What was already there

`routes/ui_routes.py` built `tracks_by_disc` and passed it to the template —
**and no template used it**. The grouping logic existed and nothing rendered it.
Worse, it normalised discs a *second* time, inline:

```python
disc_number = safe_int(track.get("disc_number"))
if not disc_number or disc_number < 1:
    disc_number = 1
```

while `helpers/track_ordering.py` did the same thing again for ordering. Two
definitions of "what disc is this" — which is exactly how a header ends up
saying `Disc 2` above rows sorted as `Disc 1`.

## Changes

- **`helpers/track_ordering.disc_sort_value(track)`** — one definition, used by
  *both* `album_track_sort_key` and the route's grouping. It preserves the
  reported behaviour rather than changing it: a bogus `disc_number` of 0 folds
  into disc 1 (the "disc 1 and disc 0" split on single-disc releases, pinned by
  `test_disc_zero_normalised_to_one`). *Decision recorded: keep the merge; this
  change only adds headers.*
- The route now sets `track["disc_group"]` from that helper and computes
  `show_disc_headers = len(tracks_by_disc) > 1`, so a single-disc album never
  grows a header saying "Disc 1" above its only row.
- Both templates (`templates/pages/album_detail.html` and
  `test_site/templates/Pages/album_detail.html`) render a `disc-group-row`
  `<tr>` with `colspan="5"` (the table's column count), guarded by
  `show_disc_headers`.

### Why the template looks the way it does

```jinja
{% set disc_changed = loop.changed(track.disc_group) %}
{% if show_disc_headers and (loop.first or disc_changed) %}
```

`loop.changed` remembers the **previous row's** value, so it has to be
*evaluated* on every row. Written as `loop.first or loop.changed(x)` it is
short-circuited on row 1, never records it, and row 2 then looks like a new
group — putting a header above **every** row instead of every group. Verified
against the installed Jinja (3.1.6): short-circuit renders `[1][1][2][3]`, the
`set` form renders `[1][2][3]`.

### Safe for the album scripts

The header carries **no `data-track-id`**, and every row lookup in
`album_detail.js` / `album.js` keys on that attribute — `_placeMissingRow`,
select-all, and the duplicate badges therefore skip it for free. Checked: no
script walks `tbody tr` unconditionally.

## Tests

`tests/test_album_disc_group_headers.py` — **35 tests**:

- `disc_sort_value` across padded/slash/blank/zero/absent inputs, and
  **grouping never disagrees with ordering** (same function, same number);
- the route uses the helper (scoped check: the grouping block contains no
  inline `safe_int`, since the route still uses it elsewhere legitimately);
- the `> 1` threshold and that it reaches the template;
- per template: guarded, `loop.changed` assigned *before* it is tested, no
  `data-track-id`, `colspan="5"`, and the two trees' markup byte-identical;
- the pattern itself renders `[1][2][3]`, plus the known-bad short-circuit
  shape `[1][1][2][3]` so the assertion stays honest.

Oracle: stashing all four source files → collection error (the module-level
import of `disc_sort_value` fails), all four markers restored.
