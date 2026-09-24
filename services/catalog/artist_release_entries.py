"""Build the artist page's "missing" + "upcoming" release entries.

Extracted from ``routes/ui_routes.py::_build_artist_detail_payload`` so the
decision logic is a PURE function that can be tested directly. It was previously
inline in a ~600-line payload builder, which meant the only way to exercise it
was to drive the whole page (DB + MusicBrainz-adjacent lookups), so the rules
below had no test at all.

Two independent pipelines discover releases, and this merges them:

* ``missing_releases`` — written by the missing-releases SCAN, which browses
  MusicBrainz release GROUPS.
* ``upcoming_releases`` — written by ``services/upcoming_releases/`` (Wikipedia
  scraper + MusicBrainz fetcher), which drives the Upcoming Releases page.

The artist page read only the first, so an announcement the upcoming pipeline
had already discovered was invisible there until the scan happened to cache it.
Merging is safe because de-duplication is by NORMALISED TITLE across both
sources — the two pipelines genuinely overlap, because both ultimately describe
the same MusicBrainz release groups.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from helpers.normalization_service import normalize_title_for_lookup
from services.catalog.release_categories import (
    UPCOMING_KEY,
    category_for_musicbrainz,
    is_upcoming_date,
    normalise_category,
)


def _as_int(value: Any) -> int | None:
    """Best-effort int, or None. Never raises on junk."""
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _year_of(value: Any) -> int | None:
    """Leading 4-digit year from a full/partial date, else None."""
    raw = str(value or "").strip()
    if len(raw) >= 4 and raw[:4].isdigit():
        return int(raw[:4])
    return None


def build_missing_and_upcoming_entries(
    *,
    missing_rows: Iterable[Mapping[str, Any]] = (),
    upcoming_rows: Iterable[Mapping[str, Any]] = (),
    owned_titles: Iterable[str] = (),
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Release entries for the artist page, missing + upcoming merged.

    ``missing_rows`` are ``missing_releases`` rows; ``upcoming_rows`` are
    ``upcoming_releases`` rows; ``owned_titles`` are the album names the artist
    already has in the library (raw — normalised here).

    Returns a list of template-shaped dicts, each carrying ``is_missing`` and
    (when applicable) ``is_upcoming``, plus the private ``_category`` the
    section bucketing reads.

    Rules, each of which exists because of a specific defect:

    1. **A release already owned is not missing.** Seeded from ``owned_titles``
       so an upcoming row for an album in the library does not grow a second,
       contradictory "Missing" copy.
    2. **Upcoming is decided from the DATE, never the year.** The previous
       ``year > now.year`` test was wrong for most of the calendar — on
       2026-09-24 a release dated 2026-12-05 compared ``2026 > 2026`` = False, so
       every album still to come THAT year was labelled plain "Missing".
    3. **Upcoming overrides the release TYPE, not the reverse.** A future-dated
       studio album is primary ``Album``, so classifying by type first would
       leave the Upcoming section unreachable for the commonest case of all.
    4. **Undated rows are never promoted.** An announcement with no date cannot
       be shown as upcoming; it is skipped rather than guessed at.
    5. **One entry per title across both sources**, so a release present in both
       tables (or twice within one) renders once. The ``missing_releases`` row
       wins, because it carries cover art, a release id and a stored category.
    """
    seen: set[str] = set()
    for title in owned_titles:
        key = normalize_title_for_lookup(str(title or ""))
        if key:
            seen.add(key)

    entries: list[dict[str, Any]] = []

    # ── Cached missing releases (authoritative: art, id, category) ──────────
    for row in missing_rows or ():
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        key = normalize_title_for_lookup(title)
        if not key or key in seen:
            continue
        seen.add(key)

        first_release = str(row.get("first_release_date") or "")
        is_upcoming = is_upcoming_date(first_release, today=today)

        stored_category = str(row.get("category") or "").strip()
        if is_upcoming:
            category = UPCOMING_KEY
        elif stored_category:
            # Resolves canonical keys, legacy display labels ("Live Album") and
            # composite type strings, so rows written before the category
            # registry existed still land in the right section.
            category = normalise_category(stored_category)
        else:
            category = category_for_musicbrainz(
                str(row.get("primary_type") or ""),
                row.get("secondary_types") or "",
            )

        entries.append({
            "album": title,
            "title": title,
            "album_year": _year_of(first_release) or _as_int(row.get("release_year")),
            "track_count": 0,
            "avg_stars": None,
            "total_duration": 0,
            "is_missing": True,
            "is_upcoming": is_upcoming,
            "first_release_date": first_release,
            "cover_art_url": row.get("cover_art_url") or "",
            "release_id": str(row.get("release_id") or ""),
            "_category": category,
        })

    # ── Upcoming releases discovered by the other pipeline ──────────────────
    for row in upcoming_rows or ():
        title = str(row.get("album_name") or "").strip()
        if not title:
            continue
        key = normalize_title_for_lookup(title)
        # Rule 5: already rendered from the cached missing list, or already
        # owned. Either way it must not appear twice.
        if not key or key in seen:
            continue

        release_date = str(row.get("release_date") or "").strip()
        if not release_date:
            # A year-only row still classifies, but a genuinely undated row
            # (rule 4) does not.
            release_date = str(row.get("release_year") or "").strip()
        if not is_upcoming_date(release_date, today=today):
            continue

        seen.add(key)
        entries.append({
            "album": title,
            "title": title,
            "album_year": _year_of(release_date) or _as_int(row.get("release_year")),
            "track_count": 0,
            "avg_stars": None,
            "total_duration": 0,
            "is_missing": True,
            "is_upcoming": True,
            "first_release_date": release_date,
            # The upcoming pipeline stores no art, and inventing a URL here
            # would request a file that does not exist.
            "cover_art_url": "",
            "release_id": str(row.get("release_group_mbid") or ""),
            "_category": UPCOMING_KEY,
        })

    return entries
