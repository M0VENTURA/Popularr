"""AI-generated cover art for the TOP GENRE playlists (Gemini).

Scope:
    Only the ``{Genre} - Top Tracks`` playlists. Essential Collections keep their
    artist-image cover, and no other generated playlist is touched.

Flow per genre playlist:
    1. Check if a cover already exists for this playlist in the state file.
    2. If a cover exists and ``regenerate`` is NOT requested, skip generation.
    3. If ``regenerate`` IS requested, enforce a 1-hour cooldown rule based on
       the last generation timestamp.
    4. Build the prompt using the custom album-style artwork prompt template.
    5. Ask Gemini for one image.
    6. Upload it through Navidrome's native artwork endpoint.
    7. Record the prompt hash, playlist ID, and timestamp in the state file.

⚠️ Failure is NEVER fatal and never silent: every path returns a reason string,
and the caller logs it.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

import structlog

#: ⚠️ structlog, NOT ``logging.getLogger``.
logger = structlog.get_logger(__name__)

_STATE_FILE_NAME = "genre_playlist_covers.json"


def build_cover_prompt(genre: str, template: str | None = None, playlist_name: str = "") -> str:
    """Render the custom prompt for *genre*."""
    name = str(genre or "").strip()
    pname = str(playlist_name or "").strip() or name
    text = str(template or "").strip()
    if not text:
        text = (
            "Create square album-style artwork for the playlist \"{playlist}\". "
            "The music genre is {genre}. Interpret the genre intelligently and create a "
            "distinctive visual identity based on its musical character, cultural associations, "
            "era, atmosphere, aesthetics, instrumentation and visual traditions. Do not rely on "
            "a fixed list of genre templates. Adapt the artwork specifically to {genre}, including "
            "niche and hybrid genres. Where the genre belongs to a broader musical family, "
            "preserve some visual connection to that family while emphasizing the characteristics "
            "that distinguish {genre} from related genres. Create a polished, detailed composition "
            "suitable for professional playlist artwork. It should remain visually strong and "
            "recognizable at thumbnail size. Use a clear focal point, deliberate composition, "
            "cohesive colour palette, atmospheric lighting and appropriate visual depth. "
            "Include the playlist title \"{playlist}\" prominently and legibly as part of the "
            "artwork. Typography should complement the visual identity of {genre}, while "
            "prioritizing accurate spelling and readability. Avoid generic music imagery unless "
            "genuinely appropriate to {genre}. Avoid unnecessary text, logos, artist names, "
            "album names and branding. The final image should feel like a unique cover created "
            "specifically for \"{playlist}\", rather than a generic representation of the genre."
        )
    if "{genre}" in text:
        try:
            text = text.replace("{genre}", name)
        except Exception as exc:
            logger.debug("Cover prompt genre substitution failed: %s", exc)
    if "{playlist}" in text:
        try:
            text = text.replace("{playlist}", pname)
        except Exception as exc:
            logger.debug("Cover prompt playlist substitution failed: %s", exc)
    return text


#: Kept for backward compatibility with the config system.
DEFAULT_PROMPT_TEMPLATE = ""


def _state_path() -> str:
    from helpers.config_helpers import get_state_directory

    return os.path.join(get_state_directory(), _STATE_FILE_NAME)


def _load_state() -> dict[str, Any]:
    """``{genre_key: {"hash": ..., "playlist_id": ..., "timestamp": ...}}``"""
    try:
        with open(_state_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return dict(data)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("[GENRE_COVER] cover state unreadable: %s", exc)
    return {}


def _state_entry(state: dict[str, Any], genre_key: str) -> tuple[str, str, float]:
    """``(prompt_hash, playlist_id, timestamp)`` recorded for *genre_key*."""
    raw = state.get(genre_key)
    if isinstance(raw, dict):
        return (
            str(raw.get("hash") or ""),
            str(raw.get("playlist_id") or ""),
            float(raw.get("timestamp") or 0.0),
        )
    if isinstance(raw, str):
        return raw, "", 0.0
    return "", "", 0.0


def _save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning("[GENRE_COVER] could not record cover state: %s", exc)


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def generate_genre_cover(
    genre: str,
    playlist_name: str,
    client: Any,
    *,
    regenerate: bool = False,
    playlist_id: str | None = None,
) -> dict[str, Any]:
    """Generate + upload a cover for one genre playlist with existence and cooldown checks.

    Returns ``{"ok", "status", "reason", "playlist_id"}`` where *status* is one
    of ``generated`` / ``skipped`` / ``failed`` / ``disabled``. Never raises.
    """
    from helpers.config_helpers import get_gemini_config, get_genre_cover_config

    result: dict[str, Any] = {"ok": False, "status": "failed", "reason": "", "playlist_id": ""}

    cfg = get_genre_cover_config()
    if not cfg["enabled"]:
        return {**result, "status": "disabled", "reason": "genre AI covers disabled"}

    gem = get_gemini_config()
    if not gem["enabled"] or not gem["api_key"]:
        return {**result, "status": "disabled", "reason": "Gemini is not configured"}

    if client is None:
        return {**result, "status": "failed", "reason": "no Navidrome client"}

    prompt = build_cover_prompt(
        genre, cfg["prompt_template"], playlist_name=str(playlist_name or genre or "")
    )
    digest = _prompt_hash(prompt)
    genre_key = str(genre or "").strip().casefold()

    # Resolve the playlist ID first
    resolved_id = str(playlist_id or "").strip()
    if not resolved_id:
        try:
            playlist = client.find_playlist_by_name(playlist_name)
        except Exception as exc:
            logger.warning("[GENRE_COVER] playlist lookup failed for %s: %s", playlist_name, exc)
            playlist = None
        if not playlist or not playlist.get("id"):
            return {**result, "status": "failed", "reason": "no Navidrome match"}
        resolved_id = str(playlist["id"])
    result["playlist_id"] = resolved_id

    # Check if a cover already exists for this playlist
    state = _load_state()
    recorded_hash, recorded_pid, recorded_timestamp = _state_entry(state, genre_key)
    cover_exists = bool(recorded_hash and recorded_pid and recorded_pid == resolved_id)

    _forced = bool(regenerate or cfg["regenerate"])

    if cover_exists and not _forced:
        return {
            **result,
            "ok": True,
            "status": "skipped",
            "reason": "cover already exists for this playlist",
        }

    if cover_exists and _forced:
        # Enforce 1-hour cooldown between forced regenerations
        elapsed = time.time() - recorded_timestamp
        if elapsed < 3600:
            remaining_mins = int((3600 - elapsed) / 60)
            return {
                **result,
                "ok": True,
                "status": "skipped",
                "reason": f"regeneration cooldown active (wait {remaining_mins} more minutes)",
            }

    from api_clients.gemini import generate_image

    image, mime, reason = generate_image(
        prompt, api_key=gem["api_key"], model=gem["model"],
    )
    if not image:
        logger.warning(
            "[GENRE_COVER] image generation failed", genre=genre, reason=reason,
        )
        return {**result, "status": "failed", "reason": reason or "no image"}

    try:
        ok, upload_reason = client.upload_playlist_cover(resolved_id, image, mime)
    except Exception as exc:
        logger.warning("[GENRE_COVER] cover upload raised for %s: %s", playlist_name, exc)
        return {**result, "status": "failed", "reason": str(exc)}

    if not ok:
        logger.warning(
            "[GENRE_COVER] cover upload rejected for %s: %s", playlist_name, upload_reason,
        )
        return {**result, "status": "failed", "reason": upload_reason or "upload rejected"}

    # Update state file with new hash, playlist ID, and current timestamp
    state[genre_key] = {
        "hash": digest,
        "playlist_id": resolved_id,
        "timestamp": time.time(),
    }
    _save_state(state)
    logger.info(
        "[GENRE_COVER] cover applied", genre=genre, playlist=playlist_name,
        playlist_id=resolved_id, bytes=len(image), mime=mime,
    )
    return {**result, "ok": True, "status": "generated", "reason": ""}
