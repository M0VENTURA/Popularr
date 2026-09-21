"""
Canonical normalization for titles, artists, albums, and filenames.

✅ SINGLE SOURCE OF TRUTH
✅ No matching or business logic
✅ Used by queue, matching, enrichment, metadata
"""

from __future__ import annotations

import os
import re
import unicodedata
from typing import Any

from helpers.config_helpers import get_queue_matching_config_v2


# =============================================================================
# UNICODE PUNCTUATION EQUIVALENTS
# =============================================================================

UNICODE_PUNCT_MAP = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",  # ‘’‚‛
    "\u201c": '"', "\u201d": '"', "\u2032": "'", "\u2033": '"',  # “”′″
    "\u2013": "-", "\u2014": "-", "\u2015": "-",  # – — ―
    "\u00a0": " ",  # non-breaking space
})


def normalize_unicode_punctuation(value: str) -> str:
    """Convert smart quotes, primes, dashes and NBSP to ASCII equivalents."""
    if not value:
        return value
    return value.translate(UNICODE_PUNCT_MAP)


# =============================================================================
# CORE NORMALIZATION
# =============================================================================

def clean_title(
    value: str,
    *,
    remove_brackets: bool = True,
    remove_single_release: bool = True,
    remove_remaster: bool = True,
) -> str:
    """Shared title cleanup pipeline."""
    if not value:
        return ""

    if remove_brackets:
        value = strip_parentheses(value, full=True)

    if remove_single_release:
        value = strip_single_release_suffix(value)

    if remove_remaster:
        value = strip_remaster_suffix(value)

    return value.strip()


def normalize_isrc(value: Any) -> str:
    """Normalize an ISRC / ISRC-list to a bare 12-char code (uppercased)."""
    if value is None:
        return ""

    if isinstance(value, (list, tuple, set)):
        for item in value:
            code = normalize_isrc(item)
            if code:
                return code
        return ""

    raw = str(value)
    cleaned = re.sub(r"[{}]", " ", raw)
    for code in re.split(r"[/,;|\s]+", cleaned):
        code = code.strip().upper()
        if re.fullmatch(r"[A-Z]{2}[0-9A-Z]{3}[0-9]{7}", code):
            return code
    return re.sub(r"[{}]", "", raw).strip().upper()


def normalize_string(value: str) -> str:
    """Canonical normalization: lowercase, remove accents, remove punctuation, collapse whitespace."""
    if not value:
        return ""

    value = value.lower().strip()
    value = normalize_unicode_punctuation(value)

    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))

    value = re.sub(r"[^\w\s]", " ", value)

    return re.sub(r"\s+", " ", value).strip()


def normalize_filename(value: str) -> str:
    """Normalize filenames for matching: remove extension, normalize text."""
    if not value:
        return ""

    value = re.sub(r"\.[a-z0-9]{2,5}$", "", value, flags=re.IGNORECASE)
    return normalize_string(value)


def normalize_the_prefix(name: str) -> str:
    """Transform artist or playlist names starting with 'The ' to 'Name, The'.

    Example:
        'The Offspring' -> 'Offspring, The'
        'The Rasmus' -> 'Rasmus, The'
        'A Perfect Circle' -> 'A Perfect Circle' (unchanged)
    """
    cleaned = str(name or "").strip()
    if cleaned.lower().startswith("the "):
        base_name = cleaned[4:].strip()
        if base_name:
            return f"{base_name}, The"
    return cleaned


# =============================================================================
# GENERIC CLEANING HELPERS
# =============================================================================

def strip_parentheses(value: str, full: bool = False) -> str:
    """Remove parentheses. full=True removes all bracket sections, full=False removes trailing only."""
    if not value:
        return ""

    if full:
        return re.sub(r"\s*\([^)]*\)", "", value).strip()

    return re.sub(r"\s*\([^)]*\)\s*$", "", value).strip()


def strip_brackets(value: str) -> str:
    """Remove both () and [] — useful for filenames."""
    return re.sub(r"(\(.*?\)|\[.*?\])", "", value or "").strip()


FEAT_SUFFIX_RE = re.compile(
    r"""
    \s+
    (?:\[|\()?\s*
    (?:feat\.?|ft\.?|featuring|with|w/|&|and)
    \s+
    [^\]\)\[]*
    (?:\]|\)|$)
    """,
    re.IGNORECASE | re.VERBOSE,
)


def strip_featured_artist(value: str) -> str:
    if not value:
        return ""
    return FEAT_SUFFIX_RE.sub("", value).strip()


TITLE_FEAT_SUFFIX_RE = re.compile(
    r"""
    \s+
    (?:\[|\()?\s*
    (?:feat\.?|ft\.?|featuring)
    \s+
    [^\]\)\[]*
    (?:\]|\)|$)
    """,
    re.IGNORECASE | re.VERBOSE,
)


def strip_featured_guest_suffix(value: str) -> str:
    """Strip a trailing featured-guest credit from a TITLE."""
    if not value:
        return value
    cleaned = TITLE_FEAT_SUFFIX_RE.sub("", value).strip()
    return cleaned or value


# =============================================================================
# SUFFIX STRIPPING
# =============================================================================

SINGLE_RELEASE_SUFFIX_RE = re.compile(
    r"\s*\(\s*(?:radio\s+(?:edit|mix|version)|single\s+(?:version|edit|mix)|album\s+version)\s*\)\s*$",
    re.IGNORECASE,
)

REMASTER_SUFFIX_RE = re.compile(
    # "Remastered", "Remastered 2026", "2026 Remaster", "Remastered Version"
    # and the bracketed/dashed forms of each. The trailing ``version`` is
    # optional and common in the wild ("Song (Remastered Version)") while
    # being invisible to the previous pattern, which silently left the marker
    # in place.
    r"\s*(?:-|\(|\[)?\s*(?:\d{4}\s*)?remaster(?:ed)?(?:\s*\d{4})?(?:\s+version)?\s*(?:\)|\])?\s*$",
    re.IGNORECASE,
)


