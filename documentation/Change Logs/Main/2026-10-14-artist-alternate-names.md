# Artist alternate names — one artist under many names, not covers (2026-10-14)

## What was requested
> Can we add a section under about for the Artist page to have alternate names
> for artists. This should have it so that artists with both names will be
> merged under the one artist so aren't accidentally detected as cover songs,
> Such as Electric Callboy and Eskimo Callboy.

## Root cause
Every identity comparison in the app compares artist NAMES. A renamed band's
recording credited under its other name ("Eskimo Callboy" vs "Electric
Callboy") looked like a different artist, so the cover detector's self-credit
guards — `_detect_via_isrc`'s `_same_artist`, and the relation/writer paths'
`names_match` — reported the band's own song as a cover of somebody else.

## The change

### Storage
* **New table `artist_aliases`** (`alias_key` casefolded PK, `alias` display
  form, `artist_key`/`artist` canonical). A name can only belong to ONE
  artist — an alias claimed by two would make resolution ambiguous — so the
  conflict is reported (409), never silently re-pointed.
* Bootstrap: `db/schema.py` (`TABLES_TO_ENSURE` + artist_key index).
* Migration `024_artist_aliases` chained from `023`, inspector-guarded like
  the rest of the chain (bootstrap remains authoritative).

### The identity merge
* **`services/metadata/artist_alias_service.py`** owns the mapping:
  `resolve_artist_alias` (chain-following, cycle-guarded), `get_alias_group`,
  `add_alias`, `remove_alias`. The whole tiny table is TTL-cached (30 s) and
  invalidated on every write, because resolution runs inside cover detection —
  a hot path must never issue one query per comparison. Read failures degrade
  to the pre-alias behaviour (input unchanged), never to a crash.
* **`helpers/normalization_service.py::names_match`** resolves BOTH sides
  through it first. `names_match` is the primitive the cover detector uses at
  all 14 comparison sites, so one wiring reaches the ISRC guard, the
  relation paths and the writer paths — with no aliases registered the
  resolution is a lookup on an empty map, i.e. exactly the old behaviour.
* Chains resolve (A→B then B→C ⇒ A→C), so every page of a group shows every
  member, and both pages of a plain pair show the same group.

### Endpoints (`routes/artist_routes.py`)
* `GET /api/artist/aliases?artist=` → the group (canonical + other names).
* `POST /api/artist/aliases` `{artist, alias}` → add; 400 for missing/self,
  **409** naming the owner when another artist holds the name; idempotent
  when already linked.
* `POST /api/artist/aliases/remove` → unlink (idempotent). Removing the
  group's canonical name from an alias's own page unlinks THAT page (its
  binding row is the target) — deleting "the canonical's row" would be a
  no-op and the badge would reappear on reload.

### Page (test_site; live tree untouched per the test_site-only preference)
* **New "Alternate Names" card in the About tab** of
  `artist_detail_v2.html`, between External IDs and Similar Artists:
  server-rendered badges (× via `data-action` delegation — names can contain
  quotes, so the value is read HTML-escaped from the attribute), an input +
  Add button (Enter submits, bound in JS so no inline handler), and a
  "merged as <canonical>" hint when the page's own name is an alias.
* `artist-detail-extras.js`: `renderArtistAliases`/`addArtistAlias`/
  `removeArtistAlias` + delegated bindings in `init()`; `addArtistAlias`
  exported globally for the template's onclick (templates have no compiler).
  Errors surface from the JSON body (the 409 names the owning artist), not
  from a generic catch.
* `routes/ui_routes.py` payload: `artist_aliases` + `artist_alias_canonical`
  (failure-tolerant — the section renders empty, the page never dies).

### ⭐ The engine-dispose trap (found by the sweep, fixed twice)
Reading a MISSING `artist_aliases` raised `OperationalError` inside
`db_session`, whose transient-error path **disposes the shared engine** — for
an in-memory test DB that erased EVERY table, and an unrelated cover suite
then failed five queries later with "no such table: tracks". Fixed by:
1. `_table_exists` guards on the QUIET reads (`load_all_aliases`,
   `find_alias` return empty when absent) — a missing table is an answer, not
   an error; production creates it at bootstrap, a pre-bootstrap reader must
   degrade to pre-alias behaviour instead of poisoning the pool; and
2. `tests/conftest.py` now recreates `artist_aliases` alongside
   `album_art`/`missing_releases` after a mid-suite dispose (same rationale
   as those two).

## Tests
* **New** `tests/test_artist_alternate_names.py` (34): resolution (unknown ⇒
  input, registered ⇒ canonical, chains, symmetric `names_match`,
  no leak into unrelated names, `_same_artist`); **behavioural cover tests**
  through `_detect_via_isrc` with a stubbed MB lookup (alias suppresses the
  false cover · the control without the alias still detects · an unrelated
  artist still detects — the alias merges two names, not every artist);
  group display (both pages agree, chains show every member); CRUD semantics
  (idempotency, 409 with owner, self/missing 400, sibling removal, canonical
  removal unlinks the page, **write invalidates the cache immediately**);
  endpoints (contract + validation + auth control + URL map); page wiring
  (card inside the About tab, input/button, server-rendered list, merge hint,
  JS exports/delegation, payload keys).
* **Oracle**: with the WIRING reverted (service kept) → **16 failed / 18
  passed** — exactly the names_match/cover/endpoint/page tests, controls
  green. With the service removed as well → **34 errors** (the fixture cannot
  even import the feature).
* ⭐ Oracle also caught a false pass: the first ISRC fake used the flat
  `{"name": ...}` credit shape; `artist_from_credit` reads the nested
  `entry["artist"]["name"]`, so `rec_artist` was "" and the "not a cover"
  test passed vacuously. Fixed to the nested shape.

## Verification
Baseline comparison where red (identical sets, all pre-existing):
`test_artist_tracklist_loading` 4F/29P. Green: cover suites
(11 + 19 + 13 + 14 + 12), `test_compilation_cover_and_rating` 12,
artist-page suites (26 + 28 + 21), `test_rebuilt_pages_have_no_dead_buttons` 6,
`test_templates_tag_attribute_integrity` 120, `test_migrations_idempotent` 13,
`test_scan_identity_and_year_tags` 55, `test_lastfm_diacritic_folding` 42,
`test_no_boolean_int_sql_mixups` 11, `test_album_name_filter_matches` 39,
`test_scan_db_first_persistence` 11, `test_jsonb_drift_repair` 24. `node
--check` + `py_compile` clean on every edited file.

## Known follow-up (not in this change)
The merge is IDENTITY-level: cover detection and every `names_match` caller
treat the names as one. The artist page's own album list/stats/report are
still keyed by the single stored artist name — extending those queries to the
group is a separate, larger change (one artist-keyed query in the payload
builder plus the cached stats sources).
