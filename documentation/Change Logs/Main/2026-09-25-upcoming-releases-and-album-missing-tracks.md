# Upcoming releases on the artist page + missing tracks listed on the album page

**Date:** 2026-09-25
**Area:** `routes/ui_routes.py`, `services/catalog/`, `templates/components/`,
`static/js/`, `test_site/*`
**Reported (two requests):**

> 1. I want missing releases on the artist page to also show releases that
>    aren't out yet but listed as upcoming.
> 2. It currently flags missing tracks too, but doesn't show them on the album
>    page to select to download them.

---

## Part 1 — Upcoming releases on the artist page

### Three defects, one report

**1. `is_upcoming` was computed but never READ.**
`routes/ui_routes.py` attached the key to every missing release, but the
component the artist page actually renders (`components/_release_section.html`)
had **zero** references to it. Only the **orphaned**
`_album_category_section.html` reads it, and that file is imported by
`downloads/monitor.html` — not the artist page. A not-yet-released album
therefore rendered as a plain "Missing" row.

**2. The `upcoming_releases` table was never read by the artist page.**
That table is filled by an **independent** pipeline
(`services/upcoming_releases/` — Wikipedia scraper + MusicBrainz fetcher) which
drives the Upcoming Releases page. So an announcement already discovered there
stayed invisible on the artist page until the scan happened to cache the same
release group into `missing_releases`.

**3. ⚠️ `upcoming` was decided from the YEAR, which is wrong most of the time.**
The test was `release_year > datetime.now().year`. On 2026-09-24 a release
dated **2026-12-05** compared `2026 > 2026` = **False** — so every album still
to come *that year* was labelled plain "Missing", which is most of an artist's
forthcoming output at any moment. Found while fixing (1).

### The change

* **NEW `services/catalog/artist_release_entries.py`** — the merge rules as a
  **pure function**. They were previously inline in a ~600-line payload builder,
  reachable only by driving the whole artist page (DB + MusicBrainz-adjacent
  lookups), which is why none of them had a test. Rules:
  1. a release already owned is not missing (seeded from the library);
  2. upcoming is decided from the **date**, with partial MusicBrainz dates
     (`"2027"`, `"2027-03"`) resolving to the **latest** instant they could
     mean, so a March announcement is not treated as already passed;
  3. **upcoming overrides the release TYPE** — an artist's next album is primary
     `Album`, so classifying by type first would leave the Upcoming section
     unreachable for the commonest case of all;
  4. **undated rows are never promoted** (the chosen policy — no date, no
     justification);
  5. **one entry per title across BOTH sources**, with the cached
     `missing_releases` row winning because it carries cover art, a release id
     and a stored category.
* **`services/catalog/release_categories.py`** — new `upcoming` category
  (`UPCOMING_KEY`, icon `bi-calendar-event`, section id
  `upcoming-releases`), `is_upcoming_date()`, `upcoming` **first** in
  `ORDERED_KEYS`, legacy aliases, exports.
* **Both `_release_section.html` templates** — a distinct **Upcoming** badge
  (with the year), a **third** `data-status="upcoming"` value, an Upcoming
  filter radio, and split counts (`missing_only` / `upcoming`).

### ⚠️ Why `upcoming` is a THIRD status, not a second

An unreleased album **is** missing from the collection — but a user filtering
for "Missing" wants something they can go and download **now**. Conflating the
two would point them at an album that does not exist yet. More importantly the
counts are now split, because "N missing" was **advertising downloads that do
not exist**.

### Two JS bugs this would have introduced

* **`isMissing` selected the ENDPOINT.** In both copies of the drift-guarded
  `artist-releases.js`, `isMissing = data-status === 'missing'` decides whether
  the tracklist reads the local `tracks` table or MusicBrainz. With a third
  status, an upcoming album would have been sent to `/api/album/tracklist`,
  which has no rows for it, and the expander would have looked broken. Now
  "is it library?" — `!== 'library'`.
