"""Smart playlist generation service.

Generates and manages Navidrome Smart Playlist (.nsp) files based on
library content, including:

Key Functions:
    - sanitize_playlist_name(): Strip unsafe characters from names.
    - create_nsp_file(): Write playlist JSON data to disk.
    - create_or_update_playlist_for_artist(): Generate an "Essential"
      playlist for a specific artist.
    - refresh_all_playlists_from_db(): Rebuild all playlists from current
      database ratings.

Architecture:
    Reads tracks with high ratings (>= 4) from the database and generates
    ``.nsp`` files that Navidrome auto-loads from its Playlists directory.
"""
from __future__ import annotations

import json
import logging
import os
import re

from sqlalchemy import text

from db.engine import db_session
from helpers.normalization_service import normalize_the_prefix


def sanitize_playlist_name(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in ("-", "_", " ")).strip()


def playlist_path(playlist_name: str) -> str:
    music_folder = os.environ.get("MUSIC_FOLDER", "/music")
    playlists_dir = os.path.join(music_folder, "Playlists")
    os.makedirs(playlists_dir, exist_ok=True)
    return os.path.join(playlists_dir, f"{sanitize_playlist_name(playlist_name)}.nsp")


def delete_nsp_file(playlist_name: str) -> None:
    path = playlist_path(playlist_name)
    if os.path.exists(path):
        os.remove(path)


def create_nsp_file(playlist_name: str, playlist_data: dict) -> bool:
    try:
        with open(playlist_path(playlist_name), "w", encoding="utf-8") as handle:
            json.dump(playlist_data, handle, indent=2)
        return True
    except Exception:
        return False


def _playlists_dir() -> str:
    return os.path.join(os.environ.get("MUSIC_FOLDER", "/music"), "Playlists")


def is_safe_playlist_path(file_path: str) -> bool:
    """True when *file_path* is a real file inside the Playlists directory.

    Enforces the playlists-root boundary for any user-supplied ``file_path``
    so the playlist read/export/rename routes cannot be used for path
    traversal (reading/renaming arbitrary files elsewhere on disk).  Uses
    ``realpath`` so symlink escapes are also rejected, and requires the path
    to actually exist (a missing file is not a readable playlist).
    """
    if not file_path:
        return False
    try:
        from services.infrastructure.filesystem_service import is_path_under_directory
        return os.path.isfile(file_path) and is_path_under_directory(file_path, _playlists_dir())
    except Exception:
        return False


def create_m3u_file(playlist_name: str, tracks: list[dict]) -> str | None:
    """Write an ``{name}.m3u`` into the Playlists directory.

    ``tracks`` entries: ``{file_path, title, artist, duration}``.  Returns the
    written path, or None on failure.
    """
    try:
        playlists_dir = _playlists_dir()
        os.makedirs(playlists_dir, exist_ok=True)
        path = os.path.join(playlists_dir, f"{sanitize_playlist_name(playlist_name)}.m3u")
        lines = ["#EXTM3U"]
        for t in tracks or []:
            artist = str(t.get("artist") or "")
            title = str(t.get("title") or "")
            duration = 0
            try:
                duration = max(0, int(float(t.get("duration") or 0) or 0))
            except (TypeError, ValueError):
                duration = 0
            lines.append(f"#EXTINF:{duration},{artist} - {title}")
            lines.append(str(t.get("file_path") or title))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        return path
    except Exception as exc:
        logging.getLogger(__name__).warning("M3U write failed for %s: %s", playlist_name, exc)
        return None


def _count_m3u_tracks(file_path: str) -> int:
    """Count ``#EXTINF`` entries in an M3U file without parsing the whole thing."""
    count = 0
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as handle:
            for raw in handle:
                if raw.startswith("#EXTINF:"):
                    count += 1
    except Exception:
        pass
    return count


def read_m3u_file(file_path: str) -> list[dict]:
    """Parse an .m3u/.m3u8 file into ``{title, artist, duration, file_path}``."""
    tracks: list[dict] = []
    pending_extinf: str | None = None
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line == "#EXTM3U":
                    continue
                if line.startswith("#EXTINF:"):
                    pending_extinf = line[len("#EXTINF:"):]
                    continue
                if line.startswith("#"):
                    continue
                title, artist, duration = line, "", 0
                if pending_extinf:
                    dur_part, _, label = pending_extinf.partition(",")
                    try:
                        duration = max(0, int(float(dur_part)))
                    except ValueError:
                        duration = 0
                    artist, _, title = label.rpartition(" - ") if " - " in label else ("", "", label)
                    pending_extinf = None
                tracks.append({"title": title, "artist": artist, "duration": duration, "file_path": line})
    except Exception as exc:
        logging.getLogger(__name__).warning("M3U read failed for %s: %s", file_path, exc)
    return tracks


