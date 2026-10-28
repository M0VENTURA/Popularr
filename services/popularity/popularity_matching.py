"""Artist/title matching helpers for popularity provider lookups."""

from __future__ import annotations

import re
import unicodedata

from helpers.normalization_service import (
    FEAT_SUFFIX_RE,
    strip_cover_attribution,
    strip_diacritics,
    strip_remaster_qualifier,
    version_tokens_in_descriptor,
)

ARTIST_JOIN_RE = re.compile(
    r"""
    \s+
    (?:&|and|x|×|\+)
    \s+
    """,
    re.IGNORECASE | re.VERBOSE,
)


def clean_artist_spacing(value: str) -> str:
    """Clean whitespace while preserving casing for provider lookups."""
    return re.sub(r"\s+", " ", (value or "").strip())


# Hard version markers that distinguish a DIFFERENT take of a song from its
# canonical recording.  A "(Live)" / "(Acoustic)" / "(Instrumental)" /
# "(Orchestral)" / "(Remix)" / "(Demo)" version must never inherit the
# canonical track's Last.fm / ListenBrainz popularity — the two are different
# performances with different real audiences.
#
# ⚠️ "remaster" / "remastered" are DELIBERATELY NOT here. A remaster is a
# mastering-engineering pass on the exact same studio performance — the
# listener pressing "Higher (remastered 2024)" is listening to the flagship
# studio song "Higher", and the audience is the SAME audience. MusicBrainz
# mints a new Recording for it (the audio fingerprint and mastering credits
# changed), which splits the ListenBrainz playcount across two recordings and
# leaves the remaster with a few hundred plays against the original's
# hundreds of thousands. Treating the marker as a hard variant is what
# stranded the remastered track on its own tiny count.
#
# So a remastered title is COMPATIBLE with its canonical recording, and the
# remaster's figures resolve to the canonical studio recording.
#
# ⚠️ This is NOT a mirror of
# ``services/queue/queue_matching_config.TITLE_VARIANT_TOKENS`` — that set
# governs whether two FILENAME forms refer to the same downloadable file,
# where "remaster" IS a different file. This set governs whether two
# RECORDINGS are the same PERFORMANCE for popularity aggregation, where a
# remaster is the same performance and "remaster" is deliberately absent.
_HARD_VARIANT_TOKENS = frozenset({
    "acoustic", "demo", "instrumental", "intro", "live",
    "mix", "orchestral", "remix",
})
_SOFT_VARIANT_TOKENS = frozenset({"version", "edit", "radio"})


def _extract_variant_tokens(title: str) -> frozenset[str]:
    """Return the hard/soft version-marker tokens present in a title."""
    if not title:
        return frozenset()
    words = set(re.sub(r"[^a-z0-9]+", " ", str(title).lower()).split())
    return frozenset(words & (_HARD_VARIANT_TOKENS | _SOFT_VARIANT_TOKENS))


def title_variants_compatible(title_a: str, title_b: str) -> bool:
    """True when two titles carry COMPATIBLE version markers.

    A hard variant marker (live / acoustic / instrumental / orchestral /
    remix / demo / remaster) must align between the two titles: a
    "(Live)" version is a different performance from the plain track, so
    they must never be treated as the same recording for popularity
    aggregation.  Soft markers ("version", "edit", "radio") may be absent
    from either side.

    Examples:
        title_variants_compatible("See You in Hell", "See You in Hell (Live)")  -> False
        title_variants_compatible("See You in Hell (Live)", "See You in Hell (Live)") -> True
        title_variants_compatible("Hypa Hypa (Remix)", "Hypa Hypa")  -> False
        title_variants_compatible("Herzblut", "Herzblut (Radio Edit)")  -> True  # soft
        title_variants_compatible("Higher", "Higher (remastered 2024)")  -> True  # same performance
    """
    va = _extract_variant_tokens(title_a) - _SOFT_VARIANT_TOKENS
    vb = _extract_variant_tokens(title_b) - _SOFT_VARIANT_TOKENS
    if not va and not vb:
        return True
    if not va or not vb:
        return False
    return bool(va & vb)


#: Gimmick re-recording suffixes.  These are the markers that name a DIFFERENT
#: PERFORMANCE without using any version keyword at all, so neither
#: ``_HARD_VARIANT_TOKENS`` nor ``normalize_for_aggregation``'s bracket sweep
#: sees them.  Matched as a phrase on a normalised key, and written with both
#: spellings because ``normalize_for_aggregation`` folds diacritics ("für hunde"
#: becomes "fur hunde") while other callers pass raw text.
_NOVELTY_PHRASE_RE = re.compile(r"\b(?:for dogs|f[üu]r hunde)\b", re.IGNORECASE)


