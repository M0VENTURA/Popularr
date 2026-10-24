# Rebuilt provider wrappers, revision 2.
# Exported under a distinct filename to distinguish this replacement.
"""Popularity provider acquisition and conservative recording matching.

This module does not decide star ratings or single status. Release recording
identity is preserved: a remaster label or a later release date is not evidence
that a recording MBID should be replaced by one from an earlier edition.

Aggregated user/listener counts are sums of provider buckets, NOT deduplicated
people. Public function names and existing positional arguments are preserved.
New optional alternate-rendition arguments are appended for compatibility.
"""
from __future__ import annotations

import math
import re
import threading
import time
from collections import Counter
from typing import Any, TypedDict

import structlog

from api_clients.lastfm import LastFmClient
from api_clients.listenbrainz import (
    get_recording_popularity_batch as lb_get_recording_popularity_batch,
    get_listenbrainz_popularity as lb_get_listenbrainz_popularity,
    get_listenbrainz_score as lb_get_listenbrainz_score,
    get_release_metadata_batch as lb_get_release_metadata_batch,
)
from helpers.normalization_service import (
    normalize_unicode_punctuation,
    strip_cover_attribution,
    strip_diacritics,
    strip_featured_artist,
    REMASTER_MARKER_RE,
    titles_match_for_review,
)
from services.popularity.popularity_matching import (
    ARTIST_JOIN_RE,
    aggregation_keys_compatible,
    choose_best_provider_counts,
    get_artist_lookup_candidates,
    get_primary_artist_preserve_case,
    normalize_for_aggregation,
    title_variants_compatible,
)

logger = structlog.get_logger(__name__)
DEFAULT_LISTENBRAINZ_BATCH_SIZE = 100
_CACHE_TTL = 3600.0
_NEGATIVE_CACHE_TTL = 60.0
_CACHE_MAX_ENTRIES = 2048
_CACHE_LOCK = threading.Lock()
# Values are (expiry, payload). Network calls never run under _CACHE_LOCK.
_lastfm_artist_catalog_cache: dict[Any, tuple[float, Any]] = {}
_lastfm_artist_max_cache: dict[Any, tuple[float, Any]] = {}
_ONLINE_CATALOGUE_CACHE: dict[Any, tuple[float, Any]] = {}
_work_recordings_cache: dict[Any, tuple[float, Any]] = {}


class AlbumRecordingMatch(TypedDict, total=False):
    listenbrainz_listens: int
    listenbrainz_users: int
    recording_mbid: str | None
    release_track_title: str
    local_disc_number: int
    local_track_number: int
    length_verified: bool
    title_is_unique: bool
    matched_by: str


def _cache_get(cache: dict, key: Any) -> Any:
    now = time.monotonic()
    with _CACHE_LOCK:
        item = cache.get(key)
        if item is None:
            return None
        expiry, value = item
        if expiry <= now:
            cache.pop(key, None)
            return None
        return value


def _cache_put(cache: dict, key: Any, value: Any) -> None:
    ttl = _CACHE_TTL if value else _NEGATIVE_CACHE_TTL
    now = time.monotonic()
    with _CACHE_LOCK:
        if len(cache) >= _CACHE_MAX_ENTRIES:
            for expired in [k for k, (expiry, _) in cache.items() if expiry <= now]:
                cache.pop(expired, None)
            if len(cache) >= _CACHE_MAX_ENTRIES:
                cache.pop(next(iter(cache)))
        cache[key] = (now + ttl, value)


def clear_provider_memory_caches() -> None:
    """Clear this module's caches, for example after provider configuration changes."""
    with _CACHE_LOCK:
        for cache in (_lastfm_artist_catalog_cache, _lastfm_artist_max_cache,
                      _ONLINE_CATALOGUE_CACHE, _work_recordings_cache):
            cache.clear()


def _parse_number(value: Any, default: int = 0) -> int:
    try:
        return int(str(value if value not in (None, "") else default)
                   .split("/", 1)[0].strip() or default)
    except (TypeError, ValueError, OverflowError):
        return default


def _count(value: Any) -> int:
    return max(0, _parse_number(value))


def _duration(value: Any) -> float:
    try:
        result = float(value or 0)
        return result if math.isfinite(result) and result > 0 else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _length_agreement(local_seconds: Any, release_ms: Any) -> bool | None:
    local = _duration(local_seconds)
    remote = _duration(release_ms)
    if not local or not remote:
        return None
    return abs(local * 1000 - remote) <= 5000


def _artist_cache_key(value: str) -> str:
    # Do not strip diacritics here: different artist names must not share a
    # bucket merely because transliteration makes their spellings identical.
    return normalize_unicode_punctuation(str(value or "")).casefold().strip()


def _token_similarity(a: str, b: str) -> float:
    from services.popularity.popularity_math import fuzzy_match_score
    return float(fuzzy_match_score(a, b))


def _shared_mb(client: Any = None) -> Any:
    if client is not None:
        return client
    try:
        from services.enrichment.musicbrainz_service import get_shared_mb_client
        return get_shared_mb_client()
    except Exception as exc:
        logger.debug("MusicBrainz client unavailable", error=str(exc))
        return None


def _unique_ids(values: Any) -> list[str]:
    return list(dict.fromkeys(str(v).strip() for v in values if v and str(v).strip()))


def _lb_batch(mbids: Any) -> dict[str, dict[str, Any]]:
    """Deduplicate, chunk, and retain successful chunks after partial failures."""
    ids = _unique_ids(mbids)
    output: dict[str, dict[str, Any]] = {}
    for index in range(0, len(ids), DEFAULT_LISTENBRAINZ_BATCH_SIZE):
        chunk = ids[index:index + DEFAULT_LISTENBRAINZ_BATCH_SIZE]
        try:
            result = lb_get_recording_popularity_batch(chunk) or {}
            if isinstance(result, dict):
                output.update({k: v for k, v in result.items() if isinstance(v, dict)})
        except Exception as exc:
            logger.debug("ListenBrainz batch lookup failed", size=len(chunk), error=str(exc))
    return output


