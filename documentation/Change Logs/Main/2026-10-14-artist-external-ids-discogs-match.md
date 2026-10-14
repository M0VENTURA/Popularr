# Edit External IDs: it saves, it clears, and Discogs matches the right artist (2026-10-14)

## What was reported
> The edit external IDs on the artist page doesn't save when updating the
> discogs ID. It should also have a button to search artists with the same
> name to adjust the match if it's incorrectly matched on discogs. Discogs
> seems to match the first artist, not the one with the same albums

## Three defects behind the one report

### 1. The Save button was dead on the rebuilt page
The modal's button carries **no inline `onclick`** — it is bound in JS — and
`test_site/static/js/pages/artist.js` never bound `#editArtistIdsSaveBtn`
(only the LIVE `artist-page.js` did). Clicking Save sent **nothing at all**.
The modal also opened **unseeded**: the inputs are empty markup, so the form
showed blank fields over an artist whose ids are set.

### 2. A wrong id could not be removed
`update_ids` wrote only **truthy** values. Clearing the Discogs box therefore
saved nothing and the bad id survived the reload — the "doesn't save" the user
saw. A save that matched **no tracks** also replied `success: true`, so the
page reloaded unchanged with no explanation.

### 3. Discogs matched `results[0]`
`DiscogsHttpClient.get_artist_id` took the **first search hit**,
unconditionally. Discogs ranks its own search, so a name shared by several
artists resolved to whichever Discogs listed first — not the artist whose
release list contains the library's albums.

## The change

### Discogs resolution (`api_clients/discogs_http.py`)
Resolution is now, cheapest evidence first:
1. a candidate whose name matches **exactly** (normalised) is preferred — a
   tribute act or a different spelling loses here at **no extra request cost**;
2. with several candidates left **and** album titles supplied, each candidate's
   release list is compared against those albums and the best overlap wins —
   the "same albums" rule, capped at `_MAX_ALBUM_SCORED_CANDIDATES` (3)
   requests at Discogs' 1 req/sec budget;
3. otherwise the first candidate stands, so a single unambiguous hit behaves
   exactly as before.
New `search_artists()` (candidates with id/name/url/thumb) and
`matched_artist_albums()` (which library albums a Discogs artist also has).
Album comparison strips Discogs' pressing/format suffixes
(`"Tekkno (CD, Album)"` ⇒ `"tekkno"`), or nothing would ever match.

**Both automatic call sites now pass the artist's albums**: the scan prefetch
(from rows already in memory) and `album_stage._fetch_discogs_artist_id` (one
cheap `SELECT DISTINCT album`, once per artist — the id is memory-cached).

### Saving (`services/metadata/artist_metadata_service.py`)
* A key **present** in the payload is written even when **empty** (⇒ `NULL`),
  so a wrong id can be cleared. An **absent** key is still skipped, so a caller
  updating one field never wipes the other.
* `rows_updated == 0` now returns **404** with "No tracks found for '<artist>'"
  instead of a success that changed nothing.

### The picker
* **New service** `search_discogs_artists(artist)`: Discogs candidates scored
  by how many of the artist's library albums appear in each candidate's release
  list, sorted best-first (unscored candidates still listed, no album evidence).
  Explains itself when the integration is unconfigured.
* **New endpoint** `GET /api/artist/discogs-search?artist=` — rate-limited
  network work, so it runs off the event loop.
* **Modal UI (both trees)**: a "Find" button beside the Discogs ID field, a
  candidate list showing each artist's matched albums and Discogs id, a
  "Use this ID" button (delegated via `data-action` — an artist name with a
  quote would break an inline onclick) and an "Open" link. Choosing a candidate
  fills the field; **Save** commits it.
* **The live partial had NO ids modal at all** (`showModal` found no element,
  so the live Edit button opened nothing) — added with the same ids the live
  `artist-page.js` already binds.
* Save now **reports failure** in the modal (inline status line + toast) instead
  of silently doing nothing, and the rebuilt page **seeds** the inputs from the
  displayed values.

## Tests
`tests/test_artist_external_ids_discogs.py` (34): resolution (single hit, exact
name beats the first hit, album overlap decides between same-named artists, no
overlap ⇒ no invented winner, the scoring cap, empty name), album-title
normalisation through Discogs suffixes, `update_ids` (clears, leaves absent keys
alone, 404 when nothing matched, 400s), the picker service (sort order, payload,
unconfigured, missing artist), the endpoint (contract, 400, auth, URL map), and
the modal wiring in both trees (button bound, inputs seeded, failures reported,
search control present, handler exported, candidate rows delegated, album match
shown).
**Oracle**: with all 11 source files reverted (tests kept) → **27 failed / 7
passed** — the 7 are controls (first-hit fallback, no-overlap fallback, absent
key, `matched_albums` shape).

## Verification
Green: `test_artist_page_contract` 26, `test_artist_alternate_names` 34,
`test_artist_edit_release_modal` 21, `test_templates_tag_attribute_integrity`
120, `test_rebuilt_pages_have_no_dead_buttons` 6,
`test_no_undefined_names_in_production` 7, `test_discogs_single_detection` 8,
`test_discogs_promo_detection` 14, `test_discogs_single_format_gate` 16,
`test_discogs_throttle_lock` 6, `test_artist_metadata_once_per_artist` 12,
`test_artist_scan_loading` 6, `test_artist_page_is_db_only` 28,
`test_api_rate_limit_enforcement` 16+1s, `test_scan_db_first_speedups` 15.
**Baseline-identical** (pre-existing, unchanged): `test_scan_stall_fixes` 7F/8P
and `test_full_scan_as_artist_pipeline` 1F/6P — the same 8 failures at
`b827bdfb`.

## Notes / follow-ups
* The picker scores up to 5 candidates (one Discogs request each, 1 req/sec),
  so a search can take a few seconds; the button shows a spinner and disables.
* The automatic rule still needs albums to disambiguate; an artist whose rows
  have no album titles keeps the exact-name/first-hit behaviour.
