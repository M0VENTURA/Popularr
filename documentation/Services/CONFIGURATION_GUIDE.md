# Configuration Guide for Popularr Services

This document lists all configurable parameters that have been identified across the services layer. These values can now be adjusted via `config.yaml` instead of being hardcoded in the source code.

## How to Configure

Add these sections to your `config.yaml` file. Only specify values you want to override - all other values will use sensible defaults.

## 1. Popularity Scoring

### Weights (`weights` or `popularity.weights`)
Controls how different popularity sources are weighted in the final score.

```yaml
popularity:
  weights:
    lastfm: 0.55        # Default: 0.55 (55%)
    listenbrainz: 0.35  # Default: 0.35 (35%)
    age: 0.10           # Default: 0.10 (10%)
  track_timeout_seconds: 30  # THE single scan-timeout knob: per-track allowance
                             # (default: 30, clamped 10-600; 0 = disable the whole
                             # safety net). Album budget = this * track count;
                             # artist budget = 1800s fixed overhead + this * the
                             # artist's total track count.
  scan_threads: 4             # Concurrent per-track workers during a popularity scan (default: 4, 1-8)
  prefetch_budget_seconds: 360  # Per-artist prefetch / post-singles enrichment budget (default: 360, clamped 120-1800)
```

**Note:** Values are automatically normalized to sum to 1.0. `popularity.weights` takes precedence; the top-level `weights` block is used as a fallback.

### Scoring Adjustments (`single_detection`)
Controls the boost/floor/penalty applied by the popularity scan pipeline.

```yaml
single_detection:
  zscore_medium_threshold: 0.6      # Medium↔Low confidence boundary (default: 0.6)
  zscore_high_threshold: 1.0        # High↔Medium confidence boundary (default: 1.0)
  standout_gap_z: 0.75              # Standout gap z-score (default: 0.75)
  single_boost: 1.15                # Score multiplier for confirmed singles (default: 1.15)
  metadata_score_floor: 5.0         # Min score for tracks with a confirmed MBID (default: 5.0)
  live_weight_penalty: 0.5          # Last.fm weight fraction for live tracks (default: 0.5)
  instrumental_weight_penalty: 0.8  # Last.fm weight fraction for instrumental versions (default: 0.8)
  popularity_5star_z_threshold: 2.0 # Popularity-only 5★ z threshold (default: 2.0)
  lb_unreliable_5star_threshold: 0.50 # LB percentile for Last.fm-unreliable 5★ rescue (default: 0.50)
```

### Standout Detection & Star Ratings (`single_detection`)
Controls z-score thresholds for standout track detection and star rating assignment.

```yaml
single_detection:
  album_zscore_threshold: 0.8      # Min z-score for standout detection (default: 0.8)
  artist_zscore_threshold: 2.2     # Min z-score for artist outliers (default: 2.2)
  artist_top_percentile: 0.10      # Top % of artist catalog for small artists (default: 0.10)
  artist_top_percentile_large: 0.25  # Top % for artists with > large threshold tracks (default: 0.25)
  artist_catalog_large_threshold: 30 # Catalogue size where the large top % kicks in (default: 30)
  artist_min_tracks: 10            # Min tracks for artist stats (default: 10)
  
  # Star rating criteria
  # 1-4★ are assigned from the album's own popularity z-score bands
  # (after 5★ singles/standouts are set): Z >= +0.5 → 4★, -0.5 <= Z < +0.5
  # → 3★, -1.2 <= Z < -0.5 → 2★, Z < -1.2 → 1★.
  star_5:
    album_z: 1.0
    artist_z: 1.2
    artist_pct: 0.10
  star_4:
    album_z: 0.5
    artist_z: 1.0
    artist_pct: 0.20
  star_3:
    album_z: -0.5
  star_2:
    album_z: -1.2
  star_1:
    album_z: -1.2
    default: true
```

### Scan Caching (`features`)
Controls how often mature tracks are re-scored, plus the per-scan-type rescan
windows.  Albums older than `old_album_age_months` (default 48) are **old
albums** and use the longer `*_old_album_skip_days` windows (default 30) —
their popularity changes far less often.  A singles scan refreshes stale
popularity whenever an album is outside the popularity window.

```yaml
features:
  mature_track_min_age_years: 2  # Tracks at/above this age keep existing popularity unless data missing
  album_skip_days: 7             # Full-scan rescan window (days)
  popularity_skip_days: 7        # Popularity-scan rescan window (days)
  singles_skip_days: 7           # Singles-scan rescan window (days)
  metadata_skip_days: 0          # Metadata-scan rescan window (days)
  album_skip_min_tracks: 1       # Minimum tracks for a valid album
  old_album_age_months: 48       # Album age (months) that makes an album "old"
  album_old_album_skip_days: 30  # Full-scan window for old albums
  popularity_old_album_skip_days: 30  # Popularity-scan window for old albums
  singles_old_album_skip_days: 30     # Singles-scan window for old albums
  metadata_old_album_skip_days: 30    # Metadata-scan window for old albums
```

### Single-Detection Source Confidence (`features.source_*_confidence`)
Controls which sources count as high / medium evidence in the single-detection
confidence decision (`low` excludes a source).

```yaml
features:
  source_discogs_confidence: "high"
  source_musicbrainz_confidence: "high"
  source_discogs_video_confidence: "medium"
  source_musicbrainz_compilation_confidence: "medium"
  source_lastfm_confidence: "medium"
  source_radio_edit_confidence: "medium"
```

## 2. Genre Aggregation

### Genre Source Weights (`genres.weights`)
Controls how much weight each genre source contributes to aggregated genres.