def _sum_counts(counts: dict, mbids: Any) -> tuple[int, int]:
    entries = [counts.get(mbid) or {} for mbid in _unique_ids(mbids)]
    return (sum(_count(e.get("total_listen_count")) for e in entries),
            sum(_count(e.get("total_user_count")) for e in entries))


_FEATURED_ARTIST_RE = re.compile(
    r"^(.*?)\s+(?:feat\.?|ft\.?|featuring)\s+(.*)$", re.IGNORECASE,
)


def invert_featured_artist(artist_name: str) -> str:
    match = _FEATURED_ARTIST_RE.match(artist_name or "")
    if not match:
        return artist_name or ""
    primary, featured = (part.strip() for part in match.groups())
    return f"{featured} feat. {primary}" if primary and featured else artist_name


def _is_featured_artist(artist_name: str) -> bool:
    return bool(_FEATURED_ARTIST_RE.match(artist_name or ""))


# Restrict markers to version annotations, not arbitrary words in song titles.
_ANNOTATION_RE = re.compile(r"[\[(]([^\])]+)[\])]|\s+[-\u2013\u2014]\s+(.+)$")
_PERFORMANCE_PATTERNS = {
    "live": r"\blive\b",
    "acoustic": r"\b(?:acoustic|unplugged|stripped)\b",
    "orchestral": r"\b(?:orchestral|orchestra|symphonic)\b",
    "demo": r"\bdemo\b",
    "instrumental": r"\binstrumental\b",
    "karaoke": r"\bkaraoke\b",
    "remix": r"\b(?:remix|club mix|extended mix|dub mix)\b",
    "edit": r"\b(?:radio edit|single edit|extended edit|edit)\b",
    "alternate": r"\b(?:alternate|alternative take|alt|take)\b",
    "session": r"\b(?:session|rehearsal|jam[- ]along)\b",
    "rerecording": r"\bre[- ]?record(?:ed|ing)\b",
    "piano": r"\bpiano version\b",
}
_PERFORMANCE_RES = {key: re.compile(value, re.I)
                    for key, value in _PERFORMANCE_PATTERNS.items()}
_ALTERNATE_PERFORMANCE_RE = re.compile(
    r"[\[(][^\])]*\b(?:live|unplugged|acoustic|orchestral|symphonic|demo|"
    r"instrumental|karaoke|remix|alternate|alt|take|session|rehearsal|jam[- ]along)"
    r"\b[^\])]*[\])]|\s+-\s*(?:live|unplugged|acoustic|orchestral|symphonic|"
    r"demo|instrumental|karaoke|remix|alternate|alt|take|session|rehearsal|jam[- ]along)\s*$",
    re.I,
)


def _performance_types(title: str) -> frozenset[str]:
    annotations = " ".join((m.group(1) or m.group(2) or "")
                           for m in _ANNOTATION_RE.finditer(str(title or "")))
    return frozenset(key for key, pattern in _PERFORMANCE_RES.items()
                     if pattern.search(annotations))


def _is_alternate_performance_title(rec_title: str) -> bool:
    return bool(_performance_types(rec_title))


def _alt_status_matches(target_title: str, candidate_title: str) -> bool:
    return _alt_status_matches_ctx(target_title, candidate_title)


def _alt_status_matches_ctx(
    target_title: str, candidate_title: str, *, target_is_live: bool = False,
    target_is_alt_rendition: bool = False,
) -> bool:
    target = set(_performance_types(target_title))
    candidate = set(_performance_types(candidate_title))
    if target_is_live:
        target.add("live")
    if target:
        return target == candidate
    if target_is_alt_rendition:
        # A boolean says the version is alternate, but not WHICH alternate.
        # Refuse speculative aggregation until a version-bearing title is supplied.
        return False
    return not candidate


def _recording_title(recording: dict[str, Any]) -> str:
    """Include recording disambiguation, where MB can store live information."""
    title = str(recording.get("title") or "").strip()
    comment = str(recording.get("disambiguation") or "").strip()
    return f"{title} ({comment})" if comment else title


_CREDIT_SPLIT_RE = re.compile(
    r"\s*(?:,|&|/|\+|\u00d7|\band\b|\bwith\b|\bvs\b\.?|\bversus\b|"
    r"\bfeat(?:uring)?\b\.?|\bft\b\.?)\s*", re.I,
)
_CREDIT_JOIN_TOKENS = frozenset({"feat", "feats", "featured", "featuring", "ft",
                                  "with", "vs", "versus", "and"})


def _resolve_credit_alias(part: str) -> str:
    if not part:
        return part
    try:
        from services.metadata.artist_alias_service import resolve_artist_alias
        return str(resolve_artist_alias(part) or part)
    except Exception:
        return part


def _credit_identity_tokens(value: str) -> frozenset[str]:
    # Preserve Unicode and single-character names. Do not drop 'the' or 'x'.
    tokens: set[str] = set()
    for part in _CREDIT_SPLIT_RE.split(str(value or "")):
        part = normalize_unicode_punctuation(_resolve_credit_alias(part.strip()))
        tokens.update(token for token in re.findall(r"[^\W_]+", part.casefold())
                      if token not in _CREDIT_JOIN_TOKENS)
    return frozenset(tokens)


def _credit_name(credit: Any) -> str:
    if not isinstance(credit, dict):
        return ""
    artist = credit.get("artist") or {}
    return str(credit.get("name") or
               (artist.get("name") if isinstance(artist, dict) else "") or "").strip()


def _release_credit_text(track: dict[str, Any]) -> str:
    credits = track.get("artist-credit") or []
    if not credits and isinstance(track.get("recording"), dict):
        credits = track["recording"].get("artist-credit") or []
    return "".join(_credit_name(c) + str(c.get("joinphrase") or "")
                   for c in credits if isinstance(c, dict) and _credit_name(c)).strip()


def _first_credit_name(recording: dict[str, Any]) -> str:
    for credit in recording.get("artist-credit") or []:
        name = _credit_name(credit)
        if name:
            return name
    return ""


def _normalize_artist(name: str) -> str:
    return _artist_cache_key(get_primary_artist_preserve_case(name))


def _artist_matches(target: str, candidate: str) -> bool:
    left = _credit_identity_tokens(target)
    right = _credit_identity_tokens(candidate)
    return bool(left and right and left == right)


