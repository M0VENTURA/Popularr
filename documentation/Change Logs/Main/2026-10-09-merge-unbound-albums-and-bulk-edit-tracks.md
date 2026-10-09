# Merge unbound same-name albums - bulk "Edit Tracks" on the album page

**Date:** 2026-10-09 - **Area:** album page
**Reported:**

> But I also want it to show on similar albums with no release ID but the same
> name with a different year. It would be good to have an option on the multiple
> select to select multiple tracks and select **Edit Tracks** (next to rename
> selected) which would give access to the items that are in the edit album page
> for those tracks to realign to a different album if needed.

Clarified with the user: **merge into one page** (same as the release-group
case), and Edit Tracks should expose the **realign-identity** field set.

---

## 1 - The year stops identifying an album, entirely

Follow-on to `9615991d`, which made a release group beat a year. This closes
the remaining half: albums with **no release ID** were still split by year.

| condition | scope |
|---|---|
| URL segment is a UUID | that release group |
| **one** release group on the rows | that release group |
| **several** release groups sharing a name | the one holding the most tracks |
| **no** release-group data | **every year's tracks - one page** |

A trailing year is now *only* a legacy address. A GET carrying one **302s to
the canonical URL** - the release group when known, otherwise the bare album
URL - so `.../1999` and `.../2015` both land on the same merged page instead of
serving two sliced views. The save redirect drops its year branch for the same
reason: a save lands on the canonical address.

**This is a deliberate contract reversal**, and the tests that pinned the old
contract were rewritten rather than deleted:

* `tests/test_album_year_disambiguation.py` (2026-08-28: *"same-name albums
  released in different years must not be merged"*) - now pins the merge, the
  year-address redirect, and keeps the single-year control. Its module
  docstring records the reversal and the original rule.
* `tests/test_album_release_group_scope.py` - the "year beats several release
  groups" ordering assertion is replaced by `test_the_year_never_scopes_anything`
  (`elif explicit_year` must not exist at all), and the no-release-ID case now
  asserts a merge.

**The "option to fix the year"** is the same Edit Album -> *Original Year* /
*Release Year* pair as before; it now applies to one merged page.

## 2 - Bulk "Edit Tracks" beside "Rename Selected"

The selection toolbar on the album page gained **Edit Tracks** (both trees),
opening a modal with the Edit Album page's **identity** fields:

`Album Title` - `Album Artist` - `Original Year` - `Release Year` -
`Track Number` - `Disc Number` - `Track Title` - `Track Artist` - `Composer`

-> `POST /api/track/bulk-update` (`routes/track_routes.py`)

* **Partial by construction** - only non-blank fields are posted, so an
  untouched box leaves that column alone.
* **Allow-listed** - anything else is `400` with the field named, never
  silently dropped: a field the caller believes it set must never look applied.
* **Only the selected ids** are written (one statement), then the same
  `sync_track_tags_to_file` the single-track endpoint uses, so the rows *and*
  the tags Navidrome reads move together - the album-save fan-out rule.
* One Navidrome scan for the whole batch, not one per track; `file_failures`
  names any track whose file tags could not be written.

Scope deviation, flagged: the button/modal live in `templates/`
(`static/js/album_detail.js`) as well as `test_site/` - live is the default
mode, so a test_site-only change would be invisible. Both are trivially
revertible independently.

`comment` is deliberately **not** offered: `tracks` has no `comment` column
(see the 2026-09-23 note), so accepting it would be a silent no-op.

## Tests

`tests/test_album_bulk_edit_tracks.py` - **12 new**: the route is registered;
ids/fields required; an unsupported field refused with its name; **only the
selected tracks move** (a third track stays put); a blank field leaves that
column alone; all nine identity fields accepted; both templates render the
button beside *Rename Selected* plus the modal and every field id; both scripts
define/export the handlers, post to the endpoint with the **selection** helper,
and keep `sync_to_file: true`.

Rewritten to the new contract: `test_album_year_disambiguation.py` (3) and
`test_album_release_group_scope.py` (4).

**Oracle** - the six source files reverted: **16 failed, 10 passed**; restored:
**26 passed**.

**Sweep** - 60 test files touching `ui_routes` / `track_routes` / `album_detail`
/ `album_routes` / `metadata_proposal_service` / `metadata-review`: baseline
**7** -> changed **7**, `Compare-Object` **(none)** in either direction.

`import app` -> 394 routes (+1), `node --check` clean on both album scripts,
`ast.parse` clean on both route modules.

## Files

- `routes/ui_routes.py`
- `routes/track_routes.py`
- `templates/pages/album_detail.html`
- `test_site/templates/Pages/album_detail.html`
- `static/js/album_detail.js`
- `test_site/static/js/pages/album.js`
- `tests/test_album_bulk_edit_tracks.py` (new)
- `tests/test_album_release_group_scope.py`
- `tests/test_album_year_disambiguation.py`
