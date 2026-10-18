"""Google Gemini client — text-to-image generation for playlist artwork.

Only the ONE call this app needs: ``generateContent`` on an image-capable Gemini
model, reading the inline base64 image out of the response parts.

⚠️ Image output is requested with ``generationConfig.responseModalities =
["IMAGE"]``. A TEXT+IMAGE model still answers with a text part BEFORE (or after)
the image, so the parts are SCANNED for ``inlineData`` rather than assuming the
image is ``parts[0]`` — assuming position is how a perfectly good response gets
reported as "no image came back".

The key travels in the ``x-goog-api-key`` HEADER, never a ``?key=`` query string:
a URL is copied into logs, screenshots and error messages far more often than a
header is.

Nothing here knows about playlists or Navidrome — see
``services/playlists/genre_cover_service.py`` for that.
"""

from __future__ import annotations

import base64
from typing import Any

import structlog

from api_clients import session

#: ⚠️ structlog, NOT ``logging.getLogger``. Every call below passes key=value
#: context, and a STDLIB logger raises ``TypeError: Logger._log() got an
#: unexpected keyword argument`` on those — inside the failure handlers, which
#: is exactly where losing the message hurts most.
logger = structlog.get_logger(__name__)

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"

#: An image-capable Gemini model. Overridable per install (``gemini.model``),
#: because model names for this family have changed more than once.
DEFAULT_MODEL = "gemini-2.5-flash-image"


def _extract_inline_image(payload: Any) -> tuple[bytes | None, str]:
    """Pull the first inline image out of a ``generateContent`` response.

    Walks EVERY part rather than indexing ``parts[0]``: a TEXT+IMAGE reply
    carries a text part too, and which one comes first is not guaranteed.
    """
    try:
        candidates = (payload or {}).get("candidates") or []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content") or {}
            for part in (content.get("parts") or []):
                if not isinstance(part, dict):
                    continue
                inline = part.get("inlineData") or part.get("inline_data")
                if not isinstance(inline, dict):
                    continue
                data = inline.get("data")
                if not data:
                    continue
                try:
                    return base64.b64decode(data), str(
                        inline.get("mimeType") or inline.get("mime_type") or "image/png"
                    )
                except Exception as exc:
                    logger.warning("Gemini image part could not be decoded: %s", exc)
                    return None, ""
    except Exception as exc:
        logger.debug("Gemini response parse failed: %s", exc)
    return None, ""


def _blocked_reason(payload: Any) -> str:
    """A safety/recitation block, or "" when the response was not blocked."""
    try:
        feedback = ((payload or {}).get("promptFeedback") or {})
        if feedback.get("blockReason"):
            return f"blocked: {feedback.get('blockReason')}"
        for candidate in ((payload or {}).get("candidates") or []):
            finish = str((candidate or {}).get("finishReason") or "").upper()
            if finish and finish not in ("STOP", "FINISH_REASON_UNSPECIFIED"):
                return f"blocked: {finish.lower()}"
    except Exception:
        pass
    return ""


def generate_image(
    prompt: str,
    *,
    api_key: str,
    model: str | None = None,
    timeout: float = 120.0,
) -> tuple[bytes | None, str, str]:
    """Generate ONE image for *prompt*.

    Returns ``(image_bytes, mime_type, reason)``. On success *reason* is ``""``;
    on failure ``image_bytes`` is ``None`` and *reason* names the cause. Never
    raises — a caller generating playlist art must not lose its scan to a 429.

    Reasons: ``no_api_key``, ``empty_prompt``, ``blocked: …``, ``no_image``,
    ``http_<code>`` (401 = bad key, 429 = quota/rate), ``exception``.
    """
    key = str(api_key or "").strip()
    if not key:
        return None, "", "no_api_key"
    text = str(prompt or "").strip()
    if not text:
        return None, "", "empty_prompt"

    resolved_model = str(model or "").strip() or DEFAULT_MODEL
    url = f"{API_ROOT}/models/{resolved_model}:generateContent"
    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    try:
        response = session.post(
            url,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )
    except Exception as exc:
        logger.warning("Gemini request failed: %s", exc)
        return None, "", "exception"

    if response.status_code >= 400:
        # ⚠️ The BODY is logged, not just the code: Google puts the actionable
        # sentence there ("API key not valid", "model not found", quota detail),
        # and a bare ``http_400`` sends the operator to the wrong place.
        detail = ""
        try:
            err = (response.json() or {}).get("error") or {}
            detail = str(err.get("message") or "").strip()
        except Exception:
            detail = (response.text or "")[:200]
        logger.warning(
            "Gemini image request rejected",
            status=response.status_code, model=resolved_model, detail=detail,
        )
        return None, "", f"http_{response.status_code}"

    try:
        payload = response.json() or {}
    except Exception as exc:
        logger.warning("Gemini returned a non-JSON response: %s", exc)
        return None, "", "exception"

    image, mime = _extract_inline_image(payload)
    if image:
        return image, mime or "image/png", ""
    return None, "", _blocked_reason(payload) or "no_image"