def _search_item_artist(item: dict[str, Any]) -> str:
    artist = item.get("artist")
    if isinstance(artist, dict):
        return str(artist.get("name") or "").strip()
    return str(artist or "").strip()


def resolve_isrc_recording(
    isrc: str, *, mb_client: Any = None, title: str = "", artist: str = "",
    is_live_release: bool = False, target_is_alt_rendition: bool = False,
) -> dict[str, str | None] | None:
    from helpers.normalization_service import normalize_isrc
    try:
        normalized = str(normalize_isrc(str(isrc or "")) or "").strip().upper()
        normalized = normalized.replace("-", "").replace(" ", "")
        if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}", normalized):
            return None
        client = _shared_mb(mb_client)
        if client is None:
            return None
        recordings = client.lookup_by_isrc(normalized) or []
        candidates: list[tuple[float, dict[str, Any]]] = []
        for rec in recordings:
            if not isinstance(rec, dict) or not rec.get("id"):
                continue
            if artist and not _artist_matches(artist, _release_credit_text(rec)):
                continue
            if (title or is_live_release or target_is_alt_rendition) and not _alt_status_matches_ctx(
                title, _recording_title(rec), target_is_live=is_live_release,
                target_is_alt_rendition=target_is_alt_rendition,
            ):
                continue
            score = 1.0
            if title:
                left = normalize_for_aggregation(title)
                right = normalize_for_aggregation(str(rec.get("title") or ""))
                if not left or not right or not aggregation_keys_compatible(left, right):
                    continue
                score = _token_similarity(left, right)
                if score < 0.90:
                    continue
            candidates.append((score, rec))
        candidates.sort(key=lambda pair: (-pair[0], str(pair[1].get("id"))))
        if not candidates:
            return None
        # ISRCs can be attached to multiple recordings. Do not resolve a tie by order.
        best_score, best = candidates[0]
        if any(abs(score - best_score) < 0.02 and rec["id"] != best["id"]
               for score, rec in candidates[1:]):
            return None
        return {"recording_mbid": str(best["id"]),
                "title": str(best.get("title") or "").strip(),
                "artist": _first_credit_name(best)}
    except Exception as exc:
        logger.debug("ISRC lookup failed", isrc=isrc, error=str(exc))
        return None


def extract_recording_mbid(track: dict[str, Any]) -> str | None:
    for key in ("recording_mbid", "musicbrainz_recording_mbid", "mbid"):
        value = track.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def get_listenbrainz_batch_for_tracks(tracks: list[dict[str, Any]]) -> dict[str, dict[str, int | None]]:
    return _lb_batch(extract_recording_mbid(track) for track in tracks)


def _resolve_release_mbid(artist: str, album: str, tracks: list[dict[str, Any]]) -> str:
    stored = _unique_ids(t.get("musicbrainz_albumid") or t.get("musicbrainz_album_mbid")
                         for t in tracks)
    if len(stored) == 1:
        return stored[0]
    if len(stored) > 1:
        logger.warning("Conflicting release MBIDs in local album", artist=artist, album=album)
        return ""
    try:
        from api_clients.musicbrainz_http import escape_lucene_special_chars
        client = _shared_mb()
        if client is None:
            return ""
        query = (f'artist:"{escape_lucene_special_chars(artist)}" '
                 f'AND release:"{escape_lucene_special_chars(album)}"')
        candidates = []
        for rel in client.search_releases(query, limit=10) or []:
            if not isinstance(rel, dict) or not rel.get("id"):
                continue
            if not _artist_matches(artist, _release_credit_text(rel)):
                continue
            left = normalize_for_aggregation(album)
            right = normalize_for_aggregation(str(rel.get("title") or ""))
            if not left or not right or not aggregation_keys_compatible(left, right):
                continue
            if not _alt_status_matches(album, str(rel.get("title") or "")):
                continue
            score = _token_similarity(left, right)
            if score >= 0.95:
                candidates.append((score, str(rel["id"])))
        candidates.sort(key=lambda pair: (-pair[0], pair[1]))
        # Edition ties are not broken arbitrarily. Per-track lookup is safer.
        if candidates and (len(candidates) == 1 or candidates[0][0] - candidates[1][0] >= 0.03):
            return candidates[0][1]
    except Exception as exc:
        logger.debug("Release search failed", artist=artist, album=album, error=str(exc))
    return ""


def track_identity_key(local_title_key: str, disc: Any = None, position: Any = None) -> str:
    disc_i = max(1, _parse_number(disc, 1))
    pos_i = _parse_number(position)
    return f"{local_title_key}#{disc_i}:{pos_i}" if pos_i > 0 else str(local_title_key or "")


def album_recording_batch_key(
    artist: str, title: str, disc: Any = None, track_number: Any = None,
) -> str:
    base = f"{str(artist or '').strip().lower()}::{str(title or '').strip().lower()}"
    pos = _parse_number(track_number)
    return f"{base}::{max(1, _parse_number(disc, 1))}::{pos}" if pos > 0 else base


def _index_release_tracklist(
    media: list[Any], titles_to_mbids: dict[str, dict[str, Any]],
    position_index: dict[tuple[int, int], dict[str, Any]], recording_mbids: list[str],
) -> None:
    for medium in media:
        if not isinstance(medium, dict):
            continue
        disc = max(1, _parse_number(medium.get("position"), 1))
        for trk in medium.get("tracks") or []:
            if not isinstance(trk, dict):
                continue
            rec = trk.get("recording") or {}
            if not isinstance(rec, dict):
                rec = {}
            title = str(trk.get("title") or rec.get("title") or "").strip()
            key = normalize_for_aggregation(title)
            if not key:
                continue
            mbid = str(rec.get("id") or trk.get("recording_mbid") or "").strip()
            pos = _parse_number(trk.get("position") or trk.get("number"))
            row = {"title": title, "mbid": mbid, "credit": _release_credit_text(trk),
                   "length_ms": trk.get("length") or rec.get("length"),
                   "disc": disc, "position": pos,
                   "recording_title": _recording_title(rec)}
            entry = titles_to_mbids.setdefault(key, {
                "title": title, "mbids": [], "rows": [], "position": pos,
                "disc": disc, "length_ms": row["length_ms"],
            })
            entry["rows"].append(row)
            if mbid:
                entry["mbids"].append(mbid)
                recording_mbids.append(mbid)
            if pos > 0:
                existing = position_index.get((disc, pos))
                if existing is not None:
                    existing["ambiguous"] = True
                else:
                    position_index[(disc, pos)] = dict(
                        row, key=key, mbids=[mbid] if mbid else [], ambiguous=False,
                    )


