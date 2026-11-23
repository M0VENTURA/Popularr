"""Redacted configuration export.

The point of an export is to be SHAREABLE — to attach to a bug report, diff two
installs, or keep a snapshot before retuning the scoring. That is only safe if
credentials never leave the box, so every secret is replaced by a marker that
still says whether one is SET (which is what you actually need for support)
without carrying the value.

The distinction that matters:

  "lastfm_api_key": "<redacted>"     → a value is configured
  "lastfm_api_key": ""               → nothing is configured

so the export never disguises "missing" as "present" or vice versa.
"""

from __future__ import annotations

import copy
from typing import Any

#: Placeholder for a credential that IS configured.
REDACTED = "<redacted>"

#: Name fragments that mark a leaf as a credential. Matched against the KEY
#: only, never the value — a value-based heuristic would both miss an API key
#: stored under an unexpected name and redact a harmless field like a playlist
#: literally called "Secret Garden".
_SECRET_KEY_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "access_key",
    "private_key",
    "credential",
)

#: Short forms that only count when they are a whole path component.
#:
#: ``_pass`` is the abbreviation the shipped config actually uses (``nav_pass``),
#: and a plain substring match on "pass" would also mask "bypass"/"compass", so
#: these are matched as a trailing component instead. A test asserts the shipped
#: secret keys are all caught.
_SECRET_SUFFIXES = ("pass", "pwd", "key", "auth")

#: Keys that name a USER rather than a credential. They are not secret, but the
#: export is meant to be shareable, so they are masked too — a support thread
#: does not need to know the Navidrome account name, only that one is set.
_IDENTITY_KEY_FRAGMENTS = ("user", "username", "login", "email")

#: Whole blocks whose leaf values are credentials or identities.
_SECRET_CONTAINERS = ("auth", "credentials", "secrets")


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    if any(f in lowered for f in _SECRET_KEY_FRAGMENTS):
        return True
    if any(lowered == s or lowered.endswith("_" + s) for s in _SECRET_SUFFIXES):
        return True
    # ``user``/``login`` only count as identity when they are the whole key or a
    # trailing component — "num_users" is a count, not an identity.
    return any(lowered == f or lowered.endswith("_" + f) or lowered.startswith(f + "_")
               for f in _IDENTITY_KEY_FRAGMENTS)


def _redact_value(value: Any) -> str:
    """Marker that preserves "configured vs not" without the value."""
    if value is None or value == "" or value == [] or value == {}:
        return ""
    return REDACTED


def redact_config(value: Any, *, _depth: int = 0) -> Any:
    """Recursively replace credential and identity leaves with a marker."""
    if _depth > 20:  # pathological nesting guard
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, child in value.items():
            lowered = str(key).lower()
            if any(c in lowered for c in _SECRET_CONTAINERS) and not isinstance(child, (dict, list)):
                out[key] = _redact_value(child)
            elif _is_secret_key(str(key)) and not isinstance(child, (dict, list)):
                out[key] = _redact_value(child)
            elif _is_secret_key(str(key)) and isinstance(child, list):
                # e.g. navidrome_users: [{user, password}] — keep the shape and
                # the COUNT, drop every field that identifies someone.
                out[key] = [
                    redact_config(item, _depth=_depth + 1) if isinstance(item, dict) else _redact_value(item)
                    for item in child
                ]
            else:
                out[key] = redact_config(child, _depth=_depth + 1)
        return out
    if isinstance(value, list):
        return [redact_config(item, _depth=_depth + 1) for item in value]
    return value


#: Defaults that the UI renders as `placeholder`. Shipped here rather than
#: imported so the export stays cheap, and audited against the config getters by
#: tests/test_config_export.py so it cannot silently drift.
DEFAULT_PLACEHOLDERS: dict[str, Any] = {}


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            flat.update(_flatten(child, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        flat[prefix] = value
    else:
        flat[prefix] = value
    return flat


def _numbers_equal(a: Any, b: Any) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def build_config_export(
    config: dict[str, Any],
    *,
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the shareable export payload.

    Returns a dict with:
      * ``settings`` — every setting, credentials and identities redacted;
      * ``changed_from_default`` — flat ``dotted.key: value`` of settings that
        differ from their shipped default, which is the audit the Config page
        otherwise makes you do by hand across 400+ fields;
      * ``redacted_keys`` — the dotted paths that were masked, so a reader knows
        what is missing rather than assuming there was nothing to redact.
    """
    safe = redact_config(copy.deepcopy(config))
    redacted_paths: list[str] = []

    def _collect(original: Any, sanitised: Any, prefix: str = "") -> None:
        if isinstance(original, dict) and isinstance(sanitised, dict):
            for key in original:
                path = f"{prefix}.{key}" if prefix else str(key)
                if sanitised.get(key) == REDACTED:
                    redacted_paths.append(path)
                else:
                    _collect(original[key], sanitised.get(key), path)
        elif isinstance(original, list) and isinstance(sanitised, list):
            for index, (left, right) in enumerate(zip(original, sanitised)):
                _collect(left, right, f"{prefix}[{index}]")

    _collect(config, safe)

    changed: dict[str, Any] = {}
    if defaults:
        flat_defaults = _flatten(defaults)
        for path, actual in _flatten(safe).items():
            if path not in flat_defaults:
                continue
            expected = flat_defaults[path]
            if _numbers_equal(actual, expected):
                continue
            if isinstance(actual, (list, dict)):
                continue
            changed[path] = actual

    return {
        "settings": safe,
        "changed_from_default": changed,
        "redacted_keys": sorted(redacted_paths),
    }
