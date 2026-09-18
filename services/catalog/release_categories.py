"""Canonical release-type categories for the artist-page discography.

ONE source of truth.  Before this module six different places classified a
release into a discography bucket, each with its own rule list:

* ``services.catalog.album_classification_service.classify_album_type``
* ``services.metadata.artist_scan_service._categorize_release``
* ``services.popularity.release_cache_service._derive_musicbrainz_category``
* ``services.popularity.release_cache_service._derive_discogs_category``
* ``services.popularity.release_cache_service._fallback_release_category``
* ``services.metadata.artist_service.get_correction_albums._classify``

They disagreed, and all of them shared the same flaw: only ``live`` /
``compilation`` / ``remix`` were recognised, and anything else MusicBrainz
reported fell through to the STUDIO bucket.  That silently misfiled
``Album + Field recording``, ``Album + DJ-mix + Mixtape/Street``,
``Album + Soundtrack``, ``Album + Demo``, ``Album + Spokenword``,
``Album + Interview`` and ``Album + Audio drama`` as studio albums.  The
in-library classifier was worse still — an early ``if "album" in raw_type:
return "album"`` made its own ``soundtrack`` and ``remix`` branches unreachable,
so a remix album you OWNED filed under Studio while the identical missing
release filed under Remix.

The rule this module enforces: **a release carrying any secondary type is never
a plain studio album.**  That single invariant is what stops the whole class of
bug, including secondary types MusicBrainz adds later.

Categories are open-ended
-------------------------
Sections are driven by the DATA, not a fixed list.  A category exists in the UI
only when something is in it (the templates hide empty sections), so an artist
with no field recordings never grows a "Field Recordings" heading.  Adding a new
MusicBrainz secondary type is a one-line change to ``_SECONDARY_SPECS`` — no
template, route or JavaScript change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class CategorySpec:
    """Display metadata for one discography category."""

    key: str
    label: str
    icon: str
    #: DOM id / anchor suffix for the section.  The standard six keep the ids
    #: they have always had (``albums``, ``live-albums``, …) because page
    #: anchors, the nav list and the toggle-missing handlers all reference them.
    section_id: str


#: Sections that predate this module.  Their KEYS are stable and are what the
#: database ``missing_releases.category`` / template ids already use, so they
#: must not be renamed.  Labels and order are deliberately unchanged so an
#: existing library looks identical after this refactor.
_STANDARD_SPECS: Final[tuple[CategorySpec, ...]] = (
    CategorySpec("album", "Studio Albums", "bi-disc", "albums"),
    CategorySpec("ep", "EPs", "bi-collection", "eps"),
    CategorySpec("single", "Singles", "bi-music-note-beamed", "singles"),
    CategorySpec("compilation", "Compilations", "bi-collection-play", "compilations"),
    CategorySpec("live_album", "Live Albums", "bi-mic", "live-albums"),
    CategorySpec("remix_album", "Remix Albums", "bi-arrow-repeat", "remix-albums"),
)

#: Every remaining MusicBrainz SECONDARY type, keyed by its lowercased
#: MusicBrainz spelling.  Labels follow MusicBrainz's own vocabulary so what the
#: page shows is what the source actually says.
_SECONDARY_SPECS: Final[tuple[tuple[str, CategorySpec], ...]] = (
    ("soundtrack", CategorySpec("soundtrack", "Soundtracks", "bi-film", "soundtracks")),
    ("demo", CategorySpec("demo", "Demo Recordings", "bi-file-earmark-music", "demos")),
    ("field recording", CategorySpec("field_recording", "Field Recordings", "bi-broadcast", "field-recordings")),
    ("dj-mix", CategorySpec("dj_mix", "DJ-mixes", "bi-sliders", "dj-mixes")),
    ("mixtape/street", CategorySpec("mixtape_street", "Mixtapes & Street", "bi-cassette", "mixtapes")),
    ("spokenword", CategorySpec("spokenword", "Spoken Word", "bi-chat-quote", "spoken-word")),
    ("interview", CategorySpec("interview", "Interviews", "bi-question-circle", "interviews")),
    ("audiobook", CategorySpec("audiobook", "Audiobooks", "bi-book", "audiobooks")),
    ("audio drama", CategorySpec("audio_drama", "Audio Dramas", "bi-file-earmark-play", "audio-dramas")),
    ("score", CategorySpec("score", "Scores", "bi-music-note-list", "scores")),
)

#: Catch-all for a secondary type MusicBrainz adds that is not mapped above.
#: Deliberately NOT the studio bucket: an unrecognised type must never claim to
#: be a studio album.
_OTHER_SPEC: Final[CategorySpec] = CategorySpec(
    "other", "Other Releases", "bi-collection", "other-releases"
)

#: key -> spec, for every category the UI can render.
SPECS: Final[dict[str, CategorySpec]] = {
    spec.key: spec for spec in _STANDARD_SPECS
} | {spec.key: spec for _mb, spec in _SECONDARY_SPECS} | {_OTHER_SPEC.key: _OTHER_SPEC}

#: The studio bucket.  Named so callers can express "is this a plain studio
#: album?" without repeating the string.
STUDIO_KEY: Final[str] = "album"

#: Canonical section order.  Standard sections keep their historical positions;
#: everything else follows, with the catch-all last.
ORDERED_KEYS: Final[tuple[str, ...]] = (
    tuple(spec.key for spec in _STANDARD_SPECS)
    + tuple(spec.key for _mb, spec in _SECONDARY_SPECS)
    + (_OTHER_SPEC.key,)
)

_ORDER_INDEX: Final[dict[str, int]] = {key: i for i, key in enumerate(ORDERED_KEYS)}

#: Legacy display labels and bucket keys seen in stored data and call sites.
#: ``missing_releases.category`` holds DISPLAY labels ("Live Album") while the
#: in-library classifier historically returned KEYS ("live_album"), so both
#: spellings must resolve.
_LEGACY_ALIASES: Final[dict[str, str]] = {
    "album": "album",
    "albums": "album",
    "studio": "album",
    "studio album": "album",
    "studio albums": "album",
    "ep": "ep",
    "eps": "ep",
    "extended play": "ep",
    "single": "single",
    "singles": "single",
    "compilation": "compilation",
    "compilations": "compilation",
    "live": "live_album",
    "live album": "live_album",
    "live albums": "live_album",
    "remix": "remix_album",
    "remixes": "remix_album",
    "remix album": "remix_album",
    "remix albums": "remix_album",
    "soundtrack": "soundtrack",
    "soundtracks": "soundtrack",
    "demo": "demo",
    "demo recordings": "demo",
    "field recording": "field_recording",
    "field recordings": "field_recording",
    "dj-mix": "dj_mix",
    "dj mixes": "dj_mix",
    "dj-mixes": "dj_mix",
    "mixtape/street": "mixtape_street",
    "mixtapes & street": "mixtape_street",
    "spokenword": "spokenword",
    "spoken word": "spokenword",
    "interview": "interview",
    "interviews": "interview",
    "audiobook": "audiobook",
    "audiobooks": "audiobook",
    "audio drama": "audio_drama",
    "audio dramas": "audio_drama",
    "score": "score",
    "scores": "score",
    "other": "other",
    "other releases": "other",
}

#: MusicBrainz secondary type -> category key.  Covers ALL secondary types,
#: including the three that map onto standard sections — ``_SECONDARY_SPECS``
#: above only lists the EXTRA ones because live/compilation/remix already have
#: standard specs, but the precedence scan below still needs to resolve them.
_MB_SECONDARY_TO_KEY: Final[dict[str, str]] = {
    "live": "live_album",
    "compilation": "compilation",
    "remix": "remix_album",
    "soundtrack": "soundtrack",
    "demo": "demo",
    "field recording": "field_recording",
    "dj-mix": "dj_mix",
    "mixtape/street": "mixtape_street",
    "spokenword": "spokenword",
    "interview": "interview",
    "audiobook": "audiobook",
    "audio drama": "audio_drama",
    "score": "score",
}

def _slug(value: str) -> str:
    """Lowercase alphanumeric-only key for tolerant type matching.

    MusicBrainz spells a secondary type "Field recording" but composite
    album-type strings and hand-written values variously appear as
    "fieldrecording", "field-recording" or "Field Recording".  Comparing on a
    punctuation- and space-insensitive slug makes all of them resolve to the
    same category instead of one spelling silently landing in the catch-all.
    """
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


#: Slug -> canonical MusicBrainz secondary spelling, so any spelling variant
#: resolves.  Built from the single canonical map above.
_SECONDARY_BY_SLUG: Final[dict[str, str]] = {
    _slug(mb): mb for mb in _MB_SECONDARY_TO_KEY
}


def _resolve_mb_secondary(value: str) -> str | None:
    """Canonical MusicBrainz secondary spelling for *value*, or None."""
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    if raw in _MB_SECONDARY_TO_KEY:
        return raw
    return _SECONDARY_BY_SLUG.get(_slug(raw))


#: Which secondary type wins when a release carries several.
#: ``compilation`` still outranks ``live`` (a "live compilation" is a
#: compilation) — this preserves the behaviour the two pre-existing classifiers
#: already had, so no existing release changes section as a side effect of this
#: refactor.  ``soundtrack`` sits where ``classify_album_type`` had it, before
#: live.  Newly-recognised types are ranked after the established three.
_PRECEDENCE: Final[tuple[str, ...]] = (
    "compilation",
    "soundtrack",
    "live",
    "remix",
    "demo",
    "field recording",
    "dj-mix",
    "mixtape/street",
    "spokenword",
    "interview",
    "audiobook",
    "audio drama",
    "score",
)

#: MusicBrainz PRIMARY types that map straight to a category.
_PRIMARY_KEYS: Final[dict[str, str]] = {
    "album": "album",
    "ep": "ep",
    "single": "single",
}


def normalise_secondary_types(raw: object) -> list[str]:
    """Normalise a MusicBrainz ``secondary-types`` value to lowercase strings.

    MusicBrainz search results can carry the value as a comma-joined STRING
    ("Live,Compilation") rather than a list.  Iterating that string
    character-by-character never matched anything, which is what made every
    live/compilation release fall through to the studio bucket — so a string is
    split on commas rather than treated as an iterable of characters.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return [part.strip().lower() for part in raw.split(",") if part.strip()]
    if isinstance(raw, (list, tuple, set, frozenset)):
        return [
            str(part).strip().lower()
            for part in raw
            if isinstance(part, str) and part.strip()
        ]
    return []


