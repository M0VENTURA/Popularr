"""Popularity provider source wrappers.

This module performs provider data acquisition and light provider-specific
normalization. It should not decide star ratings or single status.
"""

from __future__ import annotations

import re
import threading
from typing import Any

import structlog

try:  # C-speed fuzzy matching -- see _token_similarity
    from rapidfuzz import fuzz as _fuzz  # type: ignore[import-untyped]
    _HAVE_RAPIDFUZZ = True
except ImportError:  # pragma: no cover -- stdlib fallback keeps matching working
    from difflib import SequenceMatcher as _difflib_matcher
    _HAVE_RAPIDFUZZ = False

from api_clients.lastfm import LastFmClient
from api_clients.listenbrainz import (
    get_recording_popularity_batch as lb_get_recording_popularity_batch,
    get_listenbrainz_popularity as lb_get_listenbrainz_popularity,
    get_listenbrainz_score as lb_get_listenbrainz_score,
    get_release_metadata_batch as lb_get_release_metadata_batch,
)
from helpers.normalization_service import strip_cover_attribution
from services.popularity.popularity_matching import (
    ARTIST_JOIN_RE,
    choose_best_provider_counts,
    get_artist_lookup_candidates,
    get_primary_artist_preserve_case,
    normalize_for_aggregation,
    title_variants_compatible,
)

logger = structlog.get_logger(__name__)

DEFAULT_LISTENBRAINZ_BATCH_SIZE = 100

# =============================================================================
# THREAD-SAFE MEMORY CACHES
# =============================================================================

_CACHE_LOCK = threading.Lock()
_lastfm_artist_catalog_cache: dict[str, list[dict[str, Any]]] = {}
_lastfm_artist_max_cache: dict[str, int] = {}


def _token_similarity(a: str, b: str) -> float:
    """Title similarity on a 0-1 scale (shared ``fuzzy_match_score``)."""
    from services.popularity.popularity_math import fuzzy_match_score
    return fuzzy_match_score(a, b)


_FEATURED_ARTIST_RE = re.compile(
    r"^(.*?)\s+(?:feat\.?|ft\.?|featuring)\s+(.*)$",
    re.IGNORECASE,
)


def invert_featured_artist(artist_name: str) -> str:
    """Swap a "Primary feat. Guest" credit to "Guest feat. Primary"."""
    match = _FEATURED_ARTIST_RE.match(artist_name or "")
    if not match:
        return artist_name or ""
    primary, featured = match.group(1).strip(), match.group(2).strip()
    if not primary or not featured:
        return artist_name
    return f"{featured} feat. {primary}"


def _is_featured_artist(artist_name: str) -> bool:
    return bool(_FEATURED_ARTIST_RE.match(artist_name or ""))


_ALTERNATE_PERFORMANCE_RE = re.compile(
    # Both bracket styles: parentheses are what Last.fm/ListenBrainz titles
    # normally use, but MusicBrainz's own release tracklists use SQUARE
    # brackets ("Farewell [Unplugged Version]"). Matching only parentheses
    # meant a release title taken straight from MusicBrainz never registered as
    # an alternate performance, so a plainly titled unplugged track could still
    # merge with its identically titled studio namesake.
    r"[([][^)\]]*\b(?:live|unplugged|acoustic|orchestral|symphonic|demo|instrumental|"
    r"karaoke|remix|alternate|alt|take|session|rehearsal|jam[- ]along)\b[^)\]]*[)\]]"
    r"|\s+-\s*(?:live|unplugged|acoustic|orchestral|symphonic|demo|instrumental|"
    r"karaoke|remix|alternate|alt|take|session|rehearsal|jam[- ]along)\s*$",
    re.IGNORECASE,
)


def _is_alternate_performance_title(rec_title: str) -> bool:
    return bool(_ALTERNATE_PERFORMANCE_RE.search(rec_title or ""))


def _alt_status_matches(target_title: str, candidate_title: str) -> bool:
    """True only if target and candidate agree on being a live/remix/alternate
    performance (or agree on *not* being one).

    Without this check, popularity data for a plain studio release can get
    inflated by summing in live/remix/alternate recordings, and conversely a
    live/remix/alternate track can silently inherit the original studio
    release's popularity numbers because a fuzzy/normalized title match
    doesn't distinguish "Song" from "Song (Live)" / "Song (Remix)".

    IMPORTANT: this is a TITLE-ONLY test. Live releases routinely ship plainly
    titled tracks -- every track on Metallica's "S&M" is titled exactly as its
    studio original -- so on those releases this returns True for the studio
    recording and lets its listener count merge in. Prefer
    ``_alt_status_matches_ctx`` and pass the release-level liveness whenever
    the caller knows it.
    """
    return _is_alternate_performance_title(target_title) == _is_alternate_performance_title(candidate_title)


def _alt_status_matches_ctx(
    target_title: str,
    candidate_title: str,
    *,
    target_is_live: bool = False,
) -> bool:
    """Alt-performance agreement with RELEASE-level liveness folded in.

    Liveness is a property of the RELEASE, not necessarily of the title. When
    ``target_is_live`` is set, the target is treated as an alternate
    performance regardless of how it is titled, so a plainly titled live track
    no longer matches -- and no longer absorbs the listener count of -- the
    identically titled studio recording.
    """
    target_alt = _is_alternate_performance_title(target_title) or bool(target_is_live)
    return target_alt == _is_alternate_performance_title(candidate_title)


def resolve_isrc_recording(
    isrc: str,
    *,
    mb_client: Any = None,
    title: str = "",
    artist: str = "",
    is_live_release: bool = False,
) -> dict[str, str | None] | None:
    """Resolve an ISRC to its MusicBrainz recording (MBID + title + artist)."""
    isrc = str(isrc or "").strip()
    if not isrc:
        return None

    if isrc.startswith("[") and isrc.endswith("]"):
        from helpers.normalization_service import normalize_isrc
        isrc = normalize_isrc(isrc)
        import re as _re
        if not _re.fullmatch(r"[A-Z]{2}[0-9A-Z]{3}[0-9]{7}", isrc):
            return None

    try:
        if mb_client is None:
            from services.enrichment.musicbrainz_service import get_shared_mb_client
            mb_client = get_shared_mb_client()

        recordings = mb_client.lookup_by_isrc(isrc) or []
        if not recordings:
            return None

        if not title and not artist:
            first = recordings[0]
            return {
                "recording_mbid": str(first.get("id") or "").strip() or None,
                "title": str(first.get("title") or "").strip(),
                "artist": _first_credit_name(first),
            }

        target_title = normalize_for_aggregation(title)
        target_artist = str(artist or "").casefold().strip()
        best = None
        best_score = 0.0

        for rec in recordings:
            rec_title_raw = str(rec.get("title") or "")
            # Don't let a live/remix/alternate recording win the match for a
            # studio-title lookup, or vice versa.
            if title and not _alt_status_matches_ctx(
                title, rec_title_raw, target_is_live=is_live_release
            ):
                continue

            rec_title = normalize_for_aggregation(rec_title_raw)
            rec_artist = str(_first_credit_name(rec) or "").casefold().strip()
            score = _token_similarity(target_title, rec_title)

            if target_artist and rec_artist:
                artist_hit = (
                    target_artist == rec_artist
                    or target_artist == get_primary_artist_preserve_case(rec_artist).casefold()
                )
                score = score * 0.7 + (1.0 if artist_hit else 0.0) * 0.3

            if score > best_score:
                best_score = score
                best = rec

        if best is None:
            return None

        return {
            "recording_mbid": str(best.get("id") or "").strip() or None,
            "title": str(best.get("title") or "").strip(),
            "artist": _first_credit_name(best),
        }
    except Exception as exc:
        logger.debug("ISRC lookup failed", isrc=isrc, error=str(exc))
        return None