def strip_single_release_suffix(value: str) -> str:
    return SINGLE_RELEASE_SUFFIX_RE.sub("", value or "").strip()


def strip_remaster_suffix(value: str) -> str:
    return REMASTER_SUFFIX_RE.sub("", value or "").strip()


# ---------------------------------------------------------------------------
# Edition annotations
#
# ONE keyword list drives every edition-annotation decision in this module.
#
# Previously `_ALBUM_EDITION_STRIP_RE` (used by `strip_album_edition_marker`)
# and `_EDITION_ANNOTATION_KEYWORDS` (used by `extract_edition_annotation`)
# carried DIFFERENT keyword sets. "tour" was present in the second and absent
# from the first, so "(tour edition)" was recognised as an edition annotation
# for compatibility checks but was invisible to the stripper - which is why
# "The Fall of Hearts (tour edition)" never collapsed.
# ---------------------------------------------------------------------------

_EDITION_ANNOTATION_KEYWORDS = frozenset({
    "anniversary", "bonus", "clean", "collector", "deluxe", "digital",
    "edition", "epic", "explicit", "expanded", "extended", "limited",
    "mastered for", "press", "production", "reissue", "remaster",
    "remastered", "special", "standard", "tour", "ultimate", "version",
})

_ALBUM_EDITION_STRIP_RE = re.compile(
    r"\s*[\(\[]\s*(?:clean|explicit|deluxe(?:\s+edition)?|deluxe\s+version|"
    r"tour\s+edition|bonus(?:\s+edition|\s+track(?:s)?)?|"
    r"japanese\s+edition|uk\s+edition|us\s+edition|eu(?:ropean)?\s+edition|"
    r"special\s+edition|expanded\s+edition|extended\s+edition|"
    r"(?:\d+\s*(?:year\s*)?)?anniversary(?:\s+edition)?|"
    r"reissue|limited\s+edition|collector(?:'s)?\s+edition|super\s+deluxe|"
    r"standard\s+edition|digital\s+edition|remaster(?:ed)?(?:\s+edition)?|"
    r"mastered\s+for\s+(?:itunes|apple\s+digital\s+masters)|"
    r"(?:bmg\s+)?club\s+edition|"
    r"(?:\d[\d,]*\s*枚限定生産特装盤|限定生産|完全生産限定|初回限定|"
    r"完全受注生産|數量限定|期間限定|予約限定|"
    r"limited\s+production(?:\s+edition)?|first\s+press(?:\s+edition)?|"
    r"premium\s+edition|special\s+price|"
    # A modifier-prefixed edition marker. Every alternative above pins the
    # WHOLE parenthetical, so real product names that put words BEFORE the
    # keyword never matched and the edition survived into the album name AND
    # every lookup key built from it:
    #     "American Idiot (Holiday Edition Deluxe)"       -> unchanged
    #     "Some Album (20th Anniversary Deluxe Edition)"  -> unchanged
    # This form matches when the parenthetical CONTAINS an unambiguous
    # edition/pressing word, in any order.
    #
    # The leading lookahead is the load-bearing part: markers naming a FORM
    # ("Live", "Remix", "Acoustic", "Unplugged", "Instrumental", "Demo",
    # "Karaoke") are deliberately PRESERVED — they distinguish different
    # RECORDINGS, not different pressings of the same one. A bare "version" is
    # deliberately NOT a keyword for the same reason: "(Acoustic Version)" and
    # "(Boogie Version)" must survive.
    r"(?![^)\]]*\b(?:live|remix|acoustic|unplugged|instrumental|demo|karaoke)\b)"
    r"[^)\]]*?\b(?:edition|deluxe|anniversary|expanded|extended|limited|"
    r"special|bonus|collector(?:'s)?|ultimate|standard|digital|premium|reissue)\b"
    r"[^)\]]*))"
    r"\s*[\)\]]\s*$",
    re.IGNORECASE,
)

# A trailing pair of IDENTICAL parenthetical markers, e.g.
# "(tour edition) (tour edition)" or "(mastered for iTunes) (mastered for
# iTunes)". Tag sources that duplicate an edition marker once per re-tag
# produce this shape for markers NOT in the keyword list above - the generic
# rule collapses them regardless of language/content.
#
# FIXED: the previous pattern was
#     r"\s*(\([^()]*\))\s*\(\1\)\s*$"
# where group 1 captured the parentheses THEMSELVES, so the backreference
# `\(\1\)` demanded a literal "((tour edition))" - doubled parens. It could
# never match real input, so this rule had never once fired and every
# duplicated marker survived. The group now captures only the INNER text.
_REPEATED_TRAILING_MARKER_RE = re.compile(
    r"\s*\(([^()]*)\)\s*\(\s*\1\s*\)\s*$",
    re.IGNORECASE,
)

# Any bracketed annotation, anywhere in the string.
_ANNOTATION_RE = re.compile(r"\s*([\(\[])([^)\]]*)([\)\]])")


def _annotation_key(text: str) -> str:
    """Punctuation- and case-insensitive comparison key for annotation text."""
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").casefold()).strip()


def annotation_keys(name: str) -> list[str]:
    """Normalised keys for every bracketed annotation in ``name``."""
    return [
        key
        for key in (
            _annotation_key(m.group(2))
            for m in _ANNOTATION_RE.finditer(str(name or ""))
        )
        if key
    ]


def has_edition_annotation(name: str) -> bool:
    """True when ``name`` carries an edition/version style annotation."""
    return any(
        any(keyword in key for keyword in _EDITION_ANNOTATION_KEYWORDS)
        for key in annotation_keys(name)
    )


