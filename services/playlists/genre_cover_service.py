"""AI-generated cover art for the TOP GENRE playlists (Gemini).

Scope, deliberately narrow:
    only the ``{Genre} - Top Tracks`` playlists. Essential Collections (artist
    playlists) keep the artist-image cover they already get, and no other
    generated playlist is touched.

Flow, per genre playlist that this scan CREATED or UPDATED:
    1. build the prompt from the genre name + the configured template
    2. skip ONLY when the recorded art still matches AND it was applied to
       THIS playlist id (state file); anything unconfirmed re-applies
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

#: ⭐⭐ THE COVER PROMPT
#: ─────────────────────────────────────────────────────────────────────────
#: Why the old template produced near-identical covers
#: ------------------------------------------------
#: The previous template always asked for the same thing — "cinematic,
#: atmospheric scene, rich colour, dramatic lighting, highly detailed digital
 #: painting" — so every genre got a glowing, misty, fantasy-landscape
#: render. The model collapsed every prompt onto the SAME visual formula.
#: That is the look your ear flagged as "AI".
#: ⭐ What this template does differently
#: -------------------------------------
#: 1. RANDOM CREATIVE STIMULUS  —  medium, composition, lighting, texture,
#:    palette strategy and conceptual approach are chosen at random per call.
#:    A House cover and a Metalcore cover no longer share a formula; two
#:    House covers made months apart do not either.
#: 2. ONE CENTRAL CONCEPT  —  explicitly forbids the "guitar + skull +
#:    forest + city + lightning + smoke" montage the model otherwise
#:    defaults to. The art director develops ONE idea and commits to it.
#: 3. ANTI-CLICHÉ  —  a blocklist of visual shortcuts image models
#:    over-use, both the universal ones (instruments, headphones, vinyl,
#:    waveforms) and the genre-agnostic heavy-music ones (castles, skulls,
#:    dead trees, fire). "Do not assume heavy music requires X".
#: 4. LET THE MODEL INVENT THE PALETTE  —  colour comes from the music's
#:    character and a randomly chosen *strategy* (monochromatic, muted +
#:    accent, warm dominant, …), never from a hard-coded genre→colour map.
#: 5. TYPOGRAPHY IS SEPARATE  —  the playlist title is rendered in its own
#:    recommended style, NOT painted by the model (painted text is the most
#:    recognisable "AI" tell). Your application composites the title on
#:    afterwards; the artwork reserves space for it.
#: 6. AUTHENTICITY > RANDOMNESS  —  the random stimuli are suggestions. The
#:    art director may adapt or reject any of them if they conflict with the
#:    music. Musical identity wins.
#
# Seedable RNG so the same genre + timestamp reproduces, but different
# timestamps vary. ``random`` is fine here — this is art direction, not
# security.
import random

#: The random creative axes. Each call picks one from each list, so the
#: prompt varies every time. The model receives them as *suggestions*, not
#: mandatory ingredients.
_RANDOM_MEDIA = [
    "35mm film photography", "medium-format photography", "large-format photography",
    "oil painting", "acrylic painting", "gouache painting", "watercolour painting",
    "charcoal drawing", "graphite drawing", "ink illustration", "pen illustration",
    "screen printing", "risograph", "linocut", "woodcut",
    "collage (physical, tactile)", "mixed media on paper", "mixed media on canvas",
    "sculpture (clay, plaster, found objects)", "textile / fabric art",
    "cyanotype", "daguerreotype-style toning", "polaroid / instant film",
    "digital painting (non-photorealistic)", "vector art (limited palette)",
    "stained glass", "mosaic", "enamelwork", "metal etching",
]
_RANDOM_COMPOSITIONS = [
    "extreme close-up / macro", "intimate detail of a single object",
    "wide landscape with a small human element", "bird's-eye / overhead",
    "low-angle looking up", "tight symmetrical framing",
    "rule-of-thirds with negative space on the left",
    "rule-of-thirds with negative space on the right",
    "central subject surrounded by texture", "panoramic crop",
    "fragmented / cropped composition", "looking through a window or frame",
    "reflection in water or glass", "long exposure / motion blur",
]
_RANDOM_LIGHTINGS = [
    "hard directional studio lighting", "soft diffused natural light",
    "golden hour / warm sunset", "cold overcast daylight",
    "single dramatic spotlight", "rim lighting / backlight",
    "low-key / chiaroscuro", "high-key / bright and airy",
    "neon / artificial coloured light", "candlelight / warm flicker",
    "moonlight / blue night", "fog / haze diffusion",
    "harsh midday sun", "light through coloured glass",
]
_RANDOM_TEXTURES = [
    "aged uncoated print", "glossy magazine print", "faded polaroid",
    "grainy film (ISO 800+)", "clean digital finish",
    "torn paper edge", "creased / folded surface",
    "dust and scratches", "chemical staining / light leaks",
    "fabric weave visible", "rough concrete surface",
    "rusted metal surface", "weathered wood grain",
    "water damage / warping", "embossed / debossed print",
]
_RANDOM_PALETTES = [
    "restrained monochromatic", "muted + single accent",
    "warm dominant", "cold dominant", "high contrast",
    "low contrast / muted", "complementary clash",
    "analogous / harmonious", "duotone (two colours only)",
    "earth tones only", "pastel + desaturated",
    "neon / saturated against dark", "sepia / warm vintage",
    "cool silver + blue", "deep jewel tones",
]
_RANDOM_CONCEPTS = [
    "transform an everyday object into an emotional metaphor",
    "capture a fleeting moment of human stillness",
    "exploit extreme scale contrast (tiny vs vast)",
    "find beauty in decay or abandonment",
    "show the same subject from an unexpected angle",
    "use shadow and silhouette to suggest rather than show",
    "freeze motion in an unusual medium (water, smoke, dust)",
    "juxtapose organic and geometric forms",
    "render an abstract emotion as a physical texture",
    "use negative space as the primary subject",
    "find the extraordinary in the mundane",
    "show a landscape at an unusual time of day",
    "use weather (fog, rain, snow) as the main element",
    "exploit reflection or refraction",
    "show a process (melting, burning, growing, eroding)",
]


def _random_creative_stimulus() -> dict[str, str]:
    """One random creative direction per cover call.

    Each axis is a SUGGESTION, not a constraint — the art director may
    adapt or reject any of them. This is what makes covers differ from
    one generation to the next without losing musical credibility.
    """
    return {
        "medium": random.choice(_RANDOM_MEDIA),
        "composition": random.choice(_RANDOM_COMPOSITIONS),
        "lighting": random.choice(_RANDOM_LIGHTINGS),
        "texture": random.choice(_RANDOM_TEXTURES),
        "palette_strategy": random.choice(_RANDOM_PALETTES),
        "conceptual_approach": random.choice(_RANDOM_CONCEPTS),
    }


def build_cover_prompt(genre: str, template: str | None = None, playlist_name: str = "") -> str:
    """Render the prompt for *genre*.

    ``{genre}`` and ``{playlist}`` are substituted when present. ⚠️ A template
    that is missing the placeholder is used AS-IS rather than raising:
    a user-edited template is still a valid prompt, and ``str.format`` on a
    stray brace (a legitimate part of a prompt) raises ``KeyError``/
    ``ValueError`` — which would take the whole genre pass down over a typo
    in a text box.

    When no custom template is configured, a RANDOM creative stimulus is
    generated and embedded so consecutive calls for the same genre do NOT
    produce identical art.
    """
    name = str(genre or "").strip()
    pname = str(playlist_name or "").strip() or name
    text = str(template or "").strip()
    if not text:
        # No custom template — build a varied prompt with random stimulus.
        s = _random_creative_stimulus()
        text = (
            "Create original square 1:1 album-cover artwork for a music playlist "
            "called \"{playlist}\". The playlist is in the {genre} genre.\n\n"
            "DEVELOP ONE CENTRAL VISUAL CONCEPT. The cover must have one dominant "
            "idea, a clear focal point, and a coherent composition. Do NOT "
            "combine multiple unrelated genre symbols into a collage or montage.\n\n"
            "SUGGESTED CREATIVE STIMULI (adapt, reinterpret, or reject any that "
            "conflict with the musical identity — musical authenticity wins):\n"
            f"  Medium: {s['medium']}\n"
            f"  Composition: {s['composition']}\n"
            f"  Lighting: {s['lighting']}\n"
            f"  Texture: {s['texture']}\n"
            f"  Palette strategy: {s['palette_strategy']}\n"
            f"  Conceptual approach: {s['conceptual_approach']}\n\n"
            "Translate the music's atmosphere, rhythm, emotional character and "
            "cultural context into visual form. Do NOT literally illustrate the "
            "genre name. Do NOT prescribe the palette — derive an appropriate "
            "one from the genre's character, the chosen medium, the subject and "
            "the lighting.\n\n"
            "DO NOT AUTOMATICALLY USE: musical instruments, microphones, "
            "headphones, speakers, vinyl records, equalizer graphics, waveform "
            "visuals, streaming UI elements, concert photography, neon music "
            "symbols, guitars, drums, DJ equipment. Do NOT use these unless the "
            "art director determines one is essential to an unusually strong "
            "concept. Prefer metaphor, atmosphere, photography, abstraction, "
            "environment, material, human traces or visual storytelling.\n\n"
            "DO NOT AUTOMATICALLY RESORT TO: castles, warriors, skulls, "
            "graveyards, dead trees, swamps, fire, lightning, medieval scenery, "
            "fantasy typography — even for heavy music. These are clichés, not "
            "requirements. An element is justified only if the musical identity "
            "and the chosen concept specifically call for it.\n\n"
            "TYPOGRAPHY: Render the playlist title \"{playlist}\" as a clean, "
            "geometric, sans-serif wordmark in the lower third of the image. "
            "Use a single colour from the artwork's palette — white, black or "
            "a muted tone — whichever provides the strongest contrast. "
            "Keep it small, understated and legible at thumbnail size. "
            "No ornamental fonts, no drop shadows, no bevels, no outlines, "
            "no distressed metal text, no fake 3D.\n\n"
            "The artwork must look like a real record sleeve — not like a "
            "rendered search query, a stock image, or a playlist thumbnail. "
            "Square format. Visually complete edge to edge. No interface "
            "elements, no streaming-service branding, no watermarks, no fake "
            "text other than the playlist title."
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


#: Kept for backward compatibility with the config system. The active
#: template is now built dynamically by ``build_cover_prompt``.
DEFAULT_PROMPT_TEMPLATE = ""


def _state_path() -> str:
    from helpers.config_helpers import get_state_directory

    return os.path.join(get_state_directory(), _STATE_FILE_NAME)


def _load_state() -> dict[str, Any]:
    """``{genre_key: {"hash": ..., "playlist_id": ...}}`` for covers applied.

    ⚠️ Values are returned AS-IS. An earlier version coerced every value with
    ``str(v)``, which flattens a ``{"hash", "playlist_id"}`` entry into a junk
    one-line string and LOSES the playlist id. Legacy entries (a bare hash
    string) are still understood by :func:`_state_entry`.

    Best-effort: an unreadable/corrupt state file means "nothing recorded yet",
    which at worst regenerates art — never a crash before the upload.
    """
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


def _state_entry(state: dict[str, Any], genre_key: str) -> tuple[str, str]:
    """``(prompt_hash, playlist_id)`` recorded for *genre_key*.

    A LEGACY entry is a bare hash string with no playlist id, so ``playlist_id``
    comes back as ``""``. The caller treats a missing id as "not confirmed on
    any playlist" and re-applies the cover once, upgrading the entry.
    """
    raw = state.get(genre_key)
    if isinstance(raw, dict):
        return str(raw.get("hash") or ""), str(raw.get("playlist_id") or "")
    if isinstance(raw, str):
        return raw, ""
    return "", ""


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
    playlist_id: str | None = None,
) -> dict[str, Any]:
    """Generate + upload a cover for one genre playlist.

    Returns ``{"ok", "status", "reason", "playlist_id"}`` where *status* is one
    of ``generated`` / ``skipped`` / ``failed`` / ``disabled``. Never raises.

    ``playlist_id`` is the Navidrome playlist the art belongs to. Pass it when
    the caller has ALREADY resolved the playlist (the finalise stage maps names
    to ids from its existing ``getPlaylists`` fetch), which avoids a redundant
    lookup per genre playlist.
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

    # Resolve the playlist BEFORE deciding anything: generating an image we
    # cannot attach would spend quota for nothing, AND the playlist's ID is
    # part of the "does the cover already exist" decision below.
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

    # ── DOES THIS PLAYLIST ALREADY CARRY OUR ART? ──────────────────────────
    #
    # Skip ONLY when the recorded art still matches AND the record names the
    # playlist it was applied to and that id equals this playlist's id.
    #
    # ⚠️ The prompt hash alone is NOT proof the cover exists. It says "we once
    # made this image for this genre", not "this playlist still has it". A
    # playlist that was deleted and recreated gets a NEW id and NO image, and a
    # cover removed by hand leaves the hash intact — both would be skipped
    # forever by the old hash-only check.
    #
    # ⚠️ A missing recorded id (a legacy ``{genre: hash}`` entry) is therefore
    # treated as UNCONFIRMED and the cover is applied once more; the entry then
    # carries the id and every later scan skips. That costs at most one extra
    # image per genre for a pre-existing state file, and it is what makes a
    # playlist that lost its cover heal on the next scan.
    #
    # ⚠️ The CONFIG's regenerate flag is honoured too, not just the caller's
    # argument: the setting is what the user turns on, and a caller that forgets
    # the parameter must not silently make the option a no-op.
    _forced = bool(regenerate or cfg["regenerate"])
    recorded_hash, recorded_pid = _state_entry(_load_state(), genre_key)
    if not _forced and recorded_hash == digest:
        if recorded_pid and recorded_pid == resolved_id:
            return {**result, "ok": True, "status": "skipped",
                    "reason": "cover already applied to this playlist"}
        logger.info(
            "[GENRE_COVER] cover not confirmed on this playlist — re-applying",
            genre=genre,
            playlist=playlist_name,
            playlist_id=resolved_id,
            recorded_playlist_id=recorded_pid or None,
        )

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
        # ⚠️ ``resolved_id``, NOT the ``playlist_id`` parameter. The parameter
        # is only the caller's OPTIONAL hint and is None when it is not given;
        # uploading to None would POST to ``/api/playlist/None/image`` and fail
        # with 404 — a cover silently never applied on the default call path.
        ok, upload_reason = client.upload_playlist_cover(resolved_id, image, mime)
    except Exception as exc:
        logger.warning("[GENRE_COVER] cover upload raised for %s: %s", playlist_name, exc)
        return {**result, "status": "failed", "reason": str(exc)}

    if not ok:
        logger.warning(
            "[GENRE_COVER] cover upload rejected for %s: %s", playlist_name, upload_reason,
        )
        return {**result, "status": "failed", "reason": upload_reason or "upload rejected"}

    state = _load_state()
    # Record the art AND the playlist it landed on. Without the id a recreated
    # playlist can never be distinguished from one that already has the cover,
    # so the entry would suppress the repair forever.
    state[genre_key] = {"hash": digest, "playlist_id": resolved_id}
    _save_state(state)
    logger.info(
        "[GENRE_COVER] cover applied", genre=genre, playlist=playlist_name,
        playlist_id=resolved_id, bytes=len(image), mime=mime,
    )
    return {**result, "ok": True, "status": "generated", "reason": ""}