def _position_title_corroborates(local_title: str, release_title: str) -> bool:
    if not local_title or not release_title:
        return False
    # A cover annotation is not evidence that two recordings are identical.
    if strip_cover_attribution(local_title) != local_title or strip_cover_attribution(release_title) != release_title:
        return False
    left = strip_featured_artist(local_title) or local_title
    right = strip_featured_artist(release_title) or release_title
    return bool(titles_match_for_review(left, right))


def _position_alias_allowed(
    local_title: str, release_title: str | None, *, local_track_count: int,
    release_track_count: int, layout_verified: bool = False,
    length_verified: bool = False, artist_verified: bool = False,
) -> bool:
    # Equal totals alone never establish an edition or a translated title.
    if _position_title_corroborates(local_title, release_title or ""):
        return True
    return bool(local_track_count > 0 and local_track_count == release_track_count
                and layout_verified and length_verified and artist_verified)


def _release_year(value: Any) -> int | None:
    match = re.search(r"(?<!\d)(\d{4})(?!\d)", str(value or ""))
    return int(match.group(1)) if match else None


def _release_is_remaster_edition(release: dict[str, Any]) -> bool:
    """Explicit label only. Release age does not establish remaster status."""
    return bool(REMASTER_MARKER_RE.search(str(release.get("title") or "")))


def _original_release_of_group(release: dict[str, Any], mb_client: Any) -> dict[str, Any] | None:
    """Legacy helper retained; never used to substitute recording identities.

    Returns the earliest dated release in the fetched page, not a guarantee of
    the original edition. The browse wrapper's pagination contract is unknown.
    """
    group = release.get("release-group") or {}
    if not isinstance(group, dict) or not group.get("id"):
        return None
    try:
        releases = mb_client.browse_releases_for_group(group["id"], inc="media", limit=50) or []
        dated = [r for r in releases if isinstance(r, dict) and _release_year(r.get("date"))]
        return min(dated, key=lambda r: (str(r.get("date")), str(r.get("id")))) if dated else None
    except Exception as exc:
        logger.debug("Release-group browse failed", error=str(exc))
        return None


def get_listenbrainz_album_tracklist_with_release(
    artist: str, album: str, tracks: list[dict[str, Any]],
) -> tuple[dict[str, AlbumRecordingMatch], str]:
    if not tracks:
        return {}, ""
    release_mbid = _resolve_release_mbid(artist, album, tracks)
    if not release_mbid:
        return {}, ""
    titles: dict[str, dict[str, Any]] = {}
    positions: dict[tuple[int, int], dict[str, Any]] = {}
    ids: list[str] = []
    source = ""
    # Prefer authoritative MB track rows and artist credits. LB is a fallback.
    client = _shared_mb()
    if client is not None:
        try:
            release = client.get_release(release_mbid, inc="recordings+artist-credits") or {}
            if isinstance(release, dict):
                _index_release_tracklist(release.get("media") or [], titles, positions, ids)
                if ids:
                    source = "musicbrainz"
        except Exception as exc:
            logger.debug("MB release tracklist failed", release_mbid=release_mbid, error=str(exc))
    if not ids:
        # Discard partial indices before trying a different source.
        titles.clear()
        positions.clear()
        try:
            response = lb_get_release_metadata_batch([release_mbid]) or {}
            container = response.get("releases", response) if isinstance(response, dict) else {}
            release = container.get(release_mbid) or {} if isinstance(container, dict) else {}
            if isinstance(release, dict):
                _index_release_tracklist(release.get("media") or [], titles, positions, ids)
                if ids:
                    source = "listenbrainz"
        except Exception as exc:
            logger.debug("LB release metadata failed", release_mbid=release_mbid, error=str(exc))
    if not ids:
        return {}, release_mbid
    counts = _lb_batch(ids)
    local_rows = [t for t in tracks if normalize_for_aggregation(str(t.get("title") or ""))]
    local_title_counts = Counter(normalize_for_aggregation(str(t.get("title") or "")) for t in local_rows)
    local_positions = [(max(1, _parse_number(t.get("disc_number"), 1)),
                        _parse_number(t.get("track_number"))) for t in local_rows]
    local_layout = Counter(disc for disc, pos in local_positions if pos > 0)
    release_layout = Counter(disc for disc, pos in positions)
    layout_verified = bool(
        len(local_positions) == len(set(local_positions))
        and all(pos > 0 for _, pos in local_positions)
        and set(local_positions) == set(positions)
        and local_layout == release_layout
        and not any(row.get("ambiguous") for row in positions.values())
    )
    out: dict[str, AlbumRecordingMatch] = {}

    def payload(row: dict[str, Any], track: dict[str, Any], method: str,
                unique: bool = False) -> AlbumRecordingMatch:
        total, users = _sum_counts(counts, [row.get("mbid")])
        return {"listenbrainz_listens": total, "listenbrainz_users": users,
                "recording_mbid": row.get("mbid") or None,
                "release_track_title": str(row.get("title") or ""),
                "local_disc_number": max(1, _parse_number(track.get("disc_number"), 1)),
                "local_track_number": _parse_number(track.get("track_number")),
                "length_verified": _length_agreement(track.get("duration"), row.get("length_ms")) is True,
                "title_is_unique": unique, "matched_by": method}

    def row_compatible(track: dict[str, Any], row: dict[str, Any]) -> bool:
        if not row.get("mbid"):
            return False
        if _length_agreement(track.get("duration"), row.get("length_ms")) is False:
            return False
        local_artist = str(track.get("artist") or "").strip()
        credit = str(row.get("credit") or "").strip()
        # Missing or generic compilation credits cannot corroborate a row,
        # but must not prevent a unique release-title identity being returned.
        generic = {"various artists", "various", "va", "[unknown]", "unknown"}
        if local_artist.casefold() not in generic and local_artist and credit:
            if not _artist_matches(local_artist, credit):
                return False
        local_types = _performance_types(str(track.get("title") or ""))
        release_types = _performance_types(str(row.get("title") or ""))
        if local_types and local_types != release_types:
            return False
        return True

    # Publish only unambiguous title identities. No first-MBID picks or sums
    # across different performances sharing a normalised title.
    for key, entry in titles.items():
        rows = entry["rows"]
        if len(rows) != 1 or not rows[0].get("mbid") or local_title_counts[key] != 1:
            continue
        track = next(t for t in local_rows if normalize_for_aggregation(str(t.get("title") or "")) == key)
        if row_compatible(track, rows[0]):
            out[key] = payload(rows[0], track, "title", unique=True)

    for track in local_rows:
        title = str(track.get("title") or "")
        key = normalize_for_aggregation(title)
        disc = max(1, _parse_number(track.get("disc_number"), 1))
        pos = _parse_number(track.get("track_number"))
        if pos <= 0 or (out.get(key) or {}).get("title_is_unique"):
            continue
        alias = track_identity_key(key, disc, pos)
        candidates = (titles.get(key) or {}).get("rows") or []
        local_artist = str(track.get("artist") or "")
        local_tokens = _credit_identity_tokens(local_artist)
        matches = [row for row in candidates if local_tokens
                   and _credit_identity_tokens(str(row.get("credit") or "")) == local_tokens
                   and row_compatible(track, row)]
        # A short credit can be an abbreviated featured credit; do not guess.
        abbreviated = any(local_tokens < _credit_identity_tokens(str(row.get("credit") or ""))
                          for row in candidates) if local_tokens else False
        if len(candidates) > 1 and len(matches) == 1 and not abbreviated:
            out[alias] = payload(matches[0], track, "artist")
            continue
        row = positions.get((disc, pos))
        if not row or row.get("ambiguous") or not row_compatible(track, row):
            continue
        length_verified = _length_agreement(track.get("duration"), row.get("length_ms")) is True
        artist_verified = _artist_matches(local_artist, str(row.get("credit") or ""))
        if not _position_alias_allowed(
            title, row.get("title"), local_track_count=len(local_rows),
            release_track_count=len(positions), layout_verified=layout_verified,
            length_verified=length_verified, artist_verified=artist_verified,
        ):
            continue
        # For a missing marker or a duplicate title, require actual duration
        # corroboration before letting position choose the recording.
        if not length_verified:
            continue
        out[alias] = payload(row, track, "position")
        if local_title_counts[key] == 1:
            out[key] = dict(out[alias])
    logger.info("Preloaded ListenBrainz album tracklist", count=len(out), artist=artist,
                album=album, release_mbid=release_mbid, source=source)
    return out, release_mbid