def _first_credit_name(recording: dict[str, Any]) -> str:
    """Primary artist name from a recording's artist-credit."""
    credits = recording.get("artist-credit") or []
    for credit in credits:
        if isinstance(credit, dict):
            name = credit.get("name") or ""
            if name:
                return str(name)
        elif credit:
            return str(credit)
    return ""


def extract_recording_mbid(track: dict[str, Any]) -> str | None:
    return track.get("recording_mbid") or track.get("musicbrainz_recording_mbid") or track.get("mbid")


def get_listenbrainz_batch_for_tracks(tracks: list[dict[str, Any]]) -> dict[str, dict[str, int | None]]:
    mbids = [extract_recording_mbid(track) for track in tracks]
    mbids = [mbid for mbid in mbids if mbid]
    output: dict[str, dict[str, int | None]] = {}

    for index in range(0, len(mbids), DEFAULT_LISTENBRAINZ_BATCH_SIZE):
        chunk = mbids[index:index + DEFAULT_LISTENBRAINZ_BATCH_SIZE]
        try:
            output.update(lb_get_recording_popularity_batch(chunk))
        except Exception as exc:
            logger.debug("ListenBrainz batch lookup failed", error=str(exc))
    return output


def _resolve_release_mbid(artist: str, album: str, tracks: list[dict[str, Any]]) -> str:
    for t in tracks:
        mbid = str(t.get("musicbrainz_albumid") or t.get("musicbrainz_album_mbid") or "").strip()
        if mbid:
            return mbid

    try:
        from api_clients.musicbrainz_http import escape_lucene_special_chars
        from services.enrichment.musicbrainz_service import get_shared_mb_client
        client = get_shared_mb_client()
        query = (
            f'artist:"{escape_lucene_special_chars(artist)}" '
            f'AND release:"{escape_lucene_special_chars(album)}"'
        )
        releases = client.search_releases(query, limit=5) or []
        artist_norm = _normalize_artist(artist)
        best_mbid = ""
        best_score = 0.0

        for rel in releases:
            if not isinstance(rel, dict):
                continue
            title = str(rel.get("title") or "").strip()
            credits = rel.get("artist-credit") or []
            names = []

            for credit in credits:
                if isinstance(credit, dict):
                    art = credit.get("artist") or {}
                    names.append(art.get("name") or credit.get("name") or "")

            if names and not any(_normalize_artist(n) == artist_norm for n in names):
                continue

            score = _token_similarity(title.lower(), album.lower())
            if score > best_score:
                best_score = score
                best_mbid = str(rel.get("id") or "").strip()

        if best_mbid and best_score >= 0.8:
            logger.debug("Resolved release via MB search", artist=artist, album=album, release_mbid=best_mbid)
            return best_mbid

    except Exception as exc:
        logger.debug("Release search failed", artist=artist, album=album, error=str(exc))
    return ""


def _normalize_artist(name: str) -> str:
    return get_primary_artist_preserve_case(name).casefold().strip()


def track_identity_key(local_title_key: str, disc: Any = None, position: Any = None) -> str:
    """Position-qualified alias for a title-keyed album-tracklist entry.

    A normalised TITLE cannot identify a row: dArtagnan's "Helden X Hymnen"
    files both the album's title track AND its "(Unplugged Version)" rendition
    under the same title "Helden X Hymnen", and those are two different
    recordings on the release (positions 1 and 15). Keyed by title alone the
    loop collapsed them onto whichever row it reached last, so one of the two
    ended up with the other's recording — the reported "both rows report the
    same score" duplication.

    The release's own tracklist settles it by POSITION, so the position-matched
    identity is also published under this alias while the plain title key keeps
    its existing meaning for every current consumer.
    """
    try:
        disc_i = int(str(disc if disc not in (None, "") else 1).split("/")[0].strip() or 1)
    except (TypeError, ValueError):
        disc_i = 1
    try:
        pos_i = int(str(position or "").split("/")[0].strip() or 0)
    except (TypeError, ValueError):
        pos_i = 0
    if pos_i <= 0:
        return str(local_title_key or "")
    return f"{local_title_key}#{disc_i}:{pos_i}"


def album_recording_batch_key(
    artist: str,
    title: str,
    disc: Any = None,
    track_number: Any = None,
) -> str:
    """Key for the per-album recording-identity batch ``track_stage`` consumes.

    ``"<artist>::<title>"`` is the legacy title-only form; appending
    ``"::<disc>::<track>"`` makes the key ROW-specific, which is what keeps two
    same-titled rows of one album apart (see ``track_identity_key``). Both the
    producer (``scan_stage_runner._build_album_recording_batch``) and the
    consumer (``track_stage._resolve_track_mb_metadata``) build it through this
    one function so the two formats cannot drift apart.
    """
    base = f"{str(artist or '').strip().lower()}::{str(title or '').strip().lower()}"
    try:
        disc_i = int(str(disc if disc not in (None, "") else 1).split("/")[0].strip() or 1)
    except (TypeError, ValueError):
        disc_i = 1
    try:
        track_i = int(str(track_number or "").split("/")[0].strip() or 0)
    except (TypeError, ValueError):
        track_i = 0
    if track_i <= 0:
        return base
    return f"{base}::{disc_i}::{track_i}"


def _index_release_tracklist(
    media: list[Any],
    titles_to_mbids: dict[str, dict[str, Any]],
    position_index: dict[tuple[int, int], dict[str, Any]],
    recording_mbids: list[str],
) -> None:
    for medium in media:
        if not isinstance(medium, dict):
            continue
        disc = medium.get("position")

        for trk in medium.get("tracks") or []:
            if not isinstance(trk, dict):
                continue
            title = str(trk.get("title") or "").strip()
            if not title:
                continue

            rec = trk.get("recording") or {}
            rec_mbid = str(rec.get("id") or trk.get("recording_mbid") or "").strip()
            key = normalize_for_aggregation(title)

            entry = titles_to_mbids.setdefault(key, {
                "title": title,
                "mbids": [],
                "position": trk.get("position") or trk.get("number"),
                "disc": disc,
                "length_ms": trk.get("length"),
            })

            if rec_mbid:
                entry["mbids"].append(rec_mbid)
                recording_mbids.append(rec_mbid)

            try:
                pos = int(trk.get("position") or trk.get("number") or 0)
            except (TypeError, ValueError):
                pos = 0

            try:
                disc_i = int(disc) if disc not in (None, "") else 1
            except (TypeError, ValueError):
                disc_i = 1

            if pos > 0:
                pos_key = (disc_i, pos)
                pos_entry = position_index.setdefault(pos_key, {
                    "key": key,
                    # The release's OWN title for this position. It is the only
                    # place a version marker survives when the local file is
                    # titled plainly ("Farewell [Unplugged Version]" on the
                    # release vs "Farewell (feat. Patty Gurdy)" in the library).
                    "title": title,
                    "length_ms": trk.get("length"),
                    "mbids": [],
                })
                if rec_mbid:
                    pos_entry["mbids"].append(rec_mbid)


