# One album, one card: the artist page and the album page now group releases the same way

**Date:** 2026-10-26
**Area:** `ui` (artist page releases), `metadata` (Edit Release modal)
**Files:** `routes/ui_routes.py` (`plan_artist_release_cards`, `_build_artist_detail_payload`),
`routes/album_routes.py` (`/api/album/metadata`),
`templates/components/_release_section.html` + `test_site/…` twin,
`templates/components/modals/_artist_modals.html` + `test_site/…` twin,
`static/js/artist-releases.js` + `test_site/static/js/pages/artist-releases.js`,
`tests/test_artist_page_release_cards.py` (new)

## Reported symptom

> The artist page is sometimes showing releases multiple times, selecting edit
> or opening the album will only show one item will all releases inside it.

## Root cause — two grouping rules for one dataset

**The album page** (`album_detail`) is explicit that a year is not an identity:

* one release group on the rows → **one** page, even when the URL carries a year;
* several release groups sharing a name → the one with the **most tracks**, with
  rows carrying no group folded into it;
* no release-group data → **every year merged**, because "the SAME NAME across
  years is the same album to the person looking at it".

**The artist page** disagreed in exactly the cases that produce duplicates:

1. **No release-group data.** Cards were keyed `name + track_year`. A re-press
   tagged with its pressing year, or a compilation crediting its source
   recordings (`Battlefield Vietnam` holds tracks tagged 1963 … 2004), therefore
   became several cards for an album the album page shows as one.
2. **Several release groups, plus a row with no group.** That row got its own
   year-keyed card — a card no address could ever open, because the album page
   resolves an unscoped URL to the *primary* release.

Both kinds of extra card then linked by `/<year>`, which the album page
**canonicalises away** (a year segment is a legacy address). So every duplicate
resolved to the same release group and opened the **same** page — "one item with
all the releases inside it".

## Fix

### 1. One grouping rule — `plan_artist_release_cards`

A pure, testable function in `routes/ui_routes.py` that maps
`(album name, release group)` → the card key, mirroring the album page:

| case | cards |
|---|---|
| no release-group data | one, keyed by name (the plan is empty; the caller falls back to `::name`) |
| one release group | one, keyed by that group (rows with no group join it) |
| several release groups | one per group; rows with no group join the **primary** (most tracks, first-seen wins a tie) |

The route now looks the card up rather than re-deriving it, so the card and its
tracks cannot diverge. Two comments in the route that described the old
year-keyed behaviour are gone with it.

### 2. Cards link to their own release

`_release_section.html` (the component the live artist page actually renders)
linked Open by `/<year>`. It now computes the same scope
`_album_category_section.html` has always called `album_scope`:

```jinja
{%- set scope = ('/' ~ rgid) if rgid else (('/' ~ year) if year else '') -%}
```

⚠️ The existing `TestTheLinksPreferTheReleaseGroup` only guarded
`_album_category_section.html` — the **legacy** component. The live one was
never checked, which is how the year link survived the original fix.

### 3. Edit addresses the card it was opened from

This was not only cosmetic. The modal prefilled from
`/api/album/metadata?artist=&album=` and **POSTed to `/album/<artist>/<album>`**
— both unscoped. With several release groups under one name the album route
picks the primary one, so **editing card #2 wrote its values onto card #1's
tracks**.

* the row's `data-rgid` is carried into a `#editReleaseRgid` hidden input
  (deliberately **without** a `name`, so the generic `[name]` save sweep does
  not post the address as a field);
* the prefill sends `&release_group=…`;
* the POST URL gains `/<release-group>`.

`/api/album/metadata` applies the scope in Python and falls back to the unscoped
set when nothing matches, so a release whose rows carry no group still fills the
form instead of looking like a missing album.

### 4. A card's year is the album's year

`album_detail` computes `max(year_values)`; the artist page took the **first**
track's year. On a merged card that meant the same album could read 1996 on one
load and 1997 on the next — the same confusion in a different place. It is now
the maximum too.

## Verification

`tests/test_artist_page_release_cards.py` (30 tests):

| Group | Pins |
|---|---|
| The card plan | one name across years → one card; one group across years → one card; several groups → a card each; a group-less row joins the primary; tie → first seen; case-insensitive names; blank names ignored; two names never share a card |
| The route | the plan is the only grouping rule; no year-keyed key anywhere; the `::name` card for group-less albums; the card carries its group; the year is the maximum |
| The links | `scope` prefers the release group; the Open link uses it and the bare-year append is gone; the edit button carries `data-rgid` — **both trees** |
| The Edit modal | the scope holder exists with no `name`; the row seeds it; the prefill is scoped; the POST URL is scoped — **both trees** |
| The endpoint | reads `release_group`; a scoped request returns only that release's values (**behavioural**, seeded SQLite rows with two release groups); an unknown scope still returns the album |

* **Oracle:** with the eight changed source files stashed, **26 of 30** fail.
* **Regression sweep** (`artist`, `release`, `template`, `no_undefined`,
  `no_python_syntax`, `bootstrap`): failure sets **identical** to the stashed
  baseline in every chunk. Pass counts rise by exactly the new tests.
* One assertion in `tests/test_album_release_group_scope.py` was updated because
  the key construction **moved** into the shared function; the intent (a card
  key is a release, never a year) is unchanged and now also covered by a
  negative assertion on the old `name + year` form.

## ⚠️ Unrelated pre-existing failure found while sweeping

`tests/test_static_js_is_not_jinja.py::test_static_js_contains_no_jinja[static\js\artist_detail.js]`
fails at HEAD as well (identical in both sweeps). `static/js/artist_detail.js`
contains **314 Jinja tokens** — it is a pasted copy of the artist page template
(`{% extends "base.html" %}` on line 1). A browser throws a `SyntaxError` on the
first one and discards the whole file, so every function it defines is dead.
Not touched here; it needs its own fix.