def get_listenbrainz_album_tracklist(
    artist: str, album: str, tracks: list[dict[str, Any]],
) -> dict[str, AlbumRecordingMatch]:
    return get_listenbrainz_album_tracklist_with_release(artist, album, tracks)[0]


def get_listenbrainz_popularity_for_track(track: dict[str, Any]) -> dict[str, int | None]:
    mbid = extract_recording_mbid(track)
    if mbid:
        try:
            result = lb_get_listenbrainz_popularity(mbid)
            if isinstance(result, dict):
                return result
        except Exception as exc:
            logger.debug("LB track lookup failed", recording_mbid=mbid, error=str(exc))
    return {"total_listen_count": None, "total_user_count": None}


def get_listenbrainz_score_for_track(track: dict[str, Any]) -> int:
    mbid = extract_recording_mbid(track)
    if mbid:
        try:
            return _count(lb_get_listenbrainz_score(mbid))
        except Exception:
            pass
    return 0


def get_lastfm_track_info(
    artist: str, title: str, track_mbid: str | None = None,
    album_artist: str | None = None, lastfm_client: LastFmClient | None = None,
) -> dict[str, Any]:
    if lastfm_client is None:
        return {"track_play": 0, "listeners": 0}
    results = []
    for candidate in _unique_ids(get_artist_lookup_candidates(artist, album_artist=album_artist)):
        try:
            result = lastfm_client.get_track_info(candidate, title, track_mbid=track_mbid)
            if isinstance(result, dict):
                results.append(result)
        except Exception:
            continue
    return choose_best_provider_counts(results) if results else {"track_play": 0, "listeners": 0}


def _configured_lastfm_client(client: Any = None, api_key: str | None = None) -> Any:
    if client is not None:
        return client
    try:
        if not api_key:
            from helpers.config_helpers import get_config
            integrations = (get_config() or {}).get("api_integrations") or {}
            api_key = (integrations.get("lastfm") or {}).get("api_key")
        return LastFmClient(api_key=api_key) if api_key else None
    except Exception as exc:
        logger.debug("Last.fm client unavailable", error=str(exc))
        return None


def _artist_catalogue(artist: str, client: Any) -> list[dict[str, Any]]:
    key = _artist_cache_key(artist)
    if not key or client is None:
        return []
    cached = _cache_get(_lastfm_artist_catalog_cache, key)
    if cached is not None:
        return [dict(item) for item in cached]
    catalogue: list[dict[str, Any]] = []
    try:
        from services.popularity.popularity_cache_service import get_artist_top_tracks_map
        mapping = get_artist_top_tracks_map(client, artist) or {}
        for title, entry in mapping.items():
            if not isinstance(entry, dict):
                continue
            # Prefer a raw provider title when the map retains one. A map
            # whose keys discarded version markers cannot prove an alt match.
            raw_title = str(entry.get("name") or entry.get("title") or title)
            catalogue.append({"name": raw_title,
                              "listeners": _count(entry.get("lastfm_listeners")),
                              "playcount": _count(entry.get("lastfm_playcount")),
                              "artist": artist,
                              "_raw_title_verified": bool(entry.get("name") or entry.get("title"))})
    except Exception as exc:
        logger.debug("Last.fm catalogue map failed", artist=artist, error=str(exc))
    if not catalogue and hasattr(client, "get_artist_top_tracks"):
        try:
            raw = client.get_artist_top_tracks(artist) or []
            if isinstance(raw, dict):
                raw = (raw.get("toptracks") or {}).get("track", [])
            if isinstance(raw, dict):
                raw = [raw]
            for entry in raw:
                if isinstance(entry, dict):
                    item = dict(entry)
                    item.setdefault("artist", artist)
                    catalogue.append(item)
        except Exception as exc:
            logger.debug("Last.fm catalogue fetch failed", artist=artist, error=str(exc))
    _cache_put(_lastfm_artist_catalog_cache, key, catalogue)
    return [dict(item) for item in catalogue]


