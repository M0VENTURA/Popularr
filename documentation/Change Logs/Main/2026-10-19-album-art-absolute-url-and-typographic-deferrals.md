# Scan findings: the album-art protocol bug, and typographic deferrals

**Date:** 2026-10-19 · **Area:** `playlists` / `metadata`

Two of the six flagged findings were real defects. Both are fixed here, each with
an oracle-verified test. The other four are recorded below **deliberately
unchanged**, because they are either working as designed or are observations
rather than faults.

---

## 1. `UnsupportedProtocol` fetching album art — a relative path handed to `httpx`

### Reported

> An `UnsupportedProtocol` error triggered while attempting to fetch album art
> for Carpathian's *Nothing to Lose*. The request failed because the target URL
> was a relative path (`/api/album/Carpathian/Nothing%20to%20Lose/art`) missing
> the `http://` or `https://` prefix.

### Root cause — two consumers wanting opposite things from one field

`artist_metadata_service.get_artist_image()` resolves an artist image in three
steps. The third serves *this app's own* album-art route when the artist has no
image of its own:

```python
url = "/api/album/{artist}/{album}/art".format(artist=_quote(artist, safe=""), ...)
```

`_quote(..., safe="")` is what turns the space into `%20`, so this is precisely
the reported string. The path is **correct** — the artist page puts it in an
`<img src>`, where the browser resolves it against whatever origin the page was
loaded from, which is what lets the app work behind a LAN IP *and* a reverse
proxy without storing an origin anywhere.

The bug is on the consuming side. `playlist_service._fetch_artist_image_bytes()`
takes that same field and does:

```python
resp = session.get(image_url, timeout=15)   # httpx
```

A URL with no scheme and no host has nothing to connect to, so `httpx` raises
`UnsupportedProtocol`. It was caught and logged at **debug** level, so the only
symptom was a playlist cover that silently never appeared.

### Fix — the backend builds the absolute form, at the point of use

New `playlist_service._app_local_base_url()`:

| order | source |
|---|---|
| 1 | `POPULARLR_APP_BASE_URL` — explicit override for a reverse proxy / unusual bind |
| 2 | the host:port from `SPTNR_GUNICORN_BIND` (default `0.0.0.0:5000`) |

⚠️ **A wildcard bind is rewritten to loopback.** `0.0.0.0` is a *listen*
address, not a destination — dialling it fails, which would have replaced one
broken URL with another.

⚠️ **The absolute form is NOT stored in the database.** A stored origin is wrong
for anyone reaching the app by a different name (LAN IP, proxy, container
hostname), and the artist page would then serve a broken `<img>` to those
clients. Only the backend needs it, so only the backend builds it.

If the fetch still fails (an unusual network namespace, a bind nothing can
dial), the cover falls back to `_album_art_bytes_for_artist()` — the bytes are one
query away and a cover should not be lost to an unreachable loopback. ⚠️ That
fallback differs in one respect: the HTTP route is Navidrome-*first* and may
return the server's own cover, while the fallback returns the artwork stored in
`album_art`. Same album, possibly a different source.

### Tests

`tests/test_playlist_cover_absolute_url.py` — **14 passed**. Verified as an
oracle: **9 of them fail on pristine `develop`**, including the exact Carpathian
path. The 5 that pass either way are the controls (absolute provider URLs must
not be rewritten; no URL means no fetch).

---

## 2. Typographic variants deferred as metadata changes

### Reported

> Several tracks were deferred for manual approval because the local files use
> straight apostrophes while MusicBrainz proposes curly apostrophes. This
> affected Catatonia's "Baby, It's Cold Outside" and "You've Got a Lot to Answer
> For", as well as Carpathian's "End of the 1980's" and "Drop It Like It's Hot".

### Root cause — the comparison key ignored typography

`metadata_proposal_service._norm()` was case- and whitespace-insensitive only:

```python
return " ".join(_as_text(value).casefold().split())
```

So `U+0027` and `U+2019` produced different keys and the diff reported a change.

