"""
Configuration helpers for Popularr.

Handles loading YAML config + caching.
"""

from __future__ import annotations
from typing import Any
import yaml
import os
import re

# -----------------------------------------------------------------------------
# Cache
# -----------------------------------------------------------------------------

_CONFIG_CACHE = None


# -----------------------------------------------------------------------------
# Core loader
# -----------------------------------------------------------------------------

#: How close two lengths must be to count as the SAME recording, in seconds.
#: The default every duration check used to hardcode for itself.
_LENGTH_TOLERANCE_DEFAULT_SECONDS = 5.0

#: Bounds for a configured tolerance. Zero is allowed and means "exact match
#: only" — a legitimate choice for a library of untouched rips. A negative
#: tolerance is meaningless, and an enormous one would disable the check
#: entirely while still LOOKING configured, so both ends are clamped.
_LENGTH_TOLERANCE_MAX_SECONDS = 120.0


def get_length_tolerance_seconds() -> float:
    """How close two durations must be to count as the same recording.

    Config section: ``queue.matching.tolerance_duration_sec`` in config.yaml

    ⭐ ONE NUMBER, ONE PLACE. This tolerance is asked in FIVE places that each
    used to answer it differently, and the disagreements were user-visible:

    * the album page's "Length differs" notice (5s)
    * the download queue's pre-copy verify (20s)
    * whether an existing library file may stand in for a download (a 2% ratio)
    * the candidate scorer's length bonus (a 10% ratio, still a bonus)
    * the queue metadata matcher's strong-reject (a hardcoded 30s)

    A file 19 seconds off a 4-minute track therefore PASSED the queue's own
    acceptance gate while the album page reported it as a different version of
    the recording. Two verdicts, one file.

    ⚠️ THE SECTION IS ``queue.matching`` BECAUSE THAT KEY ALREADY HAD A UI. Two
    other sections (``matching.duration_tolerance_seconds`` and
    ``downloads.matching.duration_tolerance_seconds``) carry the same name and
    are read by NOBODY — they were configurable on paper only. Rather than add a
    fourth, this reads the one a user can actually see and set on the Config
    page, and the dead pair is documented as superseded.

    NOTE: ``queue.matching.strict_duration_sec`` (2s) is a DIFFERENT thing and is
    deliberately not touched — it is the high-confidence ACCEPT band ("exact
    title + this close == very strong evidence"), not a tolerance for rejecting.

    Returns seconds as a float. An unreadable config, a bad value or a missing
    key all fall back to 5.0 rather than raising: a matching check must never
    fail the operation it is checking.
    """
    try:
        raw = (
            (get_config() or {})
            .get("queue", {})
            .get("matching", {})
            .get("tolerance_duration_sec", _LENGTH_TOLERANCE_DEFAULT_SECONDS)
        )
        value = float(raw)
    except (TypeError, ValueError):
        return _LENGTH_TOLERANCE_DEFAULT_SECONDS
    except Exception:
        return _LENGTH_TOLERANCE_DEFAULT_SECONDS
    if value < 0:
        return 0.0
    return min(value, _LENGTH_TOLERANCE_MAX_SECONDS)


def get_matching_config() -> dict[str, Any]:
    """Legacy shim over ``get_length_tolerance_seconds`` — see the note there.

    Kept because the key set is referenced in the docs; it no longer reads
    ``matching.*`` itself, so the two getters cannot drift apart.
    """
    cfg = get_config()

    matching = cfg.get("matching", {})

    return {
        "min_accept_score": matching.get(
            "min_accept_score",
            0.45,
        ),
        "duration_tolerance_seconds": get_length_tolerance_seconds(),
        "early_accept_length_tolerance": matching.get(
            "early_accept_length_tolerance",
            2,
        ),
        "top_n_candidates": matching.get(
            "top_n_candidates",
            50,
        ),
    }


