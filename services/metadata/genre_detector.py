"""
Genre detection service.

Detects special genre tags (Christmas, Cover, Live, Acoustic, Orchestral,
Instrumental, Remix) based on track metadata, audio features, and
title/album analysis.
"""

from __future__ import annotations

import json
import re
from typing import Any


class GenreDetector:
    """Detects special genre tags for tracks based on metadata and audio features."""

    # ── Keyword sets ──────────────────────────────────────────────────────

    CHRISTMAS_KEYWORDS = frozenset({
        "christmas", "xmas", "holiday", "noel", "santa", "sleigh",
        "jingle", "silent night", "holy night", "winter wonderland",
        "deck the halls", "carol", "advent",
    })

    COVER_KEYWORDS_TITLE = frozenset({
        "(cover)", "(tribute)", "(originally by)", "cover version",
        "tribute to", "in the style of",
    })

    COVER_KEYWORDS_ALBUM = frozenset({
        "tribute", "covers", "tribute to", "covering", "in the style",
    })

    # NOTE: a bare " live " (space-padded) was removed from this set. It is
    # not a reliable live-performance signal mid-title — it matched ordinary
    # sentences such as "I Live Alone" and "We Live In Hope". Every genuine
    # form is still covered: bracketed "(live)"/"[live]", the "- Live"
    # suffix, "Live at/from/in", "Live Version", and a trailing "… Live"
    # (see ``_LIVE_TITLE_PATTERNS`` below).
    LIVE_KEYWORDS_TITLE = frozenset({
        "(live)", "[live]", "live at", "live from", "live version",
        "- live", "– live", "— live",
    })

    # Title-level live patterns that need anchoring rather than a substring
    # test. Kept separate from the keyword set so the boundaries are explicit.
    _LIVE_TITLE_PATTERNS = (
        r"[\(\[][^)\]]*\blive\b[^)\]]*[\)\]]",   # "(Live)", "(Recorded Live)"
        r"[-–—]\s*live\b",                        # "Song - Live"
        # "Live at/from/in …" only counts at the START of the title or right
        # after a separator/bracket. Requiring that anchor is what separates
        # "Live In Tokyo" from the ordinary sentence "We Live In Hope", which
        # an unanchored \blive\s+in\b matched.
        r"(?:^|[\(\[]|[-–—]\s*)live\s+(?:at|from|in)\b",
        r"\blive\s+(?:version|session|tour)\b",
        r"\blive\s*$",                            # "Song Live"
    )

    LIVE_KEYWORDS_ALBUM = frozenset({
        "unplugged", "live at", "live from", "in concert",
        "live session", "bbc live", "live in", "live tour", "(live)", "[live]",
    })

    ACOUSTIC_KEYWORDS = frozenset({
        "(acoustic)", "acoustic version", "- acoustic", " acoustic ",
    })

    REMIX_KEYWORDS_TITLE = frozenset({
        "(remix)", " remix", "- remix", "remix version", "remixed", "remix edit",
    })

    REMIX_KEYWORDS_ALBUM = frozenset({
        "remix", "remixes", "remixed", "remix album", "(remix)", "+remix",
    })

    ORCHESTRAL_KEYWORDS = frozenset({
        "orchestral", "symphonic", "symphony", "philharmonic",
        "orchestra", "orchestrated",
    })

    # ── False-positive guards ─────────────────────────────────────────────
    #
    # Several keywords above are substrings of ordinary words, and a bare
    # ``in`` test matches them:
    #
    #   " live "   matched "I Live Alone", "We Live In Hope"
    #   "covers"   matched "Undercovers", "Discovers"
    #   "carol"    matched "Carolina", "Caroline"
    #   "santa"    matched "Santana", "Santa Monica"
    #   " remix"   matched nothing harmful, but "remixed" matched "unremixed"
    #
    # Keywords that are whole words (rather than bracketed or punctuated
    # fragments) are therefore matched on WORD BOUNDARIES. Bracketed forms
    # like "(live)" and punctuated forms like "- live" stay as substring
    # tests, since they are already unambiguous.
    _WORD_BOUNDARY_KEYWORDS = frozenset({
        "carol", "santa", "sleigh", "jingle", "noel", "advent", "holiday",
        "covers", "covering", "tribute",
        "remix", "remixes", "remixed",
        "orchestra", "orchestral", "symphony", "symphonic", "philharmonic",
        "orchestrated",
    })

    @staticmethod
    def _keyword_hit(haystack: str, keyword: str) -> bool:
        """Case-insensitive keyword test with word-boundary guarding.

        ``haystack`` is expected to already be lowercased.
        """
        if not haystack or not keyword:
            return False
        kw = keyword.strip().lower()
        if not kw:
            return False

        if kw in GenreDetector._WORD_BOUNDARY_KEYWORDS:
            return bool(re.search(rf"\b{re.escape(kw)}\b", haystack))

        # A bare single word that is not bracketed/punctuated is still risky
        # as a raw substring ("live" inside "alive"/"delivery"), so guard it
        # too. Multi-word and punctuated keywords are matched literally.
        if kw.isalpha():
            return bool(re.search(rf"\b{re.escape(kw)}\b", haystack))

        return kw in haystack

    # ── Public API ────────────────────────────────────────────────────────

    def detect_special_tags(
        self,
        track_name: str,
        album_name: str,
        artist_genres: list[str] | None = None,
        audio_features: dict | None = None,
        album_type: str | None = None,
    ) -> set[str]:
        """Detect all special genre tags for a track.

        Args:
            track_name: Track title.
            album_name: Album name.
            artist_genres: List of artist genres from Spotify.
            audio_features: Dict of audio features (acousticness, liveness, …).
            album_type: Album type string (may contain ``+live``, ``(remix)``, etc.).

        Returns:
            Set of detected special tags (e.g. ``{"Live", "Acoustic"}``).
        """
        tags: set[str] = set()

        track_lower = (track_name or "").lower()
        album_lower = (album_name or "").lower()
        genres_lower = [g.lower() for g in (artist_genres or [])]

        if self._detect_christmas(track_lower, album_lower, genres_lower):
            tags.add("Christmas")

        if self._detect_cover(track_lower, album_lower):
            tags.add("Cover")

        if self._detect_live(track_lower, album_lower, audio_features, album_type):
            tags.add("Live")

        if self._detect_acoustic(track_lower, audio_features):
            tags.add("Acoustic")

        if self._detect_remix(track_lower, album_lower, album_type):
            tags.add("Remix")

        orchestral, instrumental = self._detect_orchestral_instrumental(
            track_lower, audio_features,
        )
        if orchestral:
            tags.add("Orchestral")
        if instrumental:
            tags.add("Instrumental")

        return tags

    # ── Internal detectors ────────────────────────────────────────────────

    @staticmethod
    def _detect_christmas(track_lower: str, album_lower: str, genres_lower: list[str]) -> bool:
        for kw in GenreDetector.CHRISTMAS_KEYWORDS:
            if GenreDetector._keyword_hit(track_lower, kw) or GenreDetector._keyword_hit(album_lower, kw):
                return True

        for genre in genres_lower:
            if "christmas" in genre or "holiday" in genre:
                return True

        return False

    @staticmethod
    def _detect_cover(track_lower: str, album_lower: str) -> bool:
        for kw in GenreDetector.COVER_KEYWORDS_TITLE:
            if GenreDetector._keyword_hit(track_lower, kw):
                return True

        for kw in GenreDetector.COVER_KEYWORDS_ALBUM:
            if GenreDetector._keyword_hit(album_lower, kw):
                return True

        return False

    @staticmethod
    def _detect_live(
        track_lower: str,
        album_lower: str,
        audio_features: dict | None,
        album_type: str | None,
    ) -> bool:
        if album_type:
            t = album_type.lower()
            if "+live" in t or "(live)" in t:
                return True

        for pat in GenreDetector._LIVE_TITLE_PATTERNS:
            if re.search(pat, track_lower):
                return True

        for kw in GenreDetector.LIVE_KEYWORDS_TITLE:
            if GenreDetector._keyword_hit(track_lower, kw):
                return True

        live_patterns = [
            r"\blive\s+at\b", r"\blive\s+in\b", r"\blive\s+from\b",
            r"\blive\s+session\b", r"\blive\s+tour\b",
            r"\(live\)", r"\[live\]", r"-\s*live\b", r"\s+live\s*$",
            r"\bconcert\b", r"\bin\s+concert\b",
        ]
        for pat in live_patterns:
            if re.search(pat, album_lower):
                return True

        for kw in GenreDetector.LIVE_KEYWORDS_ALBUM:
            if GenreDetector._keyword_hit(album_lower, kw):
                return True

        if audio_features and audio_features.get("liveness", 0) > 0.8:
            return True

        return False

    @staticmethod
    def _detect_acoustic(track_lower: str, audio_features: dict | None) -> bool:
        for kw in GenreDetector.ACOUSTIC_KEYWORDS:
            if GenreDetector._keyword_hit(track_lower, kw):
                return True

        if audio_features and audio_features.get("acousticness", 0) > 0.7:
            return True

        return False

    @staticmethod
    def _detect_remix(
        track_lower: str, album_lower: str, album_type: str | None,
    ) -> bool:
        if album_type:
            t = album_type.lower()
            if "+remix" in t or "(remix)" in t:
                return True

        for kw in GenreDetector.REMIX_KEYWORDS_TITLE:
            if GenreDetector._keyword_hit(track_lower, kw):
                return True

        for kw in GenreDetector.REMIX_KEYWORDS_ALBUM:
            if GenreDetector._keyword_hit(album_lower, kw):
                return True

        return False

    @staticmethod
    def _detect_orchestral_instrumental(
        track_lower: str, audio_features: dict | None,
    ) -> tuple[bool, bool]:
        is_orchestral = False
        is_instrumental = False

        for kw in GenreDetector.ORCHESTRAL_KEYWORDS:
            if GenreDetector._keyword_hit(track_lower, kw):
                is_orchestral = True
                break

        if audio_features:
            instr = audio_features.get("instrumentalness", 0)
            acous = audio_features.get("acousticness", 0)
            if instr > 0.8:
                is_instrumental = True
            if instr > 0.8 and acous > 0.5:
                is_orchestral = True

        return is_orchestral, is_instrumental

    # ── Genre normalization ───────────────────────────────────────────────

    @staticmethod
    def normalize_genres(artist_genres: list[str] | None) -> list[str]:
        """Map raw artist genres to broad categories."""
        if not artist_genres:
            return []

        genre_map: dict[str, tuple[str, ...]] = {
            "rock": ("rock", "alternative", "indie", "grunge", "punk"),
            "metal": ("metal", "metalcore", "death metal", "black metal"),
            "pop": ("pop", "dance pop", "electropop", "synth-pop"),
            "electronic": ("electronic", "edm", "techno", "house", "dubstep", "drum and bass"),
            "hip hop": ("hip hop", "rap", "trap", "hip-hop"),
            "jazz": ("jazz", "bebop", "smooth jazz", "jazz fusion"),
            "classical": ("classical", "baroque", "romantic"),
            "country": ("country", "americana", "bluegrass"),
            "r&b": ("r&b", "soul", "funk", "neo soul"),
            "folk": ("folk", "folk rock", "singer-songwriter"),
        }

        normalized: set[str] = set()
        for genre in artist_genres:
            g = genre.lower()
            for broad, keywords in genre_map.items():
                if any(kw in g for kw in keywords):
                    normalized.add(broad)

        return sorted(normalized)