def get_listenbrainz_album_tracklist_with_release(
    artist: str,
    album: str,
    tracks: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, int | None]], str]:
    if not tracks:
        return {}, ""

    release_mbid = _resolve_release_mbid(artist, album, tracks)
    if not release_mbid:
        return {}, ""

    titles_to_mbids: dict[str, dict[str, Any]] = {}
    recording_mbids: list[str] = []
    position_index: dict[tuple[int, int], dict[str, Any]] = {}
    _tracklist_source = ""

    try:
        _lb_rel = lb_get_release_metadata_batch([release_mbid]) or {}
        if isinstance(_lb_rel, dict):
            _rel_container = _lb_rel.get("releases") if isinstance(_lb_rel.get("releases"), dict) else _lb_rel
            _rel_entry = (_rel_container or {}).get(release_mbid) or {}
            _media = _rel_entry.get("media") or []
            if _media:
                _index_release_tracklist(_media, titles_to_mbids, position_index, recording_mbids)
                _tracklist_source = "listenbrainz"
    except Exception as exc:
        logger.debug("LB release metadata failed", release_mbid=release_mbid, error=str(exc))

    if not recording_mbids:
        try:
            from services.enrichment.musicbrainz_service import get_shared_mb_client
            mb = get_shared_mb_client()
            release = mb.get_release(release_mbid, inc="recordings")
            _index_release_tracklist(release.get("media") or [], titles_to_mbids, position_index, recording_mbids)
            _tracklist_source = "musicbrainz"
        except Exception as exc:
            logger.debug("MB release tracklist failed", release_mbid=release_mbid, error=str(exc))
            return {}, release_mbid

    if not recording_mbids:
        return {}, release_mbid

    try:
        counts = lb_get_recording_popularity_batch(recording_mbids) or {}
    except Exception as exc:
        # A failed COUNT lookup must not cost us the IDENTITY: the release
        # tracklist above already established which recording each position is,
        # and that is what the caller uses to stop resolving the track with an
        # ambiguous title+artist search. Counts simply come out as zero.
        logger.debug("Recording popularity batch failed", release_mbid=release_mbid, error=str(exc))
        counts = {}

    def _sum_counts(mbids: list[str]) -> tuple[int, int]:
        total = 0
        users = 0
        for m in mbids:
            entry = counts.get(m) or {}
            total += int(entry.get("total_listen_count") or 0)
            users += int(entry.get("total_user_count") or 0)
        return total, users

    out: dict[str, dict[str, int | None]] = {}
    for key, entry in titles_to_mbids.items():
        total, users = _sum_counts(entry["mbids"])
        # IDENTITY is emitted even at ZERO listens, provided the release puts
        # this title at exactly one position. The count is not what identifies
        # the track — the album's own release is — and requiring ``total > 0``
        # is what left five of the twelve "Little Nicky" tracks (Cave, Take a
        # Picture, Natural High, Nothing, When Worlds Collide) still reading
        # "Various Artists". Their release recordings have no ListenBrainz
        # scrobbles, so they received no identity, so ``track_stage`` fell
        # through to the ambiguous per-track search, which asked MusicBrainz
        # for ``artist:"Various Artists" AND recording:"<title>"`` and matched
        # nothing at all (the scan log's ``candidate_count=0`` / ``mbid=None``).
        # The release already answered which recording each position is.
        #
        # ⚠️ Uniqueness is load-bearing. When several release tracks share a
        # normalised title (dArtagnan's "Helden X Hymnen" is positions 1 AND
        # 15) ``entry["mbids"][0]`` is merely whichever row the index reached
        # first, so a title-keyed identity would pin BOTH local rows to the
        # same recording — the exact coin flip the position alias exists to
        # avoid. Those rows are answered by the position-qualified alias
        # emitted below, and by the alias-first read in the consumer.
        _title_is_unique = len(entry["mbids"]) == 1
        if total > 0 or _title_is_unique:
            out[key] = {
                "listenbrainz_listens": total,
                "listenbrainz_users": users,
                "recording_mbid": entry["mbids"][0] if entry["mbids"] else None,
                # Consumed by the POSITION pass below, so it can tell a
                # title-derived identity it must not overwrite from an
                # arbitrary mbids[0] pick on a same-titled pair.
                "title_is_unique": _title_is_unique,
            }

    used_pos_keys: set[tuple[int, int]] = set()
    for t in tracks:
        local_title = t.get("title")
        if not local_title:
            continue

        local_key = normalize_for_aggregation(local_title)
        if not local_key:
            continue

        # A title match already answered for this key, so leave it alone — this
        # pass only fills the gap for a track whose TITLE is not on the release
        # at all (a plainly tagged rendition, whose release title carries the
        # marker). The per-ROW identity is published separately below, under a
        # position-qualified alias, and that is what disambiguates two rows of
        # one album that share a title.
        #
        # ⚠️ The guard is on the IDENTITY, not the listen count. Gating it on
        # ``listenbrainz_listens`` let this pass overwrite a correct
        # title-derived identity with a positional guess for every
        # zero-listen track, and position is exactly what cannot be trusted
        # here: a library holding a 12-track SUBSET of a 16-track release
        # numbers its files 1..12 while those recordings sit at release
        # positions 2..13, so local#N is a DIFFERENT song than release#N
        # ("Little Nicky: Cave/Take a Picture/Natural High/Nothing/When Worlds
        # Collide still read Various Artists"). Only a title-derived identity
        # that is itself UNIQUE is protected — a same-titled pair still falls
        # through to the position pass, which is the only thing that can tell
        # those two rows apart.
        _existing = out.get(local_key) or {}
        if _existing.get("recording_mbid") and _existing.get("title_is_unique"):
            continue

        try:
            local_pos = int(t.get("track_number") or 0)
        except (TypeError, ValueError):
            continue

        if local_pos <= 0:
            continue

        try:
            local_disc = int(t.get("disc_number") or 1)
        except (TypeError, ValueError):
            local_disc = 1

        pos_key = (local_disc, local_pos)
        if pos_key in used_pos_keys:
            continue

        pos_entry = position_index.get(pos_key)
        if not pos_entry or not pos_entry["mbids"]:
            continue

        mb_len_ms = pos_entry.get("length_ms")
        try:
            local_dur = float(t.get("duration") or 0)
        except (TypeError, ValueError):
            local_dur = 0.0

        if mb_len_ms and local_dur > 0:
            if abs(int(mb_len_ms) - local_dur * 1000) > 5000:
                continue

        # ``total`` is 0 for any recording nobody has scrobbled yet, which is
        # NOT a reason to throw the match away: the POSITION (with the duration
        # guard above) already established WHICH recording this track is, and
        # that identity is what the caller needs in order to stop resolving the
        # track with an ambiguous title+artist search. Emitting the entry with a
        # zero count is safe — every consumer reads the counts under a
        # truthiness guard — whereas emitting it only when ``total > 0`` is the
        # reported dArtagnan "Helden X Hymnen" defect: the three unplugged
        # tracks that have no ListenBrainz listens kept the STUDIO recording's
        # MBID, so their Last.fm listeners came from the studio recording (7.5k
        # on an album whose other tracks sit at 300-500) and they scored as the
        # album's top tracks.
        total, users = _sum_counts(pos_entry["mbids"])

        out[local_key] = {
            "listenbrainz_listens": total,
            "listenbrainz_users": users,
            "recording_mbid": pos_entry["mbids"][0],
            "release_track_title": str(pos_entry.get("title") or ""),
        }
        used_pos_keys.add(pos_key)
        logger.info(
            "Position-matched track to release",
            local_title=local_title,
            disc=local_disc,
            track=local_pos,
            duration=local_dur,
            matched_key=pos_entry.get("key"),
            release_track_title=pos_entry.get("title"),
            listens=total,
        )

    # ------------------------------------------------------------------
    # Per-ROW identity: the release's own tracklist, position matched.
    #
    # ``out`` above is keyed by normalised TITLE and therefore cannot identify a
    # row — dArtagnan's "Helden X Hymnen" files the album's title track AND its
    # "(Unplugged Version)" rendition under the same title "Helden X Hymnen",
    # and those are two DIFFERENT recordings on the release (positions 1 and 15).
    # Keyed by title alone whichever row the loop reached last won, which is why
    # both rows reported an identical score in the scan results.
    #
    # The release settles it by POSITION, so every row that has a usable track
    # number and passes the duration guard also publishes its own recording under
    # a position-qualified alias. The title-keyed entries are left EXACTLY as
    # they were, so listen-count behaviour is unchanged for every existing
    # consumer; only a caller that knows the row's position can see (and prefer)
    # the alias.
    # ------------------------------------------------------------------
    for t in tracks:
        local_title = t.get("title")
        if not local_title:
            continue
        local_key = normalize_for_aggregation(local_title)
        if not local_key:
            continue

        try:
            row_pos = int(str(t.get("track_number") or "").split("/")[0].strip() or 0)
        except (TypeError, ValueError):
            continue
        if row_pos <= 0:
            continue

        try:
            row_disc = int(str(t.get("disc_number") or 1).split("/")[0].strip() or 1)
        except (TypeError, ValueError):
            row_disc = 1

        row_pos_entry = position_index.get((row_disc, row_pos))
        if not row_pos_entry or not row_pos_entry["mbids"]:
            continue

        row_len_ms = row_pos_entry.get("length_ms")
        try:
            row_dur = float(t.get("duration") or 0)
        except (TypeError, ValueError):
            row_dur = 0.0
        if row_len_ms and row_dur > 0:
            if abs(int(row_len_ms) - row_dur * 1000) > 5000:
                continue

        row_total, row_users = _sum_counts(row_pos_entry["mbids"])
        out[track_identity_key(local_key, row_disc, row_pos)] = {
            "listenbrainz_listens": row_total,
            "listenbrainz_users": row_users,
            "recording_mbid": row_pos_entry["mbids"][0],
            "release_track_title": str(row_pos_entry.get("title") or ""),
            "local_disc_number": row_disc,
            "local_track_number": row_pos,
        }

    if out:
        logger.info(
            "Preloaded ListenBrainz album tracklist",
            count=len(out),
            artist=artist,
            album=album,
            release_mbid=release_mbid,
            source=_tracklist_source,
        )
    else:
        logger.debug("No ListenBrainz data found for album", artist=artist, album=album)

    return out, release_mbid