def dedupe_annotations(name: str) -> str:
    """Collapse repeated bracketed annotations, keeping the FIRST occurrence.

        "The Fall of Hearts (tour edition) (tour edition)"
            -> "The Fall of Hearts (tour edition)"

    Unlike `strip_album_edition_marker`, this PRESERVES one copy of the
    annotation - it is for repairing a stored/display album name, not for
    building a bare lookup key. Comparison is punctuation- and
    case-insensitive, so "(Tour Edition)" and "(tour edition)" collapse
    together. Distinct annotations are all preserved, in original order.
    """
    if not name:
        return ""

    seen: set[str] = set()

    def _sub(match: re.Match[str]) -> str:
        key = _annotation_key(match.group(2))
        if not key:
            return match.group(0)
        if key in seen:
            return ""
        seen.add(key)
        return match.group(0)

    collapsed = _ANNOTATION_RE.sub(_sub, str(name))
    return re.sub(r"\s{2,}", " ", collapsed).strip()


# Labels that describe a release FORM. Two annotations that both mention the
# same form are semantically duplicate even when their text differs, e.g.
# "(Recorded Live) (Live)" - which is exactly the damage the start-anchored
# guard in `album_stage._apply_live_remix_album_tagging()` creates.
_FORM_LABELS = (
    "live", "acoustic", "unplugged", "remix",
    "instrumental", "demo", "karaoke",
)


def collapse_redundant_form_labels(name: str) -> str:
    """Drop later annotations whose form label an earlier annotation states.

        "Song (Recorded Live) (Live)"    -> "Song (Recorded Live)"
        "Song (Live) (Live at Wembley)"  -> "Song (Live)"
        "Song (Live) (Acoustic)"         -> unchanged (different forms)

    The FIRST annotation wins, because it is the more specific one - it is
    the blindly-appended label we want to drop, not the descriptive one.
    """
    if not name:
        return ""

    seen: set[str] = set()

    def _sub(match: re.Match[str]) -> str:
        nonlocal seen
        inner = str(match.group(2) or "").casefold()
        forms = {
            label for label in _FORM_LABELS
            if re.search(rf"\b{label}\b", inner)
        }
        if not forms:
            return match.group(0)
        if forms & seen:
            return ""
        seen = seen | forms
        return match.group(0)

    collapsed = _ANNOTATION_RE.sub(_sub, str(name))
    return re.sub(r"\s{2,}", " ", collapsed).strip()


def repair_annotations(name: str) -> str:
    """Full annotation repair: identical duplicates AND redundant form labels.

    This is the function to use when repairing a STORED name. It preserves
    one copy of every distinct annotation, so editions are never lost.
    """
    return collapse_redundant_form_labels(dedupe_annotations(name))


def strip_album_edition_marker(value: str) -> str:
    """Return the album title with trailing edition marker(s) removed.

    Strips edition markers idempotently and REPEATEDLY so a mangled album
    name like "The General Strike (10 Year Anniversary) (10 Year
    Anniversary)" or "Doomsday Machine (reissue) (reissue) (reissue)"
    collapses to the clean release title — some tag sources duplicate the
    marker once per re-tag. A bare "(reissue)" / "(anniversary)" is an
    edition marker too, so it is stripped. Also handles:
      - "mastered for iTunes" / "(BMG club edition)" / "(deluxe version)"
      - "(tour edition)" / "(bonus)" / regional "(Japanese edition)"
      - Japanese limited-edition markers: "(5,000枚限定生産特装盤)",
        "(完全生産限定盤)", "(初回限定盤)" ...
      - ANY repeated identical trailing marker (generic dedup — catches
        markers outside the keyword list regardless of language).

    NOTE: this removes the marker entirely, for building lookup keys. To
    repair a stored album name while KEEPING one copy of its edition, use
    `dedupe_annotations()` instead.
    """
    cleaned = value or ""
    prev = None
    for _ in range(8):  # bounded loop — never hang on pathological input
        stripped = _ALBUM_EDITION_STRIP_RE.sub("", cleaned).strip()
        # Generic dedup of an identical repeated trailing marker.
        stripped = _REPEATED_TRAILING_MARKER_RE.sub("", stripped).strip()
        if stripped == cleaned or stripped == prev:
            break
        prev = cleaned
        cleaned = stripped
    return cleaned or (value or "")


def is_redundant_rename(old_name: str, new_name: str) -> bool:
    """True when ``new_name`` adds nothing beyond duplicated annotations."""
    return (
        dedupe_annotations(old_name).casefold()
        == dedupe_annotations(new_name).casefold()
    )


def safe_album_rename(old_name: str, new_name: str) -> tuple[str, str]:
    """Validate a proposed album rename.

    Returns ``(resolved_name, reason)``. ``resolved_name`` is empty when the
    rename must NOT be applied; ``reason`` always explains the decision so
    callers can log it.

    Rejects, in order:
      - an empty proposal or empty current name;
      - a proposal that only repeats an annotation the album already has
        (the "(tour edition) (tour edition)" defect);
      - a proposal identical to the current name after normalisation;
      - a proposal that would DROP an edition annotation the library name
        carries — editions are how separate pressings stay distinct.

    A proposal that partly duplicates is salvaged to its collapsed form
    rather than rejected outright.
    """
    old_raw = str(old_name or "").strip()
    new_raw = str(new_name or "").strip()

    if not new_raw:
        return "", "proposed name empty"
    if not old_raw:
        return "", "current name empty"

    deduped = dedupe_annotations(new_raw)

    if deduped != new_raw:
        if deduped.casefold() == dedupe_annotations(old_raw).casefold():
            return "", f"proposal only duplicated annotations: {new_raw!r}"
        return deduped, f"collapsed duplicate annotations from {new_raw!r}"

    if is_redundant_rename(old_raw, new_raw):
        return "", "no change after annotation normalisation"

    old_keys = set(annotation_keys(old_raw))
    new_keys = set(annotation_keys(new_raw))
    lost = {
        key for key in (old_keys - new_keys)
        if any(keyword in key for keyword in _EDITION_ANNOTATION_KEYWORDS)
    }
    if lost:
        return "", f"would drop edition annotation(s): {sorted(lost)}"

    return new_raw, "ok"


