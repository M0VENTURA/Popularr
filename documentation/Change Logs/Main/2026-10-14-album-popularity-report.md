# Album popularity report in the album page's Actions menu (2026-10-14)

## What was requested
> Currently we have the "Download Popularity Report" under each artist page,
> can we also add it to the actions dropdown on the album pages?

## The change
1. **One builder, two scopes.** The CSV report moved out of
   `routes/artist_routes.py` into
   `services/popularity/popularity_report_service.py`
   (`build_popularity_report`, `popularity_report_filename`,
   `POPULARITY_REPORT_COLUMNS`). The album report is the artist report
   restricted to one album: **same columns, same ordering chain, same BOM**.
   The artist route now delegates to it (83 lines of duplicated SQL removed),
   so the two files cannot drift apart in a column or a sort key — an album's
   rows also keep their relative order between the two files.
2. **New endpoint** `GET /api/album/popularity-report?artist=&album=`
   (`routes/album_routes.py`), modelled on the artist one: validates both
   parameters (a 400 must not reach the query — an empty album would silently
   download the artist's whole catalogue), runs the query off the event loop
   via `asyncio.to_thread`, and returns `text/csv` as an attachment with a
   UTF-8 BOM so Excel does not mangle accented titles.
3. **Scoped by artist AND album.** Album titles are not unique — "Greatest
   Hits" exists a thousand times over — so an album-only filter would merge
   every artist's hits album into one report. Matching is case-insensitive and
   keyed on `COALESCE(album_artist, artist)`, the same artist key the rest of
   the app uses.
4. **Filename** carries the album:
   `<Artist> - <Album> - popularity report.csv`, with quotes/separators
   stripped (a raw album title would otherwise break — or inject into — the
   `Content-Disposition` header).
5. **The dropdown item** (test_site album page, next to "Change Album Art"):
   a plain `<a href="{{ url_for(...) }}">` download, so the browser's own
   download flow handles it — the same way the artist page's copy behaves. The
   live tree's artist page has no report, so the live album page does not get
   one either (the two trees stay consistent).

## Tests
* **New** `tests/test_album_popularity_report.py` (17): downloadable CSV with
  the album in the filename; the full column set; most-popular-first ordering
  with `rank` following it; **only this album** (another album of the same
  artist excluded, and another artist's same-titled album excluded);
  compilation tracks filed under their album artist; case-insensitive
  matching; missing artist → 400 and missing album → 400; session required;
  the album file being the artist file filtered; the artist route no longer
  carrying its own SQL; and the page's menu/`url_for` contract asserted against
  the app's own URL map.
* **Oracle**: reverting the route, the service and the artist-route
  delegation (keeping the tests) fails 13 of the 17 — the 4 that still pass
  are the template/auth assertions that do not need the endpoint.

## Verification
`test_artist_popularity_report` 14 (the refactor is behaviour-preserving),
`test_album_popularity_report` 17, `test_rebuilt_pages_have_no_dead_buttons` 6,
`test_templates_tag_attribute_integrity` 120, `test_album_lookup_skip*` 37,
`test_album_metadata_review_wiring` 26, `test_album_page_missing_tracks_list`
18, `test_artist_page_contract` 26 — all green. `py_compile` clean on every
edited file.