def get_lastfm_artist_max_listeners(artist: str, api_key: str | None = None) -> int:
    key = _artist_cache_key(artist)
    if not key:
        return 0
    cached = _cache_get(_lastfm_artist_max_cache, key)
    if cached is not None:
        return int(cached)
    client = _configured_lastfm_client(api_key=api_key)
    catalogue = _artist_catalogue(artist, client)
    peak = max((_count(item.get("listeners")) for item in catalogue), default=0)
    if not peak:
        try:
            if not api_key:
                from helpers.config_helpers import get_config
                api_key = (((get_config() or {}).get("api_integrations") or {}).get("lastfm") or {}).get("api_key")
            if api_key:
                from api_clients.lastfm_http import LastFmHttpClient
                data = LastFmHttpClient(api_key=api_key).get_json(
                    "artist.getTopTracks", timeout=10, artist=artist, limit=100,
                ) or {}
                raw = (data.get("toptracks") or {}).get("track") or []
                if isinstance(raw, dict):
                    raw = [raw]
                peak = max((_count(item.get("listeners")) for item in raw
                            if isinstance(item, dict)), default=0)
        except Exception as exc:
            logger.debug("Last.fm artist peak lookup failed", artist=artist, error=str(exc))
    _cache_put(_lastfm_artist_max_cache, key, peak)
    return peak


def get_online_artist_catalogue(
    artist: str, lastfm_client: Any = None, limit: int = 200,
) -> list[tuple[str, int]]:
    """Observed top-track sample, sorted by listeners, not a full discography."""
    limit = max(0, _parse_number(limit, 200))
    key = _artist_cache_key(artist)
    if not key or limit == 0:
        return []
    cached = _cache_get(_ONLINE_CATALOGUE_CACHE, key)
    if cached is None:
        catalogue = _artist_catalogue(artist, _configured_lastfm_client(lastfm_client))
        by_title: dict[str, int] = {}
        for item in catalogue:
            title = normalize_for_aggregation(str(item.get("name") or item.get("title") or ""))
            listeners = _count(item.get("listeners"))
            if title and listeners:
                by_title[title] = max(by_title.get(title, 0), listeners)
        cached = sorted(by_title.items(), key=lambda pair: (-pair[1], pair[0]))
        _cache_put(_ONLINE_CATALOGUE_CACHE, key, cached)
    return list(cached[:limit])


def get_online_artist_track_rank(
    artist: str, track_title: str, lastfm_client: Any = None,
) -> dict[str, Any]:
    """Rank within the observed catalogue sample. A missing rank is unknown.

    Percentile is the midpoint (rank - 0.5) / sample size; it does not guarantee
    that rank 1 falls inside any particular rating band for small samples.
    """
    empty = {"rank": 0, "total": 0, "percentile": 0.0, "listeners": 0,
             "top_listeners": 0, "source": "none"}
    target = normalize_for_aggregation(track_title)
    if not target:
        return empty
    catalogue = get_online_artist_catalogue(artist, lastfm_client=lastfm_client)
    if not catalogue:
        return empty
    for index, (title, listeners) in enumerate(catalogue, 1):
        if title == target:
            return {"rank": index, "total": len(catalogue),
                    "percentile": (index - 0.5) / len(catalogue),
                    "listeners": listeners, "top_listeners": catalogue[0][1],
                    "source": "lastfm_artist_top_tracks"}
    return {"rank": 0, "total": len(catalogue), "percentile": 1.0,
            "listeners": 0, "top_listeners": catalogue[0][1],
            "source": "lastfm_artist_top_tracks_beyond"}


def _empty_lastfm() -> dict[str, Any]:
    return {"listeners": 0, "track_play": 0, "matched_tracks": []}


def _lastfm_result(items: list[dict[str, Any]]) -> dict[str, Any]:
    # Different provider buckets may contain overlapping listeners.
    return {"listeners": sum(_count(i.get("listeners")) for i in items),
            "track_play": sum(_count(i.get("playcount") or i.get("track_play")) for i in items),
            "matched_tracks": items}


def _lastfm_item_key(item: dict[str, Any]) -> str:
    url = str(item.get("url") or "").strip()
    if url:
        return url.casefold()
    return (_artist_cache_key(_search_item_artist(item)) + "::" +
            normalize_unicode_punctuation(str(item.get("name") or item.get("title") or "")).casefold().strip())


def _lastfm_candidate_ok(
    item: dict[str, Any], artist: str, title: str, *, is_live: bool, is_alt: bool,
    require_artist: bool = True,
) -> bool:
    item_title = str(item.get("name") or item.get("title") or "")
    if item.get("_raw_title_verified") is False:
        return False
    if not item_title:
        return False
    if require_artist and not _artist_matches(artist, _search_item_artist(item)):
        return False
    if not _alt_status_matches_ctx(title, item_title, target_is_live=is_live,
                                   target_is_alt_rendition=is_alt):
        return False
    left, right = normalize_for_aggregation(title), normalize_for_aggregation(item_title)
    return bool(left and right and title_variants_compatible(title, item_title)
                and aggregation_keys_compatible(left, right))