def append_annotation_once(name: str, label: str) -> str:
    """Append ``(label)`` to ``name`` only if not already annotated with it.

    Matches the label ANYWHERE inside ANY bracketed group, so
    "Song (Recorded Live)" is correctly recognised as already-live and does
    NOT become "Song (Recorded Live) (Live)". The result is passed through
    `dedupe_annotations()`, so this also repairs names already damaged by
    the previous start-anchored check.
    """
    raw = str(name or "").strip()
    label = str(label or "").strip()
    if not raw or not label:
        return dedupe_annotations(raw)

    already = bool(
        re.search(
            rf"[\(\[][^)\]]*\b{re.escape(label)}\b[^)\]]*[\)\]]",
            raw,
            re.IGNORECASE,
        )
    )
    if already:
        return dedupe_annotations(raw)

    return dedupe_annotations(f"{raw} ({label})")


def strip_search_keywords(value: str) -> str:
    """Remove parenthetical edition markers for *same-song different-cut* variants."""
    try:
        from helpers.config_helpers import get_config
        cfg = get_config() or {}
        keywords = (cfg.get("search") or {}).get("strip_keywords") or []
        if not keywords and cfg.get("strip_parentheses_filters"):
            keywords = cfg["strip_parentheses_filters"]
        keyword_set = {str(k).strip().lower() for k in keywords if str(k).strip()}
    except Exception:
        keyword_set = set()
    if not keyword_set or not value:
        return value or ""

    def _repl(match: Any) -> str:
        return "" if match.group(1).strip().lower() in keyword_set else match.group(0)

    return re.sub(r"\(([^)]*)\)", _repl, value)


# =============================================================================
# VERSION / VARIANT EXTRACTION
# =============================================================================

def get_version_keywords() -> set[str]:
    """Variant tokens used during title parsing."""
    return {
        str(token).lower()
        for token in get_queue_matching_config_v2()[
            "title_variant_tokens"
        ]
    } | {"unplugged"}


ROMAN_NUMERAL_PATTERN = r'\s+(I{1,3}|IV|V|VI{0,3}|IX|X{1,3})\s*$'
PUNCTUATION_SUFFIX_PATTERN = re.compile(r'([!+?]+)\s*$')


def extract_version_info(title: str) -> tuple[str, set[str]]:
    """Extract base title + version tags without normalizing."""
    if not title:
        return "", set()

    title_lower = title.lower()

    found_versions = {
        keyword
        for keyword in get_version_keywords()
        if re.search(
            rf"\b{re.escape(keyword)}\b",
            title_lower,
        )
    }

    suffix_match = PUNCTUATION_SUFFIX_PATTERN.search(title)
    preserved_suffix = (
        suffix_match.group(1)
        if suffix_match
        else ""
    )

    base_title = clean_title(
        title,
        remove_brackets=True,
        remove_single_release=False,
        remove_remaster=False,
    )

    base_title = re.sub(
        r"\s*-\s*(?:edit|mix|version|live|remix).*",
        "",
        base_title,
        flags=re.IGNORECASE,
    ).strip()

    roman_match = re.search(
        ROMAN_NUMERAL_PATTERN,
        base_title,
        re.IGNORECASE,
    )

    if roman_match:
        base_title = (
            base_title[:roman_match.start()]
            .strip()
        )
        base_title += f" {roman_match.group(1).lower()}"

    base_title += preserved_suffix

    return (
        base_title.strip(),
        found_versions,
    )


_EDITION_ANNOTATION_RE = re.compile(r"[\(\[]([^\)\]]+)[\)\]]\s*$", re.IGNORECASE)


def extract_edition_annotation(title: str) -> str | None:
    """Return the normalized trailing edition annotation, or None."""
    if not title:
        return None
    m = _EDITION_ANNOTATION_RE.search(title)
    if not m:
        return None
    inner = m.group(1).strip().lower()
    if not any(kw in inner for kw in _EDITION_ANNOTATION_KEYWORDS):
        return None
    return re.sub(r"[^a-z0-9]+", " ", inner).strip()


def edition_annotations_compatible(title_a: str, title_b: str) -> bool:
    """True when the edition annotations on two titles are compatible."""
    ann_a = extract_edition_annotation(title_a)
    ann_b = extract_edition_annotation(title_b)
    if ann_a is None and ann_b is None:
        return True
    if ann_a is None or ann_b is None:
        return False
    return ann_a == ann_b


#: How many of an album's titles must carry the same annotation before it is
#: treated as the album's version. Two, so a single stray marker cannot
#: redefine the whole album, and a plain majority so a release whose titles
#: only PARTLY carry the marker still resolves all of them consistently.
_ALBUM_ANNOTATION_MIN_TRACKS = 2


def album_version_annotation(
    album_name: str = "",
    track_titles: Any = None,
) -> str | None:
    """The version annotation an ALBUM's titles agree on, or None.

    WHY THIS EXISTS — the reported "Helden X Hymnen" defect. A release's
    titles routinely carry the version marker on only SOME tracks: on the
    unplugged edition of an album, most titles read "Song (Unplugged
    Version)" but a few are tagged plainly as "Song". Every per-title check
    then goes wrong for exactly those tracks:

        edition_annotations_compatible("Song", "Song (Unplugged Version)")
            -> False   (local has no annotation, candidate has one)
        edition_annotations_compatible("Song", "Song")
            -> True    (both plain)

    so the recording search SKIPS the unplugged recording and takes the
    identically titled STUDIO one. The track then carries the studio
    recording's MBID, and every popularity figure read through it
    (ListenBrainz listens by MBID, Last.fm via the release-scoped match) is
    the studio recording's — the reported "used the wrong version of the
    tracks when doing the popularity scoring".

    The album's OWN annotation is the missing context, so it is derived here
    and used as the fallback for a title that carries none.

    The album NAME is checked first (cheap and authoritative when present,
    e.g. "... (Unplugged)"), then the album's track titles BY MAJORITY.
    """
    from_album = extract_edition_annotation(album_name)
    if from_album:
        return from_album

    counts: dict[str, int] = {}
    for raw in (track_titles or []):
        annotation = extract_edition_annotation(str(raw or ""))
        if annotation:
            counts[annotation] = counts.get(annotation, 0) + 1
    if not counts:
        return None

    # Deterministic tie-break on the annotation text so two equal counts
    # cannot resolve differently between runs.
    annotation, hits = max(counts.items(), key=lambda item: (item[1], item[0]))
    total = sum(counts.values())
    if hits < _ALBUM_ANNOTATION_MIN_TRACKS or hits * 2 < total:
        return None
    return annotation


