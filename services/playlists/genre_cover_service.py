"""AI-generated cover art for the TOP GENRE playlists (Gemini).

Scope, deliberately narrow:
    only the ``{Genre} - Top Tracks`` playlists. Essential Collections (artist
    playlists) keep the artist-image cover they already get, and no other
    generated playlist is touched.

Flow, per genre playlist that this scan CREATED or UPDATED:
    1. build the prompt from the genre name + the configured template
    2. skip when this exact prompt was already applied (state file)
    3. ask Gemini for one image
    4. upload it through Navidrome's native artwork endpoint
       (``NavidromeClient.upload_playlist_cover`` — the SAME path
       ``attach_playlist_cover`` uses, including its file field and token header)
    5. record the prompt hash so scans do not re-bill for identical art

⚠️ Generation is GATED to created/updated playlists. Generating on every scan
would spend quota re-making identical covers every night; the state file then
covers the rest (a genre whose prompt changed, or ``regenerate`` turned on).

⚠️ Failure is NEVER fatal and never silent: every path returns a reason string,
and the caller logs it. An image API that is out of quota must not stop a scan,
and a cover that did not upload must not be reported as if it did.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import structlog

#: ⚠️ structlog, NOT ``logging.getLogger``. Every call below passes key=value
#: context, and a STDLIB logger raises ``TypeError: Logger._log() got an
#: unexpected keyword argument`` on those — inside the failure handlers, which
#: is exactly where losing the message hurts most.
logger = structlog.get_logger(__name__)

_STATE_FILE_NAME = "genre_playlist_covers.json"

DEFAULT_PROMPT_TEMPLATE = (
    "Square album cover artwork for a {genre} music playlist. "
    "Cinematic, atmospheric scene evoking {genre}, rich colour, dramatic "
    "lighting, highly detailed digital painting. Large bold ornamental title "
    "text \"{genre}\" centred at the top, weathered metal lettering. "
    "No band names, no artist names, no faces, no logos, no watermark."
)


def build_cover_prompt(genre: str, template: str | None = None) -> str:
    """Render the prompt for *genre*.

    ``{genre}`` is substituted when present. ⚠️ A template that is missing the
    placeholder is used AS-IS rather than raising: a user-edited template is
    still a valid prompt, and ``str.format`` on a stray brace (a legitimate part
    of a prompt) raises ``KeyError``/``ValueError`` — which would take the whole
    genre pass down over a typo in a text box.
    """
    name = str(genre or "").strip()
    text = str(template or "").strip() or DEFAULT_PROMPT_TEMPLATE
    if "{genre}" in text:
        try:
            return text.replace("{genre}", name)
        except Exception as exc:
            logger.debug("Cover prompt template substitution failed: %s", exc)
    return text


def _state_path() -> str:
    from helpers.config_helpers import get_state_directory

    return os.path.join(get_state_directory(), _STATE_FILE_NAME)


def _load_state() -> dict[str, str]:
    """``{genre_key: prompt_hash}`` for covers already applied.

    Best-effort: an unreadable/corrupt state file means "nothing recorded yet",
    which at worst regenerates art — never a crash before the upload.
    """
    try:
        with open(_state_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("[GENRE_COVER] cover state unreadable: %s", exc)
    return {}


def _save_state(state: dict[str, str]) -> None:
    path = _state_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception as exc:
        # Never fatal: losing the record costs one regenerated cover later.
        logger.warning("[GENRE_COVER] could not record cover state: %s", exc)


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def generate_genre_cover(
    genre: str,
    playlist_name: str,
    client: Any,
    *,
    regenerate: bool = False,
) -> dict[str, Any]:
    """Generate + upload a cover for one genre playlist.

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

    prompt = build_cover_prompt(genre, cfg["prompt_template"])
    digest = _prompt_hash(prompt)
    genre_key = str(genre or "").strip().casefold()

    state = _load_state()
    # ⚠️ The CONFIG's regenerate flag is honoured too, not just the caller's
    # argument: the setting is what the user turns on, and a caller that forgets
    # the parameter must not silently make the option a no-op.
    if not (regenerate or cfg["regenerate"]) and state.get(genre_key) == digest:
        return {**result, "ok": True, "status": "skipped",
                "reason": "cover already generated for this prompt"}

    # Resolve the playlist FIRST: generating an image we cannot attach would
    # spend quota for nothing.
    try:
        playlist = client.find_playlist_by_name(playlist_name)
    except Exception as exc:
        logger.warning("[GENRE_COVER] playlist lookup failed for %s: %s", playlist_name, exc)
        playlist = None
    if not playlist or not playlist.get("id"):
        return {**result, "status": "failed", "reason": "no Navidrome match"}

    playlist_id = str(playlist["id"])
    result["playlist_id"] = playlist_id

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
        ok, upload_reason = client.upload_playlist_cover(playlist_id, image, mime)
    except Exception as exc:
        logger.warning("[GENRE_COVER] cover upload raised for %s: %s", playlist_name, exc)
        return {**result, "status": "failed", "reason": str(exc)}

    if not ok:
        logger.warning(
            "[GENRE_COVER] cover upload rejected for %s: %s", playlist_name, upload_reason,
        )
        return {**result, "status": "failed", "reason": upload_reason or "upload rejected"}

    state[genre_key] = digest
    _save_state(state)
    logger.info(
        "[GENRE_COVER] cover applied", genre=genre, playlist=playlist_name,
        bytes=len(image), mime=mime,
    )
    return {**result, "ok": True, "status": "generated", "reason": ""}
