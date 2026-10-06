# Albums are scoped by release group, not by year

**Date:** 2026-10-06 · **Area:** album / artist page
**Commit:** `fix(album): scope albums by release group instead of year`

## Reported

> [Battlefield Vietnam … `/2004`] and [Battlefield Vietnam … `/1963`] — the
> same release, but split in years. The same Release MBID is assigned but has
> two different pages to browse to under test_site.

> Ones that do get split should be viewable under the artist page to be able
> to browse to each release properly to edit the information.

The screenshots make it unambiguous: `…/2004` listed **7** tracks (2, 7, 11,
13–17) and `…/1963` listed **16** (1–9, 11–17) — from the same folder
(`/music/Various Artists/2003 - Battlefield Vietnam/…`), both headed
"(2004)", same MBID. One release, cut in two.

## Why year was never a sound identity

A compilation credits its source **recordings**, so the tracks of one release
carry many different years: this album holds tracks tagged 1963 … 2004. The
old rule was `album_key = name + year`, which treats every distinct year as a
separate album — correct for a re-issue, catastrophic for a compilation.

## The rule now

| condition | scope |
|---|---|
| URL segment is a **UUID** | that release group |
| **one** release group on the rows | **no split** — even if the URL carries a year |
| several release groups sharing a name | the one holding the most tracks |
| no release-group data | the old **year** split, unchanged |

Digits still mean a year, so every existing link keeps working. The
single-release case deliberately runs *before* the explicit-year branch: the
dashboard still builds `/…/<year>` links, and with the old ordering those links
kept slicing the release.

## The artist page

`routes/ui_routes.py` grouped albums by `f"{name}::{year}"`. It now:

* pre-computes the distinct release groups **per name** (a first pass — a
  single track cannot know whether its name maps to one release or five);
* keys by `f"{name}::rg:{rg}"` when the name has one release (one card, years
  ignored), by RG when it has several (a card **each**, so every release stays
  browsable and editable — the second half of the report), and by year only
  when there is no release-group data;
* puts `release_group_mbid` on each card, which is what the link needs;
* collects each card's tracks **with the same key** (`tracks_by_key`) instead
  of re-deriving them from name + year — a re-derivation would have filled an
  RG card with the wrong rows.

## Links

`_album_category_section.html` (both trees) gained an `album_scope` variable —
`/<release_group>` when known, `/<year>` otherwise — used by all three album
links (the title, "Open album page", and "Search missing tracks"). The
inline `{% if album.get('album_year') %}/{{ … }}{% endif %}` is gone, so the
release group can never lose to the year by accident.

## Tests

`tests/test_album_release_group_scope.py` — **9 new**: the UUID segment is
parsed; **one release group never splits** (and the branch provably precedes
the explicit-year one — that ordering *is* the fix); several groups default
deterministically; no group data still splits by year; the artist page keys by
RG, cards carry `release_group_mbid`, and tracks are collected with the same
key; both templates scope their links and no longer append a bare year.

**Oracle:** stashing the three source files → **9 failed, 0 passed**; with
them, 9/9.

## Verification

- the new suite: **9 passed**
- **Oracle:** stashing the three source files → **9 failed, 0 passed**; with
  them, 9/9
- affected set (52 files): 15 failed / 1357 passed → **0 new vs baseline**
- full suite: **225 failed, 4681 passed, 2 skipped** vs the 303-failure
  baseline → **79 fixed, 0 real regressions** (the only ID absent from the
  baseline is the known native-flaky `test_sibling_torrents_root_is_searched`)

## Known follow-up

`dashboard.html` (both trees) still links with `al.album_year`. It behaves
correctly now (a single release group ignores the year), but it cannot reach a
specific release when several share a name — the artist page can. Giving the
dashboard `release_group_mbid` is a small, separate change.

## Files

- `routes/ui_routes.py`
- `templates/components/_album_category_section.html`
- `test_site/templates/components/_album_category_section.html`
- `tests/test_album_release_group_scope.py` (new)
