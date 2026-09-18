"""Artist name filing for "The"-prefixed names.

A record-shop convention: ``The Offspring`` is filed as ``Offspring, The`` so it
sorts among the O's rather than being buried in a wall of T's.  Three separate
concerns live here, and callers should pick the one they actually need:

``artist_sort_key``
    The alphabetical key — ``"offspring, the"``.  Use for ORDER BY / sorted().
``artist_sort_name``
    The DISPLAY label — ``"Offspring, The"``.  Use for what a user reads.
``artist_sort_letter``
    The section letter — ``"O"``, or ``"#"`` for anything not A-Z.

Why the label is separate from the stored name
----------------------------------------------
The stored artist name is an IDENTITY: it is the ``tracks.artist`` /
``album_artist`` value, it keys ``/artist/<name>`` URLs, and it is what
``/api/artist/image`` looks up.  Rewriting it would break all three.  So this
module only ever *derives* a label — nothing here should be written back to the
database or fed into a URL.  Callers must keep the true name for links, API
params and database writes, and use ``artist_sort_name`` purely for text.

Only "The"
----------
Deliberately narrow.  ``A`` / ``An`` are not moved (they are far more often part
of the real title than an article the listener files by), and neither are
foreign equivalents (``Les``, ``Los``, ``Die`` ...).  Widen ``ARTICLES`` only if
that becomes a genuine requirement — the SQL builder below derives from the same
constant, so both sides stay in step.

Matching rules
--------------
* Case-insensitive, and requires a space after the article.  Only a space — a
  tab is not treated as a separator, because SQL's ``LIKE`` cannot express one
  and the two implementations must agree exactly.
* One or more spaces are tolerated (``"The  Cure"`` files normally).
* ``"Theatre of Tragedy"`` and ``"Theodore"`` are untouched — ``the`` is only an
  article when a word boundary follows it.
* A bare ``"The"`` (no trailing name) is untouched.
* Surrounding whitespace is trimmed before matching.
* The core name keeps its original casing; the article is re-appended verbatim
  as it appeared.  This is a *move*, not a re-casing, so ``"THE THE"`` becomes
  ``"THE, THE"`` rather than being partially rewritten.
"""
from __future__ import annotations

import re
from typing import Final

#: Leading articles that are moved to the end.  See "Only The" above.
ARTICLES: Final[tuple[str, ...]] = ("The",)

#: ``the<space><rest>`` — the space is what stops "Theatre"/"Theodore" matching.
#: Built from ``ARTICLES`` so the SQL helper and the Python helpers can never
#: disagree about what counts as an article.
#:
#: Deliberately a literal SPACE rather than ``\s``: ``LIKE 'the %'`` in SQL can
#: only express a literal space, and the two implementations must agree exactly.
#: A tab is not a separator an artist name realistically uses, and treating it as
#: one would make Python and SQL file the same name into different sections.
_LEADING_ARTICLE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:" + "|".join(re.escape(a) for a in ARTICLES) + r") +(?P<core>.+)$",
    re.IGNORECASE | re.DOTALL,
)


def _sql_core_offset(article: str) -> int:
    """1-indexed ``SUBSTR`` offset at which the core name begins.

    ``"The Offspring"`` → 5: ``SUBSTR`` counts from 1, so positions 1-4 are
    ``"The "`` and the core starts at 5.  Contrast the Python slice index, which
    would be 4.
    """
    return len(article) + 2


def split_leading_article(name: str | None) -> tuple[str, str]:
    """Split off a leading article.

    Returns ``(core, article)`` where ``article`` is ``""`` when ``name`` does
    not start with one.  The core keeps its original casing::

        "The Offspring"  → ("Offspring", "The")
        "Theatre"        → ("Theatre", "")
        "The"            → ("The", "")
        None             → ("", "")
    """
    if not name:
        return "", ""
    text = str(name).strip()
    if not text:
        return "", ""
    match = _LEADING_ARTICLE_RE.match(text)
    if not match:
        return text, ""
    core = (match.group("core") or "").strip()
    if not core:
        # e.g. "The " — the article is the whole name, so there is nothing to
        # file it under and moving it would produce the nonsense ", The".
        return text, ""
    article = text[: len(text) - len(match.group("core"))].strip()
    return core, article


def artist_sort_key(name: str | None) -> str:
    """Alphabetical key with any leading article moved to the end.

    Lower-cased so it is a valid ``sorted()`` key and matches the SQL expression
    built by :func:`artist_sort_sql`::

        "The Offspring"     → "offspring, the"
        "The Pretty Reckless" → "pretty reckless, the"
        "Radiohead"         → "radiohead"
    """
    core, article = split_leading_article(name)
    if not article:
        return core.casefold()
    return f"{core.casefold()}, {article.casefold()}"