def get_listenbrainz_album_tracklist(
    artist: str,
    album: str,
    tracks: list[dict[str, Any]],
) -> dict[str, dict[str, int | None]]:
    return get_listenbrainz_album_tracklist_with_release(artist, album, tracks)[0]


def get_listenbrainz_popularity_for_track(track: dict[str, Any]) -> dict[str, int | None]:
    mbid = extract_recording_mbid(track)
    if not mbid:
        return {"total_listen_count": None, "total_user_count": None}
    try:
        return lb_get_listenbrainz_popularity(mbid)
    except Exception:
        return {"total_listen_count": None, "total_user_count": None}


def get_listenbrainz_score_for_track(track: dict[str, Any]) -> int:
    mbid = extract_recording_mbid(track)
    if not mbid:
        return 0
    try:
        return int(lb_get_listenbrainz_score(mbid) or 0)
    except Exception:
        return 0


def get_lastfm_track_info(
    artist: str,
    title: str,
    track_mbid: str | None = None,
    album_artist: str | None = None,
    lastfm_client: LastFmClient | None = None,
) -> dict[str, Any]:
    if lastfm_client is None:
        return {"track_play": 0, "listeners": 0}

    results = []
    for candidate in get_artist_lookup_candidates(artist, album_artist=album_artist):
        try:
            results.append(lastfm_client.get_track_info(candidate, title, track_mbid=track_mbid))
        except Exception:
            continue

    return choose_best_provider_counts(results) if results else {"track_play": 0, "listeners": 0}


def get_lastfm_artist_max_listeners(
    artist: str,
    api_key: str | None = None,
) -> int:
    if not api_key:
        from helpers.config_helpers import get_config
        cfg = get_config()
        api_key = cfg.get("api_integrations", {}).get("lastfm", {}).get("api_key", "")
    if not api_key:
        return 0

    artist_key = artist.casefold().strip()

    with _CACHE_LOCK:
        cached = _lastfm_artist_max_cache.get(artist_key)
    if cached is not None:
        return cached

    from api_clients.lastfm_http import LastFmHttpClient
    client = LastFmHttpClient(api_key=api_key)

    try:
        from api_clients.lastfm import LastFmClient as _FacadeLastFmClient
        from services.popularity.popularity_cache_service import get_artist_top_tracks_map
        _map = get_artist_top_tracks_map(_FacadeLastFmClient(api_key=api_key), artist) or {}
        _peak = max((int(e.get("lastfm_listeners") or 0) for e in _map.values()), default=0)
        if _peak > 0:
            with _CACHE_LOCK:
                _lastfm_artist_max_cache[artist_key] = _peak
            return _peak
    except Exception:
        pass

    try:
        data = client.get_json(
            "artist.getTopTracks",
            timeout=10,
            artist=artist,
            limit=100,
        )
        if not data or "error" in data:
            with _CACHE_LOCK:
                _lastfm_artist_max_cache[artist_key] = 0
            return 0

        tracks = data.get("toptracks", {}).get("track", [])
        if isinstance(tracks, dict):
            tracks = [tracks]

        max_listeners = 0
        for track in tracks:
            if isinstance(track, dict):
                try:
                    listeners = int(track.get("listeners", 0) or 0)
                    if listeners > max_listeners:
                        max_listeners = listeners
                except (ValueError, TypeError):
                    continue

        with _CACHE_LOCK:
            _lastfm_artist_max_cache[artist_key] = max_listeners
        return max_listeners

    except Exception as exc:
        logger.debug("Failed to get Last.fm top tracks", artist=artist, error=str(exc))
        with _CACHE_LOCK:
            _lastfm_artist_max_cache[artist_key] = 0
        return 0


