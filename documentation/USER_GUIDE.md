# Popularr — User Guide

Popularr is a self-hosted music management companion for **Navidrome**. It reads
your library, scores every track for popularity, turns those scores into
1–5★ ratings, detects singles and cover songs, generates playlists, downloads
missing music over Soulseek (slskd), and pushes the ratings back into
Navidrome so every app that reads your library sees the same stars.

This guide explains how the application works, what the important settings do,
and — most importantly — **how the popularity pipeline turns raw listen counts
into star ratings, and how each configuration change affects that process.**

---

## 1. How the Application Works (Big Picture)

```text
Navidrome library ──► import ──► local database (tracks, artists, albums)
                                     │
          ┌──────────────────────────┼──────────────────────────┐
          ▼                          ▼                          ▼
   Popularity scan             Singles scan              Metadata scan
   (score every track)         (find the singles)        (MBIDs, genres, art)
          │                          │                          │
          └──────────────┬───────────┴───────────┬──────────────┘
                         ▼                       ▼
                 Star ratings (1–5★)      Cover detection + playlists
                         │
                         ▼
               Synced back to Navidrome (every user)
```

1. **Import** — Popularr scans Navidrome's library (or a watched downloads
   folder) and mirrors it into its own PostgreSQL database. It does not move or
   rename anything in your library unless you tell it to.
2. **Scans** — the Scan page offers six independent scan options. Each one
   updates a different part of the data (see §2).
3. **Ratings** — popularity scores are converted to 1–5★ ratings (§4) and
   written back to Navidrome, so Plex, Jellyfin, DSub and anything else that
   reads Navidrome ratings shows the same stars.
4. **Extras** — playlists are generated from high-rated tracks, missing
   releases can be downloaded via Soulseek, and corrections are offered when
   the metadata you have disagrees with MusicBrainz.

---

## 2. The Six Scan Options

Every scan starts from the **Scan** page (or the per-artist / per-album scan
selector). They are fully independent — running one does not run the others.

| Scan | What it does | Cost |
| --- | --- | --- |
| **Full** | Everything below, in sequence: basic metadata → popularity → singles → per-track metadata/covers/genres → full album enrichment (art, artist bios, similar artists) → album cover pass → star ratings | High |
| **Navidrome** | Re-imports the library (new/changed tracks, folders, tags) into the local database | Medium |
| **Popularity** | Scores every track using Last.fm / ListenBrainz / release age and re-rates it. **Only basic metadata is fetched** — the minimum needed for scoring | Medium |
| **Metadata** | The full metadata import: MusicBrainz IDs, release info, genres, album art, artist bios, similar artists, live/remix tagging, alternate takes | Medium |
| **Singles** | Re-runs single detection only — labels each track high/medium/low confidence single using Discogs/MusicBrainz/Last.fm evidence | Low–Medium |
| **Essentia** | On-device machine-learning mood & genre tagging (TensorFlow). No internet needed, but takes several seconds per file | High (local) |

### What a Full scan does, in order

The Full scan is the only option that runs everything. The passes are
**decoupled and ordered** so the expensive enrichment never blocks the things
that depend on it:

1. **Basic metadata** — album type detection (MusicBrainz release-group) and
   the artist MusicBrainz ID. Both are inputs to singles detection.
2. **Popularity scoring** — per track, from Last.fm + ListenBrainz + age.
3. **Singles detection** — per track, using the fresh scores and album type.
4. **Per-track metadata + cover + genre** — MusicBrainz recording lookups
   (batched per album), per-track cover checks, genre aggregation.
5. **Full album enrichment** — album art, artist biography/country, Last.fm
   artist tags, similar artists, Discogs artist ID, live/remix tagging,
   alternate-take marking.
6. **Album cover pass** — the full CoverDetector (ISRC → MusicBrainz cover
   relations → writer analysis → heuristics → work-history fallback); confirmed
   covers are renamed to `Title (Artist Cover)`.
7. **Star ratings** — assigned, persisted, and synced to Navidrome.

> Because the passes are decoupled, you can run "Singles" without re-scoring
> popularity, or "Metadata" without touching scores. Each has its own rescan
> window (§3).

---

## 3. Rescan Windows — When a Scan Skips an Album

Scans are incremental. An album that was already processed **within the skip
window** is skipped with a single log line — no API calls, no re-scoring, no
re-sync. This keeps repeat scans fast and kind to API rate limits.

Every scan option has its own window, in **Config → Analytics & Scoring →
Scan Behaviour**:

| Setting | Config key | Default | Meaning |
| --- | --- | --- | --- |
| Full Scan Rescan Window | `album_skip_days` | 7 | Full scans skip albums scanned within N days |
| Popularity Scan Window | `popularity_skip_days` | 7 | Popularity scans skip albums re-scored within N days |
| Singles Scan Window | `singles_skip_days` | 7 | Singles scans skip albums assessed within N days |
| Metadata Scan Window | `metadata_skip_days` | 0 | Metadata scans **always run** unless you raise this |
| Minimum Tracks for Skip Check | `album_skip_min_tracks` | 1 | Albums below this track count are not treated as valid albums |
| Skip Unchanged Albums | `skip_unchanged_albums` | True | Also skip when every track already has the data this scan would produce (scores / singles verdicts) |
| Run Singles on Skipped Albums | `run_singles_on_skipped_albums` | False | Opt-in: an album the window would skip is still visited when a track has **no** singles verdict, so a track the scan missed is never skipped for ever (costs Discogs/MusicBrainz lookups, for those tracks only) |
| Flag Cover Songs During Scans | `cover_detection_enabled` | True | Master toggle for the cover pass |
| Mature Track Freeze (years) | `mature_track_min_age_years` | 2 | Tracks at/above this age keep their stored popularity (no re-fetch) |

**The golden rule: `0` means "always run".** Set a window to 0 and that scan
will re-process every album every time.

**Skips never apply to:** forced scans (Force checkbox), individual album
scans, or artist-targeted scans from the artist page. Those always process.

---

## 4. How Popularity Is Calculated

This is the heart of the app. Every track gets a **final score** that is a
blend of three raw signals, then a series of corrections, and finally the score
is converted to a star rating.

### 4.1 The raw blend

```text
final_raw = (lastfm_weight  × lastfm_listeners)
          + (listenbrainz_weight × listenbrainz_listens)
          + (age_weight × age_score)
```

