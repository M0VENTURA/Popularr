# Album art: existing art is no longer looked up again (2026-10-08)

## Reported

> If album art already exists, it shouldn't look for it each time.

## Root cause — a dead branch made the 2026-09-21 fix inert

`2026-09-21-album-art-navidrome-first.md` promised:

> `get_or_fetch_album_art()` … asks Navidrome next, and — when Navidrome has
> no copy — **keeps what it already holds** instead of re-downloading the same
> provider art.

It did not. The stored blob was read into `data` and then **reassigned**:

```python
data, mime, cached_source = fetch_album_art_record(...)   # the stored blob
...
data = fetch_album_art_from_navidrome(artist, album)      # ← overwrites it
if data:
    ...
# "Navidrome has no copy … whatever we already hold STANDS"
if data:                    # ← UNREACHABLE: data is None whenever we get here
    return data, mime or "image/jpeg"
```

So for **every album whose art came from a provider** (`musicbrainz`, `discogs`,
`audiodb`, `itunes`, `missing_releases`, `unknown`, `""` — all upgradable, so
none of them short-circuit), each call:

1. read the stored blob, then threw it away;
2. hit the `missing_releases` cover URL **over HTTP** (that fast path ran
   *before* the cache decision, so it re-downloaded the picture);
3. asked Navidrome; and when Navidrome had nothing,
4. fell through to **Cover Art Archive → Discogs → AudioDB**, re-downloading
   art we already held — one CAA log line per imported track — and
5. `return None, None` at the end if no provider answered, **discarding a perfectly
   good stored cover entirely.**

The scan pipeline (`album_stage.py`) had this right all along — it keeps
`_cached_blob` in its own variable and logs `album art kept`. Only the
orchestrator was broken.

## Fix

`services/enrichment/album_art_service.py::get_or_fetch_album_art` — the stored
art is now held in `stored` for the whole function and is never overwritten:

| stored art | source | behaviour |
|---|---|---|
| exists | `navidrome` / `upload` / `url` | returned immediately — nothing to look for |
| exists | provider | **one** Navidrome check to *upgrade* it; if Navidrome has nothing → **return the stored art and stop** |
| none | — | missing-releases URL → Navidrome → CAA → Discogs → AudioDB (order unchanged) |

The `missing_releases` fast path moved *inside* the "nothing stored yet" branch,
so it can no longer re-download a URL for an album that already has a picture.

**What still runs when art exists:** two DB reads and Navidrome's local
`getCoverArt` — and that check is itself memoised on a miss for
`_NAVIDROME_MISS_TTL_SECONDS` (6 h), while a hit rewrites the row as
`source="navidrome"`, which is final. **No online provider is ever contacted
again for an album that already has art**, and a stored picture can never be
lost.

## Tests — the suite was red *and* mis-wired

`tests/test_album_art_navidrome_first.py` was **5 failed / 19 passed** at base.
Three of those failures were the harness reading the **real** database instead
of the fake:

1. **`_FakeResult` treated a tuple row as a list of rows.**
   `_FakeSession([(blob, mime, source)])` → `first()` returned only the first
   *cell* (`b"stored-caa"`), so `fetch_album_art_record` indexed into a `bytes`
   and produced ints instead of `(blob, mime, source)` → "no stored art".
2. **`_patch_db` patched the wrong module.** `fetch_album_art_record` lives in
   `db.repositories.metadata` and does its own `from db.engine import db_session`,
   so patching only the module under test left it reading the real DB. Two tests
   failed for that reason and `test_the_album_page_path_asks_navidrome_first`
   *passed for the same wrong reason*.
3. **Dict rows reached `row[0]`** — `KeyError(0)`, whose `str()` is literally
   `"0"`, was logged as `Navidrome guard check failed error=0` and failed OPEN,
   silently dropping the song id. Two more failures.

New class `TestExistingArtIsNeverLookedUpAgain` (4) pins the report directly:

* no online provider is consulted when art exists (all three are patched to
  `pytest.fail`);
* three consecutive calls still never reach a provider;
* the `missing_releases` URL is **not** downloaded when art exists;
* **CONTROL** — provider art can still be *upgraded* to Navidrome's copy, so the
  2026-09-21 feature is preserved.

> ⚠️ Two harness traps, both recorded: `client.get.side_effect = pytest.fail(...)`
> **raises at assignment time** — it must be `lambda *a, **k: pytest.fail(...)`,
> or the test fails without the code ever running.

## Verification

* `tests/test_album_art_navidrome_first.py` → **28 passed** (base: 19/5).
* **Oracle** — reverting ONLY the production file (harness left fixed) →
  **4 failed**, exactly `test_provider_art_stands_when_navidrome_has_none` + the
  three new contract tests; the upgrade CONTROL passed both ways. Restored → 28 passed.
* **Sweep** — 63 art/cover/download/embed/metadata/album/import files,
  clean `origin/develop` vs this change: **base `87 failed / 2072 passed`** vs
  **new `82 failed / 2082 passed`**; `Compare-Object` on the sorted `^FAILED`
  lines = **empty for "only in CHANGED"** (0 regressions) and the 5 fixed are
  exactly this suite's pre-existing failures.