def category_for_musicbrainz(
    primary_type: str | None,
    secondary_types: object = None,
) -> str:
    """Category key for a MusicBrainz release-group.

    The invariant: a release with ANY secondary type is never ``album``.
    """
    primary = str(primary_type or "").strip().lower()
    secondary = normalise_secondary_types(secondary_types)

    # Canonicalise spellings so "fieldrecording" / "Field-Recording" /
    # "Field recording" all reach the same category.
    secondary = [_resolve_mb_secondary(item) or item for item in secondary]

    # Legacy single-value storage puts a SECONDARY spelling where a primary
    # belongs ("live", "remix", "compilation", "soundtrack").  Treat it as the
    # secondary type it is, otherwise it would fall through to the catch-all
    # instead of the section it clearly names.
    primary_secondary = _resolve_mb_secondary(primary)
    if primary_secondary is not None:
        if primary_secondary not in secondary:
            secondary.append(primary_secondary)
        primary = "album"

    # A single/EP is still a single/EP even when it also carries a secondary
    # type — that is the historical precedence and it matches how releases are
    # described ("Live EP" reads as an EP).
    if primary == "single" or "single" in secondary:
        return "single"
    if primary == "ep" or "ep" in secondary:
        return "ep"

    # Secondary type decides the rest, by precedence.
    present = set(secondary)
    for mb_type in _PRECEDENCE:
        if mb_type in present:
            return _MB_SECONDARY_TO_KEY.get(mb_type, _OTHER_SPEC.key)

    # ⚠️ THE INVARIANT.  A release carrying a secondary type we do not have a
    # mapping for must NOT fall through to the studio bucket — that is precisely
    # the bug this module exists to fix, and MusicBrainz keeps adding secondary
    # types.  An unknown type is "something else", never a plain studio album.
    if secondary:
        return _OTHER_SPEC.key

    if primary in _PRIMARY_KEYS:
        return _PRIMARY_KEYS[primary]

    # A non-album/ep/single primary type (Broadcast, Other, …) with no
    # secondary type.  Not a studio album — it is simply something else.
    if primary:
        return _OTHER_SPEC.key

    return STUDIO_KEY