```yaml
genres:
  weights:
    musicbrainz: 0.40   # Default: 0.40 (most authoritative)
    discogs: 0.25       # Default: 0.25
    audiodb: 0.20       # Default: 0.20
    lastfm: 0.10        # Default: 0.10
    spotify: 0.05       # Default: 0.05
```

### Genre Synonyms (`genres.synonyms`)
Maps variant genre names to canonical forms.

```yaml
genres:
  synonyms:
    "hip hop": "hip-hop"
    "r&b": "rnb"
    "rhythm and blues": "rnb"
    # Add your own mappings here
```

## 3. Queue Matching

### Matching Thresholds (`queue.matching`)
Controls how queue items are matched to downloaded files.

```yaml
queue:
  matching:
    threshold: 0.65              # Min similarity score (default: 0.65)
    partial_match: 0.7           # Score for substring matches (default: 0.7)
    strict_duration_sec: 2       # Strict duration tolerance (default: 2)
    tolerance_duration_sec: 5    # Lenient duration tolerance (default: 5)
    soft_variants:               # Variants that don't prevent matching
      - edit
      - radio
      - version
      - mix
    hard_variants:               # Variants that indicate different versions
      - live
      - acoustic
      - remix
      - demo
      - instrumental
```

## 4. slskd Timeouts (`slskd.timeouts`)
Controls timeout behavior for slskd transfers.

```yaml
slskd:
  timeouts:
    min_retry_delay_minutes: 60           # Default: 60
    long_retry_delay_minutes: 1440        # Default: 1440 (24 hours)
    remotely_queued_timeout_minutes: 60   # Default: 60
    active_state_timeout_minutes: 240     # Default: 240 (4 hours)
    inter_item_delay_seconds: 5           # Default: 5
    
    # Per-state timeouts (minutes)
    state_timeouts:
      "Requested": 30
      "Queued, Remotely": 60
      "Queued, Locally": 60
      "Initializing": 120
      "InProgress": 240
      "Queued": 60
      "In Progress": 240
      "Downloading": 240
```

## 5. Last.fm Service (`lastfm`)
Controls Last.fm API behavior and caching.

```yaml
lastfm:
  min_artist_plays: 20          # Default: 20
  min_similarity_score: 0.46    # Default: 0.46
  max_similar_per_artist: 5     # Default: 5
  max_albums_per_artist: 5      # Default: 5
  recent_months: 3              # Default: 3
  cache_ttl_hours: 24           # Default: 24
  max_retries: 3                # Default: 3
  retry_backoff: 1.5            # Default: 1.5
  rate_limit_delay: 0.5         # Default: 0.5 seconds
```

## 6. Download Matching (`downloads.matching`)
Controls how downloaded files are matched to library tracks.

```yaml
downloads:
  matching:
    min_accept_score: 0.45              # Default: 0.45
    duration_tolerance_seconds: 5       # Default: 5
    early_accept_length_tolerance: 2    # Default: 2
    top_n_candidates: 50                # Default: 50
```

## 7. Filesystem Settings (`filesystem`)
Controls file handling and supported formats.

```yaml
filesystem:
  audio_formats:
    - mp3
    - flac
    - m4a
    - ogg
    - wav
    - aac
    - wma
```

**Note:** If specified, this completely overrides the default list.

## 8. Wikidata Entity Disambiguation (`wikidata`)
Controls terms used to identify musician entities.

```yaml
wikidata:
  musician_terms:
    - singer
    - musician
    - band
    - rapper
    # ... (adds to default list, doesn't replace)
```

## Migration Status

The following services have been updated to use centralized configuration:

- ✅ `services/popularity/popularity_config.py` - Already uses config
- ✅ `services/enrichment/genre_aggregation_service.py` - **TODO**: Update to use `get_genre_weights()` and `get_genre_synonyms()`
- ✅ `services/queue/queue_metadata_matcher.py` - **TODO**: Update to use `get_queue_matching_config_v2()`
- ✅ `services/queue/queue_config.py` - **TODO**: Update to use `get_slskd_timeouts()`
- ✅ `services/enrichment/lastfm_service.py` - **TODO**: Update to use `get_lastfm_config()`
- ✅ `services/downloads/match_engine.py` - **TODO**: Update to use `get_download_matching_config()`
- ✅ `services/infrastructure/filesystem_service.py` - **TODO**: Update to use `get_supported_audio_formats()`
- ✅ `services/enrichment/artist_bio_service.py` - **TODO**: Update to use `get_musician_terms()`

## Benefits of Centralized Configuration

1. **No Code Changes Required**: Adjust behavior by editing `config.yaml` instead of modifying Python files
2. **Consistent Defaults**: All services use the same default values
3. **Type Safety**: Configuration getters ensure proper type conversion
4. **Documentation**: All configurable values are documented in one place
5. **Testing**: Easier to test different configurations without code changes

## Example Complete Configuration

```yaml
# config.yaml

popularity:
  weights:
    lastfm: 0.6
    listenbrainz: 0.3
    age: 0.1

single_detection:
  album_zscore_threshold: 1.0
  artist_zscore_threshold: 2.5
  star_5:
    album_z: 1.2
    artist_z: 1.5

genres:
  weights:
    musicbrainz: 0.5
    discogs: 0.3
    lastfm: 0.2

queue:
  matching:
    threshold: 0.7
    tolerance_duration_sec: 8

slskd:
  timeouts:
    min_retry_delay_minutes: 120
    active_state_timeout_minutes: 180

lastfm:
  cache_ttl_hours: 48
  max_retries: 5
```