def aggregation_keys_compatible(key_a: str, key_b: str, *, min_similarity: float = 0.90) -> bool:
    """True when two ``normalize_for_aggregation`` keys denote the same RECORDING.

    ⭐ This is the gate that stops a base track being summed into a novelty
    re-recording of it.  It exists because the obvious gate does not work:
    aggregation compared keys with ``fuzzy_match_score``, which is
    ``rapidfuzz.token_set_ratio`` — and that metric returns **1.0 whenever one
    token set is a SUBSET of the other**, by design, since "all the important
    words are present" is exactly what it measures.  A ``>= 0.90`` threshold
    therefore ACCEPTED every strict subset, so

        "fucked with an anchor"  vs  "fucked with an anchor for dogs"

    scored a perfect 1.0 and the original studio track's 84,200 listeners were
    summed into its novelty re-recording (and vice versa — the metric is
    symmetric).  The threshold could never reject the case it was written to
    catch.

    ⚠️ "The candidate has extra tokens" is NOT itself the discriminator.  A
    ``token_set_ratio`` subset match is also how the aggregator legitimately
    collects a track's session/provenance-annotated spellings — Last.fm files
    "100 Words (from Sing the Sorrow sessions 2003)" as a separate row from
    "100 Words", and those two ARE the same recording, so they must still sum.

    ⚠️ Nor is this the place to enforce version-marker agreement.  That is
    already done upstream by ``title_variants_compatible`` and
    ``_alt_status_matches_ctx``, which are CONTEXT-AWARE — they fold in the
    release-level liveness and the caller's ``target_is_alt_rendition``, so a
    plainly titled track on an unplugged release still matches its unplugged
    counterpart.  An equivalent check here would be a context-free duplicate of
    that logic, and it rejected exactly that case (an alternate rendition whose
    own title carries no marker) when it was tried.

    So this checks the ONE thing nothing upstream can see: a novelty phrase,
    which names a different performance without using any version keyword at
    all, so neither the marker sets nor the bracket sweep in
    ``normalize_for_aggregation`` will ever spot it.

    The phrase list is a blocklist, not a general solution: an unlisted gimmick
    suffix will still aggregate.  That is stated rather than hidden — every
    entry is a real, observed case, and widening it speculatively risks
    misclassifying real album tracks, which is the worse error.
    """
    if not key_a or not key_b:
        return False
    if key_a == key_b:
        return True

    tokens_a = set(key_a.split())
    tokens_b = set(key_b.split())
    if tokens_a == tokens_b:
        return True

    # A novelty phrase on ONE side only: one of the two is a gimmick
    # re-recording, so the pair is not the same recording.
    if bool(_NOVELTY_PHRASE_RE.search(key_a)) != bool(_NOVELTY_PHRASE_RE.search(key_b)):
        return False

    from services.popularity.popularity_math import fuzzy_match_score

    return fuzzy_match_score(key_a, key_b) >= min_similarity


def build_artist_variants(artist: str) -> list[str]:
    """Generate artist name variants (main artist, featured artists, combinations)."""
    variants: set[str] = set()
    value = clean_artist_spacing(artist)
    if not value:
        return []
    variants.add(value)

    for pattern in [r"\s+feat\.\s+", r"\s+ft\.\s+", r"\s+featuring\s+"]:
        if re.search(pattern, value, flags=re.I):
            parts = re.split(pattern, value, maxsplit=1, flags=re.I)
            main, featured = parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""
            if main:
                variants.add(main)
            if featured:
                variants.add(featured)
            if main and featured:
                variants.add(f"{main} & {featured}")

    return sorted(v for v in variants if v)


def get_primary_artist_preserve_case(artist: str) -> str:
    """Return likely primary artist while preserving original casing."""
    if not artist:
        return ""
    return clean_artist_spacing(FEAT_SUFFIX_RE.split(artist, maxsplit=1)[0])


def get_artist_lookup_candidates(artist: str, album_artist: str | None = None) -> list[str]:
    """Build provider lookup candidates in preferred order."""
    candidates: list[str] = []
    seen: set[str] = set()

    def add(value: str | None):
        value = clean_artist_spacing(value or "")
        key = value.casefold()
        if value and key not in seen:
            candidates.append(value)
            seen.add(key)

    add(artist)
    add(get_primary_artist_preserve_case(artist))
    add(album_artist)
    add(get_primary_artist_preserve_case(album_artist or ""))
    # Collab / multi-artist credits ("BABYMETAL & Electric Callboy",
    # "A x B", "A and B"): each sub-artist may index the same track under
    # its own catalogue, so query each part individually too.
    if ARTIST_JOIN_RE.search(artist or ""):
        for part in ARTIST_JOIN_RE.split(artist or ""):
            add(part.strip())
    if album_artist and ARTIST_JOIN_RE.search(album_artist or ""):
        for part in ARTIST_JOIN_RE.split(album_artist or ""):
            add(part.strip())
    return candidates


def make_artist_match_key(artist: str) -> str:
    """Internal-only canonical artist key for matching/cache grouping."""
    artist = get_primary_artist_preserve_case(artist)
    artist = unicodedata.normalize("NFKC", artist)
    artist = artist.casefold()
    # NFKC alone does NOT remove combining marks, so "Lïve" kept its umlaut and
    # produced a different key from "Live" — the two spellings then occupied
    # separate cache buckets and each missed the other's data.
    artist = strip_diacritics(artist)
    return re.sub(r"\s+", " ", artist).strip()