def parse_composite_type(value: str | None) -> tuple[str, list[str]]:
    """Split a stored composite type into ``(primary, [secondary, ...])``.

    Handles both the ``album+live`` and ``album+dj-mix+mixtape/street`` shapes
    the album-type pipeline writes, plus the pre-composite values
    (``album``, ``live``, ``remix``, ``compilation``, ``ep``, ``single``).
    """
    parts = [part.strip().lower() for part in str(value or "").split("+") if part.strip()]
    if not parts:
        return "", []
    return parts[0], parts[1:]


def category_for_album_type(value: str | None) -> str:
    """Category key from a stored album-type string (composite or plain)."""
    primary, secondary = parse_composite_type(value)
    if not primary:
        return STUDIO_KEY
    return category_for_musicbrainz(primary, secondary)


def category_for_album_row(album_row: dict) -> str:
    """Category key for a library album row, preferring its stored type.

    Falls back to the album TITLE only when no type is stored at all, because a
    stored MusicBrainz type is authoritative and a title heuristic must not
    override it.
    """
    raw_type = str(
        album_row.get("musicbrainz_albumtype")
        or album_row.get("spotify_album_type")
        or album_row.get("album_type")
        or ""
    ).strip()

    if raw_type:
        return category_for_album_type(raw_type)

    title = str(album_row.get("album") or "")
    if title:
        try:
            from services.catalog.album_classification_service import (
                detect_greatest_hits_album,
                is_live_album_enhanced,
            )
        except Exception:  # pragma: no cover - defensive
            detect_greatest_hits_album = None
            is_live_album_enhanced = None

        lower = title.lower()
        if (detect_greatest_hits_album and detect_greatest_hits_album(title, "")) or (
            is_live_album_enhanced and is_live_album_enhanced(title)
        ) or "unplugged" in lower or "in concert" in lower:
            return "live_album"
        if "compilation" in lower:
            return "compilation"
        if "soundtrack" in lower:
            return "soundtrack"
        if "remix" in lower:
            return "remix_album"

    return STUDIO_KEY