def list_nsp_playlists() -> list[dict]:
    """Return smart .nsp files AND generated .m3u playlists from the Playlists dir."""
    playlists_dir = _playlists_dir()
    if not os.path.isdir(playlists_dir):
        return []

    found = []
    for file_name in sorted(os.listdir(playlists_dir)):
        lower = file_name.lower()
        is_m3u = lower.endswith(".m3u") or lower.endswith(".m3u8")
        if not (lower.endswith(".nsp") or is_m3u):
            continue
        file_path = os.path.join(playlists_dir, file_name)

        if is_m3u:
            found.append({
                "name": os.path.splitext(file_name)[0],
                "file_name": file_name,
                "file_path": file_path,
                "comment": "Generated playlist",
                "rules": {},
                "track_count": _count_m3u_tracks(file_path),
                "rule_based": False,
                "kind": "m3u",
            })
            continue

        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue

        embedded = data.get("tracks") or []
        track_ids = data.get("trackIds") or []
        embedded_count = len(embedded) if isinstance(embedded, list) else 0
        id_count = len(track_ids) if isinstance(track_ids, list) else 0
        rules = data.get("rules") or {}

        found.append({
            "name": str(data.get("name") or file_name[:-4]),
            "file_name": file_name,
            "file_path": file_path,
            "comment": str(data.get("comment") or ""),
            "rules": rules if isinstance(rules, dict) else {},
            "track_count": embedded_count or id_count,
            "rule_based": bool(rules),
            "kind": "nsp",
        })
    return found