_ONLINE_CATALOGUE_CACHE: dict[str, list[tuple[str, int]]] = {}


def get_online_artist_catalogue(
    artist: str,
    lastfm_client: Any = None,
    limit: int = 200,
) -> list[tuple[str, int]]:
    """Return ``[(normalized_title, listeners)]`` for an artist's GLOBAL catalogue.

    This is the ONLINE counterpart to ``finalise_stage.compute_track_artist_scores``,
    which can only read the LOCAL database. For an artist the library holds one
    or two songs by (the common compilation/soundtrack case) the local DB has no
    catalogue to rank against at all, so a genuinely huge song can never be
    recognised as such.

    ``artist.getTopTracks`` returns the artist's discography ordered by global
    playcount, so the returned position IS the artist's real-world popularity
    ranking for that song.  ``listeners`` is the same metric the scan already
    records on each track as ``lastfm_listeners``, which means a track can be
    compared against this list directly -- no scale conversion, no inference.

    Results are cached per artist for the process lifetime (mirroring
    ``_lastfm_artist_max_cache``) so a multi-disc compilation only ever pays for
    one request per credited artist.
    """
    artist_key = str(artist or "").casefold().strip()
    if not artist_key:
        return []

    with _CACHE_LOCK:
        cached = _ONLINE_CATALOGUE_CACHE.get(artist_key)
    if cached is not None:
        return cached

    entries: list[tuple[str, int]] = []
    try:
        if lastfm_client is None:
            from helpers.config_helpers import get_config
            api_key = (get_config().get("api_integrations", {}) or {}).get("lastfm", {}).get("api_key", "")
            if api_key:
                from api_clients.lastfm import LastFmClient
                lastfm_client = LastFmClient(api_key=api_key)

        if lastfm_client is not None:
            from services.popularity.popularity_cache_service import get_artist_top_tracks_map
            _map = get_artist_top_tracks_map(lastfm_client, artist) or {}
            for norm_title, counts in _map.items():
                listeners = int((counts or {}).get("lastfm_listeners") or 0)
                if norm_title and listeners > 0:
                    entries.append((str(norm_title), listeners))
    except Exception as exc:
        logger.debug("Online artist catalogue fetch failed", artist=artist, error=str(exc))
        entries = []

    entries.sort(key=lambda item: item[1], reverse=True)

    with _CACHE_LOCK:
        _ONLINE_CATALOGUE_CACHE[artist_key] = entries
    return entries


def get_online_artist_track_rank(
    artist: str,
    track_title: str,
    lastfm_client: Any = None,
) -> dict[str, Any]:
    """Locate ``track_title`` in the artist's GLOBAL catalogue.

    Returns ``{"rank", "total", "percentile", "listeners", "top_listeners",
    "source"}`` where ``rank`` is 1-based (1 = the artist's most popular song
    worldwide) and ``percentile`` is ``rank / total`` (0.02 = top 2%).

    ``rank``/``total`` are ``0``/``0`` when the artist has no online catalogue
    or the title is not in it -- callers must treat that as "unknown", never as
    "unpopular", so a lookup miss cannot demote a track.
    """
    empty = {
        "rank": 0, "total": 0, "percentile": 0.0,
        "listeners": 0, "top_listeners": 0, "source": "none",
    }
    target = normalize_for_aggregation(track_title)
    if not target:
        return empty

    catalogue = get_online_artist_catalogue(artist, lastfm_client=lastfm_client)
    if not catalogue:
        return empty

    for index, (norm_title, listeners) in enumerate(catalogue, start=1):
        if norm_title == target:
            return {
                "rank": index,
                "total": len(catalogue),
                # MID-RANK percentile. A plain ``rank / total`` would put the
                # artist's #1 song at 1/47 = 2.1%, just OUTSIDE a 2% 5★ cut-off
                # -- so the most popular song the artist ever released could
                # never reach the top band on a catalogue of more than 50
                # entries. The midpoint of the rank's interval keeps rank #1
                # inside the top band for any catalogue size while preserving
                # the ordering for every other rank.
                "percentile": (index - 0.5) / len(catalogue),
                "listeners": listeners,
                "top_listeners": int(catalogue[0][1]),
                "source": "lastfm_artist_top_tracks",
            }

    # Not found: the artist HAS a catalogue but this title is not among its
    # charted tracks. Report it as a long-tail track rather than a miss, so it
    # lands at the bottom of the ladder instead of being skipped entirely.
    return {
        "rank": len(catalogue) + 1,
        "total": len(catalogue),
        "percentile": 1.0,
        "listeners": 0,
        "top_listeners": int(catalogue[0][1]),
        "source": "lastfm_artist_top_tracks_beyond",
    }