def make_track_match_key(artist: str, title: str) -> str:
    """Canonical key for combining variants of the same song."""
    artist_key = make_artist_match_key(artist)
    title_key = unicodedata.normalize("NFKC", title or "").casefold()
    title_key = strip_diacritics(title_key)
    title_key = re.sub(r"\s+", " ", title_key).strip()
    return f"{artist_key}::{title_key}"


def normalize_for_aggregation(title: str) -> str:
    """Aggressively normalise title for local provider-count aggregation."""
    value = str(title or "").lower()
    # Fold diacritics FIRST.  Without this the ``[^a-z0-9]+`` sweep below turns
    # the combining mark into a SPACE and splits the word: "Café" -> "caf ",
    # "Hoppípolla" -> "hopp polla".  The provider's catalogue holds the ASCII
    # spelling, so neither key could ever match and the track silently fell
    # through to a low-count fallback.
    value = strip_diacritics(value)
    # Strip trailing cover attributions ("(PSY Cover)", "[Foo Cover]") so a
    # cover track correlates with its canonical Last.fm row — the popular
    # "Gangnam Style" single vs the low-listen "Gangnam Style (PSY Cover)"
    # album row are the same song and must collapse to one key.
    value = strip_cover_attribution(value)
    # Remove ONLY mastering/reissue wording. The old single regex removed an
    # ENTIRE bracket whenever it contained "remaster", so
    # "Song (Live - 2011 Remaster)" became "song" and could inherit the studio
    # recording's audience. ``strip_remaster_qualifier`` keeps "Live" (and every
    # other protected descriptor) while dropping the mastering half.
    value = strip_remaster_qualifier(value)

    def _drop_bracket_unless_protected(match: re.Match[str]) -> str:
        # A soft annotation ("feat. X", "Album Version") is safe to drop, but
        # a combined one ("Live - Album Version") names a DIFFERENT
        # performance and must survive.
        text = match.group(0)
        return text if version_tokens_in_descriptor(text) else ""

    value = re.sub(
        r"\s*[\(\[][^)\]]*(feat\.|featuring|ft\.|single version|album version)[^)\]]*[)\]]",
        _drop_bracket_unless_protected,
        value,
        flags=re.IGNORECASE,
    )

    def _drop_dash_unless_protected(match: re.Match[str]) -> str:
        return match.group(0) if version_tokens_in_descriptor(match.group(1)) else ""

    value = re.sub(
        r"\s*-\s*((?:single version|album version).*)$",
        _drop_dash_unless_protected,
        value,
        flags=re.IGNORECASE,
    )
    # Unparenthesised "feat. Guest" / "featuring Guest" suffixes — the album
    # version of a song is frequently stored without brackets, and correlating
    # it with the bracket-carrying single requires both forms to collapse to
    # the same key (legacy parity with old_system/popularity_helpers.py).
    def _drop_feat_suffix_unless_protected(match: re.Match[str]) -> str:
        return match.group(0) if version_tokens_in_descriptor(match.group(1)) else ""

    value = re.sub(
        r"\s+(?:feat\.?|ft\.?|featuring)\s+(.*)$",
        _drop_feat_suffix_unless_protected,
        value,
        flags=re.IGNORECASE,
    )
    # Drop dashes that are NOT space-delimited ("Ph4/NT0‐mA" → "Ph4/NT0mA",
    # unicode/ASCII variants) — some sources omit the separator entirely, so
    # both variants must collapse to the same key.  Space-delimited dashes
    # ("Foo - Bar") stay separators.
    value = re.sub(r"(?<=\S)[\u2010\u2011\u2012\u2013\u2014\u2015\u2212-](?=\S)", "", value)
    # ⭐ Keep NON-ASCII letters and digits. ``[^a-z0-9]`` deleted every
    # non-Latin character, so a Cyrillic or CJK title normalised to the EMPTY
    # string — and every such title on one album then collided on that single
    # key. Measured: Arkona's 14 Cyrillic tracks all keyed to "" and shared ONE
    # prefetched ListenBrainz slot, so the same count (109,751) was read on
    # every row and the album's whole distribution flattened. ``\w`` is
    # Unicode-aware (letters/digits/underscore), so distinct scripts now keep
    # distinct keys. ``_`` is folded to a separator first so its handling is
    # unchanged.
    value = value.replace("_", " ")
    value = re.sub(r"[^\w]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def choose_best_provider_counts(results: list[dict]) -> dict:
    """Pick strongest provider result across artist/title variants."""
    if not results:
        return {}
    def score(item: dict):
        return (
            int(item.get("listeners", 0) or 0),
            int(item.get("playcount", item.get("track_play", 0)) or 0),
            int(item.get("listen_count", item.get("total_listen_count", 0)) or 0),
        )
    return sorted(results, key=score, reverse=True)[0]