def read_nsp_playlist(file_path: str) -> dict | None:
    """Read an .nsp file for display.

    Embedded ``tracks`` are normalized to ``{id, title, artist, album,
    rating}`` entries.  Rule-based playlists have no embedded track list,
    so ``_tracks`` is empty and the caller may resolve via Navidrome.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    tracks = []
    embedded = data.get("tracks") or []
    if isinstance(embedded, list):
        for index, entry in enumerate(embedded, start=1):
            if not isinstance(entry, dict):
                continue
            tracks.append({
                "id": str(entry.get("id") or f"{os.path.basename(file_path)}#{index}"),
                "title": str(entry.get("title") or f"Track {index}"),
                "artist": str(entry.get("artist") or ""),
                "album": str(entry.get("album") or ""),
                "rating": entry.get("rating"),
            })

    data["_tracks"] = tracks
    data["_file_path"] = file_path
    data["_file_name"] = os.path.basename(file_path)
    return data


def rename_nsp_playlist(file_path: str, new_name: str, new_file_name: str | None = None) -> dict:
    """Rename a smart playlist: update its embedded ``name`` and rename the
    .nsp file on disk.  Returns ``{name, file_path, file_name}``.

    Raises ValueError for invalid input or an existing target file.
    """
    new_name = str(new_name or "").strip()
    if not new_name:
        raise ValueError("Playlist name is required")

    with open(file_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Playlist file is not valid JSON")

    data["name"] = new_name

    safe_file = str(new_file_name or "").strip()
    safe_file = sanitize_playlist_name(safe_file or new_name)
    if not safe_file:
        raise ValueError("File name is required")
    if not safe_file.lower().endswith(".nsp"):
        safe_file = f"{safe_file}.nsp"

    target_path = os.path.abspath(os.path.join(os.path.dirname(file_path), safe_file))
    if os.path.abspath(file_path) != target_path and os.path.exists(target_path):
        raise ValueError(f"A playlist file named '{safe_file}' already exists")

    with open(file_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)

    if os.path.abspath(file_path) != target_path:
        os.rename(file_path, target_path)

    return {
        "name": new_name,
        "file_path": target_path,
        "file_name": os.path.basename(target_path),
    }


def create_or_update_playlist_for_artist(artist_name: str, tracks: list):
    """Generate an 'Essential' playlist for a specific artist.

    The playlist's display ``name`` (and therefore the on-disk file name
    derived from it) is passed through ``normalize_the_prefix`` so artists
    starting with "The " sort/display the way a media library would, e.g.
    "The Offspring" -> "Offspring, The".

    NOTE: ``rules.artist`` intentionally stays as the raw ``artist_name``.
    That value is matched against the artist tag stored in the library, so
    flipping it would break Navidrome's smart-playlist artist matching.
    """
    display_name = normalize_the_prefix(artist_name)
    playlist_name = f"{display_name} (Essential Playlist)"
    data = {
        "name": playlist_name,
        "comment": "Generated by Popularr",
        "rules": {"artist": artist_name},
        "tracks": tracks or [],
    }
    return create_nsp_file(playlist_name, data)


def refresh_all_playlists_from_db():
    """Rebuild the legacy per-artist NSP files from current DB ratings."""
    with db_session() as session:
        result = session.execute(text("SELECT DISTINCT COALESCE(NULLIF(album_artist, ''), artist) AS artist FROM tracks WHERE rating >= 4"))
        artists = [str(row[0]) for row in result.fetchall() or [] if row[0]]
        count = 0
        for artist in artists:
            result = session.execute(text("SELECT id, title, rating FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND rating >= 4"), {"artist": artist})
            tracks = [{"id": str(row[0]), "title": str(row[1]), "rating": int(row[2])} for row in result.fetchall() or []]
            if create_or_update_playlist_for_artist(artist, tracks):
                count += 1
        return count


def create_or_update_loved_tracks_playlist() -> dict:
    """Build a per-user 'Loved Tracks' .nsp playlist from hearted tracks.

    Uses the ACTIVE user's favourites (``user_favourites`` entity_type=track).
    Creates/updates ``Loved Tracks.nsp`` in the Playlists directory so
    Navidrome picks it up as a smart playlist.  Returns the playlist entry
    (name/file_path/track_count) or an error dict.
    """
    from services.favourites_service import favourite_ids
    try:
        hearted = favourite_ids("track")
        if not hearted:
            return {"success": True, "track_count": 0, "note": "no hearted tracks"}

        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT id, title, artist FROM tracks
                    WHERE CAST(id AS TEXT) IN (SELECT value FROM json_each(:ids))
                    ORDER BY title
                """),
                {"ids": json.dumps(hearted)},
            ).fetchall() or []

        tracks = [
            {
                "id": str(r[0]),
                "title": str(r[1] or ""),
                "artist": str(r[2] or ""),
                "rating": 5,
            }
            for r in rows
        ]

        playlist_name = "Loved Tracks"
        data = {
            "name": playlist_name,
            "comment": "Heart tracks in Popularr to build this playlist (synced with Navidrome favourites)",
            "rules": {},
            "tracks": tracks,
        }
        ok = create_nsp_file(playlist_name, data)
        if not ok:
            return {"success": False, "error": "Failed to write Loved Tracks playlist"}
        return {"success": True, "track_count": len(tracks), "name": playlist_name,
                "file_path": playlist_path(playlist_name)}
    except Exception as exc:
        logger.warning("[PLAYLISTS] Loved Tracks playlist generation failed: %s", exc)
        return {"success": False, "error": str(exc)}


logger = logging.getLogger(__name__)


def _fetch_artist_image_bytes(artist_name: str) -> bytes | None:
    """Reuse the /artist-page image pipeline: resolved URL -> image bytes."""
    try:
        from services.metadata.artist_metadata_service import get_artist_image
        data, _code = get_artist_image(artist_name)
        image_url = data.get("image_url") if isinstance(data, dict) else None
        if not image_url:
            return None
        from api_clients import session
        resp = session.get(image_url, timeout=15)
        if resp.status_code == 200 and resp.content:
            return resp.content
    except Exception as exc:
        logger.debug("[PLAYLISTS] Artist image fetch failed for %s: %s", artist_name, exc)
    return None