⭐ **WHY THE ALBUM-LEVEL PATH, SPECIFICALLY.** All four examples are *singles*,
where the album title equals the track title. Track titles already go through
`titles_match_for_review` → `normalize_title_for_compare` → `normalize_string`,
which folds punctuation — but the **album-level** comparison
(`_norm(proposed) == _norm(current.get(form_field))`) had no such matcher. The gap
was only visible on the fields that lacked the second rule.

### Fix — fold typographic variants in the key

`_norm` now runs `normalize_unicode_punctuation()` first, which maps smart quotes,
primes, typographic dashes and NBSP onto their ASCII equivalents.

⚠️ **It MAPPES, it does not DELETE.** That distinction is the whole safety
argument: `AC/DC` and `ACDC` still differ, `Song` and `Song (Live)` still differ,
two different titles still differ. The tests pin exactly that, so a future
"improvement" that strips punctuation wholesale fails immediately.

---

## Verification

| suite | result |
|---|---|
| `tests/test_playlist_cover_absolute_url.py` | 14 passed (9 fail on pristine) |
| `tests/test_typographic_variants_are_not_changes.py` | 24 passed (12 fail on pristine) |
| playlist / cover / index sweep — 13 suites | 334 passed, **0 failed** |
| metadata / proposal / pending sweep — 36 suites | 793 tests; 4 files failing |

The 4 failing files are **pre-existing and identical on pristine `develop`**,
verified file by file:

| file | pristine | fixed |
|---|---|---|
| `test_album_save_background_phase` | 1/41 | 1/41 |
| `test_metadata_fanout_to_files` | 1/13 | 1/13 |
| `test_metadata_update_album_split` | 3/10 | 3/10 |
| `test_release_extended_metadata` | 2/16 | 2/16 |

**Zero regressions.**

---

## ⚠️ Investigated and deliberately NOT changed

### Title expansion deferrals (Candlemass, "Into the Unfathomed Tower")

> MusicBrainz attempted to overwrite the local title with a massive string
> appending all seven roman numeral movements.

**Working as designed.** A proposal that appends seven movement names to a short
title is a large change, and deferring it puts the decision in front of a human
instead of letting a scan silently rewrite the title and the file tags. This is
the same class as `titles_match_for_review`'s rule that a *cover* marker must be
surfaced rather than swallowed: when the claim cannot be verified from the data,
the operator decides.

Worth noting for anyone revisiting it: the movements are genuine parts of the
title on some releases, so the "right" answer is per-release and cannot be
inferred. Left alone.

### Stale Navidrome song IDs ("Holiday - Top Tracks" dropped one track)

The log's own diagnosis — a stale song ID from Navidrome 0.64's ID re-encoding —
is consistent with what the playlist index now reports: it counts entries that
resolve to no library track (`unresolved`) rather than dropping them silently, so
this is **observable**. A full Navidrome rescan followed by a fresh import is the
remedy, exactly as the log advised.

**Not a code defect**, and not fixable server-side: once Navidrome re-encodes an
ID, the old one can only be replaced by re-importing the library.

### Multi-user handling (skipping `bayneberry` for `moventura`)

**Validation — no action.** `_navidrome_clients()` is deliberately one client per
SERVER, not per account, precisely so two accounts on `192.168.1.200:4533` cannot
ping-pong playlists against each other. The observed behaviour is that guard
working.

### Genre exclusion (1,512 metal tracks blocked from "Alternative Rock")

**Validation — no action.** The genre-boundary logic did its job.

---

## Files

| File | Change |
|---|---|
| `services/playlists/playlist_service.py` | `_app_local_base_url()`, absolute-URL resolution, DB fallback |
| `services/metadata/metadata_proposal_service.py` | `_norm()` folds typographic punctuation |
| `tests/test_playlist_cover_absolute_url.py` | **new** — 14 tests |
| `tests/test_typographic_variants_are_not_changes.py` | **new** — 24 tests |