def get_aggregated_lastfm_popularity(
    artist: str,
    track_title: str,
    lastfm_client: Any = None,
    isrc: str | None = None,
    recording_mbid: str | None = None,
    is_live_release: bool = False,
    target_is_alt_rendition: bool = False,
) -> dict[str, Any]:
    """Aggregate Last.fm listener counts for one track.

    ``is_live_release`` must be set when the track belongs to a LIVE release.
    Last.fm resolves by title+artist, so a plainly titled live track (e.g.
    every track on "S&M") otherwise matches the studio recording of the same
    name and absorbs its catalogue-wide listener count.

    ``target_is_alt_rendition`` is the same guard for an alternate RENDITION
    that is not a live recording -- an unplugged/acoustic/remix take whose own
    library title does not say so. The caller learns it from the album's
    MusicBrainz release tracklist (see
    ``scan_stage_runner._build_album_recording_batch``), which is the only place
    the marker survives once a library title has lost it. Both flags mean the
    same thing to the lookup below -- "the target is an alternate performance,
    so a differently-typed candidate must not be merged into it" -- and they are
    kept separate because liveness ALSO drives the live weight penalty and the
    live star caps, which a studio unplugged take must not incur.
    """
    if lastfm_client is None:
        return {"listeners": 0, "track_play": 0, "matched_tracks": []}

    # The target is an alternate performance -> a plain studio namesake (and a
    # differently-typed take) must not be summed into it.
    target_is_alt = bool(is_live_release) or bool(target_is_alt_rendition)

    track_title = strip_cover_attribution(track_title) or track_title
    is_featured = (
        "feat" in str(artist or "").casefold()
        or "feat" in str(track_title or "").casefold()
    )

    primary_artist = get_primary_artist_preserve_case(artist)
    artist_key = primary_artist.casefold().strip()
    catalog = []

    try:
        from services.popularity.popularity_cache_service import get_artist_top_tracks_map
        _map = get_artist_top_tracks_map(lastfm_client, primary_artist) or {}
        catalog = [
            {
                "name": key,
                "listeners": int(e.get("lastfm_listeners") or 0),
                "playcount": int(e.get("lastfm_playcount") or 0),
            }
            for key, e in _map.items()
            if e.get("lastfm_listeners")
        ]
    except Exception:
        catalog = []

    with _CACHE_LOCK:
        in_cache = artist_key in _lastfm_artist_catalog_cache
        if in_cache and not catalog:
            catalog = _lastfm_artist_catalog_cache[artist_key]

    if not catalog and not in_cache and hasattr(lastfm_client, "get_artist_top_tracks"):
        try:
            fetched_catalog = lastfm_client.get_artist_top_tracks(primary_artist)
            with _CACHE_LOCK:
                _lastfm_artist_catalog_cache[artist_key] = fetched_catalog
                catalog = fetched_catalog
        except Exception:
            catalog = []

    target = normalize_for_aggregation(track_title)
    matched = []
    listeners = 0
    playcount = 0

    for item in catalog or []:
        item_title = item.get("name") or item.get("title") or ""
        if normalize_for_aggregation(item_title) != target:
            continue
        # Same normalized title can still be a live/remix/alternate take
        # (normalization strips punctuation, not performance-type markers) -
        # don't let it merge with a differently-typed target. Release-level
        # liveness and the album release's own version marker are folded in so a
        # plainly titled live/unplugged track does not match its studio
        # namesake.
        if not _alt_status_matches_ctx(track_title, item_title, target_is_live=target_is_alt):
            continue
        matched.append(item)
        listeners += int(item.get("listeners", 0) or 0)
        playcount += int(item.get("playcount", item.get("track_play", 0)) or 0)

    if not matched and ARTIST_JOIN_RE.search(artist or ""):
        collab_parts = [p.strip() for p in ARTIST_JOIN_RE.split(artist or "") if p.strip()]
        if len(collab_parts) >= 2:
            for part in collab_parts:
                try:
                    from services.popularity.popularity_cache_service import get_artist_top_tracks_map
                    _part_map = get_artist_top_tracks_map(lastfm_client, part) or {}
                    _part_catalog = [
                        {
                            "name": key,
                            "listeners": int(e.get("lastfm_listeners") or 0),
                            "playcount": int(e.get("lastfm_playcount") or 0),
                        }
                        for key, e in _part_map.items()
                        if e.get("lastfm_listeners")
                    ]

                    _key = part.casefold().strip()
                    with _CACHE_LOCK:
                        part_in_cache = _key in _lastfm_artist_catalog_cache

                    if not _part_catalog and not part_in_cache and hasattr(lastfm_client, "get_artist_top_tracks"):
                        _fetched = lastfm_client.get_artist_top_tracks(part)
                        with _CACHE_LOCK:
                            _lastfm_artist_catalog_cache[_key] = _fetched
                        _part_catalog = _fetched

                    for item in _part_catalog or []:
                        item_title = item.get("name") or item.get("title") or ""
                        if normalize_for_aggregation(item_title) != target:
                            continue
                        if not _alt_status_matches_ctx(
                            track_title, item_title, target_is_live=target_is_alt
                        ):
                            continue
                        matched.append(item)
                        listeners += int(item.get("listeners", 0) or 0)
                        playcount += int(item.get("playcount", item.get("track_play", 0)) or 0)
                except Exception:
                    continue

    if is_featured or not matched:
        search = get_search_aggregated_lastfm_popularity(
            artist,
            track_title,
            lastfm_client=lastfm_client,
            is_live_release=is_live_release,
            target_is_alt_rendition=target_is_alt_rendition,
        )
        search_listeners = int(search.get("listeners") or 0)
        if search_listeners > listeners:
            listeners = search_listeners
            playcount = int(search.get("track_play") or 0)
            matched = search.get("matched_tracks") or matched

    if matched:
        return {"listeners": listeners, "track_play": playcount, "matched_tracks": matched}

    # Final fallback: a bare title+artist lookup. This CANNOT distinguish a
    # live/unplugged/remix take from its studio namesake, so it is skipped
    # entirely on a live release or a known alternate rendition rather than
    # returning a known-contaminated count.
    if target_is_alt:
        logger.debug(
            "Skipping Last.fm title+artist fallback on alternate rendition",
            artist=artist,
            track=track_title,
            live=bool(is_live_release),
            alt_rendition=bool(target_is_alt_rendition),
        )
        return {"listeners": 0, "track_play": 0, "matched_tracks": []}

    try:
        info = lastfm_client.get_track_info(artist, track_title)
        primary = {
            "listeners": int(info.get("listeners", 0) or 0),
            "track_play": int(info.get("track_play", 0) or 0),
            "matched_tracks": [],
        }
    except Exception:
        primary = {"listeners": 0, "track_play": 0, "matched_tracks": []}

    variants: dict[str, tuple[int, int]] = {"Primary": (primary["listeners"], primary["track_play"])}
    best = primary
    best_key = "Primary"

    def _consider(arm: str, stats: dict[str, Any]) -> None:
        nonlocal best, best_key
        _listeners = int((stats or {}).get("listeners") or 0)
        _playcount = int((stats or {}).get("track_play") or (stats or {}).get("playcount") or 0)
        variants[arm] = (_listeners, _playcount)
        if _listeners > best["listeners"] or (_listeners == best["listeners"] and _playcount > best["track_play"]):
            best = {"listeners": _listeners, "track_play": _playcount, "matched_tracks": []}
            best_key = arm

    need_fallback = (best["listeners"] == 0 and best["track_play"] == 0) or _is_featured_artist(artist)

    if need_fallback:
        if isrc or recording_mbid:
            try:
                _isrc_rec = None
                if isrc:
                    _isrc_rec = resolve_isrc_recording(
                        isrc,
                        title=track_title,
                        artist=artist,
                        # ``target_is_alt``, not ``is_live_release``: the ISRC
                        # arm must not resolve a plain studio ISRC for an
                        # unplugged/acoustic take either.
                        is_live_release=target_is_alt,
                    )
                _arm_mbid = ((_isrc_rec or {}).get("recording_mbid") or recording_mbid)
                _arm_artist = (_isrc_rec or {}).get("artist") or artist
                _arm_title = (_isrc_rec or {}).get("title") or track_title

                if _arm_mbid:
                    _isrc_stats = lastfm_client.get_track_info(_arm_artist, _arm_title, track_mbid=_arm_mbid)
                    _consider("ISRC", _isrc_stats)
            except Exception as exc:
                logger.debug("Last.fm ISRC fallback arm failed", error=str(exc))

        if _is_featured_artist(artist):
            try:
                inverted = invert_featured_artist(artist)
                if inverted != artist:
                    _inv_stats = lastfm_client.get_track_info(inverted, track_title)
                    _consider("Inverted", _inv_stats)
            except Exception as exc:
                logger.debug("Last.fm inverted arm failed", artist=artist, error=str(exc))

    if best_key != "Primary":
        _detail = {k: v[0] for k, v in variants.items() if v[0] > 0}
        logger.info(
            "Last.fm multi-arm lookup completed",
            variants_queried=len(variants),
            max_listeners=best["listeners"],
            details=_detail,
        )
        return {
            "listeners": best["listeners"],
            "track_play": best["track_play"],
            "matched_tracks": best["matched_tracks"],
            "sources_queried": len(variants),
            "variant_detail": _detail,
        }
    return best