| Weight | Default | What it measures |
| --- | --- | --- |
| Last.fm | 0.55 | Scrobbles/listener counts for the track |
| ListenBrainz | 0.35 | Listen counts (requires the track's MusicBrainz ID) |
| Age | 0.10 | Release recency — newer releases get a small bump |

Weights are **normalised automatically**, so they don't need to sum to 1.
If a source has no data for a track (e.g. no ListenBrainz MBID), its weight is
silently ignored and the remaining sources carry the score. There is also a
**dynamic weighting** fallback that re-balances Last.fm vs ListenBrainz when
the two disagree strongly.

**Config:** Config → Analytics & Scoring → **Popularity Weights**.

### 4.2 Album-relative re-anchoring

Raw scores from different sources live on different scales, so the raw score
is **re-anchored against the album's own distribution**. Each track's final
score reflects how it stands out *within its album* (its **album z-score**),
not just its absolute popularity. This is why a 50-point track can be a 5★
album standout while a 70-point track on a stacked album is only a 3★.

### 4.3 Artist context

The same re-anchoring happens at the **artist-catalogue** level: every track
also gets an **artist z-score** against all of the artist's scored tracks.
The top of the catalogue (default **top 10%**, configurable) is marked as
"popularity standout" — marked tracks can earn 5★ without any single-detection
evidence. Artists with more than 30 scored tracks use a wider band (top 25%);
a MEDIUM-confidence single inside the widened band is bumped to HIGH → 5★.

### 4.4 Corrections applied to the score

| Adjustment | Default | Effect |
| --- | --- | --- |
| Single Boost | 1.15× | Confirmed singles get a score multiplier |
| Metadata Score Floor | 5.0 | Tracks with a confirmed MusicBrainz ID never score below this |
| Live Weight Penalty | 0.5 | Live tracks' Last.fm weight is halved |
| Organic Floor (score) | 45.0 | A confirmed single needs at least this score to earn 5★/4★-floor |
| Organic Floor (listeners) | 1,000 | …or at least this many Last.fm listeners |
| Mature Track Freeze | 2 yrs | Old tracks keep their stored popularity instead of re-fetching |

**Config:** Config → Analytics & Scoring → Single Detection & Star Ratings →
*Score Adjustments*.

### 4.5 Where to see the numbers

- **Track page** — final score, per-source scores, z-scores, single status.
- **Scan logs** — `/logs` (or the dashboard's scanning panel) shows each
  track's score, album/artist z-scores, and the exact weights used per album
  (`⚖️ WEIGHTS:` line).

---

## 5. Star Ratings (1–5★)

Ratings are assigned *after* the score pipeline, per album:

### 5★ paths

A track earns 5★ through **any** of:

- a **HIGH-confidence single** (confirmed by Discogs/MusicBrainz evidence) that
  clears the era's catalogue/album gates (§5.2);
- a **catalogue standout** (top-10% of the artist's discography, no single
  evidence needed);
- an **album+artist double standout** (album z ≥ 1.0 **and** artist z ≥ 1.2)
  that is also top-10% marked.

### The rating ladder (everything else)

The remaining tracks are rated purely by **album z-score** (no artist context):

| Stars | Album z-score | Meaning |
| --- | --- | --- |
| 4★ | ≥ 0.5 | Album standout / fan favourite |
| 3★ | ≥ −0.5 | Standard album track |
| 2★ | ≥ −1.2 | Deep cut / minor track |
| 1★ | below −1.2 | Filler / outlier |

An **epsilon buffer** (0.5 score points) lets tracks sitting exactly on a
boundary share the higher tier instead of splitting by a single scrobble.

**Config:** Config → Analytics & Scoring → Single Detection & Star Ratings →
*Star Ratings*.

### 5.2 Album scaling — era 5★ caps

Each album is classified into an **era** by how it performs against the
artist's own discography peak (`R_eff = album median × age-skew / peak`),
and each era limits how many 5★ a single album can carry:

| Era | Peak ratio | Album top-N | Max 5★ per album |
| --- | --- | --- | --- |
| Peak (career high) | ≥ 0.75 | 3 | 4 |
| Solid (core discography) | ≥ 0.40 | 3 | 3 |
| Minor (deep cuts / early-late career) | below 0.40 | 3 | 2 |

Surplus 5★ candidates are demoted to 4★ (weakest by album z first).

---

## 6. Single Detection

A track is a "single" when the evidence says so. Detection combines several
**sources**, each weighted high/medium/low (Low effectively disables that
source):

| Source | Default weight | Evidence |
| --- | --- | --- |
| Discogs | high | A single/EP release match |
| MusicBrainz | medium | A release-group single match |
| MusicBrainz (compilation) | medium | Match on compilation/single tracks |
| Discogs video | medium | Official-video match |
| Last.fm | medium | Single confirmation/tag |
| Radio Edit | medium | "Radio Edit" version match |

The confidence boundaries are **album z-scores**: ≥ 1.0 = HIGH, ≥ 0.6 =
MEDIUM, below = LOW. Compilation/Various-Artists albums skip the top-50%
popularity gate (every track is checked); single-artist albums only check the
top half of the album's popularity.

Singles drive: the **Single Boost**, the 5★ paths, "In Queue" behaviour on the
album page, and the alternate-take handling.

**Config:** Config → Analytics & Scoring → Single Detection & Star Ratings →
*Confidence Boundaries* and *Single Detection Sources*.

---

## 7. Genres & Similar Artists

Genres are aggregated across providers with weights (**Config → Analytics &
Scoring → Genre Aggregation**):

| Source | Weight |
| --- | --- |
| MusicBrainz | 0.40 |
| Discogs | 0.25 |
| AudioDB | 0.20 |
| Essentia | 0.20 |
| Last.fm | 0.10 |

Weights are normalised — only their relative size matters. A **synonyms** map
(`variant: canonical`, one per line) merges duplicates like "hip hop" → "hip-hop".
Similar artists come from Last.fm and ListenBrainz (thresholds under Config →
Matching → Last.fm Advanced).

---

## 8. Important Settings Tour

### System tab

- **Music Users** — the Navidrome connection(s): base URL, username, password,
  plus each user's Last.fm username and ListenBrainz token. Multi-user support:
  stars can sync to every user.
- **Logging** — verbosity (Debug writes `debug.log`; applies immediately).
- **Automation & schedulers** — watcher interval, auto-import, auto popularity,
  daily MusicBrainz release refresh, upcoming-releases automation.

### Integrations tab

- **API keys** — Last.fm, Discogs (token), ListenBrainz, AudioDB. MusicBrainz
  needs no key, but its toggle disables MB-powered singles detection, album
  lookups, upcoming-releases refreshes and MBID re-matching.
- **Artist Biography** — Wikidata disambiguation terms.

### Downloads tab

- **slskd (Soulseek)** — URL + API key, transfer timeouts.
- **Quality filter** — accept only FLAC / MP3 320kbps (tolerance 5 kbps);
  note the format toggles replace the stored priority list on save.
- **Watcher** — scans the downloads folder and imports completed files.
- **Retry / cleanup schedulers** — retry failed downloads, clean the queue.

### Files tab

- **Naming convention** — how downloaded/renamed files are organised
  (`{album_artist}/{year} - {album}/{track_number}. {artist} - {title}`).
- **Conversion** — FLAC → MP3 (bitrate, original-file handling).
- **Tag writing** — **the master toggle**: turn it off and Popularr becomes a
  read-only database scanner — no audio file is ever modified (great for
  SMB/NFS libraries). Ratings-only / fill-missing-only / embed-lyrics /
  preserve-timestamps refine what gets written.
- **Essentia** — model paths, mood threshold (0.005), genre threshold (15%),
  per-file timeout, mood/genre toggles, BPM import.

### Matching tab

- **Track / Queue matching** — fuzzy and score thresholds for matching local
  tracks to provider results.
- **Search filters** — keywords that strip edition markers like `(Remastered)`
  during searches (live/remix/acoustic variants are kept intact).
- **Last.fm Advanced** — min artist plays, min similarity, cache TTL, rate
  limit delay (raise this if you hit 429s), retries.

### Analytics & Scoring tab

- **Popularity Weights** (§4.1), **Scan Behaviour** (§3),
  **Single Detection & Star Ratings** (§5–6), **Genre Aggregation** (§7),
  **Album Scaling** (§5.2), **Upcoming Releases** (Wikipedia sources + daily
  MusicBrainz refresh windows).

### Playlists tab

- Essential playlists, genre top-tracks playlists, New Music playlist — with
  thresholds (create ≥ 100, delete < 80, top N = 500, min stars = 4, genres per
  track = 3) and name templates (`{genre}`, `{artist}` placeholders).

---

## 9. How Configuration Changes Take Effect

| Change | When it applies |
| --- | --- |
| Log level, automation toggles, watcher settings | Immediately on save |
| API keys / Navidrome credentials | Next API call (immediate) |
| Scoring weights, star bands, single-detection settings | **Next scan** (re-run the relevant scan — existing scores are not retroactively recomputed) |
| Skip windows | Next scan (no history is rewritten) |
| Naming convention / conversion / tag-writing | Next organise/rename operation |
| Essentia paths | Next Essentia scan |
| Playlist thresholds | Next scan that generates playlists |

**Important:** changing a weight or star band does not recalculate stored
scores. Re-run the **Popularity** scan (or Full) to apply new scoring settings
to the library. Set the relevant window to 0 temporarily if you want every
album reprocessed immediately.

---

## 10. Playlists

- **Auto-generated (per scan):** `{artist} - Essential Collection.m3u` from an
  artist's 4★/5★ tracks (needs more than 12 unique 4★/5★ tracks), `{genre} -
  Top Tracks.m3u` for genres clearing the create threshold, and a rolling
  `New Music.m3u` (most recently added 4★/5★ tracks, capped at 100).
- **Manual:** on any track's ⋮ menu → *Add to Playlist* (existing playlists or
  create a new one); the Playlists page imports Exportify CSVs and can
  **Export** any playlist as a ZIP of its local audio files (progress bar
  included).
- Playlists are files in `{MUSIC_FOLDER}/Playlists` — Navidrome picks them up
  on its next scan.

---

## 11. Downloads

Missing releases can be queued from MusicBrainz search, the album page
("Download Missing Tracks"), and recommended-playlist generation. Downloads
run through **slskd/Soulseek**; the queue page shows progress, retries failed
transfers, and the watcher imports completed files into the library using the
naming convention and conversion settings. The quality filter rejects
low-quality matches unless you disable it.

---

## 12. Day-to-Day Tips

- **First run:** complete the Setup wizard, then run a **Full** scan. Let it
  finish — scores and singles verdicts build up over successive scans as data
  arrives.
- **Keep ratings fresh:** the weekly rhythm is usually *one full scan per
  week* (default windows) plus the automatic daily/weekly schedulers.
- **Something looks wrong?** Run a **forced** scan of the album/artist
  (Force checkbox) — it bypasses every skip window and refreshes from the
  APIs.
- **Tracks missing MBIDs:** the album page's `💡 N Missing MBIDs` badge is
  clickable — it filters to the unlinked tracks; use **Link** to resolve them
  (better ListenBrainz scoring and cover matching).
- **Read-only library:** if you run an external tagger (Beets, Picard), switch
  **Tag Writing** off so scans never touch your files.
- **API rate limits:** if you see 429s in the logs, raise the Last.fm rate
  limit delay (Matching → Last.fm Advanced) and consider raising the rescan
  windows.
- **Logs:** `/logs` has a live stream, level filters, search, and ZIP-friendly
  download. The dashboard's scanning panel shows the current scan live.

---

*Config lives in `config.yaml` (mounted per your docker-compose); the
database is PostgreSQL. The documentation sidebar contains deeper technical
references for each subsystem.*