def _lastfm_direct(client: Any, artist: str, title: str, mbid: str | None = None) -> dict[str, Any]:
    try:
        info = (client.get_track_info(artist, title, track_mbid=mbid) if mbid
                else client.get_track_info(artist, title)) or {}
        if not isinstance(info, dict):
            return _empty_lastfm()
        returned_artist = _search_item_artist(info)
        if returned_artist and not _artist_matches(artist, returned_artist):
            return _empty_lastfm()
        returned_title = str(info.get("name") or info.get("title") or "")
        if returned_title and not _lastfm_candidate_ok(
            dict(info, artist=returned_artist or artist), artist, title,
            is_live=False, is_alt=False,
        ):
            return _empty_lastfm()
        return {"listeners": _count(info.get("listeners")),
                "track_play": _count(info.get("track_play") or info.get("playcount")),
                "matched_tracks": []}
    except Exception as exc:
        logger.debug("Last.fm direct lookup failed", artist=artist, track=title, error=str(exc))
        return _empty_lastfm()


def get_search_aggregated_lastfm_popularity(
    artist: str, track_title: str, lastfm_client: Any = None,
    is_live_release: bool = False, target_is_alt_rendition: bool = False,
) -> dict[str, Any]:
    if lastfm_client is None:
        return _empty_lastfm()
    artist = normalize_unicode_punctuation(artist)
    track_title = normalize_unicode_punctuation(track_title)
    track_title = strip_cover_attribution(track_title) or track_title
    if not normalize_for_aggregation(track_title):
        return _empty_lastfm()
    matched: dict[str, dict[str, Any]] = {}
    primary = get_primary_artist_preserve_case(artist)
    candidates = _unique_ids([artist, primary] + list(get_artist_lookup_candidates(artist)))
    for candidate in candidates:
        try:
            results = lastfm_client.search_track(candidate, track_title, limit=20) or []
        except Exception as exc:
            logger.debug("Last.fm search failed", artist=candidate, error=str(exc))
            continue
        for item in results:
            if not isinstance(item, dict) or not _lastfm_candidate_ok(
                item, artist, track_title, is_live=is_live_release,
                is_alt=target_is_alt_rendition,
            ):
                continue
            key = _lastfm_item_key(item)
            previous = matched.get(key)
            if previous is None or _count(item.get("listeners")) > _count(previous.get("listeners")):
                matched[key] = dict(item)
    if matched:
        return _lastfm_result(list(matched.values()))
    # Guard explicit title markers as well as release-derived flags.
    if is_live_release or target_is_alt_rendition or _is_alternate_performance_title(track_title):
        return _empty_lastfm()
    return _lastfm_direct(lastfm_client, artist, track_title)


def get_aggregated_lastfm_popularity(
    artist: str, track_title: str, lastfm_client: Any = None,
    isrc: str | None = None, recording_mbid: str | None = None,
    is_live_release: bool = False, target_is_alt_rendition: bool = False,
) -> dict[str, Any]:
    if lastfm_client is None:
        return _empty_lastfm()
    artist = normalize_unicode_punctuation(artist)
    track_title = normalize_unicode_punctuation(track_title)
    track_title = strip_cover_attribution(track_title) or track_title
    if not normalize_for_aggregation(track_title):
        return _empty_lastfm()
    matched: dict[str, dict[str, Any]] = {}
    # A collaboration is not automatically identical to each contributor's
    # solo catalogue. Search can still find the full credited collaboration.
    for item in _artist_catalogue(artist, lastfm_client):
        if _lastfm_candidate_ok(item, artist, track_title, is_live=is_live_release,
                               is_alt=target_is_alt_rendition):
            matched[_lastfm_item_key(item)] = item
    best = _lastfm_result(list(matched.values())) if matched else _empty_lastfm()
    if not matched or _is_featured_artist(artist) or ARTIST_JOIN_RE.search(artist):
        search = get_search_aggregated_lastfm_popularity(
            artist, track_title, lastfm_client, is_live_release, target_is_alt_rendition,
        )
        if (search["listeners"], search["track_play"]) > (best["listeners"], best["track_play"]):
            best = search
    if best["listeners"] or best["track_play"]:
        return best
    if is_live_release or target_is_alt_rendition or _is_alternate_performance_title(track_title):
        return _empty_lastfm()
    variants: dict[str, dict[str, Any]] = {"Primary": _lastfm_direct(lastfm_client, artist, track_title)}
    if isrc or recording_mbid:
        resolved = resolve_isrc_recording(isrc, title=track_title, artist=artist) if isrc else None
        mbid = (resolved or {}).get("recording_mbid") or recording_mbid
        if mbid:
            variants["ISRC"] = _lastfm_direct(
                lastfm_client, str((resolved or {}).get("artist") or artist),
                str((resolved or {}).get("title") or track_title), str(mbid),
            )
    if _is_featured_artist(artist):
        inverted = invert_featured_artist(artist)
        if inverted != artist:
            variants["Inverted"] = _lastfm_direct(lastfm_client, inverted, track_title)
    best_key = max(variants, key=lambda key: (variants[key]["listeners"], variants[key]["track_play"]))
    result = dict(variants[best_key])
    if len(variants) > 1:
        result.update(sources_queried=len(variants), variant_detail={
            key: value["listeners"] for key, value in variants.items() if value["listeners"] > 0
        })
    return result


def _recording_artist_mbids(recording: dict[str, Any]) -> set[str]:
    output = set()
    for credit in recording.get("artist-credit") or []:
        if isinstance(credit, dict):
            artist = credit.get("artist") or {}
            if isinstance(artist, dict) and artist.get("id"):
                output.add(str(artist["id"]))
    return output


def _recording_primary_artist(recording: dict[str, Any]) -> str:
    return _first_credit_name(recording)


def _recording_work_mbids(recording: dict[str, Any]) -> set[str]:
    output = set()
    for relation in recording.get("relations") or []:
        if not isinstance(relation, dict) or str(relation.get("type") or "").casefold() != "performance":
            continue
        work = relation.get("work") or {}
        if isinstance(work, dict) and work.get("id"):
            output.add(str(work["id"]).strip())
    return output


def _recording_work_mbid(recording: dict[str, Any]) -> str:
    ids = _recording_work_mbids(recording)
    return next(iter(ids)) if len(ids) == 1 else ""