def get_search_aggregated_lastfm_popularity(
    artist: str,
    track_title: str,
    lastfm_client: Any = None,
    is_live_release: bool = False,
    target_is_alt_rendition: bool = False,
) -> dict[str, Any]:
    """Sum Last.fm listener counts across compatible title variants.

    ``is_live_release`` gates the alt-performance check the same way it does in
    ``get_aggregated_lastfm_popularity`` -- without it this function sums the
    studio recording into a live track's total, which is the single largest
    source of cross-version contamination. ``target_is_alt_rendition`` closes
    the same hole for an unplugged/acoustic/remix take whose library title lost
    its marker.
    """
    if lastfm_client is None:
        return {"listeners": 0, "track_play": 0, "matched_tracks": []}

    target_is_alt = bool(is_live_release) or bool(target_is_alt_rendition)

    track_title = strip_cover_attribution(track_title) or track_title
    target = normalize_for_aggregation(track_title)
    if not target:
        return {"listeners": 0, "track_play": 0, "matched_tracks": []}

    matched: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _collect(candidate: str) -> None:
        try:
            results = lastfm_client.search_track(candidate, track_title, limit=20)
        except Exception:
            results = []

        for item in results or []:
            if not isinstance(item, dict):
                continue
            item_title = str(item.get("name") or item.get("title") or "")
            if not item_title:
                continue

            if not title_variants_compatible(track_title, item_title):
                continue

            # A "compatible" title (e.g. same base song, different
            # bracketed suffix) can still be a live/remix/alternate take -
            # reject it unless it agrees with the target on that, with
            # release-level liveness and the album release's own version
            # marker folded in.
            if not _alt_status_matches_ctx(track_title, item_title, target_is_live=target_is_alt):
                continue

            _item_key = normalize_for_aggregation(item_title)
            if _item_key != target and _token_similarity(_item_key, target) < 0.90:
                continue

            url = str(item.get("url") or "").strip()
            key = url or f"{str(item.get('artist') or '').casefold()}::{item_title.casefold()}"

            if key in seen:
                continue

            seen.add(key)
            matched.append(item)

    primary = get_primary_artist_preserve_case(artist)
    ordered_candidates = [primary] + [
        c for c in get_artist_lookup_candidates(artist)
        if c.casefold() != primary.casefold()
    ]

    for candidate in ordered_candidates:
        _collect(candidate)

    listeners = sum(int(item.get("listeners") or 0) for item in matched)
    playcount = sum(int(item.get("playcount") or item.get("track_play") or 0) for item in matched)

    if matched:
        logger.info(
            "Aggregated Last.fm versions",
            count=len(matched),
            artist=artist,
            track=track_title,
            listeners=listeners,
            plays=playcount,
            live_release=bool(is_live_release),
        )
        return {"listeners": listeners, "track_play": playcount, "matched_tracks": matched}

    # As above: the bare lookup cannot tell a live take from its studio
    # namesake, so it is not used on live releases.
    if is_live_release:
        return {"listeners": 0, "track_play": 0, "matched_tracks": []}

    try:
        info = lastfm_client.get_track_info(artist, track_title)
        return {
            "listeners": int(info.get("listeners") or 0),
            "track_play": int(info.get("track_play") or 0),
            "matched_tracks": [],
        }
    except Exception:
        return {"listeners": 0, "track_play": 0, "matched_tracks": []}


def get_aggregated_listenbrainz_popularity(
    title: str,
    artist: str,
    primary_mbid: str | None = None,
    isrc: str | None = None,
    lb_client: Any = None,
    mb_client: Any = None,
    is_live_release: bool = False,
) -> dict[str, Any]:
    logger.debug("Fetching aggregated ListenBrainz popularity")
    mbids: set[str] = set()

    if primary_mbid:
        mbids.add(primary_mbid)

    if isrc and not primary_mbid:
        try:
            _isrc_rec = resolve_isrc_recording(
                isrc, title=title, artist=artist, is_live_release=is_live_release
            )
            _isrc_mbid = (_isrc_rec or {}).get("recording_mbid")
            if _isrc_mbid:
                mbids.add(_isrc_mbid)
                logger.debug("LB aggregation resolved ISRC to recording", isrc=isrc, recording_mbid=_isrc_mbid)
        except Exception:
            pass

    if mb_client is None:
        try:
            from services.enrichment.musicbrainz_service import get_shared_mb_client
            mb_client = get_shared_mb_client()
        except Exception:
            mb_client = None

    if mb_client and hasattr(mb_client, "search_recordings"):
        try:
            from helpers.normalization_service import (
                normalize_title_for_lucene_query,
                normalize_title_for_lookup,
                strip_single_release_suffix,
            )
            from api_clients.musicbrainz_http import escape_lucene_special_chars

            query = (
                f'recording:"{escape_lucene_special_chars(normalize_title_for_lucene_query(title))}" '
                f'AND artist:"{escape_lucene_special_chars(artist)}"'
            )
            norm_target = normalize_title_for_lookup(strip_single_release_suffix(title) or title)

            for rec in mb_client.search_recordings(query, limit=20):
                rec_id = rec.get("id")
                rec_title = str(rec.get("title") or "")
                if not rec_id or not rec_title:
                    continue

                # Only reject a candidate recording when it *disagrees* with
                # the target on being a live/remix/alternate performance.
                # A blanket exclusion of every alt-performance title would
                # also exclude the correct match when the target track
                # itself is a live/remix/alternate version.
                if not _alt_status_matches_ctx(title, rec_title, target_is_live=is_live_release):
                    continue

                norm_rec = normalize_title_for_lookup(strip_single_release_suffix(rec_title) or rec_title)
                if norm_rec == norm_target or _token_similarity(norm_rec, norm_target) >= 0.85:
                    mbids.add(rec_id)
        except Exception:
            pass

    if not mbids:
        return {"total_listen_count": 0, "total_user_count": 0, "mbids": []}

    try:
        batch = lb_get_recording_popularity_batch(list(mbids))
        listen_count = sum(int((batch.get(mbid) or {}).get("total_listen_count") or 0) for mbid in mbids)
        user_count = sum(int((batch.get(mbid) or {}).get("total_user_count") or 0) for mbid in mbids)

        logger.debug(
            "Aggregated LB recordings",
            artist=artist,
            track=title,
            listens=listen_count,
            users=user_count,
            recordings_count=len(mbids),
        )
        return {"total_listen_count": listen_count, "total_user_count": user_count, "mbids": sorted(mbids)}
    except Exception as exc:
        logger.debug("Aggregated LB failed", artist=artist, track=title, error=str(exc))
        return {"total_listen_count": 0, "total_user_count": 0, "mbids": sorted(mbids)}


