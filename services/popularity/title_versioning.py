"""Central popularity-title classification.

Separates four concepts that the provider lookup must not conflate:

* ``display_title`` — exactly what the library row shows.
* ``canonical_song_title`` — the song with recognised version descriptors
  removed (``"Song (Live at Wembley)"`` -> ``"Song"``).
* ``version_descriptor`` — the removed descriptor (``"Live at Wembley"``).
* ``popularity_lookup_title`` — the title actually sent to a provider.

The only automatic cross-title fallback this module authorises is the SAFE
remaster one: a title whose trailing mastering/reissue wording can be removed
WITHOUT removing a protected version descriptor may also be looked up under
that stripped title. A protected version (live/acoustic/demo/remix/edit/...)
never falls back to the bare song title.

``canonical_song_title`` is therefore NOT a popularity-equivalence key. It is
exposed for diagnostics and for callers that need the base song, but the
lookup candidates are the authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from helpers.normalization_service import (
    has_mastering_marker,
    normalize_unicode_punctuation,
    protected_version_tokens,
    strip_diacritics,
    strip_remaster_qualifier,
    version_tokens_in_descriptor,
)

#: Trailing bracketed qualifier (opener, inner, closer).
_TRAILING_QUALIFIER_RE = re.compile(r"\s*([(\[])([^)\]]*?)\s*([)\]])\s*$")
_TRAILING_DASH_RE = re.compile(r"\s+[-\u2013\u2014]\s+(.+)$")


def _strip_protected_version_suffixes(title: str) -> str:
    """Remove trailing annotations that name a protected version."""
    value = str(title or "").strip()
    while True:
        match = _TRAILING_QUALIFIER_RE.search(value)
        if match and version_tokens_in_descriptor(match.group(2)):
            value = value[: match.start()].strip()
            continue
        break

    dash = _TRAILING_DASH_RE.search(value)
    if dash and version_tokens_in_descriptor(dash.group(1)):
        value = value[: dash.start()].strip()
    return value


@dataclass(frozen=True)
class PopularityTitle:
    display_title: str
    canonical_song_title: str
    version_descriptor: str
    mastering_stripped_title: str
    protected_versions: frozenset[str]

    @property
    def has_protected_version(self) -> bool:
        return bool(self.protected_versions)

    def lookup_titles(self) -> tuple[tuple[str, str], ...]:
        """Ordered ``(title, match_type)`` candidates sent to providers.

        Exact first. The canonical remaster fallback is added ONLY when a
        remaster marker is present AND stripping it preserved every protected
        version descriptor.
        """
        candidates: list[tuple[str, str]] = [(self.display_title, "exact")]
        stripped = self.mastering_stripped_title
        if (
            has_mastering_marker(self.display_title)
            and stripped
            and stripped.casefold() != self.display_title.casefold()
        ):
            candidates.append((stripped, "canonical_remaster_fallback"))
        return tuple(candidates)


def classify_popularity_title(title: str) -> PopularityTitle:
    display = str(title or "").strip()
    if not display:
        return PopularityTitle("", "", "", "", frozenset())

    protected = protected_version_tokens(display)
    mastering_stripped = strip_remaster_qualifier(display)
    canonical = _strip_protected_version_suffixes(mastering_stripped)

    descriptor = ""
    if canonical and canonical.casefold() != display.casefold():
        descriptor = display[len(canonical):].strip(" -\u2013\u2014") if display.casefold().startswith(canonical.casefold()) else ""
    return PopularityTitle(
        display_title=display,
        canonical_song_title=canonical,
        version_descriptor=descriptor,
        mastering_stripped_title=mastering_stripped,
        protected_versions=protected,
    )


def popularity_lookup_titles(title: str) -> tuple[tuple[str, str], ...]:
    """Ordered provider lookup titles for *title* (exact, then safe remaster)."""
    return classify_popularity_title(title).lookup_titles()


def normalized_title_key(title: str) -> str:
    """Case/punctuation/diacritic-folded title, WITH mastering wording."""
    value = normalize_unicode_punctuation(str(title or "").lower())
    value = strip_diacritics(value)
    return " ".join(value.split())


def remaster_performance_key(title: str) -> str:
    """Dedupe key that treats mastering variants as ONE provider entity.

    Unlike :func:`services.popularity.popularity_matching.normalize_for_aggregation`,
    this deliberately PRESERVES ``feat.``/album-version distinctions, because
    those are separate provider buckets the aggregator has always summed.
    It removes only mastering/reissue wording, so "Orpheus" and
    "Orpheus (2011 Remaster)" share a key while "Song" and
    "Song (feat. X)" do not.
    """
    return " ".join(strip_remaster_qualifier(normalized_title_key(title)).split())