def _lb_result(mbids: Any, **extra: Any) -> dict[str, Any]:
    ids = sorted(_unique_ids(mbids))
    counts = _lb_batch(ids)
    total, users = _sum_counts(counts, ids)
    return {"total_listen_count": total, "total_user_count": users, "mbids": ids, **extra}


def get_aggregated_listenbrainz_popularity(
    title: str, artist: str, primary_mbid: str | None = None,
    isrc: str | None = None, lb_client: Any = None, mb_client: Any = None,
    is_live_release: bool = False, target_is_alt_rendition: bool = False,
) -> dict[str, Any]:
    # lb_client remains accepted for compatibility; counts use the established
    # module-level LB batch wrapper, whose contract is known to this module.
    mbids: set[str] = set(_unique_ids([primary_mbid]))
    client = _shared_mb(mb_client)
    if isrc and not primary_mbid:
        resolved = resolve_isrc_recording(
            isrc, mb_client=client, title=title, artist=artist,
            is_live_release=is_live_release, target_is_alt_rendition=target_is_alt_rendition,
        )
        if resolved and resolved.get("recording_mbid"):
            mbids.add(str(resolved["recording_mbid"]))
    if client is not None and hasattr(client, "search_recordings"):
        try:
            from helpers.normalization_service import normalize_title_for_lucene_query
            from api_clients.musicbrainz_http import escape_lucene_special_chars
            query = (f'recording:"{escape_lucene_special_chars(normalize_title_for_lucene_query(title))}" '
                     f'AND artist:"{escape_lucene_special_chars(artist)}"')
            target = normalize_for_aggregation(title)
            for rec in client.search_recordings(query, limit=20) or []:
                if not isinstance(rec, dict) or not rec.get("id"):
                    continue
                if not _artist_matches(artist, _release_credit_text(rec)):
                    continue
                if not _alt_status_matches_ctx(
                    title, _recording_title(rec), target_is_live=is_live_release,
                    target_is_alt_rendition=target_is_alt_rendition,
                ):
                    continue
                candidate = normalize_for_aggregation(str(rec.get("title") or ""))
                if target and candidate and aggregation_keys_compatible(target, candidate) and _token_similarity(target, candidate) >= 0.90:
                    mbids.add(str(rec["id"]))
        except Exception as exc:
            logger.debug("LB recording candidate search failed", artist=artist, track=title, error=str(exc))
    return _lb_result(mbids)


def _empty_work_lb_result() -> dict[str, Any]:
    return {"total_listen_count": 0, "total_user_count": 0, "mbids": [],
            "work_mbid": "", "source": "work"}


def get_work_level_listenbrainz_popularity(
    title: str, artist: str, artist_mbid: str = "", primary_mbid: str = "",
    isrc: str = "", lb_client: Any = None, mb_client: Any = None,
    work_mbid_hint: str = "", is_live_release: bool = False,
    target_is_alt_rendition: bool = False,
) -> dict[str, Any]:
    client = _shared_mb(mb_client)
    if client is None:
        return _empty_work_lb_result()
    seeds = set(_unique_ids([primary_mbid]))
    if not seeds and isrc:
        resolved = resolve_isrc_recording(
            isrc, mb_client=client, title=title, artist=artist,
            is_live_release=is_live_release, target_is_alt_rendition=target_is_alt_rendition,
        )
        if resolved and resolved.get("recording_mbid"):
            seeds.add(str(resolved["recording_mbid"]))
    work_mbid = str(work_mbid_hint or "").strip()
    if not work_mbid:
        for seed in sorted(seeds):
            try:
                rec = client.get_recording(seed, inc="work-rels+artist-credits") or {}
                work_mbid = _recording_work_mbid(rec)
                if work_mbid:
                    break
            except Exception as exc:
                logger.debug("Recording work lookup failed", recording_mbid=seed, error=str(exc))
    if not work_mbid:
        return _empty_work_lb_result()
    # Include client identity so injected clients cannot share stale graph data.
    cache_key = (id(client), work_mbid)
    recordings = _cache_get(_work_recordings_cache, cache_key)
    if recordings is None:
        try:
            recordings = client.browse_work_recordings(work_mbid, inc="artist-credits", limit=100) or []
            if not isinstance(recordings, list):
                recordings = []
        except Exception as exc:
            logger.debug("Work recording browse failed", work_mbid=work_mbid, error=str(exc))
            recordings = []
        _cache_put(_work_recordings_cache, cache_key, recordings)
    mbids = set(seeds)
    for rec in recordings:
        if not isinstance(rec, dict) or not rec.get("id"):
            continue
        if not _alt_status_matches_ctx(
            title, _recording_title(rec), target_is_live=is_live_release,
            target_is_alt_rendition=target_is_alt_rendition,
        ):
            continue
        if artist_mbid:
            # Presence anywhere in a multi-artist credit does not prove the
            # same credited performance. Require the full credit as well.
            if artist_mbid not in _recording_artist_mbids(rec):
                continue
        if not _artist_matches(artist, _release_credit_text(rec)):
            continue
        # A work can have edits, medleys, translations, or alternate titles.
        # Prefer under-aggregation over accepting a different recording by name.
        left = normalize_for_aggregation(title)
        right = normalize_for_aggregation(str(rec.get("title") or ""))
        if not left or not right or not aggregation_keys_compatible(left, right):
            continue
        mbids.add(str(rec["id"]))
    return _lb_result(mbids, work_mbid=work_mbid, source="work")


def get_metadata_sources_info(single_sources: list[str]) -> dict[str, Any]:
    sources = set(single_sources or [])
    flags = {"has_discogs": bool(sources & {"discogs", "discogs_video"}),
             "has_spotify": "spotify" in sources,
             "has_musicbrainz": "musicbrainz" in sources,
             "has_lastfm": "lastfm" in sources,
             "has_version_count": "version_count" in sources}
    labels = [("has_discogs", "Discogs"), ("has_spotify", "Spotify"),
              ("has_musicbrainz", "MusicBrainz"), ("has_lastfm", "Last.fm"),
              ("has_version_count", "Version Count")]
    return {**flags,
            "has_metadata": any(flags[key] for key, _ in labels if key != "has_version_count"),
            "sources_list": [label for key, label in labels if flags[key]]}