# Album-level artist placeholders that are never a TRACK artist. Kept as a fixed
# list on purpose: these values are wrong in the ARTIST column whatever the
# compilation-detection settings say, so the behaviour must not depend on config.
TRACK_ARTIST_PLACEHOLDERS = frozenset({
    "various artists", "various", "va", "v/a", "v.a.", "soundtrack", "unknown artist",
})


def is_track_artist_placeholder(value: str | None) -> bool:
    """True when an ARTIST value is an album-level placeholder, not a performer.

    A compilation's tracks are by the individual performers, so "Various Artists"
    is the ALBUM's artist, never the track's. Two consequences this exists for:

    * a MusicBrainz recording search constrained to such an artist returns
      NOTHING — every track of "Little Nicky" was searched as
      ``artist:"Various Artists"`` and came back with ``candidate_count=0``, so
      the recording (and with it the song's real artist) was never found; and
    * when a match IS made by another route, the placeholder must be replaceable
      without a forced metadata pass, because it is not a user edit.
    """
    return str(value or "").strip().casefold() in TRACK_ARTIST_PLACEHOLDERS


def is_compilation_artist(artist: str | None) -> bool:
    """Determine whether an artist string represents a compilation/various-artists release."""
    if not artist:
        return False

    cfg = get_queue_matching_config_v2()

    if not cfg["detect_compilations"]:
        return False

    compilation_artists = {
        normalize_string(a)
        for a in cfg["compilation_artists"]
    }

    return (
        normalize_string(artist)
        in compilation_artists
    )


# =============================================================================
# PRIMARY NORMALIZATION PIPELINES
# =============================================================================

def normalize_title_for_lookup(title: str) -> str:
    """Canonical matching normalization."""
    return normalize_string(
        clean_title(
            title,
            remove_brackets=True,
            remove_single_release=True,
            remove_remaster=True,
        )
    )


def normalize_title_for_mbid_match(title: str) -> str:
    """Bracket-preserving canonical normalization for MusicBrainz MBID matching."""
    return normalize_string(
        clean_title(
            title,
            remove_brackets=False,
            remove_single_release=True,
            remove_remaster=True,
        )
    )


