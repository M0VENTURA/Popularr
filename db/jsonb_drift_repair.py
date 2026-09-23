"""Startup check-and-repair for JSONB column drift. **test_site only.**

════════════════════════════════════════════════════════════════════════
WHY THIS EXISTS
════════════════════════════════════════════════════════════════════════
``db/schema.py``'s ``COLUMN_REGISTRY`` DECLARES columns as JSONB, while
``migrations/versions/001_initial_schema.py`` created several of them as
``sa.Text()``.  Only SOME ever received an ``ALTER COLUMN … TYPE JSONB``, so
the two mechanisms drifted apart and six columns remain TEXT while every
Python consumer believes they are JSONB::

    tracks.manual_genres            tracks.navidrome_genres
    tracks.spotify_genres           tracks.listenbrainz_genres
    tracks.essentia_genres          missing_releases.lastfm_tags

``_ensure_columns`` cannot converge them by design: it only runs
``ALTER TABLE … ADD COLUMN IF NOT EXISTS``, which is a no-op on a column that
already exists, whatever its type.

════════════════════════════════════════════════════════════════════════
WHAT ACTUALLY BREAKS  (verified, not assumed)
════════════════════════════════════════════════════════════════════════
NOT the readers.  ``genre_tag_aggregator.parse_json_tags`` deliberately falls
back to ``parse_delimited_tags`` when its input is not JSON, so a TEXT column
holding ``rock, metal`` and a JSONB column holding ``["rock","metal"]`` are both
read correctly.  That tolerance is exactly why the drift went unnoticed.

What breaks is code that relies on JSONB SEMANTICS:

  * ``CAST(:value AS JSONB)`` on a CSV string raises
    ``invalid input syntax for type json``.  This is the confirmed cause of an
    album save reporting "No changes were made." — the INSERT raised, the
    handler caught it at DEBUG, ``updated_count`` stayed 0, and the whole
    transaction (including the album type change) rolled back.
  * ``coerce_track_value_for_pg_type`` has no JSONB branch, so a CSV string is
    handed to PostgreSQL unchanged.

════════════════════════════════════════════════════════════════════════
WHY TEST-SITE ONLY
════════════════════════════════════════════════════════════════════════
A type conversion rewrites every row of the column and is not cheaply
reversible.  This pass exists to validate the repair against the rebuilt UI
before it is allowed near a live install — which is the entire purpose of
``features.use_test_site``.  :func:`config_enables_test_site` is the same gate
the UI cutover uses, so "test site" means one thing in this codebase.

════════════════════════════════════════════════════════════════════════
SAFETY
════════════════════════════════════════════════════════════════════════
* **Idempotent** — a correctly-typed column is skipped, never rewritten.
* **Only the six known columns**, spelled as a literal.  The list is NOT
  derived from the registry, so a future JSONB declaration cannot silently pull
  a new column into a destructive conversion.
* **Data-preserving** — ``to_jsonb(string_to_array(v, ','))`` splits exactly
  the way ``parse_delimited_tags`` does, and an already-JSON value is cast
  rather than re-split.  This mirrors the proven ALTER block already in
  ``db/schema.py`` so the two cannot converge on different shapes.
* **One column per transaction** — a failure leaves that column exactly as it
  was found, never half-converted.
* **Never raises** — a failure is logged and the next column is attempted.  A
  repair pass must not be able to stop the app from booting.
* **No index creation.**  Verified: there are NO GIN indexes on any of these
  columns.  The JSONB GIN indexes in ``INDEXES_TO_ENSURE`` cover
  ``musicbrainz_genres`` / ``discogs_genres`` / ``lastfm_tags`` /
  ``audiodb_genres`` / ``wikidata_genres`` — none of which are drifted.
  Creating indexes here would invent schema the codebase never had.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session

logger = structlog.get_logger(__name__)

#: ``{table: (columns…)}`` — the DRIFTED columns ONLY, verified against
#: migration 001 (created TEXT) cross-referenced with the ALTER blocks in
#: ``db/schema.py`` (never altered).  ``tracks`` has five,
#: ``missing_releases`` one.  Every other registry-JSONB column either got its
#: ALTER or was added by ``_ensure_columns`` with its declared type, so it is
#: genuinely JSONB already and must not be listed here.
_DRIFTED: dict[str, tuple[str, ...]] = {
    "tracks": (
        "manual_genres",
        "navidrome_genres",
        "spotify_genres",
        "listenbrainz_genres",
        "essentia_genres",
    ),
    "missing_releases": ("lastfm_tags",),
}

#: Types the drift actually produced.  Deliberately narrow — ``text`` and
#: ``character varying`` are what migration 001 used.  Anything else (a real
#: ``json``, an array, a custom type) is reported and left untouched rather
#: than guessed at.
_TEXT_TYPES: tuple[str, ...] = ("text", "character varying")


def _enabled() -> bool:
    """True only when the rebuilt UI is selected.

    Reads through the same helper the UI cutover uses, so the two cannot
    disagree about what "test site" means.  Any failure resolves to False —
    a repair pass should never run because a config read raised.
    """
    try:
        from helpers.test_site_mode import config_enables_test_site

        return bool(config_enables_test_site())
    except Exception as exc:
        logger.warning("Check-repair: could not read test-site flag; skipping", error=str(exc))
        return False


def _column_types(session: Any, table: str, columns: tuple[str, ...]) -> dict[str, str]:
    """Return ``{column: actual_pg_type}`` for ``table``.

    Uses ``to_regclass`` so a table that does not exist yields an empty map
    instead of raising, and resolves through the connection's ``search_path``
    exactly like the application's own queries — so this inspects the same
    physical table the app reads and writes.
    """
    if not columns:
        return {}
    result = session.execute(
        text(
            """
            SELECT a.attname, format_type(a.atttypid, a.atttypmod)
            FROM pg_attribute a
            WHERE a.attrelid = to_regclass(:name)
              AND a.attnum > 0
              AND NOT a.attisdropped
              AND a.attname = ANY(:columns)
            """
        ),
        {"name": table, "columns": columns},
    )
    return {str(row[0]): str(row[1]).lower() for row in result.fetchall() or []}


def _is_jsonb(pg_type: str) -> bool:
    return "jsonb" in (pg_type or "").lower()


def _is_text(pg_type: str) -> bool:
    """True for the types the drift actually produced.

    Deliberately narrow: ``character varying`` and ``text`` are what migration
    001 used.  Anything else (a real ``json``, an array, a custom type) is left
    alone rather than guessed at.
    """
    t = (pg_type or "").lower()
    return t.startswith("character varying") or t == "text" or t.startswith("text")


def _convert_using(column: str) -> str:
    """The ``USING`` expression converting ``column`` from TEXT to JSONB.

    Mirrors the existing, proven block in ``db/schema.py`` exactly, so the
    startup pass and the schema bootstrap cannot converge on different shapes::

        NULL / ''      -> '[]'::jsonb           (an empty array, not null)
        leading [ or { -> cast as-is            (already JSON; do NOT re-split)
        otherwise      -> to_jsonb(string_to_array(v, ','))

    The ``[``/``{`` guard matters: re-splitting an existing JSON literal on
    commas would shred it into one element per token.
    """
    return (
        f"CASE "
        f"WHEN {column} IS NULL OR trim({column}) = '' THEN '[]'::jsonb "
        f"WHEN left(trim({column}), 1) IN ('[', '{{') THEN {column}::jsonb "
        f"ELSE to_jsonb(string_to_array({column}, ',')) "
        f"END"
    )


def run_startup_check_repair() -> dict[str, Any]:
    """Detect and repair the JSONB/TEXT drift.  Test-site only.

    Returns a summary dict so a caller (or a test) can assert what happened
    without scraping logs::

        {"ran": bool, "reason": str,
         "checked": [...], "converted": [...], "errors": [...]}

    ``ran`` is False with a ``reason`` when the pass declined to do anything —
    which is the normal and expected outcome on a live install.
    """
    summary: dict[str, Any] = {
        "ran": False,
        "reason": "",
        "checked": [],
        "converted": [],
        "errors": [],
    }

    if not _enabled():
        summary["reason"] = "test-site mode is off"
        logger.debug("Check-repair skipped (test-site mode off)")
        return summary

    summary["ran"] = True

    # ``tracks`` is listed first on purpose: it is the table the confirmed
    # failure was observed on, so if the process is interrupted partway the
    # most valuable columns are already repaired.
    for table, columns in _DRIFTED.items():
        try:
            with db_session() as session:
                actual_types = _column_types(session, table, columns)
        except Exception as exc:
            summary["errors"].append(f"{table}: type probe failed: {exc}")
            logger.warning("Check-repair: type probe failed", table=table, error=str(exc))
            continue

        if not actual_types:
            # Table absent — a fresh install may not have every table yet.
            # Not an error and not worth a warning: there is nothing to repair.
            logger.debug("Check-repair: table not present, skipping", table=table)
            continue

        for column in columns:
            actual = actual_types.get(column)
            if actual is None:
                logger.debug(
                    "Check-repair: column not present, skipping",
                    table=table, column=column,
                )
                continue

            summary["checked"].append(f"{table}.{column}:{actual}")

            if _is_jsonb(actual):
                continue                       # already correct — idempotent
            if not _is_text(actual):
                summary["errors"].append(
                    f"{table}.{column}: unexpected type {actual!r}; left untouched"
                )
                logger.warning(
                    "Check-repair: unexpected column type, not converting",
                    table=table, column=column, pg_type=actual,
                )
                continue

            # One column per transaction: a failure must leave that column
            # exactly as it was found rather than half-converted.
            try:
                with db_session() as session:
                    session.execute(
                        text(
                            f"ALTER TABLE {table} "
                            f"ALTER COLUMN {column} TYPE JSONB "
                            f"USING {_convert_using(column)}"
                        )
                    )
                summary["converted"].append(f"{table}.{column}")
                logger.info(
                    "Check-repair: converted column to JSONB",
                    table=table, column=column, was=actual,
                )
            except Exception as exc:
                summary["errors"].append(f"{table}.{column}: {exc}")
                logger.error(
                    "Check-repair: conversion failed",
                    table=table, column=column, was=actual, error=str(exc),
                )

    if summary["converted"]:
        # The tracks column-TYPE cache was populated before this ran, so it
        # still holds the pre-conversion types.  Without this the coercion
        # layer keeps treating the converted columns as TEXT for the life of
        # the process — writes would keep failing after a successful repair,
        # which would look identical to the original bug.
        try:
            from db.repositories.popularity_repository import (
                invalidate_tracks_column_cache,
            )

            invalidate_tracks_column_cache()
        except Exception as exc:
            logger.debug("Check-repair: column cache invalidation skipped", error=str(exc))

    logger.info(
        "Check-repair complete",
        checked=len(summary["checked"]),
        converted=len(summary["converted"]),
        errors=len(summary["errors"]),
    )
    return summary
