# Upcoming releases: main-genre labels, and an adaptive dashboard poll

Date: 2026-11-28
Scope: `services/catalog/upcoming_genre_labels.py` (new),
`test_site/static/css/upcoming-genres.css` (new),
`migrations/versions/039_upcoming_release_genres.py` (new),
`routes/upcoming_releases_routes.py`,
`services/upcoming_releases/musicbrainz_fetcher_service.py`,
`db/schema.py`, `test_site/static/js/services/upcoming-releases.js`,
`test_site/static/js/utils/poller.js`,
`test_site/static/js/pages/dashboard.js`,
`test_site/templates/Pages/{downloads/upcoming,dashboard}.html`,
two test fixtures, and three new test suites.

---

## 1. Genre labels on upcoming releases

Asked for: *"show the genre it's aligned to by using the main one or two genres
from MusicBrainz. Maybe using a colour coding system for different genre labels
for main genres and sub genres."*

### The classification reuses the existing vocabulary

`services/playlists/main_genres.py` already answers this question for the
playlist builder: it resolves a genre name to its vocabulary ROOT
(`nu metal` → Metal, `grunge` → Rock, `vaporwave` → Electronic) using the
19-root vocabulary in `services/enrichment/genre_vocabulary.json`.

The new `services/catalog/upcoming_genre_labels.py` calls that, and nothing
else. A second taxonomy here would eventually disagree with the playlist names
about what genre an album is, which is the drift the shared vocabulary exists to
prevent.

It adds the two things this page needs and the playlist builder does not:

* **vote ordering** — MusicBrainz carries a `count` per genre (`pop 14`,
  `dance-pop 4`); discarding it makes "whichever sorted first" look like the
  strongest name. The higher count produces the leading chip.
* **main + sub split** — at most two roots for the coloured chips, and the
  specific names (`nu metal`, `alternative rock`) as smaller secondary chips.
  A name that IS a root is not repeated as a sub-genre.

### Where the data comes from

Two paths, because they answer different situations:

| Path | Cost | When |
|---|---|---|
| The match path (`_persist_match_result`) fetches `release-group/<mbid>` with `inc=genres+tags` | one extra MusicBrainz call per MATCH | a row is matched or a candidate confirmed — i.e. rarely, and only for rows that change |
| The discovery upsert | free | when the search already returned genres |

The extra call is deliberate and bounded: a match is an occasional event, and
the alternative is that a matched release shows no label until some later pass.

Both use the same precedence the scan's genre pass uses — the curated `genres`
list over the free-form `tags` — so an upcoming row's label means what the
library would end up with.

`musicbrainz_genres` (the raw names) and `main_genres` (the classified roots) are
stored, and the upsert **COALESCEs** rather than assigns: a re-discovery carries
no genres, and a plain `= EXCLUDED.x` would strip a label the match path had just
resolved.

`sub_genres` is **not** stored — it is derived on read, so improving the
vocabulary improves every existing row.

### The colour is CSS, not an API value

The API answers `main_genres: [{id: "metal", display: "Metal"}]`; the id becomes
a class and `static/css/upcoming-genres.css` maps it to a colour. Two reasons:
a palette change should not need a server deploy, and legibility against both
surfaces is a CSS question.

19 roots have colours. An unknown root falls back to the neutral `.ur-genre`
chip — a missing colour is cosmetic, a missing label would be data loss.

**A chip is never colour-only.** Every one carries its genre name, so the row is
readable without distinguishing red from orange — which matters for colour-blind
users, and for the phone layout where the chips are the row's only label.

### Schema

Two additive columns, declared in `db/schema.py`'s `COLUMN_REGISTRY` and added
by migration **039**, which checks the table exists *before* the column (the
defect fixed in 033/034 — an absent table has no columns, so a column-only guard
issues an `ALTER` against nothing).

---

## 2. The dashboard polled four endpoints every 5 seconds forever

`STATUS_POLL_MS` was 5000 with no alternative, so an open dashboard cost
4 × 12 × 60 ≈ **2,900 requests an hour** to discover that nothing had changed.

`poller.create()` captured `interval` in a closure, so the slow path was not
expressible: a caller that wanted "fast while busy, slow while idle" had to stop
and rebuild the poller, losing the in-flight guard and the attempt count.

* `poller.create()` now takes `setInterval(ms)` / `interval`. It changes the
  delay read at **schedule** time, re-arms a pending tick, leaves an in-flight
  request alone, and ignores a non-finite/zero value (a 0 interval would make
  `setTimeout` fire in a tight loop).
* The dashboard sets the pace from `updateActiveScans()` — the one response that
  authoritatively answers "is anything happening": **5 s while a scan is
  running, 60 s when idle**. It starts at the idle pace, because `updateAll()`
  has just run and the first reply raises it if needed.

The other three endpoints (recent scans, pending approval, queue processor) ride
the same poller, so they slow down with it.

---

## 3. What the run caught in this change

Three defects, all found by executing rather than reading:

1. **`json` was not imported** in either module that now serialises genres. Both
   would have raised `NameError` on the first match.
2. **A `#` comment inside an f-string SQL literal.** I put the COALESCE
   explanation inside the `"""…"""` that becomes the statement, so SQLite
   received `# Labels only ever FILL IN…` and answered `unrecognized token:
   "#"`. Every discovery write failed — 7 tests. Moved the explanation above the
   literal, and left a note there saying why it cannot go inside.
3. **Six test fixtures hand-write the `upcoming_releases` DDL**, so the two new
   columns were absent from them and the upsert failed with `table
   upcoming_releases has no column named musicbrainz_genres`. Two fixtures
   exercise the upsert and needed the columns; the other four only read and do
   not.

A guard now checks that **every fixture running the upsert** declares the
columns — verified non-vacuous (it inspects exactly the two that write) and it
would have caught defect 3.

---

## Verification

| | Failed | Passed | Skipped | Errors |
|---|---|---|---|---|
| Before this change | 406 | 10,740 | 317 | 29 |
| After | **406** | **10,782** | 321 | 29 |

Same failure count, 42 more passes (the new suites). The 29 errors remain
pre-existing (`test_navidrome_*` / `test_folder_match_*` need a table the
conftest fixture does not create).

New/affected suites:

* `test_upcoming_release_genre_labels.py` — **34 passed** (classification,
  schema, migration guard, backend payload, frontend markup, CSS scoping)
* `test_poller_adapts_its_interval.py` — 6 passed, 4 skipped (the skips are the
  Node-driven behavioural probes; `node` is not installed here)
* `test_upcoming_same_title_single_and_album.py`,
  `test_upcoming_musicbrainz_discovery.py` — 43 passed after the fixture fix
* `test_migrations_idempotent.py` — 13 passed, chain clean with 039
* `test_js_files_parse_without_duplicate_declarations.py` — all JS still parses

## Still open

* `artist_list` server-renders ~1000 rows with no search or pagination.
* Splitting `album.js` (4 011 lines), `config.js` (3 733), `playlist.js` (2 318),
  `download-queue.js` (2 089).
* Phase 1, the real bundle — `node` is still absent, so it cannot be built or
  verified here.
* `routes/beets_routes.py` is deleted; `old_system/deprecated/` still holds the
  archived Beets importer.