def normalize_title_for_lucene_query(title: str) -> str:
    """Punctuation-free title for MusicBrainz Lucene term queries.

    Punctuation is replaced with a SPACE (not deleted) so it still separates
    tokens: "GOLDEN HOUR: Part.4" must normalise to "golden hour part 4".
    Deleting it produced "golden hour part4", which no longer matches
    MusicBrainz's own tokenisation (the colon/period are token boundaries
    there) and made the unquoted release-group fallback miss punctuation-heavy
    titles.  Runs of whitespace left behind are collapsed at the end.
    """
    if not title:
        return ""

    value = re.sub(r"\s*\([^)]*\bcover\b[^)]*\)", "", title, flags=re.IGNORECASE)
    value = normalize_unicode_punctuation(value.lower())
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = re.sub(r"[^\w\s]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_title_for_lastfm(title: str) -> str:
    """Lighter normalization for Last.fm."""
    if not title:
        return ""

    value = clean_title(
        title,
        remove_brackets=True,
        remove_single_release=False,
        remove_remaster=True,
    )

    value = (
        value.replace("“", "")
             .replace("”", "")
             .replace("«", "")
             .replace("»", "")
             .replace("–", "-")
             .replace("—", "-")
             .replace("…", "...")
    )

    return normalize_string(value)


def normalize_artist(value: str) -> str:
    value = strip_featured_artist(value)
    return normalize_string(value)


def clean_artist_name_for_storage(value: str) -> str:
    """Conservative canonicalization for artist/album_artist DB fields."""
    if not value:
        return ""

    cleaned = " ".join(str(value).strip().split())
    if not cleaned:
        return ""

    parts = [
        p.strip()
        for p in re.split(r"\s*[•·]+\s*|\s+[/|;]+\s+", cleaned)
        if p.strip()
    ]
    if len(parts) > 1:
        buckets: dict[str, int] = {}
        for part in parts:
            key = normalize_string(part)
            buckets[key] = buckets.get(key, 0) + 1
        most_common_key = max(buckets, key=lambda k: buckets[k])
        for part in parts:
            if normalize_string(part) == most_common_key:
                cleaned = part
                break

    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9' .-]*", cleaned):
        letters = re.sub(r"[^A-Za-z]+", "", cleaned)
        if letters and (letters.islower() or letters.isupper()):
            cleaned = cleaned.title()

    return " ".join(cleaned.split())


def normalize_album(value: str) -> str:
    return normalize_string(
        clean_title(
            value,
            remove_brackets=True,
            remove_single_release=False,
            remove_remaster=True,
        )
    )


def clean_album_name_for_storage(value: str) -> str:
    """Canonicalization for the album / stored display name.

    Collapses duplicated annotations while PRESERVING the edition, so
    "The Fall of Hearts (tour edition) (tour edition)" becomes
    "The Fall of Hearts (tour edition)" rather than losing the edition.
    Use this before writing an album name to the DB or to file tags.

    Uses `repair_annotations()` rather than `dedupe_annotations()` so
    semantically-duplicate annotations with differing text - "(Recorded
    Live) (Live)" - are collapsed too, not just literal repeats.
    """
    if not value:
        return ""
    return repair_annotations(" ".join(str(value).strip().split()))


# alias
normalize = normalize_title_for_lookup


# =============================================================================
# LIGHT HEURISTICS
# =============================================================================

def detect_cover_and_normalize_title(title: str) -> tuple[bool, str]:
    """Deprecated-lite title probe: is this a COVER, and its lookup form.

    The cover verdict is deliberately ATTRIBUTION-shaped: only a trailing
    bracketed "(X Cover)" / "[Cover Version]" counts. The previous test was a
    bare ``"cover" in title.lower()``, which flagged every song merely
    CONTAINING the word — "Cover Me" was reported as a cover, and a track
    titled "Song (Disturbed Cover)" was too. A falsely flagged cover then had
    its title stripped and its cover flag stored, which is the reported
    "falsely created as a cover". Use ``normalise_scan_track_identity`` when
    you also need the CLEANED title; this function never rewrites the title.
    """
    if not title:
        return False, ""

    normalized = normalize_title_for_lookup(title)
    is_cover = bool(_COVER_ATTRIBUTION_RE.search(title))

    return is_cover, normalized


def normalise_result(result: Any) -> tuple[dict[str, Any], int]:
    """Normalise different service return shapes into (dict, int)."""
    if isinstance(result, tuple) and len(result) == 2:
        payload, status = result
        if isinstance(payload, dict) and isinstance(status, int):
            payload.setdefault("success", status < 400)
            return payload, status

    if isinstance(result, dict):
        status = 200 if result.get("success", True) else 500
        result.setdefault("success", status < 400)
        return result, status

    if isinstance(result, bool):
        return {"success": result}, 200 if result else 500

    if result is None:
        return {"success": True, "result": None}, 200

    return {"success": True, "result": result}, 200


def is_remastered_only_variant(title: str) -> bool:
    if not title:
        return False

    t = title.lower()
    return "remaster" in t or "remastered" in t


def normalise_year_tag(raw_year: str | int | None) -> str:
    """Extract a clean 4-digit year from a potentially longer date string."""
    if not raw_year:
        return ""
    m = re.search(r"((?:19|20)\d{2})", str(raw_year))
    return m.group(1) if m else str(raw_year).strip()


# =============================================================================
# TITLE CLEANUP
# =============================================================================

_COVER_ATTRIBUTION_RE = re.compile(
    r"\s*[\(\[][^\)\]]*cover[^\)\]]*[\)\]]\s*$",
    re.IGNORECASE,
)


def strip_cover_attribution(title: str) -> str:
    """Strip cover attributions from the end of a track title."""
    if not title:
        return ""
    result = _COVER_ATTRIBUTION_RE.sub("", title).strip()
    return result if result else title


_LIVE_SUFFIX_RE = re.compile(
    r"\s*[\(\[](?:[^)\]]*\b)?(?:Live|Acoustic|Unplugged)\b[^)\]]*[\)\]]\s*$",
    re.IGNORECASE,
)


def strip_live_acoustic_suffix(title: str) -> str:
    """Strip a trailing live/acoustic/unplugged annotation from a title.

    Matches the label anywhere inside the trailing bracket, so
    "Song (Recorded Live)" and "Song (Stripped Acoustic)" are handled, not
    only "Song (Live)".
    """
    if not title:
        return ""
    result = title
    for _ in range(4):
        stripped = _LIVE_SUFFIX_RE.sub("", result).strip()
        if stripped == result:
            break
        result = stripped
    return result or title


# =============================================================================
# COVER-DETECTION HELPERS
# =============================================================================

def canonical_track_title(value: str) -> str:
    """Normalize track titles so album/version variants still match canonical recordings."""
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\s+-\s+.*$", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def normalize_name(value: str) -> str:
    """Normalize person/group names for robust matching."""
    if not value:
        return ""
    normalized = value.lower().strip()
    normalized = normalized.replace("'", "'")
    normalized = re.sub(r"\b(the|and)\b", " ", normalized)
    normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def names_match(left: str, right: str) -> bool:
    """Match names with token overlap to handle middle names and variants."""
    left_norm = normalize_name(left)
    right_norm = normalize_name(right)
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm:
        return True
    left_tokens = {t for t in left_norm.split() if len(t) > 1}
    right_tokens = {t for t in right_norm.split() if len(t) > 1}
    if not left_tokens or not right_tokens:
        return False
    if left_tokens <= right_tokens or right_tokens <= left_tokens:
        return True
    intersection = left_tokens & right_tokens
    return len(intersection) >= max(2, min(len(left_tokens), len(right_tokens)))


def normalize_writer_credits(writers: list[str]) -> list[str]:
    """Split combined writer credits and dedupe names."""
    normalized: list[str] = []
    for writer in writers or []:
        text = str(writer or "").strip()
        if not text:
            continue
        parts = re.split(r"\s*[;/,&]|\s+and\s+", text, flags=re.IGNORECASE)
        for part in parts:
            name = re.sub(r"^\(+|\)+$", "", part.strip())
            if name and name not in normalized:
                normalized.append(name)
    return normalized


# =============================================================================
# DISCOGS CLEANUP
# =============================================================================

DISCOGS_ARTIST_ID_RE = re.compile(r"\[a\d+\]")

DISCOGS_ORPHANED_AKA_RE = re.compile(
    r"\baka\s*(?=\s*\(|,|\.|$)",
    re.IGNORECASE,
)

DISCOGS_LEADING_AKA_RE = re.compile(
    r"^\s*aka\s+",
    re.IGNORECASE,
)


def clean_discogs_biography(text: str) -> str:
    """Clean Discogs biography text."""
    if not text:
        return ""

    cleaned = DISCOGS_ARTIST_ID_RE.sub("", text)
    cleaned = DISCOGS_ORPHANED_AKA_RE.sub("", cleaned)
    cleaned = DISCOGS_LEADING_AKA_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)

    return cleaned.strip()


def normalize_core_title(value: str) -> str:
    """Strict title normalization for matching."""
    return normalize_string(
        clean_title(
            value,
            remove_brackets=True,
            remove_single_release=False,
            remove_remaster=False,
        )
    )