* **`rowIsLibrary` called unreleased albums "library".**
  `artist-album-filter.js::rowIsLibrary` is deliberately asymmetric ("only an
  explicit 'missing' counts as missing") so an UNKNOWN status leans to Library.
  `upcoming` is not unknown, and the album is demonstrably not owned, so it is
  now the one explicit exception.

---

## Part 2 — Missing tracks listed on the album page

### Root cause

`missing_album_tracks` holds the per-album missing set and the SCAN refreshes
it — but the only thing that ever **rendered** it was `_injectMissingTrackRows`,
which runs **exclusively** from the manual "Compare with MusicBrainz" result.
So the artist page advertised "3 missing" on a row, the user opened the album to
download them, and the album page listed nothing to click. The list was in the
database the whole time.

Two further pieces of dead/undone work confirmed the gap:

* `#albumMissingHeaderBadge` already existed in **both** album templates and **no
  script had ever populated it** — the count had nowhere to go.
* `GET /api/album/missing-tracks` already served the persisted list. The album
  page simply never called it.

### The change

Both trees load the persisted list on `DOMContentLoaded`, reusing the **same row
builder** the Compare path uses (`_buildMissingTrackRow` / `buildMissingRow`), so
the per-row controls (queue / match / hide) — and therefore the MBID / duration /
source handling — cannot drift between the two paths.

The new rows are appended AFTER the last real track row, deliberately **not**
via `_injectMissingTrackRows`: that positions each row relative to
`data.comparison`, and these rows are in no comparison. Its `indexOf` returns
`-1`, which would insert before the FIRST row every time — **reversing** the
list.

### A real bug found by the new tests

The loader's `.catch()` was a silent swallow, which **hid** a live throw:

```
TypeError: Cannot read properties of undefined (reading 'trim')
  at _getLinkedReleaseMbid (album_detail.js:451)
```

`_getLinkedReleaseMbid` read `.value.trim()` off an element that may be absent
or value-less, and it was called **inside the row loop** — so a single throw
aborted the whole loop and lost **every** row, leaving the album page looking
complete while showing nothing. The helper is now defensive, and the catch logs
loudly rather than swallowing (the page stays non-fatal: the owned tracklist
still renders).

---

## Verification

| suite | tests |
|---|---|
| NEW `tests/test_artist_upcoming_releases.py` | 42 |
| NEW `tests/test_album_page_missing_tracks_list.py` | 18 |
| `tests/test_release_categories.py` | updated (upcoming leads) |
| `tests/test_release_section_auto_collapse.py` | updated (split counts) |

**The album-page tests run the REAL page scripts in Node** against a DOM stub,
so the assertions are about **rendered rows** rather than source text that looks
right. The stub needed three things a naive one lacks, each of which would have
hidden a genuine failure:

* `innerHTML` must **populate children** — the page builds a row by assigning an
  HTML string and then querying the buttons inside it;
* `className` assignment must feed the same class set as `classList`, because
  rows are built with `row.className = '... mb-missing-row'`;
* `insertAdjacentElement` must insert into the **parent**, or every row lands
  inside the last track row and the tbody looks empty.

⚠️ `setTimeout` is deliberately **not** stubbed. An immediate synchronous version
would run the assertions before the loader's promise settles (every row count
reads 0) and a `try/catch` inside it would **swallow the error** — turning a
genuine failure into a passing empty result. An `unhandledRejection` handler
fails loudly instead, because otherwise a "renders nothing" assertion would PASS
on a broken loader.

**Mutation-verified 5/5** — each guard goes red when its behaviour is broken:
wiring removed, row builder swapped out, endpoint URL changed, badge left hidden,
dedupe removed.

**Oracle** (worktree at `74c37eaa`, changes uncommitted): sweep
`-k "artist or release or categor or upcoming or template or layout or static or
contract or album"` on both trees, failing-id sets diffed:
**zero new failures**. One test disappeared from the failure set
(`test_year_prefixed_album_matches`); it was re-run 3× at baseline and **passes in
isolation**, so it is a known order-dependent flake, **not** a fix from this work.
