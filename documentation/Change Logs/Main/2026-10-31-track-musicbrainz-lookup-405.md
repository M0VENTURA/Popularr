# The track page's MusicBrainz recording lookup returned an HTML 405

**Date:** 2026-10-31
**Area:** `routes` / track page
**Files:** `routes/track_routes.py`,
`test_site/static/js/pages/track.js`,
`tests/test_track_musicbrainz_lookup.py` (new)

## Reported symptom

> On the track page, when searching a MusicBrainz recording ID it returns
> `Network error: Server returned HTML instead of JSON (HTTP 405). This usually
> means an auth/session redirect or a server error page.`

## Root cause

The "MusicBrainz Recording ID" field's **Search** button POSTs to
`/api/track/musicbrainz`. That route was **never ported** — the old monolithic
`app.py` has `api_track_musicbrainz_lookup`, but `routes/track_routes.py` did
not.

Because the path is not static-registered, Werkzeug matched it against the
dynamic `/<track_id>` rule (GET only) with `track_id="musicbrainz"`. A POST to
a GET-only rule is a **405**, answered by Quart's HTML error page — exactly the
"Server returned HTML instead of JSON" the shared api client reports.

## Fix

* New `POST /api/track/musicbrainz` in `track_routes.py`, using the shared,
  rate-limited MusicBrainz client (not raw `requests`):
  * `mbid` in the payload → `get_recording()` resolved directly and returned
    first with confidence `1.0`;
  * `title` + `artist` → `search_recordings()` (escaped Lucene phrase),
    flattened to the shape both track UIs already expect:
    `{mbid, title, artist, album, year, length, releases, writers, composer,
    confidence, title_similarity, artist_similarity, source}`;
  * empty payload → JSON `400`, never HTML.
* The static route is defined before `/<track_id>`; static rules also win
  routing precedence for a POST.
* `test_site/static/js/pages/track.js` now:
  * **pre-fills the modal with the track's current artist and title** and
    searches immediately — the current text is the first attempt, not the
    stored recording id;
  * keeps both fields **editable inside the modal**, with its own Search button
    (and Enter), so a miss can be corrected and retried without closing it and
    editing the main form;
  * a miss reads "No matches. Adjust the artist or title above and search
    again." instead of a dead end;
  * the stored `#mbid` value is sent only as a FALLBACK when there is no text
    to search with (e.g. an empty form).

## Verification

`tests/test_track_musicbrainz_lookup.py`:

| Test | Pins |
|---|---|
| `test_it_answers_json_instead_of_the_html_405` | ⭐ title/artist search returns JSON 200 with the picker's fields |
| `test_a_pasted_recording_id_is_resolved_directly` | an MBID-only search resolves without a text search |
| `test_an_empty_request_is_a_json_400_not_html` | bad input is JSON, never Quart's HTML page |
| `test_the_modal_has_editable_artist_and_title_prefilled` | the current text is the first attempt |
| `test_the_modal_can_search_again` | its own Search button / Enter retries |
| `test_no_matches_keeps_the_fields_available` | a miss tells the user to adjust and retry |
| `test_the_current_recording_id_is_only_a_fallback` | text is searched first |