def attach_playlist_cover(playlist_name: str, artist_name: str) -> dict:
    """Give a playlist the ARTIST's image as its cover art.

    Gated by ``navidrome.playlist_cover_art`` (default false). Resolves the
    artist image bytes through the same pipeline the /artist page uses, finds
    the playlist in Navidrome by name, and uploads the image through
    Navidrome's NATIVE artwork endpoint (``POST /api/playlist/{id}/image``) —
    the source Navidrome's own docs list FIRST for playlist artwork.

    It is deliberately NOT a Subsonic ``updatePlaylist`` call any more: that
    endpoint has no cover parameter at all, so the upload was ignored, the
    server answered ``status: ok`` and this function reported success while no
    artwork was ever stored — the reported "playlist artwork isn't being added
    properly".

    Never raises. Returns ``{"ok": bool, "method": str, "reason": str}``; the
    reason is always populated on failure (``disabled``, ``no image``,
    ``no navidrome match``, ``artwork upload disabled``, ...), so the log tells
    the truth instead of claiming a cover that is not there.
    """
    try:
        from helpers.config_helpers import get_config
        cfg = get_config() or {}
        nav_cfg = cfg.get("navidrome") or {}
        if not isinstance(nav_cfg, dict) or not nav_cfg.get("playlist_cover_art", False):
            return {"ok": False, "method": "none", "reason": "disabled"}

        nav_users = cfg.get("navidrome_users") or []
        if not nav_users and nav_cfg.get("base_url"):
            nav_users = [nav_cfg]

        image_bytes = _fetch_artist_image_bytes(artist_name)
        if not image_bytes:
            return {"ok": False, "method": "none", "reason": "no image"}

        from api_clients.navidrome import NavidromeClient
        last_reason = "no navidrome match"
        for user in nav_users:
            base_url = user.get("base_url")
            username = user.get("user")
            password = user.get("pass")
            if not (base_url and username and password):
                continue
            try:
                client = NavidromeClient(base_url, username, password)
                playlist = client.find_playlist_by_name(playlist_name)
                if not playlist or not playlist.get("id"):
                    continue
                ok, reason = client.upload_playlist_cover(str(playlist["id"]), image_bytes)
                if ok:
                    logger.info("[PLAYLISTS] Cover set for %s (%s)", playlist_name, artist_name)
                    return {"ok": True, "method": "navidrome_native", "reason": "ok"}
                # Map the server's answer to something an operator can act on.
                last_reason = {
                    "disabled": "artwork upload disabled on the server",
                    "no_login": "navidrome login failed",
                    "not_found": "playlist not found on the server",
                }.get(reason, reason or "upload rejected")
                logger.warning(
                    "[PLAYLISTS] Cover upload rejected for %s: %s", playlist_name, last_reason
                )
            except Exception as exc:
                last_reason = str(exc)
                logger.warning("[PLAYLISTS] Cover upload failed for %s: %s", playlist_name, exc)
        return {"ok": False, "method": "navidrome_native", "reason": last_reason}
    except Exception as exc:
        logger.warning("[PLAYLISTS] Cover attach aborted: %s", exc)
        return {"ok": False, "method": "none", "reason": str(exc)}


def sync_playlists_public() -> dict:
    """Make every Navidrome playlist public when the config flag is enabled.

    Navidrome imports the ``.m3u`` / ``.nsp`` files from the Playlists folder
    as PRIVATE playlists; this flips their visibility to public after each
    library sync.  Gated by ``navidrome.auto_public_playlists`` (default
    false — opt-in via config.yaml or the Integrations tab).

    Returns ``{"enabled": bool, "checked": int, "made_public": int,
    "failed": int}``.
    """
    try:
        from helpers.config_helpers import get_config
        cfg = get_config() or {}
        nav_cfg = cfg.get("navidrome") or {}
        if not isinstance(nav_cfg, dict):
            nav_cfg = {}
        if not bool(nav_cfg.get("auto_public_playlists", False)):
            return {"enabled": False, "checked": 0, "made_public": 0, "failed": 0}

        nav_users = cfg.get("navidrome_users") or []
        if not nav_users and nav_cfg.get("base_url"):
            nav_users = [nav_cfg]

        from api_clients.navidrome import NavidromeClient
        checked = made_public = failed = 0
        for user in nav_users:
            base_url = user.get("base_url")
            username = user.get("user")
            password = user.get("pass")
            if not (base_url and username and password):
                continue
            try:
                client = NavidromeClient(base_url, username, password)
                for playlist in client.fetch_all_playlists() or []:
                    checked += 1
                    if str(playlist.get("public") or "").lower() == "true":
                        continue
                    playlist_id = str(playlist.get("id") or "")
                    if playlist_id and client.update_playlist_public(playlist_id, True):
                        made_public += 1
                        logger.info("[PLAYLISTS] Set public: %s", playlist.get("name"))
                    else:
                        failed += 1
            except Exception as exc:
                logger.warning("[PLAYLISTS] Public-sync failed for %s: %s", base_url, exc)
                failed += 1
        return {"enabled": True, "checked": checked, "made_public": made_public, "failed": failed}
    except Exception as exc:
        logger.warning("[PLAYLISTS] Public-sync aborted: %s", exc)
        return {"enabled": True, "checked": 0, "made_public": 0, "failed": 0}