# =============================================================================
# GENRE / TITLE PROCESSING
# =============================================================================

# Release-form labels that may be appended to a title, in canonical display
# casing. The stored casing matters: this module previously appended a
# LOWERCASE "(live)" while album_stage appended "(Live)", so the same track
# could end up with either depending on which path ran last.
_TITLE_TAG_LABELS: dict[str, str] = {
    "live": "Live",
    "unplugged": "Unplugged",
    "acoustic": "Acoustic",
    "demo": "Demo",
    "remix": "Remix",
}

# Labels that describe the SAME release form. A title already carrying any
# member of a group must not gain another member of that group — e.g.
# "(Unplugged)" already conveys "Acoustic", and "(Recorded Live)" already
# conveys "Live".
_EQUIVALENT_TAG_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"live", "unplugged"}),
    frozenset({"acoustic", "unplugged"}),
)


def _has_parenthetical_tag(title: str, tag: str) -> bool:
    """Check if *title* contains *tag* inside brackets (case-insensitive).

    FIXED: the previous pattern was ``r"\\(" + tag + r"[^)]*\\)"``, which
    anchored the tag to the START of the bracket. "(Recorded Live)" and
    "(Stripped Acoustic)" therefore did not register as already-tagged, and
    ``process_track_genres_and_title`` appended a SECOND suffix:

        "Song (Recorded Live)"     -> "Song (Recorded Live) (live)"
        "Song (Stripped Acoustic)" -> "Song (Stripped Acoustic) (acoustic)"

    The tag is now matched on a word boundary ANYWHERE inside ANY bracketed
    group, and square brackets are recognised as well as parentheses.
    """
    if not title or not tag:
        return False
    return bool(
        re.search(
            rf"[\(\[][^)\]]*\b{re.escape(tag)}\b[^)\]]*[\)\]]",
            title,
            re.IGNORECASE,
        )
    )


