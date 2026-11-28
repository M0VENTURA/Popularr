# The downloads trio is one page

Date: 2026-11-28
Scope: `routes/ui_routes.py`, `routes/preview_routes.py`,
`test_site/templates/Pages/downloads/monitor.html`,
`test_site/static/js/pages/monitor.js`, six test files, and two deleted templates.

Rebuild item from `documentation/web-ui-audit.md` §5.3 / Phase 4.

---

## Before

Three URLs served the same page with a tab removed each time:

| URL | Template | Tabs |
|---|---|---|
| `/downloads/monitor` | `downloads/monitor.html` | Folders · Soulseek · Playlists |
| `/downloads` | `downloads/queue.html` | Soulseek · Playlists · Active Queue |
| `/downloads/search` | `downloads/search.html` | Soulseek · Playlists |

Only the largest was in the navigation. The other two each carried their own
copy of the nine-script block, and they had already drifted: `queue.html` was
the only page that rendered the "Discarded downloads" section, `search.html`
was the only one that loaded `pages/search.js` (so `?q=` auto-search worked
there and nowhere else), and `/downloads` had a dead `jumpToUpcomingReleases()`
button that only `monitor.html` defined.

## After

One page — `/downloads/monitor` — and two redirects that keep the fragment and
the query string:

```
/downloads                    → 302  /downloads/monitor
/downloads/search?q=…         → 302  /downloads/monitor?q=…#soulseek
/downloads/search/soulseek    → 302  /downloads/monitor#soulseek
/downloads/search/playlists   → 302  /downloads/monitor#playlists
/downloads/search/musicbrainz → 302  /downloads/monitor#soulseek
```

`_monitor_url()` builds them; `request.query_string` is forwarded because every
"Search Soulseek" link in the app is `/downloads/search?q=…` and the panel
reads `q` on load. Dropping it would land the user on an empty search box —
the exact bug `pages/search.js` documents having fixed once already.

### Bootstrap pills ignore the hash

A redirect alone is not enough: the tab strip is `data-bs-toggle="pill"`, so
`#soulseek` opens nothing. `monitor.js` gained `activateTabFromHash()`, run on
`DOMContentLoaded` and on `hashchange`. `#unmatchedFoldersSection` is untouched
by it — that lives in the default tab and `honorFolderAnchor()` already handles
the scroll once the card exists.

### What moved onto the one page

| Was only on | Now on monitor |
|---|---|
| `queue.html` — `_discarded_downloads.html` + `discarded-downloads.js` | included above the tab strip, so it shows on every tab |
| `search.html` — `pages/search.js` | loaded last, after `csv-import.js` (`_trackPayloads`) and `soulseek-search.js` (`downloadsPage`) |

`pages/search.js` also fixes a live gap: the monitor already included
`components/search/_playlists.html`, but nothing bound its
`.js-add-track-btn` / `.js-slskd-search-btn` / `.js-replace-btn` rows, so those
buttons were inert there. Only `search.js` binds those selectors (verified by
searching `static/js`), so there is no double-handling.

Nothing was lost by dropping the dedicated "Active Queue" tab: `monitor.html`'s
Folders tab already renders `_queue_status.html`. It deliberately hides the
completed-queue card, because "Matched & Unmatched Folders" is the single place
finished folders are handled and showing both listed every one twice.

### Deleted

* `test_site/templates/Pages/downloads/queue.html`
* `test_site/templates/Pages/downloads/search.html`
* the `queue` entry in `PREVIEWABLE` (it pointed at a redirect)

---

## Tests updated

The templates were read directly by six suites, so the repoints are part of the
change rather than an afterthought:

| File | Change |
|---|---|
| `test_discarded_downloads_are_reported.py` | both `TREES` entries → `monitor.html` |
| `test_downloads_inline_handlers_resolve.py` | the jump-button param → `monitor.html` (renamed `TestTheDownloadsJumpButton`) |
| `test_item_groups_bindactions_this.py` | the prerequisites test → `monitor.html` |
| `test_manual_search_button_works_in_test_site.py` | `QUEUE_PAGE` → `monitor.html` |
| `test_mb_picker_ownership.py` | the picker-order loop iterates `(MONITOR,)` |
| `test_rebuilt_pages_have_no_dead_buttons.py` | the three `queue.html` register entries deleted (monitor's own entries cover the same `_track_edit.html` handlers) |
| `test_served_artist_album_track_handlers.py` | `queue.html` removed from the parametrize list |

Every assertion that was repointed was checked against `monitor.html` while
doing it: it includes `_soulseek_manual_search.html`, loads `services/slskd.js`,
has no `onclick="jumpToUpcomingReleases()"`, and links
`/downloads/discover/upcoming`.

### New guard

`tests/test_downloads_pages_are_one.py` pins the whole shape:

* the two subset templates are gone;
* all five routes still exist and `redirect(_monitor_url(...))` — never a second
  `render_template`;
* `_monitor_url` forwards `request.query_string`;
* every emitted fragment names a `data-bs-target` the page actually has (a typo
  would silently open the default tab);
* `monitor.js` defines **and calls** the hash activator;
* the one page still carries the queue panel, the discards section + script, the
  Soulseek panel + controller, the playlist panel + its controllers, and
  `pages/search.js`;
* the discards section renders above `searchTabs`.

---

## Verification

* LSP diagnostics clean on every touched Python/JS file.
* **The suite was not run** — no `python` on PATH and no Docker here. Before
  merging:
  `pytest tests/test_downloads_pages_are_one.py tests/test_discarded_downloads_are_reported.py tests/test_mb_picker_ownership.py tests/test_item_groups_bindactions_this.py tests/test_downloads_inline_handlers_resolve.py tests/test_rebuilt_pages_have_no_dead_buttons.py tests/test_manual_search_button_works_in_test_site.py`

## Still open

* `/metadata-compare` — orphan page, unbounded `GROUP BY`, mislabelled "Beets"
  column. The audit offers "fold into `/correcting` or rebuild as a paginated
  report"; both are larger than this change and neither is a pure deletion.
* `/search` is still a stub that opens the flyout.
* `artist_list`'s ~1000 unpaginated rows; the dashboard's five 5-second polls.
* Splitting `album.js` (4 011 lines), `config.js` (3 733), `playlist.js` (2 318)
  and `download-queue.js` (2 089).
* Phase 1, the real bundle.