def normalize_core_filename(value: str) -> str:
    value = re.sub(
        r"\.[a-z0-9]{2,5}$",
        "",
        value or "",
        flags=re.IGNORECASE,
    )
    value = strip_brackets(value)
    return normalize_string(value)


# ============================================================
# TRACK PREFIX CLEANUP
# ============================================================

def strip_track_number_prefix(title: str) -> str:
    """Remove leading track numbers and trailing Soulseek IDs."""
    if not title:
        return title

    cleaned = re.sub(
        r'^\d+(?:\s*-\s*\d+)?\s*[-\.]\s*',
        '',
        title
    ).strip()

    cleaned = re.sub(r'_\d{12,}$', '', cleaned).strip()

    return cleaned if cleaned else title


# ============================================================
# TEXT NORMALIZATION FOR MATCHING
# ============================================================

def normalize_match_text(value: str) -> str:
    if not value:
        return ""

    normalized = value.lower().strip()
    normalized = strip_track_number_prefix(normalized)

    replacements = {
        "&": "and",
        "’": "'",
        "`": "'",
        "-": " ",
        "_": " ",
        "/": " ",
        "(": " ",
        ")": " ",
        "[": " ",
        "]": " ",
    }

    for src, dst in replacements.items():
        normalized = normalized.replace(src, dst)

    return " ".join(normalized.split())


# ============================================================
# TRACK/DISC EXTRACTION (FROM TAGS)
# ============================================================

def extract_track_disc(
    value: str,
    *,
    is_filename: bool = False,
) -> tuple[int | None, int | None]:
    """Extract (track_number, disc_number)."""
    if not value:
        return None, None

    text = value
    if is_filename:
        text = os.path.splitext(os.path.basename(value))[0]

    text = str(text).strip()

    match = re.match(
        r"^\s*(\d{1,2})\s*[-/\.]\s*(\d{1,3})",
        text,
    )
    if match:
        try:
            return int(match.group(2)), int(match.group(1))
        except ValueError:
            return None, None

    match = re.match(r"^\s*(\d{1,3})", text)
    if match:
        try:
            return int(match.group(1)), None
        except ValueError:
            return None, None

    return None, None


def _coerce_position_to_int(value: Any, default: int) -> int:
    """Convert MusicBrainz position strings into an integer."""
    raw = str(value or '').strip()
    if not raw:
        return default
    if raw.isdigit():
        return int(raw)
    match = re.search(r"\d+", raw)
    if match:
        return int(match.group(0))
    return default


def sanitize_path(value: str) -> str:
    """Sanitize any string to be safe for OS file systems."""
    if not value:
        return "Unknown"
    clean = re.sub(r'[<>:"|?*\\]', '_', value)
    return clean.strip().strip(".")


def normalize_album_artist(value: str) -> str:
    """Canonical VA/Various Artists handling."""
    key = " ".join(str(value or "").lower().split())
    if any(key == v or key.startswith(f"{v} ") for v in ["various", "various artists", "va", "v/a"]):
        return "Various Artists"
    return value.strip()


def safe_int(value: Any, default: int = 0) -> int:
    """Safely convert a value to int, returning *default* on failure."""
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_str(value: Any, default: str = "") -> str:
    """Safely convert a value to string, returning *default* on failure."""
    if value is None:
        return default
    return str(value)


def queue_duration_seconds(value: Any) -> float | None:
    """Normalize a download_queue ``duration`` value to seconds."""
    if value is None or value == "":
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value / 1000 if value >= 3000 else value


def is_valid_version(track_title: str, allow_live_remix: bool = False) -> bool:
    """Validate track version against blacklist and whitelist."""
    title = track_title.lower()
    blacklist = {"live", "remix", "mix", "edit", "rework", "bootleg"}
    whitelist = {"remaster"}
    if allow_live_remix:
        blacklist = blacklist - {"live", "remix"}
    if any(b in title for b in blacklist) and not any(w in title for w in whitelist):
        return False
    return True


# =============================================================================
# SCAN-TIME TRACK IDENTITY
#
# One pass that decides what a track IS, from the messy shapes a library
# carries, and returns the canonical fields the scan persists:
#
#   artist        "Evanescence feat. 12 Stones"
#                 — a featured credit found in the TITLE is MOVED onto the
#                   artist ("Bring Me To Life feat 12 stones" -> artist gains
#                   "feat. 12 Stones", title becomes "Bring Me To Life").
#   title         "Bring Me To Life"
#                 — the credit, a "(X Cover)" attribution and a remaster
#                   marker are all removed.
#   album_artist  "Evanescence"
#                 — the PRIMARY artist; never replaced by the track artist, and
#                   left completely alone for a various-artists compilation.
#
# Applied by the SCAN only (``services.popularity.scan_hooks``), which is where
# the reported problem lives: a Navidrome import stores whatever the file says,
# and the scan is what decides the canonical record the DB and the file tags
# then share.
# =============================================================================