def _has_equivalent_tag(title: str, tag: str) -> bool:
    """True when *title* already carries *tag* or an equivalent form label."""
    tag = str(tag or "").strip().lower()
    if not tag:
        return False

    if _has_parenthetical_tag(title, tag):
        return True

    for group in _EQUIVALENT_TAG_GROUPS:
        if tag in group:
            for sibling in group:
                if sibling != tag and _has_parenthetical_tag(title, sibling):
                    return True
    return False


def _genre_list_has(genres: list[str], tag: str) -> bool:
    """True when *tag* already appears in the genre list (case-insensitive)."""
    tag = str(tag or "").strip().lower()
    if not tag:
        return False
    return any(tag == str(g or "").strip().lower() for g in genres)


def process_track_genres_and_title(
    track_title: str,
    album_name: str,
    genre_list: list[str],
) -> tuple[str, list[str]]:
    """Process track title and genres based on metadata hints.

    Rules:
    1. If the title carries ``(Live)`` / ``(Acoustic)`` / ``(Demo)`` /
       ``(Remix)`` / ``(Unplugged)``, add them to the genre list.
    2. If the album name indicates acoustic / unplugged / live, propagate
       that to the genres and — when the title does not already convey it —
       to the title.
    3. If a genre indicates acoustic / live / unplugged, append it to the
       title when the title does not already convey it.

    A tag is only appended when neither it NOR AN EQUIVALENT FORM is already
    present, so "Song (Recorded Live)" is left alone rather than becoming
    "Song (Recorded Live) (live)", and "Song (Unplugged)" does not also gain
    "(Acoustic)".

    Returns:
        Tuple of (updated_title, updated_genre_list).
    """
    updated_title = str(track_title or "")
    updated_genres = list(genre_list or [])

    # ── Step 1: Extract tags from the title and add them to the genres ────
    for tag, label in _TITLE_TAG_LABELS.items():
        if _has_parenthetical_tag(updated_title, tag):
            if not _genre_list_has(updated_genres, label):
                updated_genres.append(label)

    # ── Step 2: Propagate album-level hints to genres and title ───────────
    album_lower = (album_name or "").lower()
    album_hints = (
        ("acoustic", bool(re.search(r"\bacoustic\b", album_lower))),
        ("unplugged", bool(re.search(r"\bunplugged\b", album_lower))),
        # `live` was previously computed and then never used, so a live
        # ALBUM never propagated its label to the track genres at all.
        ("live", bool(re.search(r"\blive\b", album_lower))),
    )

    for tag_name, has_tag in album_hints:
        if not has_tag:
            continue

        label = _TITLE_TAG_LABELS.get(tag_name, tag_name.capitalize())
        if not _genre_list_has(updated_genres, label):
            updated_genres.append(label)

        if not _has_equivalent_tag(updated_title, tag_name):
            updated_title = f"{updated_title} ({label})"

    # ── Step 3: Append acoustic/live/unplugged to the title from genres ───
    for tag in ("acoustic", "live", "unplugged"):
        label = _TITLE_TAG_LABELS.get(tag, tag.capitalize())
        has_genre = any(tag == str(g or "").strip().lower() for g in updated_genres)
        if has_genre and not _has_equivalent_tag(updated_title, tag):
            updated_title = f"{updated_title} ({label})"

    return updated_title, updated_genres


# Singleton for convenience.
#
# Defined ONCE. This module previously created `_detector` twice — at the
# top of this section and again at the end of the file — so the module-level
# singleton was silently replaced on import and any state attached to the
# first instance was discarded.
_detector = GenreDetector()


def detect_special_tags(
    track_name: str,
    album_name: str,
    artist_genres: list[str] | None = None,
    audio_features: dict | None = None,
    album_type: str | None = None,
) -> set[str]:
    """Convenience wrapper — uses the singleton GenreDetector."""
    return _detector.detect_special_tags(
        track_name, album_name, artist_genres, audio_features, album_type,
    )