def normalise_category(value: str | None) -> str:
    """Resolve a stored/spelled category to a canonical key.

    Accepts canonical keys, legacy display labels ("Live Album"), and the
    composite type spellings — so rows written before this module existed keep
    working instead of silently falling into the studio bucket.  An
    unrecognised value resolves to ``other``, never to ``album``.
    """
    raw = str(value or "").strip()
    if not raw:
        return STUDIO_KEY

    lower = raw.lower()
    if lower in SPECS:
        return lower
    if lower in _LEGACY_ALIASES:
        return _LEGACY_ALIASES[lower]

    # A composite type ("album+dj-mix+mixtape/street") or a bare MusicBrainz
    # type we recognise but have no alias for.
    if "+" in raw:
        return category_for_album_type(raw)

    # A bare MusicBrainz secondary spelling, in any punctuation variant
    # ("fieldrecording", "Field-Recording").  Resolved through the same slug
    # lookup the classifier uses so a stored value cannot land in the
    # catch-all merely because of how it was spelled.
    resolved = _resolve_mb_secondary(raw)
    if resolved is not None:
        return _MB_SECONDARY_TO_KEY[resolved]

    return _OTHER_SPEC.key


def spec_for(key: str | None) -> CategorySpec:
    """Spec for a category key, falling back to the catch-all."""
    return SPECS.get(normalise_category(key), _OTHER_SPEC)


def label_for(key: str | None) -> str:
    return spec_for(key).label


def icon_for(key: str | None) -> str:
    return spec_for(key).icon


def order_index(key: str | None) -> int:
    """Sort position for a category key (unmapped keys sort last)."""
    return _ORDER_INDEX.get(normalise_category(key), len(ORDERED_KEYS))


def ordered_specs(keys: object) -> list[CategorySpec]:
    """Specs for the given keys, in canonical section order.

    This is what the templates iterate, so section order is decided here rather
    than by whatever order the database returned rows in.  Unknown keys resolve
    to the catch-all rather than being dropped, so an album can never disappear
    from the page.
    """
    canonical: list[str] = []
    seen: set[str] = set()
    for key in keys or ():
        normalised = normalise_category(str(key))
        if normalised not in seen:
            seen.add(normalised)
            canonical.append(normalised)
    canonical.sort(key=order_index)
    return [SPECS[key] for key in canonical]


__all__ = [
    "CategorySpec",
    "ORDERED_KEYS",
    "SPECS",
    "STUDIO_KEY",
    "category_for_album_row",
    "category_for_album_type",
    "category_for_musicbrainz",
    "icon_for",
    "label_for",
    "normalise_category",
    "normalise_secondary_types",
    "order_index",
    "ordered_specs",
    "parse_composite_type",
    "spec_for",
]
