# Genre playlists roll up to base genres; Christmas is built by base genre; the import waits for a running scan

**Date:** 2026-11-24
**Area:** `services/enrichment/genre_vocabulary.json` ·
`services/catalog/album_classification_service.py` ·
`services/popularity/stages/finalise_stage.py` ·
`helpers/config_helpers.py` ·
`services/scanning/pipelines/navidrome_pipeline.py` ·
`test_site/static/js/pages/config.js` · `test_site/templates/Pages/config.html`
**Commit:** `feat(playlists): base-genre rollups, Christmas by base genre, and a
Navidrome import that waits instead of deferring`

## Reported

> "Christmas playlists should follow the same rules as the covers playlist,
> where they are just created by base genre
>
> Metal genre should include all metal and metalcore based genres
>
> Punk should include all punk based genres such as emo, ska, etc
>
> Rock should include all rock based genres such as grunge, etc
>
> All of these playlists should have tracks that are 4* or higher. Similar to how
> the Essential playlists and the Top Artist playlists are created. Currently
> some 2 and 3 star tracks are being added to them.
>
> Also, the Navidrome import doesn't seem to be happening while another scan is
> happening anymore."

Confirmed shape: **keep the leaf playlists, and ADD the parent rollups** (a track
may appear in both "Metalcore - Top Tracks" and "Metal - Top Tracks"). The
emo/ska parentage was to change **in the vocabulary itself**, not just for
playlists.

## 1. Parent-genre rollups

`_create_genre_top_track_playlists` already pools each track into every one of its
final genres (`Metalcore`, `Nu Metal`, …). After that pass, a second pass copies
each entry into its **main (root)** genre's pool:

```python
_main = classify_main_genre(_entry.get("genres") or [])
...
pools[_main_id].append(_entry)
```

`classify_main_genre` is the same authority the per-genre Great Covers builder
uses: it folds a subgenre to its vocabulary ROOT through the deciding-family rule,
so no mapping table lives in the playlist code.

| leaf genre | rolled into |
|---|---|
| Nu Metal, Metalcore, Death Metal, Deathcore, Thrash Metal | **Metal** |
| Pop Punk, Hardcore Punk, Post-Hardcore, Screamo, Emo, Ska | **Punk** |
| Grunge, Alternative Rock, Indie Rock, Hard Rock, Shoegaze | **Rock** |

Nothing is removed: `Metalcore - Top Tracks` keeps existing. The only cost is that
a track can appear in two lists — which is what a rollup is.

⚠️ **The rollup key goes through `_pool_genre_key`, not `normalize_main_genre_key`.**
The two normalisers disagree on multi-word roots — `"Hip-Hop"` becomes `hip hop`
as a pool key but `hip_hop` as a main-genre id — so keying the rollup with the
latter wrote a SECOND pool and a duplicate "Hip-Hop - Top Tracks". A track already
tagged literally `Metal` is likewise not added twice (its leaf pool *is* the
rollup, and the key equality catches it).

New config key **`playlists.genre_playlists_parent_rollups`**, default **true**;
`false` restores leaf-only behaviour exactly.

## 2. Emo and Ska are Punk — in the vocabulary

`classify_main_genre(["Emo"])` answered **rock** and `(["Ska"])` answered
**reggae**, so the Punk rollup could never include them.

`emo` and `ska` were re-parented to `punk` in `genre_vocabulary.json`. Neither
declares a `family`, so `family` is inherited from the nearest declared ancestor
at load time (`genre_vocabulary.py` ~773) — re-parenting alone is enough.

Documented consequence, pinned by a test: `ska` is now a CHILD of `punk`, so the
genre-selection engine condenses a generic `Punk`/`Punk Rock` away when `Ska` is
also present (`Punk: REDUNDANT_GENERIC_PARENT (Ska)`). The track still resolves to
the punk family, so playlist membership is unchanged.

## 3. Compound genres no longer collapse to nothing

`classify_main_genre(["Emo / Screamo"])` answered **None** — no main genre at all.
`("Metalcore / Post-Hardcore")` likewise.

`iter_genre_tokens` passed a LIST entry through verbatim, so a tag stored as one
value stayed a single token and matched no vocabulary entry. Only the raw-STRING
branch split, and it split **blindly** — which is the opposite error, because
`death/doom`, `drum & bass` and `r&b` are real single genres.

