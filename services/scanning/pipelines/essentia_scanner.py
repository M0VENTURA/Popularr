"""
Essentia mood scan: local ML mood tagging via the Essentia-to-Metadata script.

Migrated from ``old_system/essentia_mood_scan.py``.

This module runs the ``tag_music.py`` script from
https://github.com/WB2024/Essentia-to-Metadata as a subprocess for each
track that has a resolvable ``file_path``, then reads the MOOD (and
optionally GENRE) tag the external tool wrote back from the audio file and
persists it in the database.

Configuration (config.yaml, ``essentia`` section):
    script_path     – absolute path to ``tag_music.py``  (required; skip scan if blank)
    models_dir      – directory containing the Essentia ``.pb`` / ``.json`` models
    mood_threshold  – minimum activation probability for a mood tag
    per_file_timeout – seconds to wait per file before skipping
    tag_genres      – also write genre tags (default: False)
    num_genres      – number of genre tags to write per track (default: 3)
    genre_threshold – genre confidence threshold in percent (default: 15.0)
    genre_format    – genre tag format: parent_child | child_parent | child_only | raw
    tag_moods       – write mood tags (default: True)
    parse_json_features – import BPM / danceability from Essentia JSON sidecars
    delete_json_after_import – delete consumed Essentia JSON sidecars after import
    json_output_dir – optional directory where Essentia writes JSON sidecars
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import structlog

from db.utils import get_db_connection_raw, is_transient_pg_startup_error
from helpers.logging_config import log_unified
from services.metadata.tag_file_service import (
    sync_track_tags_to_file,
    update_file_tags,
)

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Bundled defaults (set as ENV vars in the official Docker image)
# ---------------------------------------------------------------------------
_BUNDLED_SCRIPT_PATH = "/opt/Essentia-to-Metadata/tag_music.py"
_BUNDLED_MODELS_DIR = "/opt/essentia_models"

_ESSENTIA_NO_MODELS_PHRASE = "no classifier models were configured by default"
_ESSENTIA_GENRE_SEPARATOR = "---"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_get(row: Any, key: str, index: int = 0, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    if hasattr(row, "keys"):
        try:
            return row[key]
        except Exception:
            pass
    try:
        return row[index]
    except Exception:
        return default


def _stop_requested(progress_file: str) -> bool:
    if not progress_file or not os.path.exists(progress_file):
        return False
    try:
        with open(progress_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("stop_requested")) or (
            data.get("status") == "stopped" and not bool(data.get("is_running", False))
        )
    except Exception:
        return False


def _write_progress(progress_file: str, payload: dict[str, Any]) -> None:
    if not progress_file:
        return
    try:
        _dir = os.path.dirname(progress_file) or "."
        os.makedirs(_dir, exist_ok=True)
        _fd, _tmp = tempfile.mkstemp(dir=_dir, suffix=".tmp")
        try:
            with os.fdopen(_fd, "w", encoding="utf-8") as _f:
                json.dump(payload, _f)
            os.replace(_tmp, progress_file)
        except Exception:
            try:
                os.unlink(_tmp)
            except OSError:
                pass
            raise
    except Exception as exc:
        logger.debug("Failed writing essentia progress", error=str(exc))


# ---------------------------------------------------------------------------
# Database write helpers
# ---------------------------------------------------------------------------

#: psycopg2 raises these once a cursor/connection has been closed. A dropped
#: connection closes its cursor with it, so the FIRST failure carries the real
#: cause ("server closed the connection unexpectedly") and every later attempt
#: on the SAME cursor reports only one of these.
_CLOSED_CONNECTION_MARKERS = (
    "cursor already closed",
    "connection already closed",
    "connection is closed",
)


def _is_lost_connection(exc: BaseException) -> bool:
    """Return True when ``exc`` means the connection can no longer be used.

    Treating only the ORIGINAL drop as recoverable would be useless: the
    cursor dies with the connection, so every subsequent write raises
    "cursor already closed" and a scan would give up on each remaining track
    while reporting the symptom rather than the cause.
    """
    message = str(exc or "").lower()
    if any(marker in message for marker in _CLOSED_CONNECTION_MARKERS):
        return True
    return is_transient_pg_startup_error(exc)


def _update_with_reconnect(
    conn: Any,
    cursor: Any,
    sql: str,
    params: tuple[Any, ...],
) -> tuple[Any, Any, int]:
    """Run an UPDATE + COMMIT, reconnecting ONCE if the connection was lost.

    Returns ``(conn, cursor, rowcount)``. The caller MUST rebind its own
    ``conn``/``cursor`` from the result, because the retry runs on a NEW
    connection and the old cursor is dead.

    Why this exists: the scan holds ONE raw connection for the whole run and
    runs an Essentia subprocess (up to ``per_file_timeout`` seconds) between
    writes, so the connection sits idle for long stretches. If PostgreSQL
    drops it — a server restart, a network blip, or
    ``idle_in_transaction_session_timeout`` — the cursor is closed with it.
    Without a retry the very next write fails with "cursor already closed"
    and EVERY remaining track is silently skipped (each one logging the
    symptom, not the cause). One drop becomes one reconnect.

    Bounded: at most 2 attempts, so a genuinely broken database still fails
    fast rather than looping.
    """
    attempts = 0
    while True:
        attempts += 1
        try:
            cursor.execute(sql, params)
            conn.commit()
            return conn, cursor, int(getattr(cursor, "rowcount", 0) or 0)
        except Exception as exc:
            if attempts > 1 or not _is_lost_connection(exc):
                raise
            logger.warning(
                "Essentia scan: database connection lost, reconnecting",
                error=str(exc),
            )
            log_unified("Essentia Scan - DB connection lost, reconnecting…")
            try:
                conn.close()
            except Exception:
                pass
            conn = get_db_connection_raw(reason="essentia_scan_reconnect")
            cursor = conn.cursor()


# ---------------------------------------------------------------------------
# Tag reading helpers
# ---------------------------------------------------------------------------

def _read_essentia_mood_from_file(file_path: str) -> str | None:
    try:
        import mutagen
        from mutagen.flac import FLAC
        from mutagen.id3 import ID3
        from mutagen.mp4 import MP4
        from mutagen.oggvorbis import OggVorbis
        from mutagen.oggopus import OggOpus
        from mutagen.asf import ASF
    except ImportError as exc:
        logger.warning(
            "mutagen not available, cannot read Essentia tags",
            error=str(exc),
        )
        return None

    ext = os.path.splitext(file_path)[1].lower()
    mood: str | None = None

    try:
        if ext == ".flac":
            audio = FLAC(file_path)
            raw = audio.get("MOOD") or audio.get("mood")
            mood = raw[0] if raw else None
        elif ext == ".mp3":
            audio = ID3(file_path)
            for tag in audio.getall("COMM"):
                if getattr(tag, "desc", "") == "Essentia Mood":
                    texts = getattr(tag, "text", [])
                    if texts:
                        mood = str(texts[0])
                    break
        elif ext in (".ogg", ".oga"):
            audio = OggVorbis(file_path)
            raw = audio.get("MOOD") or audio.get("mood")
            mood = raw[0] if raw else None
        elif ext == ".opus":
            audio = OggOpus(file_path)
            raw = audio.get("MOOD") or audio.get("mood")
            mood = raw[0] if raw else None
        elif ext in (".m4a", ".m4b", ".mp4", ".aac"):
            audio = MP4(file_path)
            raw = audio.get("----:com.apple.iTunes:MOOD")
            if raw:
                first = raw[0]
                mood = first.decode("utf-8") if hasattr(first, "decode") else str(first)
        elif ext == ".wma":
            audio = ASF(file_path)
            raw = audio.get("WM/Mood")
            mood = str(raw[0]) if raw else None
        else:
            audio = mutagen.File(file_path)
            if audio and audio.tags:
                for key in ("Mood", "MOOD", "mood"):
                    val = audio.tags.get(key)
                    if val:
                        mood = str(val[0]) if isinstance(val, list) else str(val)
                        break
    except Exception as exc:
        logger.debug(
            "Failed to read Essentia mood from file",
            file=file_path,
            error=str(exc),
        )
        return None
    if not mood:
        return None
    return mood.strip() if mood.strip() else None


def _read_essentia_genre_from_file(file_path: str) -> str | None:
    try:
        import mutagen
        from mutagen.flac import FLAC
        from mutagen.id3 import ID3
        from mutagen.mp4 import MP4
        from mutagen.oggvorbis import OggVorbis
        from mutagen.oggopus import OggOpus
        from mutagen.asf import ASF
    except ImportError as exc:
        logger.warning(
            "mutagen not available, cannot read Essentia genre",
            error=str(exc),
        )
        return None

    ext = os.path.splitext(file_path)[1].lower()
    genres: str | None = None

    try:
        if ext == ".flac":
            audio = FLAC(file_path)
            raw = audio.get("GENRE") or audio.get("genre")
            if raw:
                genres = "; ".join(str(v) for v in raw if v)
        elif ext == ".mp3":
            audio = ID3(file_path)
            tcon = audio.get("TCON")
            if tcon:
                texts = getattr(tcon, "text", [])
                if texts:
                    genres = "; ".join(str(t) for t in texts if t)
        elif ext in (".ogg", ".oga"):
            audio = OggVorbis(file_path)
            raw = audio.get("GENRE") or audio.get("genre")
            if raw:
                genres = "; ".join(str(v) for v in raw if v)
        elif ext == ".opus":
            audio = OggOpus(file_path)
            raw = audio.get("GENRE") or audio.get("genre")
            if raw:
                genres = "; ".join(str(v) for v in raw if v)
        elif ext in (".m4a", ".m4b", ".mp4", ".aac"):
            audio = MP4(file_path)
            raw = audio.get("\xa9gen")
            if raw:
                genres = "; ".join(str(v) for v in raw if v)
        elif ext == ".wma":
            audio = ASF(file_path)
            raw = audio.get("WM/Genre")
            if raw:
                genres = "; ".join(str(v) for v in raw if v)
        else:
            audio = mutagen.File(file_path)
            if audio and audio.tags:
                for key in ("Genre", "GENRE", "genre", "\xa9gen"):
                    val = audio.tags.get(key)
                    if val:
                        genres = "; ".join(str(v) for v in (val if isinstance(val, list) else [val]) if v)
                        break
    except Exception as exc:
        logger.debug(
            "Failed to read Essentia genre from file",
            file=file_path,
            error=str(exc),
        )
        return None
    return genres.strip() if genres and genres.strip() else None


def _extract_child_genres(genre_str: str) -> list[str]:
    if not genre_str:
        return []
    child_genres: list[str] = []
    seen_lower: set = set()
    for part in re.split(r"[;,]", genre_str):
        part = part.strip()
        if not part:
            continue
        part = re.sub(r":\s*\d+\.?\d*\s*%?\s*$", "", part).strip()
        if _ESSENTIA_GENRE_SEPARATOR in part:
            child = part.split(_ESSENTIA_GENRE_SEPARATOR, 1)[-1].strip()
        else:
            child = part
        if child and child.lower() not in seen_lower:
            seen_lower.add(child.lower())
            child_genres.append(child)
    return child_genres


def _read_existing_tcon_genres(file_path: str) -> list[str]:
    try:
        import mutagen
        from mutagen.id3 import ID3
        from mutagen.flac import FLAC
        from mutagen.mp4 import MP4
        from mutagen.oggvorbis import OggVorbis
        from mutagen.oggopus import OggOpus
        from mutagen.asf import ASF
    except ImportError:
        return []

    ext = os.path.splitext(file_path)[1].lower()
    raw_genres: list[str] = []

    try:
        if ext == ".mp3":
            audio = ID3(file_path)
            tcon = audio.get("TCON")
            if tcon:
                raw_genres = [str(t).strip() for t in getattr(tcon, "text", []) if str(t).strip()]
        elif ext == ".flac":
            audio = FLAC(file_path)
            raw = audio.get("GENRE") or audio.get("genre") or []
            raw_genres = [str(v).strip() for v in raw if str(v).strip()]
        elif ext in (".ogg", ".oga"):
            audio = OggVorbis(file_path)
            raw = audio.get("GENRE") or audio.get("genre") or []
            raw_genres = [str(v).strip() for v in raw if str(v).strip()]
        elif ext == ".opus":
            audio = OggOpus(file_path)
            raw = audio.get("GENRE") or audio.get("genre") or []
            raw_genres = [str(v).strip() for v in raw if str(v).strip()]
        elif ext in (".m4a", ".m4b", ".mp4", ".aac"):
            audio = MP4(file_path)
            raw = audio.get("\xa9gen") or []
            raw_genres = [str(v).strip() for v in raw if str(v).strip()]
        elif ext == ".wma":
            audio = ASF(file_path)
            raw = audio.get("WM/Genre") or []
            raw_genres = [str(v).strip() for v in raw if str(v).strip()]
    except Exception as exc:
        logger.debug(
            "Failed to read existing genres from file",
            file=file_path,
            error=str(exc),
        )

    return [g for g in raw_genres if _ESSENTIA_GENRE_SEPARATOR not in g]


def _merge_genres(existing: list[str], new_genres: list[str]) -> list[str]:
    seen_lower = {g.lower() for g in existing}
    result = list(existing)
    for g in new_genres:
        if g.lower() not in seen_lower:
            seen_lower.add(g.lower())
            result.append(g)
    return result


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value) if isinstance(value, str) and value.strip() else float(value)
    except Exception:
        return None


def _read_numeric_tag_from_file(file_path: str, *keys: str) -> float | None:
    try:
        import mutagen
        audio = mutagen.File(file_path)
        if not audio or not audio.tags:
            return None
        for key in keys:
            raw = audio.tags.get(key) or audio.tags.get(key.lower()) or audio.tags.get(key.upper())
            if raw is None:
                continue
            if isinstance(raw, list) and raw:
                value = _coerce_float(raw[0])
                if value is not None:
                    return value
            if hasattr(raw, "text"):
                text_values = getattr(raw, "text", [])
                if text_values:
                    value = _coerce_float(text_values[0])
                    if value is not None:
                        return value
            value = _coerce_float(raw)
            if value is not None:
                return value
    except Exception as exc:
        logger.debug(
            "Failed to read numeric tag from file",
            file=file_path,
            error=str(exc),
        )
    return None


def _candidate_sidecar_paths(file_path: str, json_output_dir: str = "") -> list[Path]:
    src = Path(file_path)
    candidates = [
        src.with_suffix(src.suffix + ".json"),
        src.with_suffix(".json"),
        src.parent / f"{src.stem}.json",
    ]
    if json_output_dir:
        out = Path(json_output_dir)
        candidates.extend([
            out / f"{src.name}.json",
            out / f"{src.stem}.json",
        ])
    dedup: list[Path] = []
    seen = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        dedup.append(path)
    return dedup


def _extract_first_numeric(payload: Any, key_candidates: list[str]) -> float | None:
    if isinstance(payload, dict):
        for key in key_candidates:
            if key in payload:
                value = _coerce_float(payload.get(key))
                if value is not None:
                    return value
        for value in payload.values():
            nested = _extract_first_numeric(value, key_candidates)
            if nested is not None:
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = _extract_first_numeric(item, key_candidates)
            if nested is not None:
                return nested
    return None


def _extract_first_string(payload: Any, key_candidates: list[str]) -> str | None:
    """First non-empty string found under any candidate key (nested walk)."""
    if isinstance(payload, dict):
        for key in key_candidates:
            if key in payload:
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        for value in payload.values():
            nested = _extract_first_string(value, key_candidates)
            if nested is not None:
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = _extract_first_string(item, key_candidates)
            if nested is not None:
                return nested
    return None


def _read_key_tag_from_file(file_path: str) -> str | None:
    """Read a musical-key tag from the audio file (TKEY / KEY / InitialKey)."""
    try:
        import mutagen
        audio = mutagen.File(file_path)
        if not audio or not audio.tags:
            return None
        for key in ("TKEY", "key", "initialkey"):
            raw = audio.tags.get(key)
            if raw is None:
                continue
            if isinstance(raw, list) and raw and str(raw[0]).strip():
                return str(raw[0]).strip()
            if hasattr(raw, "text"):
                vals = getattr(raw, "text", []) or []
                if vals and str(vals[0]).strip():
                    return str(vals[0]).strip()
            if str(raw).strip():
                return str(raw).strip()
    except Exception as exc:
        logger.debug(
            "Failed to read key tag from file",
            file=file_path,
            error=str(exc),
        )
    return None


def _read_essentia_features_from_json(file_path: str, json_output_dir: str = "") -> dict[str, Any]:
    bpm_keys = ["bpm", "tempo", "rhythm.bpm", "musicbrainz.bpm"]
    dance_keys = ["danceability", "rhythm.danceability", "highlevel.danceability.all.danceable"]
    loudness_keys = [
        "lowlevel.loudness_ebu128.integrated",
        "tonal.loudness_ebu128.integrated",
        "loudness_ebu128.integrated",
    ]
    replaygain_keys = ["lowlevel.replay_gain", "replay_gain", "highlevel.replay_gain"]
    key_keys = ["tonal.key_key", "key_key", "highlevel.key.all.key"]
    scale_keys = ["tonal.key_scale", "key_scale", "highlevel.key.all.scale"]
    for candidate in _candidate_sidecar_paths(file_path, json_output_dir=json_output_dir):
        if not candidate.is_file():
            continue
        try:
            with candidate.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            bpm = _extract_first_numeric(payload, bpm_keys)
            danceability = _extract_first_numeric(payload, dance_keys)
            loudness = _extract_first_numeric(payload, loudness_keys)
            replaygain = _extract_first_numeric(payload, replaygain_keys)
            key_key = _extract_first_string(payload, key_keys)
            key_scale = _extract_first_string(payload, scale_keys)
            musical_key = None
            if key_key:
                musical_key = f"{key_key} {key_scale}" if key_scale else key_key
            return {
                "bpm": bpm,
                "danceability": danceability,
                "loudness": loudness,
                "replaygain": replaygain,
                "musical_key": musical_key,
                "json_path": str(candidate),
            }
        except Exception as exc:
            logger.debug(
                "Failed parsing Essentia JSON sidecar",
                candidate=str(candidate),
                error=str(exc),
            )
    return {
        "bpm": None, "danceability": None, "loudness": None,
        "replaygain": None, "musical_key": None, "json_path": None,
    }


# ---------------------------------------------------------------------------
# Main scan entry point
# ---------------------------------------------------------------------------

def run_essentia_mood_scan(
    script_path: str = "",
    models_dir: str = "",
    mood_threshold: float = 0.005,
    per_file_timeout: int = 300,
    force: bool = False,
    progress_file: str = "",
    tag_genres: bool = False,
    num_genres: int = 3,
    genre_threshold: float = 15.0,
    genre_format: str = "parent_child",
    tag_moods: bool = True,
    parse_json_features: bool = True,
    delete_json_after_import: bool = False,
    json_output_dir: str = "",
    artist_filter: str = "",
    album_filter: str = "",
    track_id_filter: str = "",
    resume_from_artist: str = "",
    cpu_nice: int = 10,
    inter_file_delay: float = 0.0,
) -> dict[str, Any]:
    """Run Essentia-based mood/genre detection on tracks with a local file path.

    Parameters match the legacy ``old_system/essentia_mood_scan.py`` signature
    so the existing pipeline (``essentia_pipeline.py``) can call it unchanged.

    Returns a result dict with keys:
        stopped, error, processed_artists, total_artists,
        scanned_tracks, updated_tracks, synced_files
    """
    # ------------------------------------------------------------------
    # Validate script_path
    # ------------------------------------------------------------------
    script_path = (script_path or "").strip()
    if not script_path:
        _env = os.environ.get("ESSENTIA_SCRIPT_PATH", "").strip()
        if _env and os.path.isfile(_env):
            script_path = _env
    if not script_path and os.path.isfile(_BUNDLED_SCRIPT_PATH):
        script_path = _BUNDLED_SCRIPT_PATH
    if not script_path:
        msg = (
            "Essentia script path is not configured. "
            "Set 'essentia.script_path' in config.yaml to the path of tag_music.py, "
            "or use Settings → Essentia → 'Download Models & Script' (or the setup wizard)."
        )
        logger.error(msg)
        log_unified(f"Essentia Scan - Error: {msg}")
        _write_progress(progress_file, {
            "is_running": False, "scan_type": "essentia_mood_scan",
            "status": "error", "error": msg,
        })
        return {"stopped": False, "error": msg,
                "processed_artists": 0, "total_artists": 0,
                "scanned_tracks": 0, "updated_tracks": 0, "synced_files": 0}

    if not os.path.isfile(script_path):
        msg = (
            f"Essentia script not found at: {script_path}. "
            "Use Settings → Essentia → 'Download Models & Script' (or the setup wizard) to install it."
        )
        logger.error(msg)
        log_unified(f"Essentia Scan - Error: {msg}")
        _write_progress(progress_file, {
            "is_running": False, "scan_type": "essentia_mood_scan",
            "status": "error", "error": msg,
        })
        return {"stopped": False, "error": msg,
                "processed_artists": 0, "total_artists": 0,
                "scanned_tracks": 0, "updated_tracks": 0, "synced_files": 0}

    # ------------------------------------------------------------------
    # Build subprocess command
    # ------------------------------------------------------------------
    models_dir = (models_dir or "").strip()
    if not models_dir:
        _env = os.environ.get("ESSENTIA_MODELS_DIR", "").strip()
        if _env and os.path.isdir(_env):
            models_dir = _env
    if not models_dir and os.path.isdir(_BUNDLED_MODELS_DIR):
        models_dir = _BUNDLED_MODELS_DIR

    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
    _essentia_scan_version: str | None = None
    try:
        import essentia
        _essentia_scan_version = getattr(essentia, "__version__", None)
    except Exception:
        pass
    if not _essentia_scan_version:
        try:
            from datetime import datetime as _dt, timezone as _tz
            _mtime = os.path.getmtime(script_path)
            _essentia_scan_version = "script-" + _dt.fromtimestamp(_mtime, tz=_tz.utc).strftime("%Y%m%d")
        except Exception:
            _essentia_scan_version = "unknown"

    mood_threshold_pct = round(float(mood_threshold) * 100.0, 4)

    _nice_prefix: list[str] = []
    _cpu_nice = int(cpu_nice)
    if _cpu_nice != 0 and sys.platform != "win32":
        _nice_prefix = ["nice", "-n", str(max(-20, min(19, _cpu_nice)))]

    python_exec = sys.executable
    base_cmd: list[str] = _nice_prefix + [
        python_exec, script_path, "--auto", "--single-file", "--overwrite", "--quiet",
    ]
    if tag_moods:
        base_cmd += ["--mood-threshold", str(mood_threshold_pct)]
    else:
        base_cmd.append("--no-moods")
    if not tag_genres:
        base_cmd.append("--no-genres")
    else:
        valid_formats = {"parent_child", "child_parent", "child_only", "raw"}
        fmt = (genre_format or "parent_child").strip()
        if fmt not in valid_formats:
            fmt = "parent_child"
        base_cmd += [
            "--genres", str(max(1, int(num_genres))),
            "--genre-threshold", str(float(genre_threshold)),
            "--genre-format", fmt,
        ]
    if models_dir:
        base_cmd += ["--model-dir", os.path.expanduser(models_dir)]

    # ------------------------------------------------------------------
    # Query tracks from DB — raw cursor is intentional here (DDL + a
    # long-running scan that commits per row); the deprecation warning is
    # suppressed via get_db_connection_raw.
    conn = get_db_connection_raw(reason="essentia_scan")
    cursor = conn.cursor()
    placeholder = "%s"

    try:
        cursor.execute("ALTER TABLE tracks ADD COLUMN IF NOT EXISTS essentia_genres TEXT")
        cursor.execute("ALTER TABLE tracks ADD COLUMN IF NOT EXISTS essentia_scan_version TEXT")
        cursor.execute("ALTER TABLE tracks ADD COLUMN IF NOT EXISTS bpm DOUBLE PRECISION")
        cursor.execute("ALTER TABLE tracks ADD COLUMN IF NOT EXISTS danceability DOUBLE PRECISION")
        cursor.execute("ALTER TABLE tracks ADD COLUMN IF NOT EXISTS musical_key TEXT")
        cursor.execute("ALTER TABLE tracks ADD COLUMN IF NOT EXISTS loudness_lufs DOUBLE PRECISION")
        cursor.execute("ALTER TABLE tracks ADD COLUMN IF NOT EXISTS replaygain DOUBLE PRECISION")
        conn.commit()
    except Exception as _col_err:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("Could not ensure essentia columns", error=str(_col_err))

    conditions: list[str] = [
        f"COALESCE(file_path, '') NOT LIKE {placeholder}",
        f"CAST(id AS TEXT) NOT LIKE {placeholder}",
        "file_path IS NOT NULL",
        "file_path != ''",
    ]
    params: list[Any] = ["__queued_for_download__%", "queue_%"]

    if not force:
        if tag_moods and not tag_genres:
            conditions.append("(mood IS NULL OR mood = '')")
        elif tag_genres and not tag_moods:
            conditions.append("(essentia_genres IS NULL OR essentia_genres = '')")
        elif tag_genres and tag_moods:
            conditions.append(
                "(mood IS NULL OR mood = '' OR essentia_genres IS NULL OR essentia_genres = '')"
            )

    artist_filter = (artist_filter or "").strip()
    album_filter = (album_filter or "").strip()
    track_id_filter = (track_id_filter or "").strip()
    resume_from_artist = (resume_from_artist or "").strip()

    if not resume_from_artist and not artist_filter and not album_filter and not track_id_filter:
        if progress_file and os.path.exists(progress_file):
            try:
                with open(progress_file, "r", encoding="utf-8") as _fp:
                    _saved = json.load(_fp)
                _saved_status = _saved.get("status", "")
                _saved_checkpoint = (_saved.get("resume_from_artist") or "").strip()
                if _saved_status not in ("complete", "completed", "stopped") and _saved_checkpoint:
                    resume_from_artist = _saved_checkpoint
                    log_unified(f"Essentia Scan - Auto-resuming from checkpoint artist '{resume_from_artist}'")
            except Exception as _pr_err:
                logger.debug(
                    "Could not read essentia resume checkpoint",
                    error=str(_pr_err),
                )

    if track_id_filter:
        conditions.append(f"CAST(id AS TEXT) = {placeholder}")
        params.append(track_id_filter)
    else:
        if artist_filter:
            conditions.append(
                f"(LOWER(COALESCE(album_artist, '')) = LOWER({placeholder})"
                f" OR LOWER(COALESCE(artist, '')) = LOWER({placeholder}))"
            )
            params += [artist_filter, artist_filter]
        if album_filter:
            conditions.append(f"LOWER(COALESCE(album, '')) = LOWER({placeholder})")
            params.append(album_filter)

    where_sql = " AND ".join(conditions)
    cursor.execute(
        f"""
        SELECT id, title, album, artist, album_artist, file_path, mood, essentia_genres
        FROM tracks
        WHERE {where_sql}
        ORDER BY COALESCE(NULLIF(album_artist, ''), artist), album, track_number, title
        """,
        tuple(params),
    )
    rows = cursor.fetchall() or []
    conn.commit()

    artists: list[str] = []
    seen: set = set()
    for row in rows:
        artist_key = (
            _row_get(row, "album_artist", 4) or _row_get(row, "artist", 3) or "Unknown"
        ).strip()
        if artist_key not in seen:
            seen.add(artist_key)
            artists.append(artist_key)

    total_artists = len(artists)
    processed_artists = 0
    scanned_tracks = 0
    updated_tracks = 0
    synced_files = 0
    current_artist: str | None = None
    current_album: str | None = None
    _album_scan_count: int = 0
    total_tracks = len(rows)

    log_unified(
        f"Essentia Scan - Starting Essentia Scan"
        f" ({total_tracks} track(s) across {total_artists} artist(s))"
    )

    _essentia_milestones_logged: set = set()
    _essentia_milestone_25 = max(1, int(total_tracks * 0.25))
    _essentia_milestone_50 = max(1, int(total_tracks * 0.50))
    _essentia_milestone_75 = max(1, int(total_tracks * 0.75))

    resume_started = not bool(resume_from_artist)

    for row in rows:
        if _stop_requested(progress_file):
            conn.commit()
            conn.close()
            log_unified(
                f"Essentia Scan - Stopped"
                f" ({scanned_tracks}/{total_tracks} tracks scanned, {updated_tracks} updated)"
            )
            _write_progress(progress_file, {
                "is_running": False, "scan_type": "essentia_mood_scan",
                "status": "stopped", "processed_artists": processed_artists,
                "total_artists": total_artists, "scanned_tracks": scanned_tracks,
                "updated_tracks": updated_tracks, "synced_files": synced_files,
                "current_artist": current_artist,
            })
            return {"stopped": True, "processed_artists": processed_artists,
                    "total_artists": total_artists, "scanned_tracks": scanned_tracks,
                    "updated_tracks": updated_tracks, "synced_files": synced_files}

        track_id = _row_get(row, "id", 0)
        file_path = _row_get(row, "file_path", 5)
        artist_key = (
            _row_get(row, "album_artist", 4) or _row_get(row, "artist", 3) or "Unknown"
        ).strip()

        if not resume_started:
            if artist_key.lower() != resume_from_artist.lower():
                continue
            resume_started = True
            log_unified(f"Essentia Scan - Resuming from Artist {artist_key}")

        if file_path and not os.path.isabs(file_path):
            _music_root = (
                os.environ.get("MUSIC_FOLDER") or os.environ.get("MUSIC_ROOT") or "/music"
            )
            file_path = os.path.join(_music_root, file_path)

        album_key = (_row_get(row, "album", 2) or "").strip()
        if artist_key != current_artist or album_key != current_album:
            if current_album is not None and _album_scan_count > 0:
                # Legacy scan_history call removed — progress_file handles tracking
                pass
            _album_scan_count = 0
            current_album = album_key
            if artist_key != current_artist:
                current_artist = artist_key
                processed_artists = min(processed_artists + 1, total_artists)
                log_unified(
                    f"Essentia Scan - Scanning Artist {current_artist}"
                    f" ({processed_artists}/{total_artists})"
                )
                _write_progress(progress_file, {
                    "is_running": True, "scan_type": "essentia_mood_scan",
                    "status": "running", "processed_artists": processed_artists,
                    "total_artists": total_artists, "scanned_tracks": scanned_tracks,
                    "updated_tracks": updated_tracks, "synced_files": synced_files,
                    "current_artist": current_artist,
                    "resume_from_artist": current_artist,
                })

        scanned_tracks += 1
        _album_scan_count += 1

        if scanned_tracks == _essentia_milestone_25 and 25 not in _essentia_milestones_logged:
            log_unified(f"Essentia Scan - 25% completed - {scanned_tracks}/{total_tracks} tracks")
            _essentia_milestones_logged.add(25)
        elif scanned_tracks == _essentia_milestone_50 and 50 not in _essentia_milestones_logged:
            log_unified(f"Essentia Scan - 50% completed - {scanned_tracks}/{total_tracks} tracks")
            _essentia_milestones_logged.add(50)
        elif scanned_tracks == _essentia_milestone_75 and 75 not in _essentia_milestones_logged:
            log_unified(f"Essentia Scan - 75% completed - {scanned_tracks}/{total_tracks} tracks")
            _essentia_milestones_logged.add(75)

        if not file_path or not os.path.isfile(file_path):
            logger.debug(
                "Essentia scan: skipping track — file not found",
                track_id=track_id,
                file_path=file_path,
            )
            _write_progress(progress_file, {
                "is_running": True, "scan_type": "essentia_mood_scan",
                "status": "running", "processed_artists": processed_artists,
                "total_artists": total_artists, "scanned_tracks": scanned_tracks,
                "updated_tracks": updated_tracks, "synced_files": synced_files,
                "current_artist": current_artist, "resume_from_artist": current_artist,
            })
            continue

        # ------------------------------------------------------------------
        # Run Essentia subprocess on this file
        # ------------------------------------------------------------------
        cmd = base_cmd + [file_path]
        _subprocess_env = os.environ.copy()
        _subprocess_env.setdefault("CUDA_VISIBLE_DEVICES", "-1")
        _subprocess_env.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=per_file_timeout, env=_subprocess_env,
            )
            if result.returncode != 0:
                stderr_text = (result.stderr or "").strip()
                stdout_text = (result.stdout or "").strip()
                _combined_output = (stderr_text + "\n" + stdout_text).lower()
                if result.returncode == 1 and _ESSENTIA_NO_MODELS_PHRASE in _combined_output:
                    logger.debug(
                        "Essentia: no SVM classifier models configured (exit code 1 ignored — continuing to read tags)",
                        file=file_path,
                    )
                else:
                    logger.warning(
                        "Essentia script returned non-zero exit code",
                        exit_code=result.returncode,
                        file=file_path,
                        output=(stderr_text or stdout_text)[:300],
                    )
                    log_unified(
                        f"Essentia Scan - Error processing {os.path.basename(file_path)}"
                        f" (exit code {result.returncode})",
                    )
                    _write_progress(progress_file, {
                        "is_running": True, "scan_type": "essentia_mood_scan",
                        "status": "running", "processed_artists": processed_artists,
                        "total_artists": total_artists, "scanned_tracks": scanned_tracks,
                        "updated_tracks": updated_tracks, "synced_files": synced_files,
                        "current_artist": current_artist,
                        "resume_from_artist": current_artist,
                    })
                    continue

            # ------------------------------------------------------------------
            # Read tags from file after Essentia ran
            # ------------------------------------------------------------------
            mood_str = _read_essentia_mood_from_file(file_path) if tag_moods else None
            genre_str = _read_essentia_genre_from_file(file_path) if tag_genres else None

            if not mood_str and not genre_str:
                logger.debug(
                    "Essentia scan: no new tags for track",
                    track_id=track_id,
                    file=file_path,
                )
                _write_progress(progress_file, {
                    "is_running": True, "scan_type": "essentia_mood_scan",
                    "status": "running", "processed_artists": processed_artists,
                    "total_artists": total_artists, "scanned_tracks": scanned_tracks,
                    "updated_tracks": updated_tracks, "synced_files": synced_files,
                    "current_artist": current_artist,
                    "resume_from_artist": current_artist,
                })
                if inter_file_delay > 0:
                    time.sleep(inter_file_delay)
                continue

            # Build UPDATE payload
            updates: dict[str, Any] = {}
            if mood_str:
                updates["mood"] = mood_str
                updates["mood_source"] = "essentia"
                updates["mood_confidence"] = 0.5

            if genre_str and tag_genres:
                child_genres = _extract_child_genres(genre_str)
                existing_genres = _read_existing_tcon_genres(file_path)
                merged_genres = _merge_genres(existing_genres, child_genres)

                if merged_genres:
                    existing_raw = _row_get(row, "essentia_genres", 7, "")
                    try:
                        existing_list = json.loads(existing_raw) if existing_raw else []
                    except (json.JSONDecodeError, TypeError):
                        existing_list = []
                    all_genres = list(dict.fromkeys(existing_list + child_genres))
                    updates["essentia_genres"] = json.dumps(all_genres)
                    # NOTE: the consensus-owned ``genres`` column is NOT
                    # written here — Essentia is a MOOD/AUDIO provider, and
                    # genre assignment flows through the consensus aggregator
                    # (track_stage) so Essentia's text tags can never dictate
                    # a track's official genres (the "Korn / Nu Metal under
                    # Essentia Moods" contamination).
                    updates["genres"] = "; ".join(merged_genres[:3])

                    # Sync merged genres to file tags
                    write_ok = update_file_tags(file_path, {"genre": "; ".join(merged_genres)})
                    if not write_ok:
                        logger.debug(
                            "Failed to write genre tags to file",
                            file=file_path,
                        )

            if parse_json_features:
                features = _read_essentia_features_from_json(file_path, json_output_dir=json_output_dir)
                if features.get("bpm") is not None:
                    updates["bpm"] = features["bpm"]
                if features.get("danceability") is not None:
                    try:
                        updates["danceability"] = round(float(features["danceability"]), 4)
                    except (ValueError, TypeError):
                        pass
                musical_key = features.get("musical_key")
                if not musical_key:
                    musical_key = _read_key_tag_from_file(file_path)
                if musical_key:
                    updates["musical_key"] = musical_key
                if features.get("loudness") is not None:
                    try:
                        updates["loudness_lufs"] = round(float(features["loudness"]), 3)
                    except (ValueError, TypeError):
                        pass
                if features.get("replaygain") is not None:
                    try:
                        updates["replaygain"] = round(float(features["replaygain"]), 3)
                    except (ValueError, TypeError):
                        pass
                if delete_json_after_import and features.get("json_path"):
                    try:
                        os.unlink(features["json_path"])
                    except OSError as _del_err:
                        logger.debug(
                            "Could not delete Essentia JSON sidecar",
                            path=features["json_path"],
                            error=str(_del_err),
                        )

            updates["essentia_scan_version"] = _essentia_scan_version
            updates["essentia_last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())

            # Apply DB updates. A reconnect here is REBOUND into the loop
            # variables so every later iteration (and the final commit/close)
            # uses the fresh connection rather than the dead one.
            set_clauses = ", ".join(f"{k} = {placeholder}" for k in updates)
            set_params = list(updates.values()) + [track_id]
            try:
                conn, cursor, _rowcount = _update_with_reconnect(
                    conn,
                    cursor,
                    f"UPDATE tracks SET {set_clauses} WHERE id = {placeholder}",
                    tuple(set_params),
                )
            except Exception as db_exc:
                logger.error(
                    "Failed to update track",
                    track_id=track_id,
                    error=str(db_exc),
                )
                try:
                    conn.rollback()
                except Exception:
                    pass
                continue

            # Counted only now that the row is actually written. Counting
            # before the attempt reported a failed save as updated, and both
            # the progress file and the final summary are read by the UI.
            # A rowcount of 0 means the track row was gone by the time the
            # UPDATE ran, so nothing was written and nothing should be claimed.
            if _rowcount > 0:
                updated_tracks += 1

            # Sync tags to audio file
            try:
                sync_ok = sync_track_tags_to_file(str(track_id))
                if sync_ok:
                    synced_files += 1
            except Exception as sync_exc:
                logger.debug(
                    "Failed to sync tags for track",
                    track_id=track_id,
                    error=str(sync_exc),
                )

            _write_progress(progress_file, {
                "is_running": True, "scan_type": "essentia_mood_scan",
                "status": "running", "processed_artists": processed_artists,
                "total_artists": total_artists, "scanned_tracks": scanned_tracks,
                "updated_tracks": updated_tracks, "synced_files": synced_files,
                "current_artist": current_artist, "resume_from_artist": current_artist,
            })

            if inter_file_delay > 0:
                time.sleep(inter_file_delay)

        except subprocess.TimeoutExpired:
            logger.warning(
                "Essentia script timed out",
                timeout=per_file_timeout,
                file=file_path,
            )
            log_unified(f"Essentia Scan - Timeout processing {os.path.basename(file_path)}")
            _write_progress(progress_file, {
                "is_running": True, "scan_type": "essentia_mood_scan",
                "status": "running", "processed_artists": processed_artists,
                "total_artists": total_artists, "scanned_tracks": scanned_tracks,
                "updated_tracks": updated_tracks, "synced_files": synced_files,
                "current_artist": current_artist, "resume_from_artist": current_artist,
            })
            continue
        except Exception as exc:
            logger.error(
                "Unexpected error processing file",
                file=file_path,
                error=str(exc),
            )
            log_unified(f"Essentia Scan - Unexpected error: {exc}")
            _write_progress(progress_file, {
                "is_running": True, "scan_type": "essentia_mood_scan",
                "status": "running", "processed_artists": processed_artists,
                "total_artists": total_artists, "scanned_tracks": scanned_tracks,
                "updated_tracks": updated_tracks, "synced_files": synced_files,
                "current_artist": current_artist, "resume_from_artist": current_artist,
            })
            continue

    conn.commit()
    conn.close()

    log_unified(
        f"Essentia Scan - Complete"
        f" ({scanned_tracks}/{total_tracks} tracks scanned, {updated_tracks} updated,"
        f" {synced_files} files synced)"
    )
    _write_progress(progress_file, {
        "is_running": False, "scan_type": "essentia_mood_scan",
        "status": "complete", "processed_artists": processed_artists,
        "total_artists": total_artists, "scanned_tracks": scanned_tracks,
        "updated_tracks": updated_tracks, "synced_files": synced_files,
        "current_artist": current_artist,
    })

    return {
        "stopped": False, "error": None,
        "processed_artists": processed_artists,
        "total_artists": total_artists,
        "scanned_tracks": scanned_tracks,
        "updated_tracks": updated_tracks,
        "synced_files": synced_files,
    }