def artist_sort_name(name: str | None) -> str:
    """Display label with any leading article moved to the end.

    This is the ONLY function intended for user-visible text.  Never pass its
    result to ``url_for``, an API query string, or a database write — it is not
    the artist's identity::

        "The Offspring"       → "Offspring, The"
        "The Pretty Reckless" → "Pretty Reckless, The"
        "Radiohead"           → "Radiohead"
    """
    core, article = split_leading_article(name)
    if not article:
        return core
    return f"{core}, {article}"


def artist_sort_letter(name: str | None) -> str:
    """Section letter for a name, ``"#"`` when it is not A-Z.

    Computed from the CORE name, so ``The Offspring`` files under ``O``.

    Deliberately ASCII-only rather than using Unicode-aware ``str.isalpha()``.
    Two reasons: the jump picker only offers tiles for ``#`` and A-Z, so a
    section letter like ``"宇"`` would be rendered but unreachable; and the SQL
    predicate that must agree with this function is ``NOT BETWEEN 'A' AND 'Z'``,
    which has no notion of Unicode letters.  Non-Latin names therefore land in
    ``#`` — which is what that bucket exists for.
    """
    core, _ = split_leading_article(name)
    if not core:
        return "#"
    first = core[0].upper()
    return first if "A" <= first <= "Z" else "#"


def _sql_core_case(column: str) -> str:
    """SQL expression yielding the CORE name (leading article removed).

    One ``WHEN`` per entry in ``ARTICLES``, so adding an article updates the
    Python regex and the SQL together — the two can never disagree.

    ``LIKE 'the %'`` on the lowered value is the SQL twin of the regex's
    "article followed by whitespace then something".  Like the regex it cannot
    match ``theatre``, because a space must follow the article.

    The inner ``TRIM`` matters: ``SUBSTR(x, 5)`` removes a fixed number of
    characters, so with more than one space after the article it would leave a
    leading space on the core.  That is not cosmetic — ``"The  Two Spaces"``
    would sort ahead of every letter and report ``' '`` as its section letter,
    diverging from the Python regex (which tolerates repeated spaces).  ``TRIM``
    keeps the two in step.
    """
    trimmed = f"TRIM({column})"
    whens = " ".join(
        f"WHEN LOWER({trimmed}) LIKE '{article.casefold()} %' "
        f"THEN TRIM(SUBSTR({trimmed}, {_sql_core_offset(article)}))"
        for article in ARTICLES
    )
    return f"CASE {whens} ELSE {trimmed} END"


def artist_sort_sql(column: str) -> str:
    """SQL ``ORDER BY`` expression matching :func:`artist_sort_key`.

    ``column`` is any SQL expression resolving to the artist name (e.g.
    ``"COALESCE(NULLIF(album_artist, ''), artist)"``).  It is interpolated, so
    it must be a literal from calling code — never user input.

    Deliberately written with ``SUBSTR`` and ``||`` rather than ``SUBSTRING(x
    FROM 5)``/``CONCAT``: both spellings work on PostgreSQL *and* SQLite, and the
    test suite runs on SQLite.

    The article test is applied to ``TRIM(...)`` so a name with stray leading
    whitespace is filed exactly as Python files it.
    """
    trimmed = f"TRIM({column})"
    core = _sql_core_case(column)
    whens = " ".join(
        f"WHEN LOWER({trimmed}) LIKE '{article.casefold()} %' "
        f"THEN LOWER({core}) || ', {article.casefold()}'"
        for article in ARTICLES
    )
    return f"CASE {whens} ELSE LOWER({trimmed}) END"


def artist_sort_letter_sql(column: str) -> str:
    """SQL expression yielding the section letter matching :func:`artist_sort_letter`.

    Upper-cases the first character of the CORE name, so ``The Offspring``
    yields ``O``.  Callers treat anything outside A-Z as ``"#"`` in their own
    grouping code, which is what the artist list already does.
    """
    return f"UPPER(SUBSTR({_sql_core_case(column)}, 1, 1))"


def artist_starts_with_letter_sql(column: str, letter_param: str = "prefix") -> str:
    """SQL predicate testing whether a name files under a letter.

    Matches on the CORE name, so the artist list's "O" section and the
    scan-letter action agree about which artists are in it::

        artist_starts_with_letter_sql("COALESCE(NULLIF(album_artist, ''), artist)")
        # → "UPPER(SUBSTR(CASE WHEN ... END, 1, 1)) LIKE :prefix"

    ``letter_param`` is the bind-parameter name the caller supplies (as ``"O%"``).
    """
    return f"{artist_sort_letter_sql(column)} LIKE :{letter_param}"


#: Public aliases — the underscore-free names read better at call sites, and
#: ``__all__`` documents the intended surface.
__all__ = [
    "ARTICLES",
    "artist_sort_key",
    "artist_sort_name",
    "artist_sort_letter",
    "artist_sort_sql",
    "artist_sort_letter_sql",
    "artist_starts_with_letter_sql",
    "split_leading_article",
]