It now runs each token through `split_genre_value`, which is the app's existing
rule: safe separators (`; | \` and `,`) always split, while `/` and `&` are applied
only to a part the vocabulary does not recognise whole.

| value | before | after |
|---|---|---|
| `Emo / Screamo` | None | punk |
| `Metalcore / Post-Hardcore` | None | metal |
| `Crust Punk` | None | punk *(now a vocabulary entry too)* |
| `death/doom` | metal | metal *(unchanged)* |
| `Drum & Bass` | electronic | electronic *(unchanged)* |
| `R&B` | r_and_b | r_and_b *(unchanged)* |

## 4. Christmas by base genre

The seasonal branch prefixed the **leaf** genre:

```python
target_norm_key = f"christmas {norm_key}"   # "Christmas Nu Metal", "Christmas Metalcore"
```

It now uses the base genre, the same rule the covers builder follows:

```
Christmas Metal - Top Tracks     (Nu Metal + Metalcore tracks)
Christmas Punk - Top Tracks
Christmas Rock - Top Tracks
Christmas - Top Tracks           (a track whose only signal is the marker)
```

The leaf is kept as the fallback for a seasonal track the vocabulary cannot
resolve to a root, so it still gets a list rather than vanishing.

While here, two defects fell out:

* **Two catch-all lists.** "Holiday - Top Tracks" and "Christmas - Top Tracks"
  both existed; which one a track landed in depended on whether its genre was
  literally "Christmas" or it was only recognised from its title. Unified to one.
  The display is "Christmas" because `_playlist_allows_christmas` matches the
  marker as a substring of the playlist NAME — a bare "Holiday" name was never
  recognised as seasonal.
* **Seasonal tracks leaked into the ordinary rollups.** `playlist_pool_genres`
  strips the Christmas marker from `genres`, so re-detecting seasonality from that
  list answered "not seasonal" and put Christmas songs in "Metal". The entry now
  carries `_seasonal`, and the rollup pass skips it.

## 5. The 4★ floor

The floor is, and stays, `playlists.genre_playlists_min_stars` (default 4),
re-asserted on the row's own `stars` after row resolution (2026-11-20). The rollup
copies pool entries that have already passed it, so the floor is inherited — a
new test drives 2★ and 3★ rows through the real builder and asserts neither
reaches **any** list, leaf or rollup.

`_verify_genre_playlist`'s membership check now accepts a rollup: a `Metalcore`
row legitimately belongs in "Metal", so an exact-name match flagged every
non-leaf member as `GENRE_MISMATCH`. This was a check that fired on correct data.
The same applies to "Christmas <root>" pools, which are base-genre lists too — a
seasonal `Metalcore` track belongs in "Christmas Metal". A **leaf** pool
("Metalcore", "Christmas Metalcore") still demands an exact match, so the check
keeps its teeth.

## 6. The Navidrome import now WAITS instead of deferring

`sync_remote_navidrome_before_import` deferred the **whole** import whenever
`get_scan_status()` reported `scanning`:

```
⚠️ Navidrome Import deferred — Navidrome is still scanning or its state cannot be verified
```

A popularity/full scan requests a rescan at its end and holds a **300 s cooldown**
(`navidrome_rescan_service`), so Navidrome was still scanning for five minutes
after every scan — and the import deferred every time, importing nothing.

`trigger_and_wait_for_scan` already **drains** a running scan (Phase 1 waits for
`scanning == False` before requesting its own), so deferring was both redundant
and wrong. The import now notes that it is waiting and continues.

The deferral that **must** survive does: an unreachable Navidrome, or a scan that
fails or times out, still returns False — importing an unverified view is what
wrote stale release ids back to the DB.

## Tests

* `tests/test_genre_playlist_base_genre_rollups.py` — 43 cases: the taxonomy
  (metal/punk/rock families), compound tokens vs protected single genres, the
  three rollups, leaf lists surviving, the switch, the 4★ floor on both routes,
  and Christmas (base-genre grouping, no leaf-named list, seasonal exclusion from
  rollups, one catch-all).
* `tests/test_navidrome_import_waits_for_a_running_scan.py` — the deferral is
  gone, the wait precedes the trigger, and an unverified Navidrome still refuses.
* Updated `test_holiday_album_propagation.py`,
  `test_genre_pooling_and_weekly_rotation.py`, `test_playlist_quota_and_config_surface.py`
  (the new config key must be emitted by `config.js` or it is dropped on save).

## Config surface

`playlists.genre_playlists_parent_rollups` — new, default `true`. Toggle:
**Playlists → Genre Playlists → "Also Build Parent-Genre Playlists"**.
