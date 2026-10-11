"""Album art upload awaits ``request.files`` before reading it.

Reported: *"Upload failed: 'coroutine' object has no attribute 'get' when
uploading a new album cover from .jpg"*.

Quart's ``request.files`` is an ASYNC attribute — the same class of property
as ``request.form``. The upload route read ``request.files.get("image")``
without awaiting, so ``request.files`` was the coroutine itself and
``.get`` raised ``AttributeError: 'coroutine' object has no attribute
'get'``. Every non-empty upload failed with the generic "Upload failed: …"
wrapper. The playlists import route (``files = await request.files``) had the
pattern correct; the album art route did not.

The guard is source-level: after stripping comments, the route must await
``request.files`` and must NOT access it without the await on any path.
"""

from __future__ import annotations

import io
import tokenize

import pytest


def _strip(source: str) -> str:
    """Remove comments/docstrings so a probe cannot match its own prose.

    ``tokenize`` separates the dot from its attribute (``request . files``),
    so the returned string also drops ALL whitespace — specifiers and values
    are compared against it in whitespace-free form (``await request.files``
    matches ``awaitrequest.files``-normalised ``await request . files``).
    """
    import re as _re

    try:
        toks = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError:
        return ""
    joined = " ".join(
        t.string for t in toks
        if t.type not in (
            tokenize.COMMENT, tokenize.STRING, tokenize.NL,
            tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
            tokenize.ENCODING,
        )
    )
    return _re.sub(r"\s+", "", joined)


def _upload_source() -> str:
    import routes.album_routes as ar

    src = io.open(ar.__file__, encoding="utf-8").read()
    idx = src.index("api_album_upload_art")
    # Extend past the whole handler (through its last return).
    end = src.index("@album_bp.route", idx)
    return src[idx:end]


def test_upload_route_awaits_request_files() -> None:
    """The handler must ``await request.files`` before any ``.get`` on it."""
    clean = _strip(_upload_source())
    assert "awaitrequest.files" in clean, (
        "the upload route never awaits request.files — request.files.get(...) "
        "reads the coroutine instead of the file dict, which is the reported "
        "'coroutine' object has no attribute 'get' failure"
    )


def test_upload_route_never_calls_get_on_unaward_files() -> None:
    """``request.files.get`` (no await) must never appear in the handler."""
    clean = _strip(_upload_source())
    assert "request.files.get" not in clean, (
        "request.files.get(...) appears without an await — Quart's request.files "
        "is an async attribute, so .get() runs on the coroutine"
    )


def test_upload_route_awaits_request_form_too() -> None:
    """The sibling async attribute keeps its await (regression guard)."""
    clean = _strip(_upload_source())
    assert "awaitrequest.form" in clean


def test_the_fix_is_not_a_comment() -> None:
    # The documentation comment about the await must not be what satisfies the
    # probe — _strip removes comments, so this only passes on real code.
    src = _upload_source()
    assert "awaitrequest.files" in _strip(src)