def _recording_work_mbid(recording: dict[str, Any]) -> str:
    for rel in recording.get("relations") or []:
        if not isinstance(rel, dict):
            continue
        if str(rel.get("type") or "").lower() != "performance":
            continue
        work = rel.get("work") or {}
        wid = str(work.get("id") or "").strip()
        if wid:
            return wid
    return ""


def _recording_artist_mbids(recording: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for credit in recording.get("artist-credit") or []:
        if not isinstance(credit, dict):
            continue
        art = credit.get("artist") or {}
        aid = str(art.get("id") or "").strip()
        if aid:
            out.add(aid)
    return out


def _recording_primary_artist(recording: dict[str, Any]) -> str:
    for credit in recording.get("artist-credit") or []:
        if not isinstance(credit, dict):
            continue
        art = credit.get("artist") or {}
        name = str(art.get("name") or credit.get("name") or "").strip()
        if name:
            return name
    return ""


def _empty_work_lb_result() -> dict[str, Any]:
    return {
        "total_listen_count": 0,
        "total_user_count": 0,
        "mbids": [],
        "work_mbid": "",
        "source": "work",
    }


def get_work_level_listenbrainz_popularity(
    title: str,
    artist: str,
    artist_mbid: str = "",
    primary_mbid: str = "",
    isrc: str = "",
    lb_client: Any = None,
    mb_client: Any = None,
    work_mbid_hint: str = "",
    is_live_release: bool = False,
) -> dict[str, Any]:
    """Work-level aggregated ListenBrainz popularity.

    Division of labour (intentional): MusicBrainz supplies the WORK GRAPH
    (which recording -> which work, all recordings of the work) and
    ListenBrainz supplies the LISTEN COUNTS -- MusicBrainz has no listening
    data, so the play counts must always come from ListenBrainz.

    ``work_mbid_hint`` lets a caller that already resolved the work MBID
    (e.g. from the release metadata's embedded work-rels) skip the per-track
    ``get_recording(work-rels)`` MusicBrainz request -- the 1 req/s bottleneck.

    ``is_live_release`` matters especially here: a work groups EVERY recording
    of a song, studio and live alike, so without release-level liveness a live
    track aggregates the whole work's listen counts.
    """
    logger.debug("Fetching Work-level aggregated ListenBrainz popularity")
    if mb_client is None:
        try:
            from services.enrichment.musicbrainz_service import get_shared_mb_client
            mb_client = get_shared_mb_client()
        except Exception:
            mb_client = None

    if mb_client is None:
        return _empty_work_lb_result()

    work_mbid = str(work_mbid_hint or "").strip()
    seed_mbids: set[str] = set()
    if primary_mbid:
        seed_mbids.add(primary_mbid)
    elif isrc:
        try:
            _isrc_rec = resolve_isrc_recording(
                isrc,
                title=title,
                artist=artist,
                mb_client=mb_client,
                is_live_release=is_live_release,
            )
            if _isrc_rec and _isrc_rec.get("recording_mbid"):
                seed_mbids.add(_isrc_rec["recording_mbid"])
        except Exception:
            pass

    if not work_mbid:
        for seed_mbid in seed_mbids:
            try:
                rec = mb_client.get_recording(seed_mbid, inc="work-rels+artist-credits")
                if not rec or not rec.get("id"):
                    continue
                work_mbid = _recording_work_mbid(rec)
                if work_mbid:
                    break
            except Exception as exc:
                logger.debug("Recording work-rels fetch failed", seed_mbid=seed_mbid, error=str(exc))

    if not work_mbid:
        logger.debug("No Work resolvable for track", artist=artist, track=title)
        return _empty_work_lb_result()

    recordings: list[dict[str, Any]] = []
    try:
        recordings = mb_client.browse_work_recordings(work_mbid, inc="artist-credits", limit=100) or []
    except Exception as exc:
        logger.debug("Work recording browse failed", work_mbid=work_mbid, error=str(exc))

    mbids: set[str] = set(seed_mbids)

    from helpers.normalization_service import strip_featured_artist
    target_artist = normalize_for_aggregation(strip_featured_artist(artist) or artist)

    for rec in recordings:
        if not isinstance(rec, dict):
            continue
        rec_id = str(rec.get("id") or "").strip()
        rec_title = str(rec.get("title") or "")

        if not rec_id or not rec_title:
            continue
        # Same reasoning as get_aggregated_listenbrainz_popularity above:
        # only reject recordings that *disagree* with the target track on
        # alt-performance status, so a live/remix/alternate target can still
        # match its own recordings within the work, without pulling in a
        # different studio release's counts (or vice versa).
        if not _alt_status_matches_ctx(title, rec_title, target_is_live=is_live_release):
            continue

        rec_artist_mbids = _recording_artist_mbids(rec)
        if artist_mbid:
            if artist_mbid not in rec_artist_mbids:
                continue
        else:
            rec_artist = _recording_primary_artist(rec)
            if not rec_artist or normalize_for_aggregation(rec_artist) != target_artist:
                continue

        mbids.add(rec_id)

    if not mbids:
        return _empty_work_lb_result()

    try:
        batch = lb_get_recording_popularity_batch(list(mbids))
        listen_count = sum(int((batch.get(mbid) or {}).get("total_listen_count") or 0) for mbid in mbids)
        user_count = sum(int((batch.get(mbid) or {}).get("total_user_count") or 0) for mbid in mbids)

        logger.debug(
            "Aggregated Work-level LB",
            artist=artist,
            track=title,
            work_mbid=work_mbid,
            listens=listen_count,
            users=user_count,
            recordings_count=len(mbids),
        )
        return {
            "total_listen_count": listen_count,
            "total_user_count": user_count,
            "mbids": sorted(mbids),
            "work_mbid": work_mbid,
            "source": "work",
        }
    except Exception as exc:
        logger.debug("Work-level LB aggregation failed", artist=artist, track=title, error=str(exc))
        return _empty_work_lb_result()


def get_metadata_sources_info(single_sources: list[str]) -> dict[str, Any]:
    has_discogs = "discogs" in single_sources or "discogs_video" in single_sources
    has_spotify = "spotify" in single_sources
    has_musicbrainz = "musicbrainz" in single_sources
    has_lastfm = "lastfm" in single_sources
    has_version_count = "version_count" in single_sources

    has_metadata = has_discogs or has_spotify or has_musicbrainz or has_lastfm

    sources_list: list[str] = []
    if has_discogs:
        sources_list.append("Discogs")
    if has_spotify:
        sources_list.append("Spotify")
    if has_musicbrainz:
        sources_list.append("MusicBrainz")
    if has_lastfm:
        sources_list.append("Last.fm")
    if has_version_count:
        sources_list.append("Version Count")

    return {
        "has_discogs": has_discogs,
        "has_spotify": has_spotify,
        "has_musicbrainz": has_musicbrainz,
        "has_lastfm": has_lastfm,
        "has_version_count": has_version_count,
        "has_metadata": has_metadata,
        "sources_list": sources_list,
    }
