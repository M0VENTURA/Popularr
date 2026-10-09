# One release, one URL - the length notice outlives Save Metadata

**Date:** 2026-10-09 - **Area:** album page
**Reported:**

> Albums with the same release group ID but have the wrong years are being
> split by year. I want them all to appear on the one release, but if the year
> is wrong I want to have an option to fix them on the album page.

> Also, when an album shows something like "incorrect track length" it shows
> until the metadata is saved. I want that to stay there always until the issue
> is resolved as a save doesn't fix the length.

Clarified with the user: the two album URLs **differ only in the final year
segment** and show **different track lists**, while the album's own Release
Group ID field **is** filled in; and the length notice is the album page's
**"Length differs"** row, which must **never be dismissible**.

---

## 1 - A year never outranks a release group

### The hole

`album_detail` resolved the page's scope in this order:

    explicit release-group segment -> ONE release group -> YEAR -> several release groups -> no data

The year branch therefore sat **above** "several release groups", so any album
whose rows carried release-group data *and* whose link carried a year was still
sliced by that year - one URL per year, each showing a different slice of the
same release. Exactly the report.

### The rule now

    explicit release-group segment -> ONE release group -> SEVERAL release groups -> year -> no data

* **One** release group -> one page, and the scope is now **recorded**
  (`album_rg_filter`) rather than merely tolerated, so the save redirect and the
  canonicalisation below know which release the page resolved to.
* **Several** release groups -> the one holding the most tracks (unchanged), but
  reached *before* the year can interfere.
* **No** release-group data at all -> the year split, unchanged. That is the only
  case where a year is still the album's identity.

### One release, one URL

A trailing year is a **legacy** address - the dashboard and the unified search
still build them. When the rows carry release-group data the page resolves the
same release whichever year is asked for, so `.../2007` and `.../2011` were two
addresses for one album. A GET with a year segment now **302s onto the release
group**, so one release has one address and a shared link can never look like a
second album. Year URLs on an album with no release-group data are untouched.

**The "option to fix the year" already exists** on that one page: Edit Album ->
**Original Year** (`album_originalyear`, writes `year`) and **Release Year**
(`release_year`), both saved to every track in the album *and* to the file tags
per the album-save fan-out. With the split gone there is a single page to apply
them on.

### Portability fix found on the way

The same route's `ORDER BY` used `track_number::text` and `...::int` -
Postgres-only casts that made **every** album-page test 500 on SQLite, so
`tests/test_album_year_disambiguation.py` had been failing since the ordering
work landed. Now `CAST(... AS TEXT)` / `CAST(... AS INTEGER)` (identical on
Postgres), with `regexp_replace` registered by the tests that drive the route.

---

## 2 - "Length differs" is a finding, not a staged edit

### Why it vanished

The rows were rendered **only** from a proposal - the "Lookup MBID" preview, or
the recommendations a scan stashed. Both are consumed by **Save Metadata** (and
by **Discard all**), so saving erased the notice while the length itself was
untouched: a metadata save cannot change a file's duration.

### The fix

* `metadata_proposal_service.album_duration_checks(artist, album, release_mbid="")`
  - new read path; resolves the album's stored binding (release, then release
  group), compares, returns `_duration_checks(...)`. **No stash.**
* `GET /api/album/duration-checks` - new route, a **sync** handler so the
  rate-limited comparison runs in the executor instead of blocking the page.
* `loadDurationChecks()` (both trees) - fetched on every album page load,
  **independently** of `loadPendingRecommendations`.
* `clearAll()` - no longer removes `.mb-duration-row`; this is what Save and
  Discard run through.
* `renderDurationChecks()` - now **replaces** its own rows, so the independent
  fetch can never double them.
* `loadPendingRecommendations()` - no longer renders them (the stash is not
  their source).

**Never dismissible, by construction:** there is no dismiss affordance, and the
rows change only when a *fresh* comparison reports the lengths agree - i.e.
after a re-download/re-tag puts the right file in place. A **failed** comparison
answers `success: False` and clears nothing: MusicBrainz being unreachable is
not evidence that the lengths now agree, and silence must never read as
"resolved".

---

## Tests

`tests/test_album_release_group_scope.py` - **4 new**
(`TestTheYearURLCannotSliceAReleaseGroup`): a year URL on a bound album 302s
onto its release group and the resulting page carries every year's tracks; two
different year URLs collapse to the *same* canonical location; **control** - an
album with no release-group data keeps its year split; and the branch order is
asserted as source (the ordering *is* the fix). Plus an autouse fixture
registering SQLite's `regexp_replace`.

`tests/test_album_duration_checks_persist.py` - **10 new**: findings come back
with nothing stashed; a failed comparison never claims "resolved"; an unbound
album says nothing rather than failing; the route is registered, validates its
names and answers 200 for an unbound album; and (both JS trees) `clearAll`
leaves the notices alone, the stash loader does not own them, they are fetched
on every page load, and rendering replaces rather than appends.

**Oracle** - the five source files reverted: **14 failed, 9 passed** (every new
test); restored: **23 passed**.

**Sweep** - 59 test files touching `ui_routes` / `album_routes` /
`metadata_proposal_service` / `metadata-review` / `album_detail`: baseline **21**
failing -> changed **7**, and all 14 differing entries are the new tests
themselves => **0 regressions** (the 7 are pre-existing and identical both sides).

`import app` -> 393 routes (+1), `node --check` on both JS files clean,
`ast.parse` clean on all three Python files.

## Known follow-up

`tests/test_album_year_disambiguation.py::test_route_defaults_to_most_recent_year`
still fails: it asserts a **year selector** that no longer exists in the album
template (`all_album_years` is passed to the template and read by nothing). The
behaviour it guards - default to the newest year when no year is given - passes.
For an album with several years and **no** release-group data there is now no UI
to reach the older editions at all; a small edition picker would close that.

## Files

- `routes/ui_routes.py`
- `routes/album_routes.py`
- `services/metadata/metadata_proposal_service.py`
- `static/js/metadata-review.js`
- `test_site/static/js/services/metadata-review.js`
- `tests/test_album_release_group_scope.py`
- `tests/test_album_duration_checks_persist.py` (new)