def _read_yaml(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
            return yaml.safe_load(content) or {}, content
    except FileNotFoundError:
        return {}, ""
    except yaml.YAMLError:
        return {}, ""


# ---------------------------------------------------------------------------
# Environment variable overrides
# ---------------------------------------------------------------------------
# Any ``POPULARLR_*`` env var overrides a config.yaml key at load time.
# This lets operators set critical values without a config file.
#
# Mapping rules (examples):
#   POPULARLR_NAV_URL            → navidrome_users[0].base_url
#   POPULARLR_NAV_USER           → navidrome_users[0].user
#   POPULARLR_NAV_PASS           → navidrome_users[0].pass
#   POPULARLR_LASTFM_API_KEY     → api_integrations.lastfm.api_key
#   POPULARLR_DISCOGS_TOKEN      → api_integrations.discogs.token
#   POPULARLR_DOWNLOADS_FOLDER   → downloads.folder
#   POPULARLR_AUTO_IMPORT        → watcher.auto_import_enabled
#   POPULARLR_SCAN_INTERVAL      → watcher.scan_interval
#   POPULARLR_ESSENTIA_ENABLED   → essentia.tag_moods
#
# Boolean env vars accept "1", "true", "yes" (case-insensitive) for True,
# everything else is False.
# ---------------------------------------------------------------------------

_POPULARLR_PREFIX = "POPULARLR_"
_TRUE_STRINGS = frozenset({"1", "true", "yes"})


def _apply_env_overrides(cfg: dict) -> None:
    """Mutate ``cfg`` in-place with values from ``POPULARLR_*`` env vars."""

    # ── Navidrome (first user) ───────────────────────────────────────
    nav_url = _pop_env("NAV_URL")
    nav_user = _pop_env("NAV_USER")
    nav_pass = _pop_env("NAV_PASS")
    if nav_url or nav_user or nav_pass:
        users = cfg.setdefault("navidrome_users", [])
        if not users:
            users.append({})
        first = users[0]
        if nav_url:
            first["base_url"] = nav_url
        if nav_user:
            first["user"] = nav_user
        if nav_pass:
            first["pass"] = nav_pass
        if not first.get("display_name"):
            first["display_name"] = first.get("user", "Admin")

    # ── API integrations ─────────────────────────────────────────────
    _set_if("LASTFM_API_KEY", cfg, "api_integrations", "lastfm", "api_key")
    _set_if("DISCOGS_TOKEN", cfg, "api_integrations", "discogs", "token")
    _set_if("AUDIODB_API_KEY", cfg, "api_integrations", "audiodb", "api_key")

    # ── Downloads ────────────────────────────────────────────────────
    _set_if("DOWNLOADS_FOLDER", cfg, "downloads", "folder")

    # ── Watcher / automation ─────────────────────────────────────────
    _set_bool_if("AUTO_IMPORT", cfg, "watcher", "auto_import_enabled")
    _set_bool_if("AUTO_POPULARITY_SCAN", cfg, "watcher", "auto_popularity_scan")
    _set_int_if("SCAN_INTERVAL", cfg, "watcher", "scan_interval")

    # ── Essentia ─────────────────────────────────────────────────────
    _set_bool_if("ESSENTIA_ENABLED", cfg, "essentia", "tag_moods")
    _set_if("ESSENTIA_MODELS_DIR", cfg, "essentia", "models_dir")
    _set_if("ESSENTIA_SCRIPT_PATH", cfg, "essentia", "script_path")


def _pop_env(suffix: str) -> str | None:
    """Read a ``POPULARLR_<suffix>`` env var, or return None.

    Reads WITHOUT removing the variable: ``os.environ.pop`` previously
    consumed the value on the first config load, so any later
    ``clear_config_cache()`` (config save from another path, partial saves,
    runtime reloads) silently lost the env overrides and fell back to the
    file's values.  Env overrides are now deterministic — they win on every
    load until the variable is unset, matching how the rest of the app
    treats environment configuration.
    """
    val = os.environ.get(f"{_POPULARLR_PREFIX}{suffix}", "").strip()
    return val if val else None


def _set_if(env_suffix: str, cfg: dict, *keys: str) -> None:
    """Set ``cfg[keys[0]][keys[1]]... = env_val`` if the env var exists."""
    val = _pop_env(env_suffix)
    if val is not None:
        d = cfg
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = val


def _set_bool_if(env_suffix: str, cfg: dict, *keys: str) -> None:
    """Like ``_set_if`` but converts the value to a bool."""
    raw = _pop_env(env_suffix)
    if raw is not None:
        d = cfg
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = raw.lower() in _TRUE_STRINGS


def _set_int_if(env_suffix: str, cfg: dict, *keys: str) -> None:
    """Like ``_set_if`` but converts the value to an int."""
    raw = _pop_env(env_suffix)
    if raw is not None:
        try:
            d = cfg
            for k in keys[:-1]:
                d = d.setdefault(k, {})
            d[keys[-1]] = int(raw)
        except ValueError:
            pass


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------

def get_config() -> dict[str, Any]:
    """
    Load config.yaml with caching, with Pydantic Settings as defaults.

    Pydantic ``Settings`` (loaded from ``POPULARLR_*`` env vars) provide
    the base values.  The YAML file overrides those defaults.  Finally,
    legacy ``POPULARLR_*`` env vars (via ``_apply_env_overrides``) can
    still override everything.

    This layered approach:
    1. Settings (env vars + code defaults) — type-safe, auto-complete
    2. ``config.yaml`` — file-based overrides
    3. Legacy ``_apply_env_overrides`` — backward compatibility
    """

    global _CONFIG_CACHE

    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE

    # ── Layer 1: Pydantic Settings (env vars + code defaults) ──────
    from helpers.settings import get_settings
    s = get_settings()

    cfg: dict[str, Any] = {
        "pg_host": s.pg_host,
        "pg_port": s.pg_port,
        "pg_user": s.pg_user,
        "pg_password": s.pg_password,
        "pg_database": s.pg_database,
        "database_url": s.database_url,
        "db_pool_size": s.db_pool_size,
        "db_pool_overflow": s.db_pool_overflow,
        "db_pool_timeout": s.db_pool_timeout,
        "music_root": s.music_root,
        "music_folder": s.music_folder,
        "downloads_folder": s.downloads_folder,
        "config_path": s.config_path,
        "log_path": s.log_path,
        "nav_url": s.nav_url,
        "nav_user": s.nav_user,
        "nav_pass": s.nav_pass,
        "lastfm_api_key": s.lastfm_api_key,
        "lastfm_api_secret": s.lastfm_api_secret,
        "discogs_token": s.discogs_token,
        "essentia_enabled": s.essentia_enabled,
        "essentia_script_path": s.essentia_script_path,
        "essentia_models_dir": s.essentia_models_dir,
        "auto_import": s.auto_import,
        "bind": s.bind,
        "workers": s.workers,
        "log_level": s.log_level,
        "state_dir": s.state_dir,
        "use_local_assets": s.use_local_assets,
    }

    # ── Layer 2: YAML file ─────────────────────────────────────────
    config_path = os.environ.get("CONFIG_PATH", s.config_path)
    yaml_cfg, _ = _read_yaml(config_path)
    _deep_merge(cfg, yaml_cfg)

    # ── Layer 3: Legacy env var overrides ──────────────────────────
    _apply_env_overrides(cfg)

    # ── Layer 4: Sync flat Pydantic keys to nested api_integrations ──
    # Required so standard Docker environment variables map to the nested
    # structures expected by the pipeline APIs (Discogs and Last.fm)
    api_int = cfg.setdefault("api_integrations", {})

    if cfg.get("lastfm_api_key") and not api_int.get("lastfm", {}).get("api_key"):
        api_int.setdefault("lastfm", {})["api_key"] = cfg["lastfm_api_key"]
    if cfg.get("lastfm_api_secret") and not api_int.get("lastfm", {}).get("api_secret"):
        api_int.setdefault("lastfm", {})["api_secret"] = cfg["lastfm_api_secret"]

    if cfg.get("discogs_token") and not api_int.get("discogs", {}).get("token"):
        api_int.setdefault("discogs", {})["token"] = cfg["discogs_token"]

    _CONFIG_CACHE = cfg

    return _CONFIG_CACHE


def _deep_merge(base: dict, override: dict) -> None:
    """Merge ``override`` into ``base`` recursively (in-place)."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def clear_config_cache():
    """Force reload on next get_config()"""
    global _CONFIG_CACHE
    _CONFIG_CACHE = None


def get_navidrome_config():
    """Return Navidrome connection settings.

    Delegates to :func:`get_navidrome_first_user`, which resolves the first
    USABLE Navidrome account in priority order:

      1. First user in ``navidrome_users`` with base_url + user + pass set —
         this is where the config UI and setup wizard write real credentials.
      2. The legacy top-level ``navidrome`` dict — but ONLY when it carries
         real connection fields.  Since the config UI also persists
         operational flags there (``auto_public_playlists``,
         ``playlist_cover_art``), a bare ``navidrome:`` section with no
         ``base_url`` must NOT be returned — otherwise callers build a
         Navidrome client with ``base_url=None`` and every request fails
         with "Navidrome base_url is empty".
      3. The flat settings/env keys (``nav_url``/``nav_user``/``nav_pass``,
         i.e. ``POPULARLR_NAV_URL`` etc).

    Returns:
        Dict with ``base_url``/``user``/``pass`` keys, or ``{}`` when no
        usable Navidrome connection is configured.
    """
    return get_navidrome_first_user()


def needs_setup(cfg: dict | None = None) -> bool:
    """True while first-run setup hasn't been completed (no usable Navidrome).

    Setup is considered complete once a Navidrome user with ``base_url`` +
    ``user`` + ``pass`` exists, either in the multi-user ``navidrome_users``
    list, the legacy single-user ``navidrome`` dict, or the flat settings/env
    keys (``nav_url`` / ``nav_user`` / ``nav_pass``).  This mirrors the
    legacy ``_needs_setup`` helper so the auth gate and the setup wizard
    agree on whether the first-run flow is still pending.
    """
    cfg = cfg if cfg is not None else get_config()
    nav_users = cfg.get("navidrome_users", [])
    if isinstance(nav_users, list) and nav_users:
        first = nav_users[0]
        return not all([first.get("base_url"), first.get("user"), first.get("pass")])
    nav = cfg.get("navidrome", {}) or {}
    if nav:
        return not all([nav.get("base_url"), nav.get("user"), nav.get("pass")])
    # Settings/env-provided flat keys (POPULARLR_NAV_URL / _USER / _PASS).
    return not all([cfg.get("nav_url"), cfg.get("nav_user"), cfg.get("nav_pass")])


# -----------------------------------------------------------------------------
# Optional (used in app.txt)
# -----------------------------------------------------------------------------

def get_all_services_status():
    """
    Placeholder — returns enabled services.
    (Extend later if needed)
    """
    cfg = get_config()

    return {
        k: v for k, v in cfg.items()
        if isinstance(v, dict)
    }


def is_service_enabled(service_name: str) -> bool:
    cfg = get_config()
    service = cfg.get(service_name, {})

    if isinstance(service, dict):
        return service.get("enabled", False)

    return False


# -----------------------------------------------------------------------------
# Config persistence & helpers
# ---------------------------------------------------------------------------

_CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yaml")


def get_watcher_settings() -> dict[str, Any]:
    """Get watcher service settings from config.

    Returns:
        Dict with watcher settings or defaults.
    """
    cfg = get_config()
    watcher = cfg.get("watcher", {})
    return {
        "scan_interval": int(watcher.get("scan_interval", 30)),
        "navidrome_sync_wait": int(watcher.get("navidrome_sync_wait", 600)),
        "auto_import_enabled": bool(watcher.get("auto_import_enabled", True)),
        "auto_popularity_scan": bool(watcher.get("auto_popularity_scan", True)),
        "downloads_watcher_enabled": bool(watcher.get("downloads_watcher_enabled", True)),
    }


def read_config_from_disk() -> dict[str, Any]:
    """Read the on-disk config, bypassing the in-memory cache.

    Exists so a writer can carry across sections it does not itself produce.
    ``save_config`` REPLACES ``config.yaml`` wholesale, and the Config page's
    collector only emits the sections it renders — so any top-level section the
    page does not know about would be **deleted** by an ordinary save. The live
    example was ``scheduler:``, which held every scheduled-scan interval: they
    were silently reset to their defaults each time Settings was saved.
    """
    try:
        cfg, _ = _read_yaml(_CONFIG_PATH)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def save_config(config_data: dict) -> bool:
    """Persist a config dict back to the YAML file and clear the cache.

    Args:
        config_data: Complete configuration dictionary to save.

    Returns:
        True on success, False on failure.
    """
    try:
        os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(config_data, f, default_flow_style=False, sort_keys=False)
        clear_config_cache()
        return True
    except Exception as exc:
        # Previously a bare `return False` — a failed save gave the caller no
        # reason at all, so a permissions/disk error looked identical to a
        # validation refusal and nothing appeared in the log.
        _log_config_error("save_config failed", _CONFIG_PATH, exc)
        return False


def save_partial_config(partial_data: dict) -> bool:
    """Merge partial configuration into the existing config and persist.

    Reads the current config from the YAML file (bypassing the in-memory
    cache), deep-merges *partial_data* into it, writes the result back,
    and clears the cache so the next ``get_config()`` call picks it up.

    Args:
        partial_data: A subset of configuration keys to merge in.

    Returns:
        True on success, False on failure.
    """
    try:
        # Read the current on-disk config (or start with an empty dict)
        existing, _ = _read_yaml(_CONFIG_PATH)
        _deep_merge(existing, partial_data)
        os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(existing, f, default_flow_style=False, sort_keys=False)
        clear_config_cache()
        return True
    except Exception as exc:
        _log_config_error("save_partial_config failed", _CONFIG_PATH, exc)
        return False


def _log_config_error(message: str, path: str, exc: Exception) -> None:
    """Best-effort error logging for config writes.

    Imported lazily and defensively: config helpers are imported very early
    in startup, potentially before logging is configured, so a failure here
    must never mask the original config error.
    """
    try:
        import structlog
        structlog.get_logger(__name__).warning(
            message,
            config_path=path,
            error=f"{type(exc).__name__}: {exc}",
        )
    except Exception:
        try:
            print(f"[config_helpers] {message} ({path}): {type(exc).__name__}: {exc}")
        except Exception:
            pass


# Minimum candidate score for a Soulseek result to be accepted as a valid match.
# Scores below this threshold trigger fallback queries (e.g. using album_artist).
_SLSKD_MIN_ACCEPT_SCORE = 0.45

# Automatic-search performance tuning: track-length-aware candidate pruning.
# Only the top-N candidates (after length-based prioritisation) receive full
# scoring.  This prevents candidate explosion on popular/ambiguous queries.
_AUTO_SEARCH_TOP_N_CANDIDATES = 50

# Early-acceptance length tolerance: when artist and title match exactly and
# the candidate duration is within this many seconds, the match is accepted
# immediately without scoring the remaining candidates.
_AUTO_SEARCH_EARLY_ACCEPT_LENGTH_TOLERANCE = 2


# Shared constants for orphan-token detection used in both
# _score_soulseek_candidate and _filename_matches_queue_item.
_ORPHAN_AUDIO_EXT_TOKENS = frozenset(
    {"mp3", "flac", "wav", "ogg", "aac", "m4a", "wma", "opus", "aiff"}
)
_ORPHAN_NUM_RE = re.compile(r'^\d{1,4}$')

# Artist names that indicate a compilation/various-artists release.  When both
# the queue item's artist and album_artist are one of these values the
# individual track artist is unknown and any non-empty file artist is accepted
# by the metadata matcher.
_GENERIC_COMPILATION_ARTISTS = frozenset({
    'various artists', 'various artist', 'various', 'va', 'v/a',
    'unknown artist', 'unknown',
    'soundtrack', 'ost',
})

# Strips "feat."/"ft."/"featuring" suffixes from artist strings when building
# fallback search queries so that "KNEECAP feat. Fawzi" becomes "KNEECAP".
_FEAT_SUFFIX_RE = re.compile(
    r'\s+(?:feat\.?|ft\.?|featuring)\s+.*$',
    re.IGNORECASE,
)

# Strip leading disc-track prefixes (e.g. "1-15 - ", "16. ", "07 ") and trailing
# Soulseek UID suffixes (e.g. "_639091010921933965") so that the core title
# and artist can be matched against the cleaned basename.
_TRACK_NUMBER_PREFIX_RE = re.compile(
    r'^(?:\d+-\d+|\d+)\s*[\.\s\-]*\s*',
)
_SOULSEEK_UID_SUFFIX_RE = re.compile(
    r'_\d{12,}$',
)


# Music-directory filesystem index cache (used by check_target_folder_exists)
# so large libraries are not walked repeatedly during batch pre-scans.
_MUSIC_DIR_FILES_CACHE_TTL_SECONDS = 60.0
_music_dir_files_cache: list[str] | None = None
_music_dir_files_cache_ts: float = 0.0

# Downloads-directory file list cache (used by _cleanup_sibling_downloads)
# to avoid repeated recursive walks.
_DOWNLOADS_DIR_FILES_CACHE_TTL_SECONDS = 30.0
_downloads_dir_files_cache: list[str] | None = None
_downloads_dir_files_cache_ts: float = 0.0

# Background pre-scan task tuning
_PRE_SCAN_INTERVAL_SECONDS = 120
_PRE_SCAN_BATCH_SIZE = 50

# Maximum seconds to wait for a Soulseek search to complete.  Polling stops as
# soon as slskd reports is_complete=True, so this is only a safety ceiling for
# searches that never finish (e.g. slskd unreachable mid-search).
_SLSKD_SEARCH_MAX_WAIT_SECONDS = 150

# Reduced maximum wait for fallback queries.  Fallback searches are lower-
# specificity alternatives tried when the primary query found nothing; a
# shorter ceiling limits the worst-case per-track search time.  Kept SHORT
# (20s) — the fallback explosion guard caps queries per track, and each
# fallback waiting 60s still burned 10-60 min/track for un-locatable
# (often pre-release) tracks in the search log.
_SLSKD_FALLBACK_SEARCH_MAX_WAIT_SECONDS = 20


def get_queue_search_tuning() -> dict[str, Any]:
    """Get queue search duration tuning (Config section ``queue``).

    Keys (all under ``queue`` in config.yaml):

    - ``search_max_wait_seconds`` (150): ceiling for the PRIMARY query.
    - ``fallback_search_max_wait_seconds`` (20): ceiling for fallback queries.
    - ``search_empty_abort_seconds`` (90): a search that has streamed ZERO
      results for this long is CANCELLED early instead of being waited out —
      slskd keeps empty searches ``InProgress`` until they time out, and the
      full ceiling on every query is what made a hopeless track burn ~9
      minutes (150s + 20 x 20s).  ``0`` disables the early abort.
    - ``search_empty_shrink_after`` (4): after this many CONSECUTIVE empty
      queries, the remaining queries wait only
      ``search_shrunken_wait_seconds`` — every more-specific variant already
      failed to surface anything, so the tail of the fallback list should not
      burn the full ceiling each.
    - ``search_shrunken_wait_seconds`` (8): the reduced wait once the shrink
      threshold has been reached.
    """
    cfg = get_config()
    queue = cfg.get("queue", {}) if isinstance(cfg, dict) else {}
    return {
        "search_max_wait_seconds": int(
            queue.get("search_max_wait_seconds", _SLSKD_SEARCH_MAX_WAIT_SECONDS)
            or _SLSKD_SEARCH_MAX_WAIT_SECONDS
        ),
        "fallback_search_max_wait_seconds": int(
            queue.get("fallback_search_max_wait_seconds", _SLSKD_FALLBACK_SEARCH_MAX_WAIT_SECONDS)
            or _SLSKD_FALLBACK_SEARCH_MAX_WAIT_SECONDS
        ),
        "search_empty_abort_seconds": float(
            queue.get("search_empty_abort_seconds", 90) or 0
        ),
        "search_empty_shrink_after": int(queue.get("search_empty_shrink_after", 4) or 0),
        "search_shrunken_wait_seconds": int(queue.get("search_shrunken_wait_seconds", 8) or 0),
    }


# Cached SlskdClient instance — config does not change at runtime so we build
# the object once and reuse it for the lifetime of the process.
_slskd_client_cache = None


def get_metadata_config():
    """Return metadata extraction configuration."""
    return {
        "enabled": True,
        "enable_flac": True,
        "enable_musicbrainz_tags": True,
        "enable_extended_tags": True,
    }


def get_queue_matching_config_legacy() -> dict:
    """Legacy queue matching config getter (deprecated).

    Deprecated: Use get_queue_matching_config_v2() instead.
    """
    cfg = get_config()

    matching = cfg.get("queue_matching", {}) or {}

    return {
        "detect_live_tracks": bool(
            matching.get(
                "detect_live_tracks",
                True,
            )
        ),

        "detect_remix_tracks": bool(
            matching.get(
                "detect_remix_tracks",
                True,
            )
        ),

        "detect_compilations": bool(
            matching.get(
                "detect_compilations",
                True,
            )
        ),

        "title_variant_tokens": set(
            matching.get(
                "title_variant_tokens",
                [
                    "acoustic",
                    "demo",
                    "edit",
                    "instrumental",
                    "intro",
                    "live",
                    "mix",
                    "orchestral",
                    "radio",
                    "remaster",
                    "remastered",
                    "remix",
                    "version",
                ],
            )
        ),

        "soft_variant_tokens": set(
            matching.get(
                "soft_variant_tokens",
                [
                    "version",
                    "edit",
                    "radio",
                ],
            )
        ),

        "compilation_artists": set(
            matching.get(
                "compilation_artists",
                [
                    "various artists",
                    "various artist",
                    "various",
                    "va",
                    "v/a",
                    "unknown artist",
                    "unknown",
                    "soundtrack",
                    "ost",
                ],
            )
        ),
    }


# -----------------------------------------------------------------------------
# Standout / star rating configuration
# -----------------------------------------------------------------------------

# Album RATIO standout defaults (the "shape of popularity" 5★ gate).
#
# These are MULTIPLIERS, not z-scores or raw counts. A z-score measures
# deviation relative to the album's own spread, which SHRINKS on a small flat
# album — so a 3-listener gap on counts of 8..14 looks like a large z-score
# while the numbers are indistinguishable from noise. Ratios are scale-free:
# "is #1 three times the median?" means the same at 100 listeners or 10M.
#
#   1. runner_up — top / #2.      Is #1 separated from its nearest rival?
#   2. median    — top / median.  How far above the album's standard fare?
#   3. floor     — top / bottom.  Total dynamic range of the album.
#
# `min_passed` is how many of the three must hold (default: all three).
# Lower it to require only two — e.g. a live album often has a flat bottom
# (crowd noise on every track) which makes the floor ratio weak even when the
# hit is obvious.
#
# Reference case (Anti-Flag acoustic; counts 14, 13, 13, ~11, 8):
#   runner_up 1.07x (needs 1.5)  median 1.27x (needs 3.0)  floor 1.75x (needs 10)
#   -> fails all three, correctly: no standout on that album.
_DEFAULT_ALBUM_RATIO_STANDOUT: dict[str, float] = {
    "enabled": 1,
    "runner_up_min": 1.5,
    "median_min": 3.0,
    "floor_min": 10.0,
    "min_passed": 3,
    "live_runner_up_min": 1.5,
    "live_median_min": 2.0,
    "live_floor_min": 5.0,
    "live_min_passed": 2,
}

# Live-album rating defaults.
#
# Live releases are rated against the ARTIST's catalogue (artist-z), not
# against their own tracks.  Album-relative z is self-referential: it lets the
# best cut on a mediocre live record score like the best cut on a landmark
# studio album.  Most live tracks sit at negative artist-z and fall naturally
# into the 1-3 bands; genuine singles rise on their own merit.
#
# Album-z acts ONLY as a gate (blocking 4-5★ when a weak-catalogue artist's
# live album merely beats their own low median) and as a promotion path (a
# decisive standout within its own release that is also respectable
# catalogue-wide).  It never lifts a rating on its own.
#
# These are surfaced here so the whole block has sane values with NO config
# file present.  finalise_stage applies its own identical fallbacks per key,
# so a partial user block is merged rather than replacing the lot.
_DEFAULT_LIVE_ALBUM_SCALING: dict[str, Any] = {
    "enabled": True,
    "mode": "artist_and_album",   # artist_and_album | artist_z
    "max_stars": 3,               # ceiling for an ordinary live track
    "artist_z_star5": 1.50,
    "artist_z_star4": 1.00,
    "artist_z_star3": -0.25,
    "artist_z_star2": -1.20,
    "album_z_gate": 2.00,         # gate AND promotion threshold
    "promote_artist_z_min": 0.50,
    "min_catalogue": 8,           # below this, artist-z is noise
    "max_5star_slots": 1,
    "max_4star_slots": 2,
    "track_fraction": 0.60,       # share of live tracks that marks a release
}


# Compilation rating against the artist's ONLINE global catalogue.
#
# A compilation ("Various Artists") groups many credited recording artists
# under one shared folder. Each track must be rated against ITS OWN artist's
# catalogue -- but the local database only knows the songs this library
# happens to own, which for a soundtrack/compilation is often one or two per
# artist. With no local distribution to rank against, a genuinely huge song
# (the artist's global #1) cannot be distinguished from filler.
#
# This block lets the compilation rater consult the artist's catalogue ONLINE
# (Last.fm ``artist.getTopTracks``, ordered by global playcount) and score the
# track by its RANK within it. ``rank_percentile_5star`` is the cut-off: a
# track inside the artist's top 2% worldwide is their signature song.
#
# ``min_local_catalogue`` mirrors the existing thin-catalogue threshold: when
# the local catalogue already has this many usable scores it is trusted and the
# online lookup is skipped, so a well-represented artist's ratings do not
# change.
_DEFAULT_COMPILATION_ONLINE_CATALOGUE: dict[str, Any] = {
    "enabled": 1,
    "min_local_catalogue": 5,
    "rank_percentile_5star": 0.02,
    "rank_percentile_4star": 0.10,
    "rank_percentile_3star": 0.35,
    "rank_percentile_2star": 0.65,
    "min_catalogue_size": 5,
    # Absolute bar a compilation's 5★ must ALSO clear. The artist-catalogue
    # percentile is scale-free, so "#1 of 9 songs" reached 5★ at 713 listeners
    # while the album's real hits sat below it. Either bar clears the gate.
    "top_tier_min_score": 55.0,
    "top_tier_min_listeners": 250000,
    # The mirror guard: a track in the compilation's own top band (by the
    # cross-artist raw blend) floors at this many stars, so a heavyweight's
    # deep discography cannot bury their genuine hit. 0 disables it.
    "standout_floor_percentile": 0.10,
    "standout_floor_stars": 4,
}


def get_compilation_online_catalogue_config(config: dict | None = None) -> dict[str, Any]:
    """Return the online-catalogue settings for compilation star rating.

    Merged over the defaults so every key is always present, even with no
    config file (a partial user block overrides only the keys it names).
    """
    cfg = config if isinstance(config, dict) else get_config()
    sd = cfg.get("single_detection", {}) if isinstance(cfg, dict) else {}
    block = sd.get("compilation_online_catalogue") if isinstance(sd, dict) else None

    rules = dict(_DEFAULT_COMPILATION_ONLINE_CATALOGUE)
    if isinstance(block, dict):
        rules.update(block)

    out: dict[str, Any] = {"enabled": 1 if _as_truthy(rules.get("enabled", 1)) else 0}
    for key in (
        "min_local_catalogue",
        "rank_percentile_5star",
        "rank_percentile_4star",
        "rank_percentile_3star",
        "rank_percentile_2star",
        "min_catalogue_size",
        "top_tier_min_score",
        "top_tier_min_listeners",
        "standout_floor_percentile",
        "standout_floor_stars",
    ):
        try:
            out[key] = float(rules.get(key, _DEFAULT_COMPILATION_ONLINE_CATALOGUE[key]))
        except (TypeError, ValueError):
            out[key] = float(_DEFAULT_COMPILATION_ONLINE_CATALOGUE[key])
    return out


def _as_truthy(value: Any) -> bool:
    """Interpret config booleans/ints/strings the way the Config page writes them."""
    if isinstance(value, str):
        return value.strip().casefold() not in ("", "0", "false", "no", "off")
    return bool(value)


#: Songs shorter than ``max_duration_seconds`` are capped at ``max_stars``.
#: A 50-second track is a skit, an interlude or a hidden-track joke — Last.fm
#: and ListenBrainz count these as full "listens", so a short track riding a
#: popular album can out-score the album's real songs on raw play counts and
#: land 4★/5★.  The cap is a FINAL clamp applied after every other star path
#: (era slots, live-album caps, single-driven floors) so nothing restores the
#: rating.  Algorithmic ratings only — a user override or a heart still wins.
_DEFAULT_SHORT_TRACK_STAR_CAP: dict[str, Any] = {
    "enabled": 1,
    "max_duration_seconds": 70.0,
    "max_stars": 2,
}


def get_short_track_star_cap_config(config: dict | None = None) -> dict[str, Any]:
    """Return the short-track star cap settings.

    Reads ``statistics.short_track_star_cap`` keys:
    - ``enabled`` (default True) — master switch
    - ``max_duration_seconds`` (default 70) — tracks shorter than this are capped
    - ``max_stars`` (default 2) — the ceiling applied to those tracks

    Merged over the defaults so every key is always present, even with no
    config file (a partial user block overrides only the keys it names).
    ``0`` disables: a zero duration threshold or a star cap of 0/1 means the
    caller should skip the clamp entirely.
    """
    cfg = config if isinstance(config, dict) else get_config()
    stats = cfg.get("statistics", {}) if isinstance(cfg, dict) else {}
    block = stats.get("short_track_star_cap") if isinstance(stats, dict) else None

    rules = dict(_DEFAULT_SHORT_TRACK_STAR_CAP)
    if isinstance(block, dict):
        rules.update(block)

    out: dict[str, Any] = {"enabled": 1 if _as_truthy(rules.get("enabled", 1)) else 0}

    try:
        out["max_duration_seconds"] = float(
            rules.get("max_duration_seconds", _DEFAULT_SHORT_TRACK_STAR_CAP["max_duration_seconds"]) or 0
        )
    except (TypeError, ValueError):
        out["max_duration_seconds"] = float(_DEFAULT_SHORT_TRACK_STAR_CAP["max_duration_seconds"])

    try:
        out["max_stars"] = int(
            rules.get("max_stars", _DEFAULT_SHORT_TRACK_STAR_CAP["max_stars"]) or 0
        )
    except (TypeError, ValueError):
        out["max_stars"] = int(_DEFAULT_SHORT_TRACK_STAR_CAP["max_stars"])

    return out


def get_standout_config() -> dict[str, Any]:
    """Get standout track detection and star rating configuration.

    Returns:
        Dict containing standout detection thresholds and star rating criteria.

    Default Values:
        - album_zscore_threshold: 0.8 (minimum z-score for standout detection)
        - artist_zscore_threshold: 2.2 (minimum z-score for artist-level outliers)
        - artist_top_percentile: 0.10 (top 10% of artist catalog)
        - artist_min_tracks: 10 (minimum tracks required for artist stats)
        - star_5: {"album_z": 1.0, "artist_z": 1.2, "artist_pct": 0.10}
        - star_4: {"album_z": 0.5, "artist_z": 1.0, "artist_pct": 0.20}
        - star_3: {"album_z": -0.5}
        - star_2: {"album_z": -1.2}
        - star_1: {"album_z": -1.2, "default": True}

    Nested blocks (merged with defaults, passed through to finalise_stage):
        - album_scaling: era rules + era boundary ratios
        - live_album_scaling: live-album artist-z bands, album-z gate,
          promotion threshold, and per-release 5★/4★ slot caps
    """
    cfg = get_config()
    sd_config = cfg.get("single_detection", {})

    defaults = {
        "album_zscore_threshold": 0.8,
        "artist_zscore_threshold": 2.2,
        "artist_top_percentile": 0.10,
        "artist_top_percentile_large": 0.25,
        "artist_catalog_large_threshold": 30,
        "artist_medium_bump_percentile": 0.20,
        "artist_min_tracks": 10,
        "popularity_5star_z_threshold": 2.0,
        "lb_unreliable_5star_threshold": 0.50,
        "listener_5star_z_threshold": 1.0,
        "standout_gap_z": 0.75,
        "star_epsilon_score_points": 0.5,
        "live_4star_requires_single": True,
        # Various-artists compilation guard: a compilation must have a
        # tracklist resembling the candidate release-group before its MBID is
        # bound or its album type adopted.  See
        # album_stage._tracklist_corroborates_release_group.
        "compilation_tracklist_guard": True,
        "compilation_tracklist_floor": 0.6,
        "star_5": {"album_z": 1.0, "artist_z": 1.2, "artist_pct": 0.10},
        "star_4": {"album_z": 0.5, "artist_z": 1.0, "artist_pct": 0.20},
        "star_3": {"album_z": -0.5},
        "star_2": {"album_z": -1.2},
        "star_1": {"album_z": -1.2, "default": True},
    }

    # Merge user config with defaults
    result = defaults.copy()
    for key in ("album_zscore_threshold", "artist_zscore_threshold",
                "artist_top_percentile", "artist_top_percentile_large",
                "artist_catalog_large_threshold",
                "artist_medium_bump_percentile",
                "artist_min_tracks",
                "popularity_5star_z_threshold", "lb_unreliable_5star_threshold",
                "listener_5star_z_threshold",
                "standout_gap_z", "star_epsilon_score_points",
                "live_4star_requires_single",
                "compilation_tracklist_guard", "compilation_tracklist_floor"):
        if key in sd_config:
            result[key] = sd_config[key]

    for star_key in ("star_5", "star_4", "star_3", "star_2", "star_1"):
        if star_key in sd_config and isinstance(sd_config[star_key], dict):
            result.setdefault(star_key, {}).update(sd_config[star_key])

    # The 3-step album scaling model (era rules + boundaries) is not a star
    # tier — pass the whole block through so era gating in finalise_stage
    # honours config.html values.  Without this, every saved era setting
    # (catalog top %, album top N, max 5★ slots, era boundaries) was silently
    # dropped and the scan always used the hardcoded defaults.
    if isinstance(sd_config.get("album_scaling"), dict):
        result["album_scaling"] = sd_config["album_scaling"]

    # Live-album rating rules (artist-z bands, album-z gate, slot caps).
    #
    # Same passthrough requirement as album_scaling above: finalise_stage
    # reads this via ``get_standout_config().get("live_album_scaling")``, so
    # omitting it here means every saved live-album setting is discarded and
    # the scan silently falls back to hardcoded defaults — with the LIVE
    # ALBUM log banner still reporting "source: defaults".
    #
    # Unlike album_scaling, the defaults are merged in rather than the block
    # being passed through raw, so the key is ALWAYS present and correctly
    # populated even with no config file.  A partial user block overrides
    # only the keys it names.
    live_scaling = dict(_DEFAULT_LIVE_ALBUM_SCALING)
    user_live_scaling = sd_config.get("live_album_scaling")
    if isinstance(user_live_scaling, dict):
        live_scaling.update(user_live_scaling)
    result["live_album_scaling"] = live_scaling

    # Album ratio standout gate (the "shape of popularity" 5★ test). Merged
    # the same way as live_album_scaling: always present, partial user block
    # overrides only the keys it names. finalise_stage reads these via
    # ``get_standout_config()["album_ratio_standout"]``.
    ratio_rules = dict(_DEFAULT_ALBUM_RATIO_STANDOUT)
    user_ratio_rules = sd_config.get("album_ratio_standout")
    if isinstance(user_ratio_rules, dict):
        ratio_rules.update(user_ratio_rules)
    result["album_ratio_standout"] = ratio_rules

    return result


# -----------------------------------------------------------------------------
# Tag Writing Configuration
# -----------------------------------------------------------------------------

def get_tagging_config() -> dict[str, Any]:
    """Get file-tag writing policy from the ``tagging`` config block.

    Lets Popularr act as a read-only database scanner / UI when the user
    runs an external tagger (Beets, MusicBrainz Picard) or the music folder
    is on a read-only/network mount:

    ```yaml
    tagging:
      write_tags_to_file: true        # master toggle: touch audio files at all
      skip_unchanged_ratings: true    # skip rating tag writes + Navidrome syncs
                                      # when the scan's star rating is unchanged
                                      # (biggest disk-write source in scans)
      write_options:
        ratings_only: false           # only write POPM/RATING, never text frames
        fill_missing_only: false      # only fill frames that are currently empty
        embed_lyrics: false           # write lyrics to USLT/SYLT frames
      preserve_file_timestamps: true  # restore mtime/atime after a write
    ```

    Defaults preserve the legacy behaviour (writes enabled, timestamps
    preserved); the config page exposes all of them.
    """
    cfg = get_config() or {}
    tagging = cfg.get("tagging") or {}
    if not isinstance(tagging, dict):
        tagging = {}
    opts = tagging.get("write_options") or {}
    if not isinstance(opts, dict):
        opts = {}
    try:
        write_enabled = bool(tagging.get("write_tags_to_file", True))
    except Exception:
        write_enabled = True
    return {
        "write_tags_to_file": write_enabled,
        "skip_unchanged_ratings": bool(tagging.get("skip_unchanged_ratings", True)),
        "sync_album_tags_on_scan": bool(tagging.get("sync_album_tags_on_scan", True)),
        "ratings_only": bool(opts.get("ratings_only", False)),
        "fill_missing_only": bool(opts.get("fill_missing_only", False)),
        "embed_lyrics": bool(opts.get("embed_lyrics", False)),
        "preserve_file_timestamps": bool(tagging.get("preserve_file_timestamps", True)),
    }


# -----------------------------------------------------------------------------
# Metadata Update Configuration
# -----------------------------------------------------------------------------

_DEFAULT_METADATA_UPDATE_FIELDS = {
    "album_name": False,
    "year": False,
    "album_artist": False,
    "genres": False,
    "cover_art": False,
    "lyrics": False,
}

# Valid ``album_name_source`` values.  ``dedupe`` MUST be present: it is the
# repair path that collapses duplicated annotations while PRESERVING the
# edition.  It was previously absent from this allow-list, so a config
# setting ``album_name_source: dedupe`` silently fell through to ``album``
# and stripped the edition instead — the exact opposite of the request, with
# nothing logged to explain it.
_VALID_ALBUM_NAME_SOURCES = ("dedupe", "album", "release", "release_group")


def get_metadata_update_config() -> dict[str, Any]:
    """Get the "Updating Metadata" scan-behaviour config block.

    Controls whether the popularity scan rewrites metadata and, when it
    does, whether the write goes to the DATABASE only or also to the audio
    FILES (``update_on_files`` per field).

    ```yaml
    metadata_update:
      album_name_source: dedupe       # dedupe | album | release_group (release = alias)
      album_name_update_target: db    # db | files
      apply_during_scan: true         # false = recommend instead of applying
      update_on_files:
        album_name: false
        year: false
        album_artist: false
        genres: false
        cover_art: false
        lyrics: false
    ```

    ``album_name_source`` decides what the album name is set to when the
    scan cleans it:
      - ``dedupe`` (default): collapse DUPLICATED annotations while
        PRESERVING the edition ("The Fall of Hearts (tour edition) (tour
        edition)" → "The Fall of Hearts (tour edition)").  This is the
        repair path — it only ever removes an annotation that repeats an
        earlier one, so no distinct annotation can be lost.
      - ``album``: the current album name with trailing edition markers
        stripped ENTIRELY ("Doomsday Machine (reissue) (reissue)" →
        "Doomsday Machine").  Legacy behaviour — this DISCARDS the edition,
        so two pressings of one release collapse to the same name.
      - ``release_group`` (alias ``release``): the MusicBrainz RELEASE-GROUP
        title, i.e. the album's main identity ("Experience"), when a
        confident match exists (falls back to ``dedupe``).

    NOTE: neither value applies the SPECIFIC release/edition title.  A release
    group has many releases, so the edition name ("Experience: Expanded
    (Remixes and B-Sides)") is stored separately on the track as
    ``release_title`` and surfaced as a tagline beneath the album name.  The
    ``release`` spelling is retained because it is persisted in existing
    config.yaml files; it behaves identically to ``release_group``.

    ``album_name_update_target`` decides where the cleaned/renamed album
    name is written:
      - ``db`` (default): update the tracks table only.
      - ``files``: update the tracks table AND rewrite the ALBUM tag on the
        audio files (Navidrome reads file tags, so it then re-serves the new
        name too).

    NOTE: for the ALBUM tag to reach disk BOTH
    ``album_name_update_target: files`` AND ``update_on_files.album_name:
    true`` are required.  With target ``db`` the rename stays in the
    database and Navidrome keeps serving the old name from the file tags.

    ``apply_during_scan`` (default ``true``) decides whether the scan ACTS on
    the MusicBrainz metadata it resolves.  When set to ``false`` the scan does
    not write it; it STASHES the recommendations on the album's track rows
    (``tracks.pending_mb_updates``) instead, so an album or artist page can
    offer them with save/discard.  This is the "recommend, don't apply" mode.
    """
    cfg = get_config() or {}
    block = cfg.get("metadata_update") or {}
    if not isinstance(block, dict):
        block = {}

    source = str(block.get("album_name_source") or "dedupe").strip().lower()
    if source not in _VALID_ALBUM_NAME_SOURCES:
        source = "dedupe"

    target = str(block.get("album_name_update_target") or "db").strip().lower()
    if target not in ("db", "files"):
        target = "db"

    update_on_files = block.get("update_on_files") or {}
    if not isinstance(update_on_files, dict):
        update_on_files = {}

    fields = dict(_DEFAULT_METADATA_UPDATE_FIELDS)
    for key, default in fields.items():
        if key in update_on_files:
            try:
                fields[key] = bool(update_on_files[key])
            except Exception:
                fields[key] = default

    return {
        "album_name_source": source,
        "album_name_update_target": target,
        "update_on_files": fields,
        # ``apply_during_scan`` — when false the scan stops APPLYING MusicBrainz
        # metadata and instead STASHES its recommendations on the track rows
        # (tracks.pending_mb_updates) for the user to save or discard later from
        # the album/artist pages. Defaults to true (existing behaviour), so an
        # existing config.yaml that lacks the key is unaffected.
        "apply_during_scan": (
            _as_truthy(block.get("apply_during_scan"))
            if "apply_during_scan" in block
            else True
        ),
    }


# -----------------------------------------------------------------------------
# Album MBID sanity guard
# -----------------------------------------------------------------------------
# Before an album MusicBrainz ID is trusted, the LOCAL artist/album text is
# compared against the ID's own resolved text.  A stored ID used to be applied
# to every track and every audio file with NO check at all, so one bad ID from
# an errant batch-tagging run could merge two unrelated albums into a single
# release (the reported Metallica / d'Artagnan "66-track super album").
#
#   min_similarity      0.65 keeps legitimate variation matching — edition
#                       markers, "&"/"feat." credits, "Vol. 1" vs "Volume 1",
#                       punctuation and case are normalised away before the
#                       comparison — while rejecting a genuinely different
#                       record.  Lower it only if real albums are refused.
#   allow_disc_folders  an album may span several folders ONLY when every extra
#                       folder is a disc folder ("CD1", "Disc 2") of the same
#                       parent.  Any other multi-folder album is two albums
#                       sharing a name, and the fan-out is refused.
_DEFAULT_ALBUM_MBID_GUARD: dict[str, Any] = {
    "enabled": True,
    "min_similarity": 0.65,
    "allow_disc_folders": True,
}


def get_album_mbid_guard_config() -> dict[str, Any]:
    """Get the album-MBID sanity-guard config block.

    Config section: ``metadata_update.album_mbid_guard`` in config.yaml

    ```yaml
    metadata_update:
      album_mbid_guard:
        enabled: true
        min_similarity: 0.65
        allow_disc_folders: true
    ```

    Merged the same way as every other nested block: the block is ALWAYS
    present and a partial user block overrides only the keys it names, so a
    value saved on the Config page can never silently revert to a default.
    """
    cfg = get_config() or {}
    block = (cfg.get("metadata_update") or {}).get("album_mbid_guard")

    guard = dict(_DEFAULT_ALBUM_MBID_GUARD)
    if isinstance(block, dict):
        if "enabled" in block:
            try:
                guard["enabled"] = bool(block.get("enabled"))
            except Exception:
                pass
        if "allow_disc_folders" in block:
            try:
                guard["allow_disc_folders"] = bool(block.get("allow_disc_folders"))
            except Exception:
                pass
        try:
            value = float(block.get("min_similarity"))
        except (TypeError, ValueError):
            value = None
        if value is not None:
            # Clamp to a sane band: 0 would disable the guard silently, and
            # anything above 1 could never match.
            guard["min_similarity"] = max(0.05, min(1.0, value))
    return guard


# -----------------------------------------------------------------------------
# Playlist Configuration
# -----------------------------------------------------------------------------

#: A track carrying a metal genre is left out of these playlists by default.
#: The crossover case is the reason: a band filed under both "alternative rock"
#: and "alternative metal" (Tool, Deftones, Manson, Slipknot) would otherwise
#: take over the rock playlist, which is meant for rock/indie/grunge acts.
DEFAULT_METAL_EXCLUDED_FROM: tuple[str, ...] = ("alternative rock",)


def get_genre_exclusion_config() -> dict[str, Any]:
    """Genre-playlist exclusion rules. Config section: ``playlists.genre_exclusions``.

    Two independent rules, both about a track's genre identity rather than its
    popularity:

    * ``own_tags_win`` — judge a track on ITS OWN genre tags whenever it has
      any, and consult the album-level blend (``tracks.genres``) only for a
      track that has none of its own. This exists because ``genres`` is NOT a
      per-track value on an ordinary album: ``sync_album_file_tags`` writes one
      blended list to every row, so a Linkin Park album gives "Iridescent" the
      same ``genres`` string as its nu-metal tracks. Without this, any
      fallback-inherited tag moves a track between playlists.
    * ``metal_excluded_from`` — a track whose genre set is metal may not enter
      these playlists. This is the "keep Alternative Rock to rock/indie/grunge"
      rule; the metal crossover belongs in the metal playlists.
    * ``use_family_tiebreak`` — HOW "is this track metal?" is decided. ``False``
      keeps the original substring test ("metal" anywhere in a token); ``True``
      asks the genre vocabulary for the track's deciding FAMILY, ranking the
      tokens by specificity then order.

      Why the family test is the better default: grunge and nu metal are both
      "not metal" and "metal" respectively by the vocabulary's own hierarchy,
      where the substring test only ever asked whether the letters m-e-t-a-l
      appeared. The reported case is Audioslave being kept out of Alternative
      Rock: its most specific own genre is ``grunge`` (family rock), while a
      metal tag sits further down the list — the substring test cannot tell that
      from Tool, whose most specific genre IS metal.

      The substring test is retained as the fallback for a token the vocabulary
      does not know, so a brand-new subgenre spelling is still caught.

    Default Values:
        - enabled: True
        - own_tags_win: True
        - metal_excluded_from: ["alternative rock"]
        - use_family_tiebreak: True
    """
    cfg = get_config() or {}
    p = cfg.get("playlists") or {}
    if not isinstance(p, dict):
        p = {}
    block = p.get("genre_exclusions")
    if not isinstance(block, dict):
        block = {}

    raw_genres = block.get("metal_excluded_from", DEFAULT_METAL_EXCLUDED_FROM)
    if isinstance(raw_genres, str):
        raw_genres = [g for g in re.split(r"[,;/\\|]+", raw_genres)]
    if not isinstance(raw_genres, (list, tuple)):
        raw_genres = list(DEFAULT_METAL_EXCLUDED_FROM)

    genres: list[str] = []
    for genre in raw_genres:
        name = str(genre or "").strip().lower()
        if name and name not in genres:
            genres.append(name)

    return {
        "enabled": bool(block.get("enabled", True)),
        "own_tags_win": bool(block.get("own_tags_win", True)),
        "metal_excluded_from": genres,
        "use_family_tiebreak": bool(block.get("use_family_tiebreak", True)),
    }


def get_playlists_config() -> dict[str, Any]:
    """Get playlist generation settings.

    Config section: ``playlists`` in config.yaml

    Returns:
        Dict of playlist toggles, name templates and size limits.

    Default Values:
        - essential_playlists_enabled: True
        - essential_max_tracks: 50
        - essential_name_template: "{artist} - Essential Collection"
        - essential_include_featured: True
        - genre_playlists_enabled: True
        - genre_playlists_delete_enabled: True
        - genre_playlists_name_template: "{genre} - Top Tracks"
        - genre_playlists_min_stars: 4
        - genre_playlists_max_genres: 3
        - genre_playlists_create_threshold: 100
        - genre_playlists_delete_threshold: 80
        - genre_playlists_max_tracks: 300
        - new_music_playlist_enabled: True
        - exclude_christmas_from_playlists: True
        - christmas_playlist_marker: "christmas"

    NOTE: ``genre_playlists_max_tracks`` (300) is deliberately larger than
    ``genre_playlists_create_threshold`` (100).  A genre with between 100 and
    300 qualifying tracks therefore yields a playlist containing its ENTIRE
    qualifying pool rather than a curated top slice.  Lower the max, or raise
    the create threshold, if you want every genre playlist to be a selection.
    Set it to ``0`` for NO cap (every qualifying track is included).

    NOTE: ``exclude_christmas_from_playlists`` keeps Christmas music out of every
    generated playlist EXCEPT those whose name contains
    ``christmas_playlist_marker`` (default "christmas"), which is what lets the
    "Christmas - Top Tracks" / "Christmas Pop - Top Tracks" playlists exist
    while ordinary genre, Essential and New Music playlists stay seasonal-free.

    NOTE: ``playlists.genre_exclusions`` is read separately by
    ``get_genre_exclusion_config`` — it is a nested block, so it is not part of
    this flat mapping.
    """
    cfg = get_config() or {}
    p = cfg.get("playlists") or {}
    if not isinstance(p, dict):
        p = {}
    # 0 must survive as 0 ("no cap") — the old ``int(... or 300)`` turned a
    # configured 0 back into 300, so the documented no-cap option could never
    # be expressed. Mirrors ``_resolve_genre_max_tracks`` in finalise_stage.
    try:
        _genre_max_tracks = int(p.get("genre_playlists_max_tracks", 300))
    except (TypeError, ValueError):
        _genre_max_tracks = 300
    return {
        "essential_playlists_enabled": bool(p.get("essential_playlists_enabled", True)),
        "essential_max_tracks": max(1, int(p.get("essential_max_tracks", 50) or 50)),
        "essential_name_template": str(
            p.get("essential_name_template") or "{artist} - Essential Collection"
        ),
        "essential_include_featured": bool(p.get("essential_include_featured", True)),
        "genre_playlists_enabled": bool(p.get("genre_playlists_enabled", True)),
        "genre_playlists_delete_enabled": bool(p.get("genre_playlists_delete_enabled", True)),
        "genre_playlists_name_template": str(
            p.get("genre_playlists_name_template") or "{genre} - Top Tracks"
        ),
        "genre_playlists_min_stars": max(1, int(p.get("genre_playlists_min_stars", 4) or 4)),
        "genre_playlists_max_genres": max(1, int(p.get("genre_playlists_max_genres", 3) or 3)),
        "genre_playlists_create_threshold": max(
            1, int(p.get("genre_playlists_create_threshold", 100) or 100)
        ),
        "genre_playlists_delete_threshold": max(
            1, int(p.get("genre_playlists_delete_threshold", 80) or 80)
        ),
        "genre_playlists_max_tracks": max(0, _genre_max_tracks),
        "new_music_playlist_enabled": bool(p.get("new_music_playlist_enabled", True)),
        "exclude_christmas_from_playlists": bool(
            p.get("exclude_christmas_from_playlists", True)
        ),
        "christmas_playlist_marker": str(
            p.get("christmas_playlist_marker") or "christmas"
        ).strip().lower() or "christmas",
    }


def get_favourite_playlists_config() -> dict[str, Any]:
    """Get the per-user favourite playlist settings.

    Config section: ``playlists`` in config.yaml

    Returns:
        Dict of toggles and the minimum star rating.

    Default Values:
        - favourite_playlists_enabled: True
        - favourite_min_stars: 4
        - favourite_artists_name: "Favourite Artists"
        - favourite_albums_name: "Favourite Albums"
        - sync_favourites_from_navidrome: False

    NOTE: these playlists are PER USER. One Navidrome user's stars produce
    playlists written to THAT user's own Navidrome account, so two accounts on
    one server never see each other's (Navidrome refuses a write to a playlist
    the calling user does not own — Subsonic code 50).
    """
    cfg = get_config() or {}
    p = cfg.get("playlists") or {}
    if not isinstance(p, dict):
        p = {}
    # ⚠️ NOT ``int(p.get("favourite_min_stars", 4) or 4)``: a configured 0 is a
    # REAL value (1 means "every track qualifies"), and ``0 or 4`` silently made
    # it 4 — the same falsy-value trap ``_resolve_genre_max_tracks``
    # (``genre_playlists_max_tracks``) and ``_genre_min_weight`` each document.
    try:
        _min_stars = int(p.get("favourite_min_stars", 4))
    except (TypeError, ValueError):
        _min_stars = 4
    return {
        "favourite_playlists_enabled": bool(p.get("favourite_playlists_enabled", True)),
        "favourite_min_stars": max(1, min(5, _min_stars)),
        "favourite_artists_name": str(
            p.get("favourite_artists_name") or "Favourite Artists"
        ).strip() or "Favourite Artists",
        "favourite_albums_name": str(
            p.get("favourite_albums_name") or "Favourite Albums"
        ).strip() or "Favourite Albums",
        "sync_favourites_from_navidrome": bool(
            p.get("sync_favourites_from_navidrome", False)
        ),
    }


# ---------------------------------------------------------------------------
# Artists excluded from generated playlists
# ---------------------------------------------------------------------------

#: Artists that must never be generated into a playlist, whatever else is on.
#:
#: ⚠️ DISTINCT FROM ``_ESSENTIAL_EXCLUDED_ARTISTS`` in ``finalise_stage``.
#: That frozenset is a CODE constant for placeholder credits ("Various Artists",
#: "Soundtrack", …) — names that are not really an artist. This is a USER list:
#: real artists the operator does not want playlist entries for (comedy acts,
#: audiobooks, an unfavourite band, …). Keeping them apart means a user can
#: remove an entry they added without deleting a placeholder guard the code
#: depends on, and vice versa.
_EXCLUDED_PLAYLIST_ARTISTS_KEY = "excluded_artists"


def _normalise_excluded_artist(name: Any) -> str:
    """Comparison key for the excluded-artist list.

    Casefolded and trimmed so "Candlemass", "candlemass " and "CANDLEMASS" are
    one entry. Punctuation is NOT folded: unlike a track title, an artist name
    is a stable identifier here, and folding "AC/DC" to "acdc" would make it
    collide with a genuinely different act called "ACDC".
    """
    return str(name or "").strip().casefold()


def get_excluded_playlist_artists() -> set[str]:
    """Artists the user excluded from generated playlists, as comparison keys.

    Config section: ``playlists.excluded_artists`` in config.yaml — a list of
    artist names.

    Returns a set of :func:`_normalise_excluded_artist` keys, because every
    caller only ever asks "is this artist excluded?".

    ⚠️ A non-list value is treated as empty rather than iterated. A hand-edited
    ``excluded_artists: Candlemass`` (a bare string) would otherwise iterate
    CHARACTER BY CHARACTER and exclude every artist whose name is a single
    letter — a silent, spectacular failure from a plausible typo.
    """
    cfg = get_config() or {}
    p = cfg.get("playlists") or {}
    if not isinstance(p, dict):
        return set()
    raw = p.get(_EXCLUDED_PLAYLIST_ARTISTS_KEY)
    if isinstance(raw, dict):
        # ``{artist: true}`` is a shape a YAML edit can easily produce; its
        # KEYS carry the same meaning as the list, so accept it.
        raw = list(raw.keys())
    if isinstance(raw, str) or not isinstance(raw, (list, tuple, set, frozenset)):
        if isinstance(raw, str) and raw.strip():
            raw = [raw]
        else:
            return set()
    return {k for k in (_normalise_excluded_artist(v) for v in raw) if k}


def set_excluded_playlist_artists(artists: Any) -> bool:
    """Replace ``playlists.excluded_artists`` and persist it.

    Sorted and de-duplicated so the stored list is stable: a UI that re-saves an
    unchanged set must not reorder the file, and an idempotent save must not
    produce a spurious diff.

    Uses :func:`save_partial_config` (a deep MERGE into the on-disk config)
    rather than :func:`save_config`, whose argument is the COMPLETE config —
    passing a section to it would delete every other key in config.yaml.
    """
    if isinstance(artists, str):
        artists = [artists]
    if not isinstance(artists, (list, tuple, set, frozenset)):
        return False

    cleaned: dict[str, str] = {}
    for value in artists:
        display = str(value or "").strip()
        key = _normalise_excluded_artist(display)
        if key and key not in cleaned:
            cleaned[key] = display
    ordered = [cleaned[k] for k in sorted(cleaned)]
    return save_partial_config({"playlists": {_EXCLUDED_PLAYLIST_ARTISTS_KEY: ordered}})


def is_artist_excluded_from_playlists(artist: str) -> bool:
    """True when *artist* is on the user's playlist-exclusion list."""
    return _normalise_excluded_artist(artist) in get_excluded_playlist_artists()


# -----------------------------------------------------------------------------
# Genre Aggregation Configuration
# -----------------------------------------------------------------------------

def get_genre_weights() -> dict[str, float]:
    """Get genre source weighting for aggregation.

    Returns:
        Dict mapping genre source names to their weights.

    Default Values:
        - musicbrainz: 0.40 (40% weight - most authoritative)
        - discogs: 0.25 (25% weight)
        - audiodb: 0.20 (20% weight)
        - essentia: 0.20 (20% weight - audio analysis)
        - listenbrainz: 0.15 (15% weight - MB-tagged, community)
        - navidrome: 0.30 (30% weight - local library tags)
        - manual: 0.30 (30% weight - explicit user edits)
        - lastfm: 0.10 (10% weight)
        - spotify: 0.05 (5% weight)

    NOTE on ``navidrome``: the aggregation service treats Navidrome tags as a
    TIE-BREAKER ONLY — they contribute no vote weight and can never introduce
    a genre on their own.  This weight is therefore unused by the current
    ranking path and is retained for compatibility.
    """
    cfg = get_config()
    genre_config = cfg.get("genres", {}).get("weights", {})

    defaults = {
        "musicbrainz": 0.40,
        "discogs": 0.25,
        "audiodb": 0.20,
        "essentia": 0.20,
        "listenbrainz": 0.15,
        "navidrome": 0.30,
        "manual": 0.30,
        "lastfm": 0.10,
        "spotify": 0.05,
    }

    return {
        key: float(genre_config.get(key, default))
        for key, default in defaults.items()
    }


def get_genre_synonyms() -> dict[str, str]:
    """Get genre synonym mappings for normalization.

    Returns:
        Dict mapping variant genre names to canonical forms.

    Default Mappings:
        - "hip hop" → "hip-hop"
        - "r&b" → "rnb"
        - "rhythm and blues" → "rnb"
    """
    cfg = get_config()
    user_synonyms = cfg.get("genres", {}).get("synonyms", {})

    defaults = {
        "hip hop": "hip-hop",
        "r&b": "rnb",
        "rhythm and blues": "rnb",
    }

    # User synonyms override defaults
    return {**defaults, **user_synonyms}


def get_genre_aggregation_config() -> dict[str, Any]:
    """Get genre aggregation tuning (junk filter + minimum vote weight).

    Config section: ``genres`` in config.yaml

    Returns:
        Dict with keys ``junk_filter`` (bool) and ``min_weight`` (float).

    Default Values:
        - junk_filter: True  (drop "seen live", years, mood words, etc.)
        - min_weight: 0.25   (minimum decayed vote weight to qualify)

    NOTE on ``min_weight``: the aggregation service applies a rank decay of
    ``1 / log2(rank + 2)`` to every vote, so this threshold is compared
    against DECAYED totals, not raw source weights.  A source weighted 0.30
    clears 0.25 at rank 0 (0.30) but fails at rank 1 (0.19).  Lower this
    value if genre lists come back shorter than expected.
    """
    cfg = get_config() or {}
    genres = cfg.get("genres") or {}
    if not isinstance(genres, dict):
        genres = {}
    try:
        min_weight = max(0.0, float(genres.get("min_weight", 0.25) or 0.25))
    except (TypeError, ValueError):
        min_weight = 0.25
    return {
        "junk_filter": bool(genres.get("junk_filter", True)),
        "min_weight": min_weight,
    }


def get_genre_selection_config() -> dict[str, Any]:
    """Get the genre SELECTION engine's tuning.

    Config section: ``genres.selection`` in config.yaml

    This drives the canonical-vocabulary engine (``genre_selection``): it
    classifies every value before scoring, weights evidence by entity level
    (track / album / artist), condenses the hierarchy, and requires each of the
    up-to-three positions to earn its place.

    Returns:
        Dict with ``enabled``, ``max_genres``, ``source_weights``,
        ``entity_weights``, the bonus/penalty terms, the confidence
        ``thresholds``, the family-slot limit, and the ``keep_together`` /
        ``conditional_keep`` pair lists.

    ⚠️ ``source_weights`` here are SEPARATE from the legacy ``genres.weights``
    block. The legacy values were tuned for the old vote-counting engine (which
    put Last.fm above ListenBrainz); these are the report's recommended provider
    trust levels, where MusicBrainz proper genres lead and a manual correction
    outranks everything. Both blocks coexist so switching engines switches the
    whole model rather than half of it.

    ``enabled`` defaults to True — the new engine is the intended behaviour — and
    setting it False restores the previous vote-counting selection exactly.
    """
    cfg = get_config() or {}
    genres = cfg.get("genres") or {}
    if not isinstance(genres, dict):
        genres = {}
    selection = genres.get("selection") or {}
    if not isinstance(selection, dict):
        selection = {}

    def _num(key: str, default: float) -> float:
        try:
            return float(selection.get(key, default))
        except (TypeError, ValueError):
            return default

    def _int(key: str, default: int) -> int:
        try:
            return int(selection.get(key, default))
        except (TypeError, ValueError):
            return default

    thresholds = selection.get("thresholds")
    if not isinstance(thresholds, dict):
        thresholds = {}

    def _threshold(key: str, default: float) -> float:
        try:
            return float(thresholds.get(key, default))
        except (TypeError, ValueError):
            return default

    def _weights(key: str) -> dict[str, float]:
        block = selection.get(key)
        if not isinstance(block, dict):
            return {}
        out: dict[str, float] = {}
        for name, value in block.items():
            try:
                out[str(name)] = float(value)
            except (TypeError, ValueError):
                continue
        return out

    def _pairs(key: str) -> list[list[str]]:
        block = selection.get(key)
        if not isinstance(block, list):
            return []
        out: list[list[str]] = []
        for item in block:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                out.append([str(item[0]).strip(), str(item[1]).strip()])
            elif isinstance(item, str) and "|" in item:
                a, _, b = item.partition("|")
                out.append([a.strip(), b.strip()])
        return out

    return {
        "enabled": bool(selection.get("enabled", True)),
        "max_genres": max(1, _int("max_genres", 3)),
        "source_weights": _weights("source_weights"),
        "entity_weights": _weights("entity_weights"),
        "agreement_2": _num("agreement_2", 0.30),
        "agreement_3": _num("agreement_3", 0.20),
        "specificity_bonus": _num("specificity_bonus", 0.05),
        "artist_only_penalty": _num("artist_only_penalty", 0.15),
        "microgenre_single_source_penalty": _num(
            "microgenre_single_source_penalty", 0.10
        ),
        "thresholds": {
            "first": _threshold("first", 0.60),
            "second": _threshold("second", 0.45),
            "third": _threshold("third", 0.35),
        },
        "family_slot_limit": max(1, _int("family_slot_limit", 2)),
        "family_override_sources": max(1, _int("family_override_sources", 2)),
        "keep_together": _pairs("keep_together"),
        "conditional_keep": _pairs("conditional_keep"),
    }


# -----------------------------------------------------------------------------
# Queue Matching Configuration
# -----------------------------------------------------------------------------

def get_queue_matching_config_v2() -> dict[str, Any]:
    """Get queue matching thresholds, variant tokens, and compilation settings.

    Consolidates legacy ``get_queue_matching_config_legacy()`` into v2.
    The legacy function is deprecated and will be removed.

    Config section: ``queue.matching`` in config.yaml

    Returns:
        Dict containing matching thresholds, variant configurations,
        compilation detection settings, and track variant tokens.

    Default Values:
        - threshold: 0.65
        - partial_match: 0.7
        - strict_duration_sec: 2
        - tolerance_duration_sec: 5
        - soft_variants: {"edit", "radio", "version", "mix"}
        - hard_variants: {"live", "acoustic", "remix", "demo", "instrumental"}
        - detect_live_tracks: True
        - detect_remix_tracks: True
        - detect_compilations: True
        - title_variant_tokens: {acoustic, demo, edit, instrumental, intro,
          live, mix, orchestral, radio, remaster, remastered, remix, version}
        - soft_variant_tokens: {version, edit, radio}
        - compilation_artists: {various artists, various, va, v/a, soundtrack, ost, ...}
    """
    cfg = get_config()
    queue_config = cfg.get("queue", {}).get("matching", {})

    return {
        # Matching thresholds
        "threshold": float(queue_config.get("threshold", 0.65)),
        "partial_match": float(queue_config.get("partial_match", 0.7)),
        "strict_duration_sec": int(queue_config.get("strict_duration_sec", 2)),
        "tolerance_duration_sec": int(queue_config.get("tolerance_duration_sec", 5)),

        # Variant tokens (used by matching + normalization)
        "soft_variants": set(queue_config.get("soft_variants", ["edit", "radio", "version", "mix"])),
        "hard_variants": set(queue_config.get("hard_variants", ["live", "acoustic", "remix", "demo", "instrumental"])),

        # --- Merged from legacy ---
        "detect_live_tracks": bool(queue_config.get("detect_live_tracks", True)),
        "detect_remix_tracks": bool(queue_config.get("detect_remix_tracks", True)),
        "detect_compilations": bool(queue_config.get("detect_compilations", True)),

        "title_variant_tokens": set(
            queue_config.get(
                "title_variant_tokens",
                [
                    "acoustic", "demo", "edit", "instrumental", "intro",
                    "live", "mix", "orchestral", "radio",
                    "remaster", "remastered", "remix", "version",
                ],
            )
        ),

        "soft_variant_tokens": set(
            queue_config.get(
                "soft_variant_tokens",
                ["version", "edit", "radio"],
            )
        ),

        "compilation_artists": set(
            queue_config.get(
                "compilation_artists",
                [
                    "various artists", "various artist", "various",
                    "va", "v/a", "unknown artist", "unknown",
                    "soundtrack", "ost",
                ],
            )
        ),
    }


def get_slskd_timeouts() -> dict[str, Any]:
    """Get slskd transfer timeout configuration.

    Returns:
        Dict containing timeout values for various transfer states.

    Default Values:
        - min_retry_delay_minutes: 60
        - long_retry_delay_minutes: 1440 (24 hours)
        - remotely_queued_timeout_minutes: 60
        - active_state_timeout_minutes: 240 (4 hours)
        - inter_item_delay_seconds: 5
        - state_timeouts: {...} (per-state timeouts)
    """
    cfg = get_config()
    slskd_config = cfg.get("slskd", {}).get("timeouts", {})

    return {
        "min_retry_delay_minutes": int(slskd_config.get("min_retry_delay_minutes", 60)),
        "long_retry_delay_minutes": int(slskd_config.get("long_retry_delay_minutes", 1440)),
        "remotely_queued_timeout_minutes": int(slskd_config.get("remotely_queued_timeout_minutes", 60)),
        "active_state_timeout_minutes": int(slskd_config.get("active_state_timeout_minutes", 240)),
        "inter_item_delay_seconds": int(slskd_config.get("inter_item_delay_seconds", 5)),
        "state_timeouts": slskd_config.get("state_timeouts", {
            "Requested": 30,
            "Queued, Remotely": 60,
            "Queued, Locally": 60,
            "Initializing": 120,
            "InProgress": 240,
            "Queued": 60,
            "In Progress": 240,
            "Downloading": 240,
        }),
    }


def get_slskd_search_cleanup_interval() -> int:
    """How many Soulseek searches between automatic stale-search cleanups.

    Config section: ``slskd.search_cleanup_interval`` in config.yaml

    The cleanup costs a ``GET /searches`` round-trip, and slskd serialises API
    operations — so running it before EVERY search delayed the search itself
    for no benefit.  It now runs once per window of N searches (search #1,
    #N+1, …).

    A value of ``0`` or less DISABLES the automatic cleanup ("never"), which is
    deliberately different from ``1`` ("before every search").

    Default: 10
    """
    try:
        cfg = get_config() or {}
        raw = (cfg.get("slskd") or {}).get("search_cleanup_interval", 10)
        return int(raw)
    except Exception:
        return 10


# -----------------------------------------------------------------------------
# Last.fm Service Configuration
# -----------------------------------------------------------------------------

def get_lastfm_config() -> dict[str, Any]:
    """Get Last.fm service configuration.

    Returns:
        Dict containing Last.fm API settings and cache configuration.

    Default Values:
        - min_artist_plays: 20
        - min_similarity_score: 0.46
        - max_similar_per_artist: 5
        - max_albums_per_artist: 5
        - recent_months: 3
        - cache_ttl_hours: 24
        - max_retries: 3
        - retry_backoff: 1.5
        - rate_limit_delay: 0.5
    """
    cfg = get_config()
    lastfm_config = cfg.get("lastfm", {})

    return {
        "min_artist_plays": int(lastfm_config.get("min_artist_plays", 20)),
        "min_similarity_score": float(lastfm_config.get("min_similarity_score", 0.46)),
        "max_similar_per_artist": int(lastfm_config.get("max_similar_per_artist", 5)),
        "max_albums_per_artist": int(lastfm_config.get("max_albums_per_artist", 5)),
        "recent_months": int(lastfm_config.get("recent_months", 3)),
        "cache_ttl_hours": int(lastfm_config.get("cache_ttl_hours", 24)),
        "max_retries": int(lastfm_config.get("max_retries", 3)),
        "retry_backoff": float(lastfm_config.get("retry_backoff", 1.5)),
        "rate_limit_delay": float(lastfm_config.get("rate_limit_delay", 0.5)),
    }


# -----------------------------------------------------------------------------
# Download Matching Configuration
# -----------------------------------------------------------------------------

def get_download_matching_config() -> dict[str, Any]:
    """Get download matching engine configuration.

    Returns:
        Dict containing download matching thresholds and settings.

    Note:
        These settings control how downloaded files are matched to existing
        library tracks and releases.

    ⚠️ ``duration_tolerance_seconds`` DELEGATES to
    ``get_length_tolerance_seconds`` rather than reading ``downloads.matching``
    itself. There used to be TWO sections claiming this key (``matching`` and
    ``downloads.matching``) and two getters reading them — and NOTHING called
    either, so the option was inert while looking configurable. One reader means
    one answer, and the album page's duration notice now agrees with the queue's
    acceptance gate.
    """
    cfg = get_config()
    download_config = cfg.get("downloads", {}).get("matching", {})

    return {
        "min_accept_score": float(download_config.get("min_accept_score", 0.45)),
        "duration_tolerance_seconds": get_length_tolerance_seconds(),
        "early_accept_length_tolerance": int(download_config.get("early_accept_length_tolerance", 2)),
        "top_n_candidates": int(download_config.get("top_n_candidates", 50)),
    }


# -----------------------------------------------------------------------------
# Filesystem Configuration
# -----------------------------------------------------------------------------

def get_supported_audio_formats() -> set[str]:
    """Get set of supported audio file extensions.

    Returns:
        Set of lowercase file extensions (e.g., {'.mp3', '.flac'}).

    Default Formats:
        .mp3, .flac, .m4a, .ogg, .wav, .aac, .wma
    """
    cfg = get_config()
    user_formats = cfg.get("filesystem", {}).get("audio_formats", [])

    defaults = {".mp3", ".flac", ".m4a", ".ogg", ".wav", ".aac", ".wma"}

    if user_formats:
        # User formats completely override defaults
        return {ext.lower() if ext.startswith('.') else f'.{ext.lower()}' for ext in user_formats}

    return defaults


# -----------------------------------------------------------------------------
# Artist Biography Configuration
# -----------------------------------------------------------------------------

def get_musician_terms() -> set[str]:
    """Get terms used to identify musician entities in Wikidata.

    Returns:
        Set of occupation/description terms that indicate a musical artist.

    Note:
        Used for entity disambiguation when multiple Wikidata results
        are returned for an artist name search.
    """
    cfg = get_config()
    user_terms = cfg.get("wikidata", {}).get("musician_terms", [])

    defaults = {
        "singer", "musician", "band", "rapper", "composer", "songwriter",
        "guitarist", "drummer", "bassist", "pianist", "vocalist", "producer",
        "dj", "disc jockey", "recording artist", "musical group", "rock group",
        "pop group", "hip-hop", "hip hop", "jazz", "blues", "country artist",
        "folk singer", "opera", "conductor", "orchestra",
    }

    if user_terms:
        return set(user_terms) | defaults

    return defaults


# =============================================================================
# Missing config getters for config.html page sections
#
# These bridge the gap between what config_helpers.py exposes and what
# config.html renders.  Every top-level section on the config page should
# have a corresponding ``get_*`` function here.
# =============================================================================


# ---------------------------------------------------------------------------
# API Integrations (api_integrations.*)
# ---------------------------------------------------------------------------

def get_api_integrations() -> dict[str, Any]:
    """Get API integration settings (Spotify, Last.fm, Discogs, etc.).

    Config section: ``api_integrations`` in config.yaml

    Returns:
        Dict of ``{service_name: {enabled, api_key, ...}}``.
    """
    cfg = get_config()
    return cfg.get("api_integrations", {})


def get_gemini_config() -> dict[str, Any]:
    """Get the Google Gemini settings.

    Config section: ``api_integrations.gemini`` in config.yaml

    Returns:
        Dict with ``enabled`` (bool), ``api_key`` (str) and ``model`` (str).

    Default Values:
        - enabled: False   (opt-in: it is a billable third-party API)
        - api_key: ""
        - model: "gemini-2.5-flash-image"

    ⚠️ ``api_key`` is read from ``api_integrations.gemini.api_key`` ONLY. It is
    deliberately NOT also accepted from the legacy top-level ``gemini_api_key``
    that a couple of the other services still honour: this key is billable, and
    a key that can arrive from two places cannot be reasoned about (nor
    redacted reliably) when one of them is stale.
    """
    cfg = get_config() or {}
    integration = cfg.get("api_integrations") or {}
    section = integration.get("gemini") if isinstance(integration, dict) else None
    if not isinstance(section, dict):
        section = {}
    model = str(section.get("model") or "").strip() or "gemini-2.5-flash-image"
    return {
        "enabled": bool(section.get("enabled", False)),
        "api_key": str(section.get("api_key", "") or ""),
        "model": model,
    }


def get_genre_cover_config() -> dict[str, Any]:
    """Get the AI genre-playlist cover settings.

    Config section: ``playlists`` in config.yaml

    Returns:
        Dict with ``enabled``, ``regenerate`` and ``prompt_template``.

    Default Values:
        - genre_playlist_ai_cover_enabled: True
        - genre_playlist_ai_cover_regenerate: False
        - genre_playlist_ai_cover_prompt: "{genre}" template (see the service)

    NOTE: applies to the TOP GENRE playlists only. The Essential Collections
    (artist playlists) keep the artist-image cover they already get.

    ⭐⭐ WHY ``enabled`` DEFAULTS TO TRUE (the reversal is deliberate).

    Gemini has EXACTLY ONE consumer in this app: the genre-playlist covers
    (``api_clients/gemini.py`` is imported by nothing else). So a second
    toggle in a DIFFERENT config section asked the user to say the same thing
    twice — "I want Gemini artwork" — and the second one was the one that was
    easy to miss.

    The reported symptom was exactly that: the Gemini key was configured, the
    log said *"Genre playlist covers: OFF — tick 'AI covers for Top Genre
    playlists'"*, and no artwork ever appeared. The billing guard is already
    where it belongs — ``api_integrations.gemini.enabled`` plus a key — so
    the covers toggle is a SECOND opt-in for the SAME billable API, which
    strands the feature instead of protecting it.

    So the toggle now DEFAULTS ON, and the billable-API opt-in remains the
    Gemini integration itself (which is off, with no key, until the user
    supplies one). Turning the covers OFF is still possible — the Config
    page's checkbox writes ``false`` — for anyone who configured Gemini for
    another reason or simply does not want the artwork.
    """
    cfg = get_config() or {}
    p = cfg.get("playlists") or {}
    if not isinstance(p, dict):
        p = {}
    return {
        "enabled": bool(p.get("genre_playlist_ai_cover_enabled", True)),
        "regenerate": bool(p.get("genre_playlist_ai_cover_regenerate", False)),
        "prompt_template": str(p.get("genre_playlist_ai_cover_prompt") or "").strip(),
    }


def get_api_integration(service: str) -> dict[str, Any]:
    """Get settings for a single API integration.

    Args:
        service: Service name e.g. ``"spotify"``, ``"lastfm"``.

    Returns:
        Dict with ``enabled``, ``api_key``, and service-specific fields.
    """
    return get_api_integrations().get(service, {})


# ---------------------------------------------------------------------------
# slskd / Soulseek basic (slskd.*)
# ---------------------------------------------------------------------------

def get_slskd_config() -> dict[str, Any]:
    """Get slskd (Soulseek) base configuration.

    Config section: ``slskd`` in config.yaml

    Returns:
        Dict with keys ``enabled``, ``web_url``, ``api_key``.
        For timeout settings see ``get_slskd_timeouts()``.
    """
    cfg = get_config()
    s = cfg.get("slskd", {})
    return {
        "enabled": bool(s.get("enabled", False)),
        "web_url": str(s.get("web_url", "") or ""),
        "api_key": str(s.get("api_key", "") or ""),
    }


# ---------------------------------------------------------------------------
# Essentia mood/genre scan (essentia.*)
# ---------------------------------------------------------------------------

def get_essentia_config() -> dict[str, Any]:
    """Get Essentia ML mood/genre scan configuration.

    Config section: ``essentia`` in config.yaml

    Returns:
        Dict with script paths, thresholds, and feature toggles.
    """
    cfg = get_config()
    e = cfg.get("essentia", {})
    return {
        "script_path": str(e.get("script_path", "") or ""),
        "models_dir": str(e.get("models_dir", "") or ""),
        "mood_threshold": float(e.get("mood_threshold", 0.005)),
        "per_file_timeout": int(e.get("per_file_timeout", 300)),
        "json_output_dir": str(e.get("json_output_dir", "") or ""),
        "tag_moods": bool(e.get("tag_moods", True)),
        "parse_json_features": bool(e.get("parse_json_features", True)),
        "delete_json_after_import": bool(e.get("delete_json_after_import", True)),
        "tag_genres": bool(e.get("tag_genres", False)),
        "num_genres": int(e.get("num_genres", 3)),
        "genre_threshold": float(e.get("genre_threshold", 15.0)),
        "genre_format": str(e.get("genre_format", "parent_child")),
    }


# ---------------------------------------------------------------------------
# Downloads base settings (downloads.*) — folder, export, format, conversion
# ---------------------------------------------------------------------------

def get_downloads_config() -> dict[str, Any]:
    """Get base download folder/format settings.

    Config section: ``downloads`` in config.yaml

    Returns:
        Dict with folder paths, naming format, and conversion options.
        For matching thresholds see ``get_download_matching_config()``.
    """
    cfg = get_config()
    d = cfg.get("downloads", {})
    return {
        "folder": str(d.get("folder", "") or ""),
        "external_export_path": str(d.get("external_export_path", "") or ""),
        "file_name_format": str(
            d.get("file_name_format",
                  "{album_artist}/{year} - {album}/{track_number}. {artist} - {title}")
            or ""
        ),
        "quality_filter": d.get("quality_filter", {}),
        "conversion": d.get("conversion", {}),
    }


# ---------------------------------------------------------------------------
# Download quality filter
# ---------------------------------------------------------------------------

def get_download_quality_filter() -> dict[str, Any]:
    """Get download quality filter settings.

    Config section: ``downloads.quality_filter`` in config.yaml

    Returns:
        Dict with ``enabled``, ``reject_others``, ``priorities``,
        ``bitrate_tolerance``.
    """
    cfg = get_config()
    qf = cfg.get("downloads", {}).get("quality_filter", {})
    return {
        "enabled": bool(qf.get("enabled", False)),
        "reject_others": bool(qf.get("reject_others", True)),
        "priorities": qf.get("priorities", []),
        "bitrate_tolerance": int(qf.get("bitrate_tolerance", 5)),
    }


# ---------------------------------------------------------------------------
# Queue metadata heal
# ---------------------------------------------------------------------------

def get_metadata_heal_config() -> dict[str, Any]:
    """Get the queue-metadata heal settings.

    Config section: ``downloads.metadata_heal`` in config.yaml

    This heal re-aligns tracks that were imported from the download queue with
    the release their queue row names, for the case where the import wrote a
    different release's album identity (year, release MBID, release group …).

    Returns:
        Dict with ``enabled``, ``run_on_startup``, and ``limit``.

    ``limit`` is a CAP ON QUEUE ROWS EXAMINED per run, because each release
    needs a MusicBrainz lookup and that client is rate-limited at 1 request per
    second — an unbounded first run would crawl. ``0`` means no cap; the run is
    idempotent, so successive runs make progress rather than repeating work.
    """
    cfg = get_config()
    mh = cfg.get("downloads", {}).get("metadata_heal", {})
    try:
        limit = int(mh.get("limit", 200) or 0)
    except (TypeError, ValueError):
        limit = 200
    return {
        "enabled": bool(mh.get("enabled", False)),
        "run_on_startup": bool(mh.get("run_on_startup", True)),
        "limit": max(0, limit),
    }


# ---------------------------------------------------------------------------
# Safe shutdown
# ---------------------------------------------------------------------------

def get_safe_shutdown_config() -> dict[str, Any]:
    """Get the safe-shutdown settings.

    Config section: ``maintenance.safe_shutdown`` in config.yaml

    This quiesces the application so PostgreSQL can be stopped cleanly for an
    update: the scheduler is paused, running scans are asked to stop, the queue
    worker goes idle, and the connection pool is released. Releasing the pool is
    what lets the postmaster's SIGTERM "smart shutdown" finish promptly instead
    of waiting on an idle client until Docker SIGKILLs it.

    Returns:
        Dict with ``enabled``, ``drain_timeout_seconds``,
        ``auto_cancel_after_minutes``, and ``release_connections``.

    ``drain_timeout_seconds`` bounds the wait for in-flight work. A queue item can
    legitimately be mid-move of a large file, and abandoning that is worse than
    reporting it, so a timeout reports what is still running rather than forcing
    the shutdown.

    ``auto_cancel_after_minutes`` bounds how long the quiesced state may persist.
    ``0`` disables the expiry. It defaults to a real value on purpose: a flag that
    never expires would leave the app permanently dormant after a forgotten
    shutdown, which would look like a hang with no visible cause.
    """
    cfg = get_config()
    ss = cfg.get("maintenance", {}).get("safe_shutdown", {})

    def _positive_int(key: str, default: int) -> int:
        try:
            return max(0, int(ss.get(key, default)))
        except (TypeError, ValueError):
            return default

    return {
        "enabled": bool(ss.get("enabled", True)),
        "drain_timeout_seconds": _positive_int("drain_timeout_seconds", 60),
        "auto_cancel_after_minutes": _positive_int("auto_cancel_after_minutes", 120),
        "release_connections": bool(ss.get("release_connections", True)),
    }


# ---------------------------------------------------------------------------
# Album save: background disk phase
# ---------------------------------------------------------------------------

def get_album_save_background_config() -> dict[str, Any]:
    """Get the album-save background-tagging settings.

    Config section: ``album_save.background`` in config.yaml

    An album save rewrites every track's audio file. Past a certain track count
    that outlives Cloudflare's 100s origin timeout (the reported **524**), so the
    disk phase is handed to a background service and the response returns as soon
    as the DATABASE half is committed.

    Returns:
        Dict with ``enabled`` and ``track_threshold``.

    ``track_threshold`` is the number of files at which the disk phase moves off
    the request. Below it the save is exactly what it always was — small albums
    finish well inside the window, and keeping them synchronous means the user
    sees the file tags confirmed in the response they already have.

    ``0`` (or ``enabled: false``) keeps EVERY save inline, which is the pre-change
    behaviour and the right fallback if the background service ever misbehaves.
    """
    cfg = get_config()
    block = cfg.get("album_save", {}).get("background", {})
    try:
        threshold = int(block.get("track_threshold", 40))
    except (TypeError, ValueError):
        threshold = 40
    return {
        "enabled": bool(block.get("enabled", True)),
        "track_threshold": max(0, threshold),
    }


# ---------------------------------------------------------------------------
# Download conversion settings
# ---------------------------------------------------------------------------

def get_download_conversion_config() -> dict[str, Any]:
    """Get download conversion (FLAC→MP3) settings.

    Config section: ``downloads.conversion`` in config.yaml

    Returns:
        Dict with ``enabled``, ``mode``, ``mp3_bitrate_kbps``,
        ``original_handling``, ``original_subfolder``.
    """
    cfg = get_config()
    cv = cfg.get("downloads", {}).get("conversion", {})
    return {
        "enabled": bool(cv.get("enabled", False)),
        "mode": str(cv.get("mode", "flac_to_mp3")),
        "mp3_bitrate_kbps": int(cv.get("mp3_bitrate_kbps", 320)),
        "original_handling": str(cv.get("original_handling", "move_to_original")),
        "original_subfolder": str(cv.get("original_subfolder", "Original")),
    }


# ---------------------------------------------------------------------------
# FLAC conversion modes
# ---------------------------------------------------------------------------
#: ``downloads.conversion.mode`` → the container it converts FLAC into.
#: ``"none"`` (move as-is) is deliberately absent: it converts nothing.
_FLAC_CONVERSION_MODES = {
    "flac_to_mp3": "mp3",
    "flac_to_m4a": "m4a",
}


def conversion_target(mode: Any) -> str:
    """``"mp3"`` / ``"m4a"`` for a conversion mode, ``""`` when it is off.

    One mapping replaces every inline ``mode == "flac_to_mp3"`` comparison in
    the codebase — each one written by hand is a place a NEW mode silently
    falls back to the default. ``download_organize_helpers`` WHITELISTS the
    modes it accepts, so before this existed an unknown value was dropped back
    to ``flac_to_mp3`` rather than rejected.
    """
    return _FLAC_CONVERSION_MODES.get(str(mode or "").strip().lower(), "")


def is_flac_conversion(mode: Any) -> bool:
    """True when ``mode`` converts FLAC files rather than moving them as-is."""
    return bool(conversion_target(mode))


# ---------------------------------------------------------------------------
# Generic feature flags (features.*)
# ---------------------------------------------------------------------------

# Feature flags whose default is True when absent from config.  ``get_feature``
# already honours a caller-supplied default, so this only documents the
# scan-behaviour flags that ship enabled.
#
# repair_album_annotations:
#   Collapse duplicated bracketed annotations in album names and track titles
#   inline during a metadata/full scan ("The Fall of Hearts (tour edition)
#   (tour edition)" → "The Fall of Hearts (tour edition)").  One copy of every
#   distinct annotation is preserved, so editions are never lost.  Set false to
#   disable the inline repair without a code change.
_DEFAULT_FEATURE_FLAGS: dict[str, Any] = {
    "repair_album_annotations": True,
}


def get_features_config() -> dict[str, Any]:
    """Get all feature flags and toggles.

    Config section: ``features`` in config.yaml

    Returns:
        Dict of feature flags including sync, scheduler, daily-release,
        and mature-track settings.  Flags in ``_DEFAULT_FEATURE_FLAGS`` are
        present even when absent from the config file, so a fresh install
        gets the intended behaviour with no config edit.
    """
    cfg = get_config()
    features = cfg.get("features", {})
    if not isinstance(features, dict):
        features = {}
    merged = dict(_DEFAULT_FEATURE_FLAGS)
    merged.update(features)
    return merged


def get_feature(key: str, default: Any = None) -> Any:
    """Get a single feature flag value.

    Args:
        key: Dot-separated path e.g. ``"sync_ratings_to_all_users"``
             or ``"retry_scheduler.interval_seconds"``.
        default: Fallback value.
    """
    cfg = get_features_config()
    parts = key.split(".")
    val: Any = cfg
    for part in parts:
        if isinstance(val, dict):
            val = val.get(part)
        else:
            return default
    return val if val is not None else default


# ---------------------------------------------------------------------------
# Navidrome base config (navidrome.*)  — not per-user, the shared part
# ---------------------------------------------------------------------------

def get_navidrome_base_config() -> dict[str, Any]:
    """Get base Navidrome server settings (not per-user).

    Config section: ``navidrome`` in config.yaml minus per-user overrides.

    Returns:
        Dict of shared Navidrome settings (host, port, protocol).
    """
    n = get_navidrome_config()
    return dict(n)


# ---------------------------------------------------------------------------
# Upcoming releases sources (upcoming_releases_sources or similar)
# ---------------------------------------------------------------------------

def get_upcoming_releases_sources() -> list[dict[str, Any]]:
    """Get Wikipedia scraping sources for upcoming album releases.

    Config section: ``upcoming_releases.sources`` in config.yaml — the same
    key the config page (``static/js/config.js``) reads and writes. The old
    top-level ``upcoming_releases_sources`` key is honoured as a fallback.

    Each source dict should contain:
        - ``key``: Unique source key (e.g. ``"2026_rock"``)
        - ``name``: Display name
        - ``url``: Wikipedia URL
        - ``columns``: Comma-separated column order (day, artist, album, genre)
        - ``enabled``: Whether the source should be scraped (default True)

    Returns:
        List of source dicts.
    """
    cfg = get_config()
    sources = (cfg.get("upcoming_releases") or {}).get("sources")
    if not isinstance(sources, list):
        sources = cfg.get("upcoming_releases_sources", [])
    return sources


# ---------------------------------------------------------------------------
# Search strip keywords (search.strip_keywords or normalization config)
# ---------------------------------------------------------------------------

def get_search_strip_keywords() -> list[str]:
    """Get keywords that trigger parenthetical-content removal during API searches.

    Config section: ``search.strip_keywords`` in config.yaml

    Returns:
        List of keyword strings (e.g. ``["remastered", "radio edit"]``).
    """
    cfg = get_config()
    return cfg.get("search", {}).get("strip_keywords", [
        "remastered", "remaster", "radio edit", "single version",
        "album version", "bonus track", "bonus",
    ])


# ---------------------------------------------------------------------------
# Navidrome users (navidrome_users[])
# ---------------------------------------------------------------------------

def get_navidrome_users() -> list[dict[str, Any]]:
    """Get all configured Navidrome user accounts.

    Each user may include display_name, base_url, user (username), pass,
    spotify_username, spotify_client_id, spotify_client_secret,
    lastfm_username, listenbrainz_user_token.

    Returns:
        List of user dicts.
    """
    cfg = get_config()
    return cfg.get("navidrome_users", [])


def get_navidrome_users_normalized() -> list[dict[str, str]]:
    """All configured Navidrome accounts, normalized to ``base_url``/``user``/``pass``.

    Falls back to the legacy top-level ``navidrome`` section when
    ``navidrome_users`` is empty, accepts ``username``/``password`` aliases,
    strips trailing slashes from ``base_url`` and drops entries without a
    URL — previously each consumer re-implemented this with subtly different
    fallbacks.
    """
    cfg = get_config()
    raw = cfg.get("navidrome_users") or []
    if not raw:
        nav = cfg.get("navidrome") or {}
        if isinstance(nav, dict) and (nav.get("base_url") or nav.get("user")):
            raw = [nav]
    normalized: list[dict[str, str]] = []
    for u in raw:
        if not isinstance(u, dict):
            continue
        entry = {
            "base_url": str(u.get("base_url", "") or "").rstrip("/"),
            "user": str(u.get("user", u.get("username", "")) or ""),
            "pass": str(u.get("pass", u.get("password", "")) or ""),
        }
        if entry["base_url"]:
            normalized.append(entry)
    return normalized


def get_navidrome_first_user() -> dict[str, str]:
    """Primary Navidrome account as a normalized dict (``{}`` when unset).

    Returns ``{}`` when no USABLE Navidrome user exists — i.e. every
    configured user has an empty ``base_url`` / ``user`` / ``pass`` (e.g. a
    setup-wizard stub row).  Callers MUST treat ``{}`` as "not configured":
    building a client from empty fields produces requests to ``/rest/...``
    (no host) which fail with ``unknown url type`` — exactly the failure
    that left the import scan importing 0 artists and the ``tracks`` table
    permanently bare.

    Env fallback (``NAV_BASE_URL``/``NAV_USER``/``NAV_PASS``) is included so
    container/env-based installs resolve the same way as config-file ones.
    """
    users = get_navidrome_users_normalized()
    if users:
        first = users[0] or {}
        if all([first.get("base_url"), first.get("user"), first.get("pass")]):
            return first
        # First configured user is empty — keep scanning for a usable one
        # before giving up (some installs leave a stub row and put the real
        # creds in a later entry).
        for u in users[1:]:
            if u and all([u.get("base_url"), u.get("user"), u.get("pass")]):
                return u
        return {}
    base_url = os.environ.get("NAV_BASE_URL", "").strip().rstrip("/")
    user = os.environ.get("NAV_USER", "").strip()
    pw = os.environ.get("NAV_PASS", "").strip()
    if base_url and user and pw:
        return {"base_url": base_url, "user": user, "pass": pw}
    return {}


# =============================================================================
# MusicBrainz User-Agent
# =============================================================================

_DEFAULT_MUSICBRAINZ_USER_AGENT = "Popularr/1.0 +https://github.com/M0VENTURA/Popularr"


def get_musicbrainz_user_agent() -> str:
    """Get the MusicBrainz API User-Agent string.

    Returns:
        User-Agent string identifying the application to MusicBrainz.
        MusicBrainz requires a unique UA for API access.

    Default:
        "Popularr/1.0 +https://github.com/M0VENTURA/Popularr"
    """
    cfg = get_config()
    return str(
        cfg.get("musicbrainz", {})
        .get("user_agent", _DEFAULT_MUSICBRAINZ_USER_AGENT)
    )


# =============================================================================
# AudioDB Configuration
# =============================================================================

def get_audiodb_config() -> dict[str, Any]:
    """Get AudioDB API configuration.

    Config section: ``api_integrations.audiodb`` in config.yaml

    Returns:
        Dict with keys ``api_key`` (str) and ``enabled`` (bool).

    Default API Key:
        "195003" (AudioDB demo/public key)
    """
    cfg = get_config()
    audiodb = cfg.get("api_integrations", {}).get("audiodb", {})
    return {
        "api_key": str(audiodb.get("api_key", "195003") or ""),
        "enabled": bool(audiodb.get("enabled", True)),
    }


# =============================================================================
# Database & State Directories
# =============================================================================

def get_config_directory() -> str:
    """The directory holding ``config.yaml`` — the persistent config volume.

    User-editable DATA that is not itself YAML lives beside the config file, in
    the ``config_data`` volume, so it survives a container rebuild. The genre
    vocabulary is the first such file; keeping it here rather than in the repo
    tree is what lets a user edit it from the Config page without the next
    ``docker compose build`` overwriting it.

    Returns:
        Absolute path string. Defaults to ``/config`` (``CONFIG_PATH``'s dirname).
    """
    return os.path.dirname(os.environ.get("CONFIG_PATH", "/config/config.yaml"))


def get_state_directory() -> str:
    """Get the directory for runtime state/progress files.
    Holds non-database JSON state: API rate-limiter counters, scan progress,
    playlist sessions, etc.  This is NOT the application database — the app is
    PostgreSQL-only and the DB connection is configured via PG_* / config.yaml.

    Returns:
        Absolute path string.

    Default:
        Value of env var ``SCAN_STATE_DIR``, or ``/state``.
    """
    return os.environ.get("SCAN_STATE_DIR", "/state")


def get_api_rate_limiter_state_file() -> str:
    """Get the file path for API rate limiter state persistence.

    Returns:
        Absolute path string.

    Default:
        ``/state/api_rate_limiter_state.json``
    """
    return os.path.join(get_state_directory(), "api_rate_limiter_state.json")


def get_navidrome_progress_file() -> str:
    """Get the file path for Navidrome scan progress tracking.

    Returns:
        Absolute path string (may be overridden by env var).

    Default:
        ``/state/navidrome_scan_progress.json``
    """
    return os.environ.get(
        "NAVIDROME_PROGRESS_FILE",
        os.path.join(get_state_directory(), "navidrome_scan_progress.json"),
    )


# =============================================================================
# Pre-scan Batch Configuration
# =============================================================================

def get_pre_scan_config() -> dict[str, Any]:
    """Get pre-scan task tuning parameters.

    Config section: ``filesystem.pre_scan`` in config.yaml

    Returns:
        Dict with keys ``interval_seconds``, ``batch_size``.

    Default Values:
        - interval_seconds: 120
        - batch_size: 50
    """
    cfg = get_config()
    pre_scan = cfg.get("filesystem", {}).get("pre_scan", {})
    return {
        "interval_seconds": int(pre_scan.get("interval_seconds", 120)),
        "batch_size": int(pre_scan.get("batch_size", 50)),
    }


# =============================================================================
# Filesystem Cache Configuration
# =============================================================================

def get_filesystem_cache_config() -> dict[str, Any]:
    """Get filesystem cache TTL configuration.

    Config section: ``filesystem.cache`` in config.yaml

    Returns:
        Dict with keys ``music_dir_cache_ttl``, ``downloads_dir_cache_ttl``.

    Default Values:
        - music_dir_cache_ttl: 60.0 seconds
        - downloads_dir_cache_ttl: 30.0 seconds
    """
    cfg = get_config()
    cache = cfg.get("filesystem", {}).get("cache", {})
    return {
        "music_dir_cache_ttl": float(cache.get("music_dir_cache_ttl", 60.0)),
        "downloads_dir_cache_ttl": float(cache.get("downloads_dir_cache_ttl", 30.0)),
    }


# =============================================================================
# Thread Pool Configuration
# =============================================================================

def get_thread_pool_config() -> dict[str, Any]:
    """Get thread pool size configuration.

    Config section: ``system.thread_pools`` in config.yaml

    Returns:
        Dict with keys ``max_workers`` (default: 20).

    Default Values:
        - max_workers: 20
    """
    cfg = get_config()
    pool = cfg.get("system", {}).get("thread_pools", {})
    return {
        "max_workers": int(pool.get("max_workers", 20)),
    }


# =============================================================================
# Scan Pipeline Configuration
# =============================================================================

def get_scan_pipeline_config() -> dict[str, Any]:
    """Get scan pipeline tuning parameters.

    Config section: ``scanning.pipeline`` in config.yaml

    Returns:
        Dict with keys ``batch_size``, ``page_size``, ``max_workers``,
        ``library_sync_batch_size``.

    Default Values:
        - batch_size: 50
        - page_size: 500
        - max_workers: 10
        - library_sync_batch_size: 1000
    """
    cfg = get_config()
    pipeline = cfg.get("scanning", {}).get("pipeline", {})
    return {
        "batch_size": int(pipeline.get("batch_size", 50)),
        "page_size": int(pipeline.get("page_size", 500)),
        "max_workers": int(pipeline.get("max_workers", 10)),
        "library_sync_batch_size": int(pipeline.get("library_sync_batch_size", 1000)),
    }


def get_track_timeout_seconds() -> int:
    """THE single scan-timeout knob: the per-track wall-clock allowance (s).

    Consolidated (2026-10-13): the old per-album and per-artist dials are
    gone — every scan budget is now DERIVED from this one value:

    * Per-album budget = ``track_timeout_seconds * track_count`` — each track
      of the album gets this much wall-clock, so a big compilation that must
      look up every track online is never cut off mid-progress, while a
      genuinely hung album is still bounded by the same product.  The fast
      one-shot album phases (enrichment, post-singles) get the flat allowance.
      See ``scan_stage_runner._resolve_album_budget`` / ``_scale_album_budget``.
    * Per-artist budget = fixed overhead (1800 s: Navidrome import + the
      Essentia pass, which have no per-track granularity) plus
      ``track_timeout_seconds`` times the artist's TOTAL track count — so
      "Various Artists", one artist holding every compilation in the library,
      is sized by its real work and never abandoned for being large.
      See the artist-budget block in ``popularity_pipeline``.

    ``0`` disables the safety net ENTIRELY: no album is ever skipped and no
    artist is ever abandoned.

    Config section: ``popularity.track_timeout_seconds`` in config.yaml.

    Default: 30 (s).  Clamped 10-600.
    """
    cfg = get_config()
    try:
        raw = (cfg.get("popularity") or {}).get("track_timeout_seconds", 30)
    except Exception:
        raw = 30
    if raw is None:
        return 30
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 30
    if value <= 0:
        # Explicit opt-out: no scan timeouts at all.
        return 0
    return max(10, min(value, 600))


def get_prefetch_budget_seconds() -> int:
    """Return the per-artist prefetch / post-singles enrichment budget (seconds).

    The per-artist prefetch and the per-album post-singles enrichment make
    synchronous network calls (Last.fm / ListenBrainz bulk lookups, MusicBrainz
    + Discogs release fetches, missing-release tracklists).  Each runs inside
    ``_bounded_call`` with a hard wall-clock budget; when the budget expires
    the call is abandoned and its caches stay cold for that album.  The
    default is kept LOW so a stalled bulk call is abandoned quickly and the
    scan keeps moving — the old system's key advantage.  Heavy catalogs can
    raise it in config.

    Config section: ``popularity.prefetch_budget_seconds`` in config.yaml.

    Default: 180 (3 min).  Clamped 120-1800.
    """
    cfg = get_config()
    try:
        budget = int((cfg.get("popularity") or {}).get("prefetch_budget_seconds", 180) or 180)
    except (TypeError, ValueError):
        budget = 180
    return max(120, min(budget, 1800))


# =============================================================================
# Matching & Threshold Configuration
# =============================================================================

def get_matching_thresholds() -> dict[str, Any]:
    """Get general matching threshold defaults.

    Config section: ``matching`` in config.yaml

    Returns:
        Dict with keys ``fuzzy_threshold``, ``score_threshold``.

    Default Values:
        - fuzzy_threshold: 0.80
        - score_threshold: 0.60
    """
    cfg = get_config()
    matching = cfg.get("matching", {})
    return {
        "fuzzy_threshold": float(matching.get("fuzzy_threshold", 0.80)),
        "score_threshold": float(matching.get("score_threshold", 0.60)),
    }


# =============================================================================
# Download Quality / Search Configuration
# =============================================================================

def get_search_quality_config() -> dict[str, Any]:
    """Get download search quality defaults.

    Config section: ``downloads.quality`` in config.yaml

    Returns:
        Dict with keys ``min_bitrate``, ``min_sample_rate``,
        ``min_length_seconds``, ``allow_low_quality_fallback``,
        ``fallback_min_bitrate``, ``allowed_formats``.

    Default Values:
        - min_bitrate: 192
        - min_sample_rate: 44100
        - min_length_seconds: 30
        - allow_low_quality_fallback: False
        - fallback_min_bitrate: 128
        - allowed_formats: {"flac", "mp3"}

    ``allowed_formats`` is the set of container extensions a DOWNLOAD may be
    accepted in (bare, lowercase, no dot). Only FLAC and MP3 are enabled by
    default: every other container — AIFF, WAV, ALAC, DSF, APE, WV — either
    cannot be written with tags by ``tag_file_service`` (its writer supports
    only mp3/flac/m4a/mp4, so an AIFF landed with NO metadata applied) or is
    otherwise not wanted in the library. A file whose type is not allowed is
    REJECTED at search time (never offered as a match) and, if one slips
    through, rejected from the queue at completion instead of being imported
    tagless. An empty list disables the check (accept any type).
    """
    cfg = get_config()
    quality = cfg.get("downloads", {}).get("quality", {})
    _raw_formats = quality.get("allowed_formats", ["flac", "mp3"])
    # Normalise to bare lowercase extensions so ".FLAC", "FLAC" and "flac"
    # all mean the same type (the config page and a hand-edited config.yaml
    # can spell it either way).
    allowed_formats = {
        str(ext).strip().lower().lstrip(".")
        for ext in (_raw_formats or [])
        if str(ext).strip()
    }
    return {
        "min_bitrate": int(quality.get("min_bitrate", 192)),
        "min_sample_rate": int(quality.get("min_sample_rate", 44100)),
        "min_length_seconds": int(quality.get("min_length_seconds", 30)),
        "allow_low_quality_fallback": bool(quality.get("allow_low_quality_fallback", False)),
        "fallback_min_bitrate": int(quality.get("fallback_min_bitrate", 128)),
        "allowed_formats": allowed_formats,
    }


def get_search_fuzzy_config() -> dict[str, Any]:
    """Get library-search fuzzy matching configuration.

    Config section: ``search.fuzzy`` in config.yaml

    Returns:
        Dict with keys ``enabled`` (default True).

    When enabled (default), ``POST /api/search`` ranks results with
    ``pg_trgm`` similarity as a tiebreaker after the exact/prefix/contains
    tiers, so typo'd queries ("Sipce Girls") still surface "Spice Girls".
    Requires the ``pg_trgm`` extension + GIN trigram indexes (migration 010);
    when the extension is absent the endpoint transparently falls back to
    the legacy ranking.
    """
    cfg = get_config()
    fuzzy = cfg.get("search", {}).get("fuzzy", {})
    return {
        "enabled": bool(fuzzy.get("enabled", True)),
    }


# =============================================================================
# Queue Worker Configuration
# =============================================================================

def get_logging_config() -> dict[str, Any]:
    """Get logging configuration.

    Config section: ``logging`` in config.yaml (with a legacy fallback to the
    top-level ``log_level`` key / ``LOG_LEVEL`` env var).

    Returns:
        Dict with key ``level`` — one of ``debug``, ``info``, ``warning``,
        ``error``.  Defaults to ``info`` (debug off).
    """
    cfg = get_config()
    logging_cfg = cfg.get("logging", {}) or {}
    raw = logging_cfg.get("level") or cfg.get("log_level")
    if not raw:
        raw = os.environ.get("LOG_LEVEL") or os.environ.get("SPTNR_LOG_LEVEL") or "info"
    level = str(raw).strip().lower()
    valid = {"debug", "info", "warning", "error", "critical"}
    if level not in valid:
        level = "info"
    return {"level": level}


def get_queue_worker_config() -> dict[str, Any]:
    """Get background queue worker configuration.

    Config section: ``queue.worker`` in config.yaml

    Returns:
        Dict with keys ``interval_seconds``, ``batch_size``,
        ``max_in_flight``.

    Default Values:
        - interval_seconds: 60
        - batch_size: 50
        - max_in_flight: 15
    """
    cfg = get_config()
    worker = cfg.get("queue", {}).get("worker", {})
    return {
        "interval_seconds": int(worker.get("interval_seconds", 60)),
        "batch_size": int(worker.get("batch_size", 50)),
        "max_in_flight": int(worker.get("max_in_flight", 15)),
    }


# =============================================================================
# End of config helpers
# =============================================================================
