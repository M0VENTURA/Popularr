# Album page: missing tracks now line up with the table (2026-10-05)

**Report:**

> The lookup mbid on the album page isn't always populating the missing tracks
> inline with the rest of the album matched by track number order.
>
> It should be with the same fields as the other tracks such as duration. So
> it's lined up with the other tracks on the table.

## Root cause

Two independent defects, in both UI trees (`static/js/album_detail.js` and
`test_site/static/js/pages/album.js`).

### 1. Six cells in a five-column table

The tracks table header is `# | Title | Duration | Rating | Actions` — five
`<th>`. The missing row rendered:

```html
<td></td>                              <!-- #      → empty -->
<td class="fst-italic">12</td>         <!-- Title  → the NUMBER lands here -->
<td colspan="3">Song <badge/></td>    <!-- Duration, Rating, Actions → the TITLE lands here -->
<td class="text-end">buttons</td>      <!-- a SIXTH cell, outside the table -->
```

So the number sat under *Title*, the title sat under *Duration*, and the action
buttons hung past the last column. Duration was never displayed at all —
`mb_duration` rode along only as a raw millisecond `data-duration` attribute on
the queue button.

### 2. Placement ignored track order

- The **Compare / Lookup-MBID** path walked backwards through
  `data.comparison` — MusicBrainz *tracklist* order — looking for the previous
  *matched* entry, and inserted after that entry's DOM row. Tracklist order and
  table order are not the same thing (two-disc releases, a folder whose own
  numbering disagrees, a release whose disc 2 is stored as disc 1), so the row
  frequently landed above or below where it belongs.
- The **page-load** path (`_appendMissingRow` / `appendMissingRow`) appended
  *every* persisted missing row after the **last** track — never inline at all.

Neither path looked at the numbers the table is actually sorted by, and neither
library row published one for the JS to read.

## Changes

- **Both templates** (`templates/pages/album_detail.html`,
  `test_site/templates/Pages/album_detail.html`) — the track `<tr>` now carries
  `data-track-number` and `data-disc-number`, the ordering key the placer reads.

- **Both row builders** — five cells, one per `<th>`:
  `#` (the number) · Title (+ `Missing` badge) · **Duration** · Rating (`—`) ·
  Actions. Duration is rendered through the existing normaliser
  (`_fmtDuration` / `fmtDuration`), which handles MusicBrainz milliseconds and
  library seconds alike, so a missing track reads `5:00` exactly like the rows
  beside it. The raw milliseconds are still passed to the queue button.

- **One shared placer**, `_placeMissingRow` / `placeMissingRow`, keyed on
  `disc × 10000 + track number`:
  - before the first library row whose key is greater;
  - otherwise behind the last library row, ahead of any sub-row hanging off it;
  - ascending order, so rows past the end of the album keep their order too.

  Both entry points now feed it — the Compare/Lookup-MBID result *and* the
  persisted list loaded on page load — so neither can regress independently.

- The old `_appendMissingRow` / `appendMissingRow` remain as thin delegates so
  existing callers keep a home; their "append after the last row" behaviour and
  the `indexOf`-returns-minus-one workaround they documented are gone.

## Tests

`tests/test_album_missing_rows_line_up.py` — 22 tests, both trees driven
through the **real page scripts** in Node against a position-aware DOM stub:

- five cells, no `colspan`, number in cell 1, title in cell 2, `5:00` in the
  Duration cell;
- library `1,3,5` + missing `4,2,9` → rendered `1,2,3,4,5,9`;
- the order key: disc before number, unknown number sorts last, unknown disc
  defaults to disc 1 (matching the template's `or 1`);
- source pins on both templates (ordering keys published, five-column header).

Oracle: reverting the four source files fails **20/22** (the two that survive
are the header column counts, which were already correct).