# feat./ft./featuring ONLY. "with", "w/" and "&" are deliberately excluded from
# TITLES: "Me & You" and "Dance with the Devil" are song titles, not credits.
# (``strip_featured_artist`` must stay permissive on the ARTIST field, where
# "X & Y" really is a second credited artist.)
TITLE_FEATURED_CREDIT_RE = re.compile(
    r"""
    \s*
    (?:\[|\()?\s*
    (?:feat\.?|ft\.?|featuring)
    \s+
    (?P<guest>[^\]\)\[]+?)
    \s*
    (?:\]|\))?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# A featured credit ALREADY on the artist field — narrow on purpose (see above),
# and used only to decide whether appending a guest would double it up.
_ARTIST_FEATURED_RE = re.compile(r"\b(?:feat\.?|ft\.?|featuring)\s+\S", re.IGNORECASE)

FEATURED_JOIN = "feat."


def _clean_featured_name(name: str) -> str:
    """Tidy a guest name lifted out of a title."""
    text = re.sub(r"\s+", " ", str(name or "").strip()).strip(" .,;:-–—")
    if not text:
        return ""
    # Capitalise ONLY an entirely lower-case credit ("12 stones" -> "12 Stones").
    # Casing is otherwise preserved, because ``str.title()`` destroys names like
    # "MC Solaar" and "will.i.am" — the guard below skips any credit holding a
    # character other than a word character, space, apostrophe or hyphen, and
    # "will.i.am" carries dots.
    if text == text.lower() and not re.search(r"[^\w\s'\-]", text):
        text = text.title()
    return text


def extract_featured_credit(title: str) -> tuple[str, str]:
    """Split a trailing "feat. X" credit off a TITLE.

    Returns ``(title_without_credit, guest)``; ``guest`` is "" when the title
    carries no credit, and the title is NEVER emptied — a title that is nothing
    but a credit is returned unchanged.
    """
    text = str(title or "").strip()
    if not text:
        return "", ""
    match = TITLE_FEATURED_CREDIT_RE.search(text)
    if not match:
        return text, ""
    guest = _clean_featured_name(match.group("guest"))
    cleaned = text[: match.start()].strip().rstrip("-–—,;:")
    if not guest or not cleaned.strip():
        return text, ""
    return cleaned.strip(), guest


def build_featured_artist(artist: str, guest: str) -> str:
    """Append a featured credit to an artist, never duplicating one."""
    base = str(artist or "").strip()
    guest = str(guest or "").strip()
    if not guest:
        return base
    if not base:
        return f"{FEATURED_JOIN} {guest}"
    if _ARTIST_FEATURED_RE.search(base):
        return base
    return f"{base} {FEATURED_JOIN} {guest}"


def normalise_scan_track_identity(
    *,
    artist: str,
    title: str,
    album_artist: str = "",
    is_va_compilation: bool = False,
) -> dict[str, Any]:
    """Canonical ``title`` / ``artist`` / ``album_artist`` for one scanned track.

    Reported cases this covers:

    * ``"Evanescence - Bring Me To Life feat 12 stones"`` becomes artist
      ``"Evanescence feat. 12 Stones"`` with title ``"Bring Me To Life"``, while
      the album artist stays ``"Evanescence"``.
    * a title falsely carrying a cover attribution — ``"Song (Disturbed
      Cover)"`` — loses the wording, and the flag it caused is reported back so
      the caller can clear a verdict that came from it.
    * ``"Song (Remastered)"`` / ``"Song (Remastered 2026)"`` /
      ``"Song - 2026 Remastered"`` lose the marker. Per the agreed rule the year
      inside the marker is DISCARDED — the track's years come from
      MusicBrainz, never from a title.

    ``album_artist`` is only ever the PRIMARY artist: an existing album artist
    keeps its own value (a featured credit on it is stripped), and it is filled
    from the primary track artist only when it was empty. A various-artists
    compilation keeps whatever album artist it has — a compilation's album
    artist is the compilation's, never the track's.
    """
    original_title = str(title or "").strip()
    original_artist = str(artist or "").strip()
    original_album_artist = str(album_artist or "").strip()

    clean_title = original_title
    had_cover = False
    had_remaster = False
    # Bounded loop: "Song (Disturbed Cover) (2011 Remastered)" is two trailing
    # markers, and stripping one exposes the next. Neither stripper can empty a
    # title (each returns its input when the result would be blank), so a
    # degenerate title like "(Remastered)" survives untouched.
    for _ in range(4):
        before = clean_title
        stripped = strip_remaster_suffix(clean_title)
        if stripped and stripped != clean_title:
            clean_title = stripped
            had_remaster = True
        stripped = strip_cover_attribution(clean_title)
        if stripped and stripped != clean_title:
            clean_title = stripped
            had_cover = True
        if clean_title == before:
            break
    clean_title = clean_title or original_title

    title_without_credit, guest = extract_featured_credit(clean_title)
    if guest:
        clean_title = title_without_credit

    new_artist = build_featured_artist(original_artist, guest)
    primary_artist = strip_featured_artist(new_artist).strip() or new_artist

    if is_va_compilation:
        final_album_artist = original_album_artist
    elif original_album_artist:
        final_album_artist = (
            strip_featured_artist(original_album_artist).strip() or primary_artist
        )
    else:
        final_album_artist = primary_artist

    return {
        "title": clean_title,
        "artist": new_artist,
        "album_artist": final_album_artist,
        "featured_artist": guest,
        "title_had_featured_credit": bool(guest),
        # True when the artist ALREADY carried a featured credit. Callers that
        # write ``album_artist`` must not act on it in that case: the album key
        # is ``COALESCE(NULLIF(album_artist,''), artist)``, so changing the
        # artist spelling (or filling an empty album artist) for a credit-laden
        # artist would move the whole album to a different key mid-scan.
        "artist_already_had_credit": bool(_ARTIST_FEATURED_RE.search(original_artist)),
        # True when a cover ATTRIBUTION was removed from the title — the caller
        # uses it to clear a cover verdict that wording had caused.
        "title_had_cover_wording": had_cover,
        "title_had_remaster_wording": had_remaster,
    }


def album_artist_key_variants(artist: str) -> list[str]:
    """Album-key spellings that must ALL be honoured for one artist.

    The album key used throughout the app is
    ``COALESCE(NULLIF(album_artist, ''), artist)``, and the scan RELOCATES a
    featured credit from the title onto ``artist``. For a row whose
    ``album_artist`` is empty that changes the key from "X feat. Y" to "X", so
    an album-scoped lookup issued with the pre-move spelling would silently find
    NOTHING for exactly the albums the relocation touched — no file-tag sync, no
    release MBID, no extended metadata. Callers therefore match against every
    spelling this returns.
    """
    keys: list[str] = []
    for candidate in (
        str(artist or "").strip(),
        strip_featured_artist(str(artist or "")).strip(),
    ):
        if candidate and candidate not in keys:
            keys.append(candidate)
    return keys
