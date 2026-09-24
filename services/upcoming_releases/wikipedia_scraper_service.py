"""Wikipedia album release scraper service.

Scrapes upcoming album releases from multiple Wikipedia pages using
BeautifulSoup for robust HTML table parsing. Supports multiple genres
and regions, with collection filtering and MusicBrainz validation.

Key Functions:
    - WikipediaReleaseScraper: Main scraper class with multi-source support.
    - scrape(): Convenience wrapper for one-shot scraping.

Architecture:
    Uses ``WikipediaClient`` from ``api_clients.wikipedia`` for HTTP requests.
    Delegates persistence to ``db.repositories.upcoming_releases``.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text

from api_clients.wikipedia import WikipediaClient
from db.engine import db_session

logger = logging.getLogger(__name__)

# Default Wikipedia sources for upcoming releases.
# "List_of_upcoming_albums" was deleted — the "List of <year> albums" pages
# (month-by-month release tables) are the canonical source, matching the
# legacy scraper. The "{year}_in_music" hub pages contain NO release tables
# (they only link to the List pages), so they are used as fallbacks only.
_year = datetime.now().year
DEFAULT_WIKIPEDIA_SOURCES: dict[str, dict[str, Any]] = {
    f"{_year}_albums": {
        "url": f"https://en.wikipedia.org/wiki/List_of_{_year}_albums",
        "name": f"{_year} Albums",
        "columns": ["day", "artist", "album", "genre"],
        "fallback_titles": [f"{_year}_in_music"],
    },
    f"{_year}_heavy_metal": {
        "url": f"https://en.wikipedia.org/wiki/{_year}_in_heavy_metal_music",
        "name": f"Heavy Metal {_year}",
        "columns": ["day", "artist", "album"],
        "fallback_titles": [],
    },
    f"{_year}_rock": {
        "url": f"https://en.wikipedia.org/wiki/{_year}_in_rock_music",
        "name": f"Rock Music {_year}",
        "columns": ["day", "artist", "album"],
        "fallback_titles": [],
    },
    f"{_year}_kpop": {
        "url": f"https://en.wikipedia.org/wiki/{_year}_in_South_Korean_music",
        "name": f"K-Pop/Korean Music {_year}",
        "columns": ["day", "album", "artist"],
        "fallback_titles": [],
    },
    f"{_year}_american": {
        "url": f"https://en.wikipedia.org/wiki/{_year}_in_American_music",
        "name": f"American Music {_year}",
        "columns": ["day", "album", "artist"],
        "fallback_titles": [],
    },
    f"{_year + 1}_albums": {
        "url": f"https://en.wikipedia.org/wiki/List_of_{_year + 1}_albums",
        "name": f"{_year + 1} Albums",
        "columns": ["day", "artist", "album", "genre"],
        "fallback_titles": [f"{_year + 1}_in_music"],
    },
}

# Month name -> number mapping
_MONTHS: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# Genre-like cell detection keywords
_GENRE_KEYWORDS: frozenset[str] = frozenset({
    "metal", "rock", "pop", "hip-hop", "hip hop", "punk", "jazz",
    "blues", "country", "folk", "electronic", "dance", "soul", "funk",
    "alternative", "indie", "ambient", "experimental", "classical",
    "r&b", "rnb", "reggae", "latin", "world", "new age",
})


class WikipediaReleaseScraper:
    """Scrapes upcoming album releases from Wikipedia pages."""

    def __init__(self, sources: dict[str, dict[str, Any]] | None = None):
        self.sources = sources or self._load_configured_sources()
        self.http = WikipediaClient()

    def _load_configured_sources(self) -> dict[str, dict[str, Any]]:
        """Load sources from config (``upcoming_releases.sources``).

        Mirrors the legacy scraper: config entries win, defaults are the
        fallback when nothing is configured. Sources disabled in the config
        page (``enabled: false``) are skipped.
        """
        try:
            from helpers.config_helpers import get_upcoming_releases_sources
            loaded: dict[str, dict[str, Any]] = {}
            for s in get_upcoming_releases_sources() or []:
                if not isinstance(s, dict):
                    continue
                if s.get("enabled", True) is False:
                    continue
                key = str(s.get("key", "")).strip()
                url = str(s.get("url", "")).strip()
                if not key or not url:
                    continue
                cols = s.get("columns")
                if isinstance(cols, str):
                    cols = [c.strip() for c in cols.split(",") if c.strip()]
                if not isinstance(cols, list) or not cols:
                    cols = ["day", "artist", "album"]
                fallback_titles = s.get("fallback_titles") or []
                if isinstance(fallback_titles, str):
                    fallback_titles = [fallback_titles]
                loaded[key] = {
                    "url": url,
                    "name": str(s.get("name", key)),
                    "columns": list(cols),
                    "fallback_titles": list(fallback_titles),
                }
            if loaded:
                logger.debug("[SCRAPER] Loaded %d sources from config", len(loaded))
                return loaded
        except Exception as exc:
            logger.warning("[SCRAPER] Could not load configured sources: %s", exc)
        return DEFAULT_WIKIPEDIA_SOURCES

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scrape_all(self) -> dict[str, Any]:
        """Scrape all configured Wikipedia sources.

        Returns:
            Dict with per-source results and aggregate counts.
        """
        # Rolling import window: only releases dated within the last N months
        # and the next M months are kept (undated/TBA rows are always kept —
        # they are genuinely unscheduled).  Prevents out-of-window rows (e.g.
        # the January block when the current month is August) from entering
        # the database at all.
        window = get_release_window()
        # Self-heal names scraped before the sanitization pass existed.
        _sanitize_stored_rows()
        results: dict[str, Any] = {
            "total_found": 0,
            "total_new": 0,
            "total_updated": 0,
            "sources": {},
        }

        for src_key, src_info in self.sources.items():
            try:
                items = self._scrape_source(src_key, src_info)
            except Exception as exc:
                logger.error("[SCRAPER] Failed to scrape %s: %s", src_info["name"], exc)
                results["sources"][src_key] = {
                    "name": src_info["name"],
                    "error": str(exc),
                    "items_found": 0,
                }
                continue

            items = [
                it for it in items
                if _within_release_window(it, window)
            ]

            new, updated = self._persist_releases(items, src_info["name"], src_key)

            results["total_found"] += len(items)
            results["total_new"] += new
            results["total_updated"] += updated
            results["sources"][src_key] = {
                "name": src_info["name"],
                "items_found": len(items),
                "items_new": new,
                "items_updated": updated,
            }

        return results

    # ------------------------------------------------------------------
    # Source scraping
    # ------------------------------------------------------------------

    def _scrape_source(self, src_key: str, src_info: dict[str, Any]) -> list[dict[str, Any]]:
        """Scrape a single Wikipedia source page.

        Returns:
            List of release dicts with artist_name, album_name, release_date, etc.
        """
        url = src_info["url"]
        name = src_info["name"]
        column_order = src_info.get("columns", ["day", "artist", "album"])
        year = self._extract_year(src_key)
        logger.debug("[SCRAPER] Scraping %s (year=%s)", name, year)

        # Fetch page HTML via Wikipedia API, trying fallback pages if needed
        page_titles_to_try = [self._url_to_page_title(url)]
        fallback_titles = src_info.get("fallback_titles", [])
        for ft in fallback_titles:
            if ft not in page_titles_to_try:
                page_titles_to_try.append(ft)

        parse_data = None
        used_title = None
        for pt in page_titles_to_try:
            parse_data = self.http.parse_page(pt, prop="text")
            if parse_data:
                used_title = pt
                logger.debug("[SCRAPER] Found content on page: %s", pt)
                break
            logger.debug("[SCRAPER] Page not found, trying fallback: %s", pt)

        if not parse_data:
            logger.warning("[SCRAPER] No parse data for %s (tried %d titles)", name, len(page_titles_to_try))
            return []

        raw_html = parse_data.get("text", {}).get("*", "")
        if not raw_html:
            logger.warning("[SCRAPER] No HTML content for %s (title=%s)", name, used_title)
            return []

        return self._parse_html_tables(raw_html, src_key, name, year, column_order)

    # ------------------------------------------------------------------
    # HTML table parsing
    # ------------------------------------------------------------------

    def _parse_html_tables(
        self,
        html: str,
        src_key: str,
        src_name: str,
        year: int,
        column_order: list[str],
    ) -> list[dict[str, Any]]:
        """Parse Wikipedia HTML tables to extract release data."""
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            logger.warning("[SCRAPER] BeautifulSoup not installed, falling back to regex")
            return self._parse_regex_fallback(html, year, column_order)

        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table", class_="wikitable")
        releases: list[dict[str, Any]] = []
        current_month = 1
        last_seen_day: int | None = None

        for table in tables:
            # Detect month from preceding heading; skip tables that are not
            # under a month heading (award ceremonies, charts, etc.).
            current_month, last_seen_day, found = self._detect_month(table, current_month, last_seen_day)
            if not found:
                continue

            rows = table.find_all("tr")
            if not rows:
                continue

            # Skip header rows
            data_rows = self._skip_header_rows(rows)
            # Reconstruct with rowspan/colspan handling
            reconstructed = self._reconstruct_rows(data_rows)

            # Whether this table actually carries a day column.  Date tables
            # may contain TBA rows whose first cell is not date-like, while
            # TBA/Unscheduled tables have no day column at all — deciding once
            # per table keeps the column mapping aligned for both.
            table_has_day = self._table_has_day_column(reconstructed)

            for cells in reconstructed:
                if len(cells) < 2:
                    continue
                release = self._parse_row(
                    cells, column_order, year, current_month, last_seen_day,
                    table_has_day=table_has_day,
                )
                if release:
                    releases.append(release)
                    if release.get("_had_date"):
                        try:
                            day = int(release["release_date"].split("-")[2])
                            last_seen_day = day
                        except (ValueError, IndexError):
                            pass

        logger.info("[SCRAPER] Parsed %s releases from %s", len(releases), src_name)
        return releases

    def _detect_month(
        self, table: Any, current_month: int, last_seen_day: int | None
    ) -> tuple[int, int | None, bool]:
        """Walk backwards from a table to find the month heading.

        Returns ``(month, last_seen_day, found)`` — ``found`` is False when no
        month heading precedes the table, which marks it as a non-release
        table (award ceremonies, charts, references, etc.).
        """
        try:
            prev = table.find_previous()
            depth = 0
            while prev and depth < 50:
                text = prev.get_text(strip=True).lower()
                for mname, mnum in _MONTHS.items():
                    if mname in text and len(text) < 100:
                        logger.debug("[SCRAPER] Month heading: %s -> %s", prev.get_text(strip=True), mnum)
                        # Extract day if present (e.g. "January 9")
                        day_match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", text)
                        if day_match:
                            d = int(day_match.group(1))
                            if 1 <= d <= 31:
                                last_seen_day = d
                        return mnum, last_seen_day, True
                if prev.name == "table":
                    break
                prev = prev.find_previous()
                depth += 1
        except Exception:
            pass
        return current_month, last_seen_day, False

    @staticmethod
    def _skip_header_rows(rows: list[Any]) -> list[Any]:
        """Skip leading navigation/header rows.

        Walks the first few rows and skips:
          - wide single-cell navigation rows (the "Go to:" jump bar on the
            List of <year> albums pages) that precede the real header;
          - header rows made up mostly of ``<th>`` cells;
          - ``<td>``-based header rows (rare) detected by cell contents.

        Stops at the first row that looks like data.
        """
        start = 0
        for i, row in enumerate(rows[:6]):
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            texts = [c.get_text(strip=True) for c in cells]
            th_count = sum(1 for c in cells if c.name == "th")
            td_count = sum(1 for c in cells if c.name == "td")

            # Single-cell navigation row (e.g. "Go to: February | March | ...")
            if len(cells) == 1:
                colspan = cells[0].get("colspan")
                try:
                    colspan = int(colspan) if colspan else 1
                except (TypeError, ValueError):
                    colspan = 1
                if colspan > 1:
                    start = i + 1
                    continue

            if th_count >= 3 and th_count > td_count:
                start = i + 1
                continue

            # TD-rendered header row (rare but present on some pages).
            if texts and WikipediaReleaseScraper._is_header_row(texts):
                start = i + 1
                continue

            break
        return rows[start:]

    @staticmethod
    def _reconstruct_rows(rows: list[Any]) -> list[list[str]]:
        """Reconstruct rows handling rowspan/colspan properly."""
        reconstructed: list[list[str]] = []
        col_tracking: dict[int, tuple[str, int]] = {}

        for row in rows:
            cells = row.find_all(["td", "th"])
            row_cells: list[str] = []
            col_idx = 0

            for cell in cells:
                # Fill in tracked rowspan cells
                while col_idx in col_tracking and col_tracking[col_idx][1] > 0:
                    row_cells.append(col_tracking[col_idx][0])
                    col_tracking[col_idx] = (
                        col_tracking[col_idx][0],
                        col_tracking[col_idx][1] - 1,
                    )
                    col_idx += 1

                value = cell.get_text(strip=True)
                row_cells.append(value)
                rowspan = int(cell.get("rowspan", 1))
                colspan = int(cell.get("colspan", 1))

                if rowspan > 1:
                    col_tracking[col_idx] = (value, rowspan - 1)
                col_idx += colspan

            # Fill remaining tracked cells
            while col_idx in col_tracking and col_tracking[col_idx][1] > 0:
                row_cells.append(col_tracking[col_idx][0])
                col_tracking[col_idx] = (
                    col_tracking[col_idx][0],
                    col_tracking[col_idx][1] - 1,
                )
                col_idx += 1

            if row_cells:
                reconstructed.append(row_cells)

        return reconstructed

    @staticmethod
    def _table_has_day_column(rows: list[list[str]]) -> bool:
        """True when the (already reconstructed) table rows carry a day column.

        Scans the first few rows: if any first cell looks like a date, the
        table is a date table (TBA rows are still handled — they simply get a
        NULL release date).  Used so that TBA/Unscheduled tables (no day
        column) are not misread as date tables.
        """
        for row in rows[:5]:
            if row and WikipediaReleaseScraper._looks_like_date_cell(row[0]):
                return True
        return False

    def _parse_row(
        self,
        cells: list[str],
        column_order: list[str],
        year: int,
        current_month: int,
        last_seen_day: int | None,
        table_has_day: bool = True,
    ) -> dict[str, Any] | None:
        """Parse a single row of cells into a release dict."""
        # Clean citation brackets (e.g. "[58]") and interlanguage markers
        # (e.g. a trailing "[ko]"), then drop trailing empty cells (an unused
        # genre/label column).
        cells = [self._clean_cell_text(c) for c in cells]
        while cells and not cells[-1]:
            cells.pop()
        if len(cells) < 2:
            return None

        # Skip header rows (e.g. ["Release date", "Artist", ...]) that were
        # rendered with <td> cells and escaped the structural header skip.
        if self._is_header_row(cells):
            return None

        # Detect if first cell is a date
        first = cells[0]
        has_date = self._looks_like_date_cell(first)

        # Map columns to values.  When the row has no day column (TBA/
        # Unscheduled tables) the 'day' slot is dropped and the remaining
        # columns shift left.
        actual_cols = column_order if (has_date or table_has_day) else [c for c in column_order if c != "day"]

        # Map columns to values
        values: dict[str, str] = {}
        ci = 0
        for col_type in actual_cols:
            if ci >= len(cells):
                break
            val = cells[ci].strip()
            if col_type == "day":
                # Always consume the day cell, even a single digit ("1".."9") —
                # previously the len<2 junk filter dropped these and every row
                # collapsed onto 2026-01-01.
                values["day"] = val
                ci += 1
                continue
            if not val:
                ci += 1
                continue
            if col_type == "genre":
                ci += 1
                continue
            if val.upper() in ("TBA", "TBD", "TBR") or self._is_genre(val):
                # Junk/skip cell (a TBA day with a shifted row, or a genre
                # leak) — drop it so the following columns stay aligned.
                ci += 1
                continue
            values[col_type] = val
            ci += 1

        artist = values.get("artist")
        album = values.get("album")
        if not artist or not album:
            return None

        # Parse day; if the cell also carries a month name (e.g. "January 9"
        # or "January9"), prefer it over the heading-detected month.
        day_str = values.get("day")
        # TBA/TBD/TBR (or a missing day cell) means no valid date — keep the
        # row but persist release_date as NULL, so the UI shows "TBA" and the
        # stale-release GC never treats it as a released album.
        if not has_date or (day_str or "").strip().upper() in ("TBA", "TBD", "TBR"):
            return {
                "artist_name": artist.strip(),
                "album_name": album.strip(),
                "release_date": None,
                "release_year": year,
                "_had_date": False,
            }

        day = self._parse_day(day_str, last_seen_day)
        if day is None:
            day = 1
        row_month = self._month_in_cell(day_str) or current_month

        # Build date string
        try:
            release_dt = datetime(year, row_month, day)
        except ValueError:
            day = 1
            try:
                release_dt = datetime(year, row_month, day)
            except ValueError:
                return None

        return {
            "artist_name": artist.strip(),
            "album_name": album.strip(),
            "release_date": release_dt.strftime("%Y-%m-%d"),
            "release_year": year,
            "_had_date": True,
        }

    # ------------------------------------------------------------------
    # Fallback regex parser
    # ------------------------------------------------------------------

    def _parse_regex_fallback(
        self, html: str, year: int, column_order: list[str]
    ) -> list[dict[str, Any]]:
        """Regex-based table row parsing when BeautifulSoup is unavailable.
        
        Respects column_order mapping instead of hardcoded positions.
        """
        releases: list[dict[str, Any]] = []
        # Match rows with at least 2 cells
        for match in re.finditer(
            r"<tr>(.*?)</tr>",
            html, re.DOTALL,
        ):
            row_html = match.group(1)
            # Extract all cell contents
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.DOTALL)
            # Clean HTML tags and citation brackets
            cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
            cells = [self._clean_cell_text(c) for c in cells]

            if len(cells) < 2:
                continue

            # Skip header rows (e.g. ["Release date", "Artist", ...])
            if self._is_header_row(cells):
                continue

            # Check if first cell is a date
            has_date = self._looks_like_date_cell(cells[0]) if cells else False
            actual_cols = column_order.copy()
            if "day" in actual_cols and not has_date:
                actual_cols.remove("day")
            
            # Map cells to column types
            values = {}
            for idx, col_type in enumerate(actual_cols):
                if idx < len(cells):
                    values[col_type] = cells[idx]
            
            # Extract artist, album, and date
            artist = values.get("artist", "")
            album = values.get("album", "")
            
            if not artist or not album or len(artist) > 200:
                continue
            
            # Parse date if present (TBA/TBD/TBR day cells => unknown date)
            day_text = (values.get("day") or "").strip().upper()
            if "day" in values and day_text not in ("TBA", "TBD", "TBR"):
                day = self._parse_day(values["day"], None)
                month = self._month_in_cell(values["day"])
                if month is None:
                    # No month context in the regex path (there is no heading
                    # detection here) — never fabricate January: treat the row
                    # as TBA so it cannot collapse every month into "January".
                    date_str = None
                    had_date = False
                elif day:
                    date_str = f"{year}-{month:02d}-{day:02d}"
                    had_date = True
                else:
                    date_str = None
                    had_date = False
            else:
                date_str = None
                had_date = False

            releases.append({
                "artist_name": artist,
                "album_name": album,
                "release_date": date_str,
                "release_year": year,
                "_had_date": had_date,
            })
        
        return releases

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _url_to_page_title(url: str) -> str:
        """Extract Wikipedia page title from URL."""
        if "en.wikipedia.org/wiki/" in url:
            title = url.split("wiki/")[-1].split("#")[0]
            return title
        return "List_of_upcoming_albums"

    @staticmethod
    def _extract_year(src_key: str) -> int:
        """Extract year from source key (e.g. '2026_albums' -> 2026)."""
        match = re.search(r"(20[2-9]\d)", src_key)
        return int(match.group(1)) if match else datetime.now().year

    @staticmethod
    def _clean_cell_text(text: str) -> str:
        """Normalize a table cell: strip citation/interlanguage brackets."""
        if not text:
            return ""
        # Citation brackets like "[58]" (optionally glued to the previous word).
        text = re.sub(r"\s*\[\d+\]\s*", " ", text)
        # Trailing interlanguage marker (e.g. "Shin Soo-hyun[ko]").
        text = re.sub(r"\s*\[\s*[a-z]{2,3}\s*\]\s*$", "", text)
        return re.sub(r"\s+", " ", text).strip()

    _HEADER_FIRST_LABELS: frozenset[str] = frozenset({
        "release date", "day", "date", "artist",
    })
    _HEADER_CELL_LABELS: frozenset[str] = frozenset({
        "release date", "day", "date", "artist", "artist(s)",
        "album", "genre", "label", "ref", "reference",
    })

    @classmethod
    def _is_header_row(cls, cells: list[str]) -> bool:
        """True when a row consists mostly of generic column labels.

        Catches header rows that are rendered with ``<td>`` cells (so the
        structural TH-based skip in ``_skip_header_rows`` misses them).
        The first cell must be a first-column label (e.g. "Release date") so a
        real data row like ``["January 9", "Artist", "Album"]`` is not
        mistaken for a header.
        """
        low = [str(c).strip().lower() for c in cells if str(c).strip()]
        if len(low) < 2:
            return False
        if low[0] not in cls._HEADER_FIRST_LABELS:
            return False
        hits = sum(1 for c in low if c in cls._HEADER_CELL_LABELS)
        return hits >= 2

    @staticmethod
    def _parse_day(day_str: str | None, last_seen: int | None) -> int | None:
        """Parse day number from a string."""
        if not day_str:
            return last_seen
        match = re.search(r"(\d{1,2})(?:st|nd|rd|th)?", day_str)
        if match:
            d = int(match.group(1))
            if 1 <= d <= 31:
                return d
        return last_seen

    _MONTH_NAME_RE = re.compile(
        r"(january|february|march|april|may|june|july|august|"
        r"september|october|november|december)",
        re.IGNORECASE,
    )
    _BARE_DAY_RE = re.compile(r"\d{1,2}(?:st|nd|rd|th)?\.?")

    @classmethod
    def _looks_like_date_cell(cls, text: str) -> bool:
        """True when a cell is a release date: a month name + day, or a bare day number."""
        if not text:
            return False
        if cls._MONTH_NAME_RE.search(text):
            return True
        return bool(cls._BARE_DAY_RE.fullmatch(text.strip()))

    @classmethod
    def _month_in_cell(cls, text: str) -> int | None:
        """Extract a month number from a date cell (e.g. 'January 9' -> 1)."""
        m = cls._MONTH_NAME_RE.search(text or "")
        if m:
            return _MONTHS[m.group(1).lower()]
        return None

    @staticmethod
    def _is_genre(val: str) -> bool:
        """Detect if a cell value looks like genre metadata instead of artist/album."""
        if "," in val:
            parts = [p.strip().lower() for p in val.split(",")]
            if len(parts) >= 2:
                return any(
                    any(kw in p for kw in _GENRE_KEYWORDS)
                    for p in parts[:2]
                )
        return False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _persist_releases(
        releases: list[dict[str, Any]], source_name: str, source_key: str | None = None
    ) -> tuple[int, int]:
        """Save scraped releases to the database.

        Precedence rule (per-album identity ``(artist_name, album_name)``):
        a Wikipedia-scraped row never clobbers an existing MusicBrainz row
        (MBID + authoritative metadata win) — it only refreshes
        ``last_seen_at``.  Against an existing Wikipedia row the new date/year
        are applied, falling back to the stored values when the scrape found
        no valid date (TBA).

        ``source_key`` records the exact scraper rule (e.g. ``2026_kpop``) that
        produced each row so the UI can render a per-rule source badge and
        filter by it.

        Returns:
            Tuple of (new_count, updated_count).
        """
        new_count = 0
        updated_count = 0

        if not releases:
            return 0, 0

        with db_session() as session:
            for release in releases:
                # Clean Wikipedia scraping artifacts before storing so the
                # DISPLAYED name and the MusicBrainz lookup both see the
                # sanitized form (e.g. "AkinmusireandMary" → "Akinmusire and Mary").
                from services.upcoming_releases.matching_service import sanitize_wiki_entry
                artist, album = sanitize_wiki_entry(
                    release["artist_name"], release["album_name"]
                )
                rel_date = release.get("release_date")
                rel_year = release.get("release_year")

                try:
                    # Case/punctuation-insensitive dedupe: "Tanzneid" and
                    # "TANZNEID" are the same release — merge into the existing
                    # row instead of creating a duplicate (the unique index is
                    # exact-match only).
                    dup = session.execute(
                        text("""
                            SELECT id FROM upcoming_releases
                            WHERE LOWER(REGEXP_REPLACE(artist_name, '[^a-zA-Z0-9]', '', 'g'))
                                  = LOWER(REGEXP_REPLACE(:artist, '[^a-zA-Z0-9]', '', 'g'))
                              AND LOWER(REGEXP_REPLACE(album_name, '[^a-zA-Z0-9]', '', 'g'))
                                  = LOWER(REGEXP_REPLACE(:album, '[^a-zA-Z0-9]', '', 'g'))
                            LIMIT 1
                        """),
                        {"artist": artist, "album": album},
                    ).fetchone()
                    if dup:
                        # Same precedence as the ON CONFLICT branch below.
                        session.execute(
                            text("""
                                UPDATE upcoming_releases SET
                                    last_seen_at = CURRENT_TIMESTAMP,
                                    source = CASE
                                        WHEN upcoming_releases.source = 'MusicBrainz Daily Collection'
                                            THEN upcoming_releases.source
                                        ELSE :source
                                    END,
                                    source_key = CASE
                                        WHEN upcoming_releases.source = 'MusicBrainz Daily Collection'
                                            THEN upcoming_releases.source_key
                                        ELSE :source_key
                                    END,
                                    release_date = CASE
                                        WHEN upcoming_releases.source = 'MusicBrainz Daily Collection'
                                            THEN upcoming_releases.release_date
                                        ELSE COALESCE(:date, upcoming_releases.release_date)
                                    END,
                                    release_year = CASE
                                        WHEN upcoming_releases.source = 'MusicBrainz Daily Collection'
                                            THEN upcoming_releases.release_year
                                        ELSE COALESCE(:year, upcoming_releases.release_year)
                                    END,
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE id = :id
                            """),
                            {"id": dup[0], "source": source_name, "source_key": source_key,
                             "date": rel_date, "year": rel_year},
                        )
                        updated_count += 1
                        continue

                    result = session.execute(
                        text("""
                            INSERT INTO upcoming_releases
                                (artist_name, album_name, source, source_key, release_date,
                                 release_year, status, last_seen_at, updated_at)
                            VALUES (:artist, :album, :source, :source_key, :date,
                                    :year, 'discovered', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                            ON CONFLICT (artist_name, album_name) DO UPDATE SET
                                last_seen_at = CURRENT_TIMESTAMP,
                                source = CASE
                                    WHEN upcoming_releases.source = 'MusicBrainz Daily Collection'
                                        THEN upcoming_releases.source
                                    ELSE EXCLUDED.source
                                END,
                                source_key = CASE
                                    WHEN upcoming_releases.source = 'MusicBrainz Daily Collection'
                                        THEN upcoming_releases.source_key
                                    ELSE EXCLUDED.source_key
                                END,
                                release_date = CASE
                                    WHEN upcoming_releases.source = 'MusicBrainz Daily Collection'
                                        THEN upcoming_releases.release_date
                                    ELSE COALESCE(EXCLUDED.release_date, upcoming_releases.release_date)
                                END,
                                release_year = CASE
                                    WHEN upcoming_releases.source = 'MusicBrainz Daily Collection'
                                        THEN upcoming_releases.release_year
                                    ELSE COALESCE(EXCLUDED.release_year, upcoming_releases.release_year)
                                END,
                                updated_at = CURRENT_TIMESTAMP
                        """),
                        {
                            "artist": artist,
                            "album": album,
                            "date": rel_date,
                            "year": rel_year,
                            "source": source_name,
                            "source_key": source_key,
                        },
                    )
                    if result.rowcount == 1:
                        new_count += 1
                    else:
                        updated_count += 1
                except Exception as exc:
                    logger.debug("[SCRAPER] Upsert failed for %s - %s: %s", artist, album, exc)

        logger.info(
            "[SCRAPER] %s: %s new, %s updated",
            source_name, new_count, updated_count,
        )
        return new_count, updated_count


def _sanitize_stored_rows() -> int:
    """Self-heal pass: re-sanitize artist/album names stored before the
    sanitization existed (e.g. "Ann Wilsonand Tripsitter" →
    "Ann Wilson and Tripsitter").

    Fixes the name in place when the cleaned key is free; deletes the
    malformed row when a clean duplicate already exists (the unique
    (artist_name, album_name) index prevents the UPDATE).
    """
    from services.upcoming_releases.matching_service import sanitize_wiki_entry

    fixed = 0
    try:
        with db_session() as session:
            rows = session.execute(
                text("SELECT id, artist_name, album_name FROM upcoming_releases")
            ).fetchall()
            for row in rows:
                row_id = row[0]
                artist, album = sanitize_wiki_entry(row[1] or "", row[2] or "")
                if artist == (row[1] or "") and album == (row[2] or ""):
                    continue
                # Use a savepoint per row: a unique-constraint violation on
                # the UPDATE aborts the whole Postgres transaction, which
                # would make the follow-up DELETE fail with
                # "current transaction is aborted" and roll back the entire
                # sanitize pass.  ``begin_nested`` scopes the failure to the
                # row so the DELETE (and the rest of the pass) still works.
                try:
                    with session.begin_nested():
                        session.execute(
                            text("UPDATE upcoming_releases SET artist_name = :a, album_name = :b WHERE id = :id"),
                            {"a": artist, "b": album, "id": row_id},
                        )
                except Exception:
                    # Clean duplicate exists → drop the malformed row.  The
                    # savepoint already rolled back the failed UPDATE, so the
                    # transaction is still usable.
                    try:
                        with session.begin_nested():
                            session.execute(
                                text("DELETE FROM upcoming_releases WHERE id = :id"),
                                {"id": row_id},
                            )
                    except Exception as exc:
                        logger.debug("[SCRAPER] Sanitize delete failed for row %s: %s", row_id, exc)
                        continue
                fixed += 1
    except Exception as exc:
        logger.warning("[SCRAPER] Stored-row sanitize pass failed: %s", exc)
    if fixed:
        logger.info("[SCRAPER] Sanitized %s stored release name(s)", fixed)
    return fixed


# ------------------------------------------------------------------
# Convenience wrapper
# ------------------------------------------------------------------

def scrape(
    sources: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Scrape all configured Wikipedia sources.

    Args:
        sources: Optional dict of source configurations. Falls back to
                 DEFAULT_WIKIPEDIA_SOURCES.

    Returns:
        Results dict with per-source details.
    """
    scraper = WikipediaReleaseScraper(sources=sources)
    return scraper.scrape_all()


def _window_days(
    feature_key: str,
    legacy_months_key: str,
    default_days: int,
) -> int:
    """Resolve one side of the release window, in DAYS.

    ⚠️ The default here IS the behaviour. It used to be
    ``30 * lookback_months`` with ``lookback_months`` defaulting to **2**, so on
    2026-09-24 the window opened on **2026-07-26** — the reported "still showing
    July". The intent is 4 weeks back / 2 weeks forward, so the defaults are
    **28 and 14 days**.

    ⚠️ The old keys (``upcoming_releases_lookback_months`` /
    ``upcoming_releases_lookahead_months``) were on NEITHER Config page and in
    NEITHER ``config.js`` collector, so the window could not be corrected from
    the UI at all. They are still honoured as a fallback so a hand-edited
    ``config.yaml`` keeps working, but the DAYS key wins.

    ⚠️ ``0`` is a legitimate value (e.g. "show nothing future-dated"), so the
    lookup must not be truthiness-tested — ``value or default`` would silently
    replace it. Same trap this codebase has hit with ``or`` before.
    """
    try:
        from helpers.config_helpers import get_feature

        raw = get_feature(feature_key, None)
        if raw is not None:
            return max(0, int(raw))

        legacy = get_feature(legacy_months_key, None)
        if legacy is not None:
            return max(0, 30 * int(legacy))
    except (TypeError, ValueError):
        # A non-numeric setting is a config error, not a reason to break the
        # page — fall through to the default.
        pass
    except Exception:
        # A config read that raises (missing file, unreadable) must not take
        # the whole Upcoming Releases view down with it.
        pass
    return default_days


def get_release_window() -> tuple[datetime, datetime]:
    """Rolling window for upcoming-release imports/display.

    Releases are kept when dated within the last ``lookback_days`` and the next
    ``lookahead_days`` from today — **default 28 / 14 days**, i.e. the last four
    weeks and the next two weeks, centred on today.

    Tunable from the Config page (``features.upcoming_releases_lookback_days`` /
    ``features.upcoming_releases_lookahead_days``).

    ⚠️ This window is what the DASHBOARD table is filtered by. Its
    ``loadUpcomingReleasesTable()`` sends no ``window`` parameter, so the
    separate "tight" clause in ``upcoming_releases_routes`` (``window_days > 0``)
    never applies — this function alone governs what the dashboard shows.
    """
    lookback_days = _window_days(
        "upcoming_releases_lookback_days",
        "upcoming_releases_lookback_months",
        28,
    )
    lookahead_days = _window_days(
        "upcoming_releases_lookahead_days",
        "upcoming_releases_lookahead_months",
        14,
    )
    now = datetime.now()
    return now - timedelta(days=lookback_days), now + timedelta(days=lookahead_days)


def _within_release_window(
    release: dict[str, Any],
    window: tuple[datetime, datetime] | None = None,
) -> bool:
    """True when a release falls inside the rolling import window.

    Dated releases outside ``[now - lookback, now + lookahead]`` are dropped
    at import time.  Undated (TBA) rows are always kept — they are genuinely
    unscheduled releases with no date to evaluate.
    """
    raw = str(release.get("release_date") or "").strip()
    if not raw:
        return True
    try:
        rel = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return True
    if window is None:
        window = get_release_window()
    start, end = window
    return start <= rel <= end


def purge_stale_upcoming_releases(days: int | None = None) -> dict[str, Any]:
    """Delete releases whose date has passed and were never acted on.

    Keeps rows the user explicitly engaged with: ``bookmarked``, ``queued``
    and ``imported``.  Runs in the daily maintenance cycle (scheduler) and
    reports the number of rows removed so callers can log it.

    The ``days`` window defaults to ``features.upcoming_releases_purge_days``
    (config page), falling back to 30 days.

    ⚠️ The purge is FLOORED at the display lookback
    (``get_release_window()``). Purge window and display window are independent
    settings, so ``upcoming_releases_purge_days: 7`` alongside a 90-day lookback
    would delete rows the dashboard is still trying to show — the table would
    silently empty from the left as the scheduler ran. Defaults (30 vs 28) do
    not overlap, but the configuration can, so the floor is enforced here rather
    than relying on the defaults agreeing.

    Returns:
        ``{"deleted": int, "days": int}`` (or the exception message when the
        table is unavailable / the query fails structurally).
    """
    try:
        if days is None:
            try:
                from helpers.config_helpers import get_feature
                days = int(get_feature("upcoming_releases_purge_days", 30) or 30)
            except Exception:
                days = 30

        # Never purge inside the window the UI displays.
        try:
            _display_start, _display_end = get_release_window()
            _display_lookback_days = max(
                0, (datetime.now().date() - _display_start.date()).days
            )
            if days < _display_lookback_days:
                # ⚠️ This module uses stdlib logging, NOT structlog — kwargs
                # style raises TypeError: Logger._log() got an unexpected
                # keyword argument, and the `except Exception` below would
                # swallow it, silently disabling the floor.
                logger.info(
                    "Upcoming purge window raised to match the display lookback "
                    "(configured=%s effective=%s)",
                    days,
                    _display_lookback_days,
                )
                days = _display_lookback_days
        except Exception:
            # A failure computing the display window must not block the purge;
            # the configured value is still applied.
            pass
        cutoff = (datetime.now().date() - timedelta(days=max(1, days))).isoformat()
        with db_session() as session:
            result = session.execute(
                text("""
                    DELETE FROM upcoming_releases
                    WHERE release_date IS NOT NULL
                      AND release_date < :cutoff
                      AND status NOT IN ('bookmarked', 'queued', 'imported')
                """),
                {"cutoff": cutoff},
            )
        deleted = result.rowcount or 0
        if deleted:
            logger.info("[SCRAPER] Purged %s stale upcoming releases (older than %s)", deleted, cutoff)
        return {"deleted": deleted, "days": days}
    except Exception as exc:
        logger.debug("[SCRAPER] Stale purge skipped: %s", exc)
        return {"deleted": 0, "days": days, "error": str(exc)}
