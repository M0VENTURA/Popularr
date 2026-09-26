"""Propose MusicBrainz metadata for an album — WITHOUT writing anything.

One engine, two callers:

*   the album page's **Lookup MBID** flow, which fills the Edit Album form and
    paints an orange "recommended" bar under every field it would change, then
    waits for the user to press *Save Metadata*;
*   the **popularity scan**, which — when metadata updating is switched OFF in
    ``config.yaml`` — stashes the same proposals on the track rows
    (``tracks.pending_mb_updates``) so an album/artist page can offer them
    later as "save or discard".

Both need the same answer to one question: *given this release, what would a
metadata import write?*  That answer must be produced without side effects —
the caller decides whether to persist it.

Design notes
------------
*   Nothing here writes to the database, to a file, or through a repository.
    The single read is the local tracklist used to compute "current" values.
*   Matching reuses :func:`services.enrichment.musicbrainz_service.compare_musicbrainz_release`,
    so a proposal is built from exactly the same track matching (disc+track
    first, fuzzy title fallback) the Compare button already uses.  A second,
    independent matcher would let the preview and the diff disagree.
*   Per-track *enrichment* (writer, cover verdict, genres) comes from
    ``fetch_musicbrainz_release_metadata``, which is the shape authority for a
    release payload — the same function the album save fans out with.
*   Fields the user has already permanently ignored (``tracks.mb_ignored_fields``)
    are never proposed again, mirroring ``_match_mb_tracks_to_library``.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Field specifications
# ---------------------------------------------------------------------------
# (form field id, human label, MusicBrainz metadata key)
# The form id is what the album page's ``setFieldValue`` writes to and what the
# orange bar is anchored under, so it must match the template's input ids.
_ALBUM_FIELD_SPECS: tuple[tuple[str, str, str], ...] = (
    ("album_title", "Album Title", "release_group_title"),
    ("album_artist", "Album Artist", "artist"),
    ("album_release_title", "Release Name", "specific_release_title"),
    ("album_originalyear", "Original Year", "original_year"),
    ("release_year", "Release Year", "version_release_year"),
    ("album_type", "Album Type", "album_type"),
    ("album_mbid", "MusicBrainz Release ID", "release_mbid"),
    ("album_release_group_mbid", "MusicBrainz Release Group ID", "release_group_mbid"),
    ("artist_mbid", "MusicBrainz Artist ID", "album_artist_mbid"),
    ("album_recordlabel", "Record Label", "recordlabel"),
    ("album_catalognumber", "Catalog Number", "catalognumber"),
    ("album_barcode", "Barcode", "barcode"),
    ("album_releasedate", "Release Date", "releasedate"),
    ("album_media", "Media Format", "media"),
    ("album_releasecountry", "Release Country", "releasecountry"),
)

#: Per-track proposals: (DB column / field name, human label, MB track key).
#: ``field`` is the column name so the staged payload can be applied verbatim.
_TRACK_FIELD_SPECS: tuple[tuple[str, str, str], ...] = (
    ("title", "Title", "mb_title"),
    ("track_number", "Track #", "mb_track_number"),
    ("disc_number", "Disc #", "mb_disc_number"),
    ("mbid", "MusicBrainz Recording ID", "mb_recording_mbid"),
    ("writer", "Writer", "writer"),
    ("musicbrainz_genres", "Genres", "musicbrainz_genres"),
)

#: Track-level enrichment keys reported separately because they are not a
#: simple "current -> proposed" string swap.
_COVER_KEYS = ("is_cover", "original_cover_artist")

#: Local columns the proposal engine reads to compute "current" values.
_LOCAL_TRACKS_SQL = """
    SELECT *
    FROM tracks
    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
      AND LOWER(COALESCE(album, '')) = LOWER(:album)
    ORDER BY COALESCE(disc_number, '1'), COALESCE(track_number, '999')
"""


def _as_text(value: Any) -> str:
    """Normalise any scalar to a trimmed string (``None`` -> ``""``)."""
    if value is None:
        return ""
    return str(value).strip()


def _norm(value: Any) -> str:
    """Comparison key — case- and whitespace-insensitive."""
    return " ".join(_as_text(value).casefold().split())


def _load_local_tracks(artist: str, album: str) -> list[dict[str, Any]]:
    """Read the album's local tracks (the only DB access in this module)."""
    try:
        with db_session() as session:
            rows = session.execute(
                text(_LOCAL_TRACKS_SQL), {"artist": artist, "album": album}
            ).fetchall()
    except Exception as exc:
        logger.warning(
            "Metadata proposal: local track read failed",
            artist=artist, album=album, error=str(exc),
        )
        return []
    return [dict(row._mapping) for row in rows or []]


def _ignored_fields(row: dict[str, Any]) -> set[str]:
    """Fields permanently ignored for this track (``mb_ignored_fields``)."""
    import json

    raw = row.get("mb_ignored_fields")
    if not raw:
        return set()
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return set()
    if not isinstance(parsed, list):
        return set()
    return {str(item) for item in parsed}


def _album_genres(mb_tracks: list[dict[str, Any]]) -> str:
    """Union of every recording's MusicBrainz genres, in first-seen order."""
    seen: list[str] = []
    for track in mb_tracks:
        raw = track.get("musicbrainz_genres") or ""
        for genre in str(raw).split(","):
            genre = genre.strip()
            if genre and genre.casefold() not in {g.casefold() for g in seen}:
                seen.append(genre)
    return ", ".join(seen)


def _album_level_proposals(
    local_tracks: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build the album-level "current -> proposed" list.

    Album-level values are duplicated across the album's track rows, so the
    first row is the representative "current" value.
    """
    if not local_tracks:
        return []

    head = local_tracks[0]

    # ``album_title`` is the release-GROUP name (the album's main identity) and
    # the release-group key is authoritative; fall back to the release title
    # only when MusicBrainz exposes no group name.
    resolved: dict[str, str] = {}
    for form_field, _label, mb_key in _ALBUM_FIELD_SPECS:
        resolved[form_field] = _as_text(metadata.get(mb_key))
    if not resolved.get("album_title"):
        resolved["album_title"] = _as_text(metadata.get("release_title"))

    resolved["album_genres"] = _album_genres(metadata.get("tracks") or [])

    # "Current" values, read from the representative row.  A couple of the
    # form fields do not map 1:1 onto a column name.
    current: dict[str, str] = {
        "album_title": _as_text(head.get("album")),
        "album_artist": _as_text(head.get("album_artist") or head.get("artist")),
        "album_release_title": _as_text(
            head.get("release_title") or head.get("albumversion")
        ),
        "album_originalyear": _as_text(head.get("year") or head.get("originalyear")),
        "release_year": _as_text(head.get("release_year")),
        "album_type": _as_text(
            head.get("spotify_album_type")
            or head.get("musicbrainz_albumtype")
            or head.get("releasetype")
        ),
        "album_mbid": _as_text(
            head.get("musicbrainz_album_mbid") or head.get("musicbrainz_albumid")
        ),
        "album_release_group_mbid": _as_text(head.get("musicbrainz_releasegroupid")),
        "artist_mbid": _as_text(head.get("musicbrainz_artistid")),
        "album_recordlabel": _as_text(head.get("recordlabel")),
        "album_catalognumber": _as_text(head.get("catalognumber")),
        "album_barcode": _as_text(head.get("barcode")),
        "album_releasedate": _as_text(head.get("releasedate")),
        "album_media": _as_text(head.get("media")),
        "album_releasecountry": _as_text(head.get("releasecountry")),
        "album_genres": _as_text(head.get("genres")),
    }

    proposals: list[dict[str, Any]] = []
    for form_field in list(resolved) + (["album_genres"] if "album_genres" in resolved else []):
        proposed = _as_text(resolved.get(form_field))
        if not proposed:
            continue
        if _norm(proposed) == _norm(current.get(form_field)):
            continue
        label = next(
            (lbl for fid, lbl, _key in _ALBUM_FIELD_SPECS if fid == form_field),
            "Genres" if form_field == "album_genres" else form_field,
        )
        proposals.append({
            "field": form_field,
            "label": label,
            "current": current.get(form_field, ""),
            "proposed": proposed,
        })

    return proposals


def _duration_checks(comparison: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Report tracks whose LENGTH does not match MusicBrainz.

    INFORMATIONAL ONLY — deliberately not routed through ``_TRACK_FIELD_SPECS``
    or ``changes``. A file's duration is intrinsic to the audio, so an import
    cannot "write" it; putting it in ``changes`` would render an "Included"
    toggle that silently discards the value on save, because ``duration`` is not
    in ``routes/ui_routes.py``'s ``_STAGED_WRITABLE`` whitelist.

    It still belongs in the review: a length mismatch is the clearest evidence
    the file is a DIFFERENT VERSION of the recording (a radio edit, a live take,
    an extended mix) rather than a tagging problem. Silence here would let a
    wrong file be "corrected" into a confident-looking wrong album.

    Reads the server-normalised fields rather than recomputing, so the review
    and the Compare button can never disagree about whether a track differs.
    """
    checks: list[dict[str, Any]] = []
    for entry in comparison:
        if not entry.get("matched") or not entry.get("library_track_id"):
            continue
        diff = entry.get("diff_fields") or []
        if "duration" not in diff:
            continue
        checks.append({
            "track_id": _as_text(entry.get("library_track_id")),
            "title": _as_text(entry.get("library_title")),
            "track_number": _as_text(entry.get("library_track_number")),
            "library_duration": _as_text(entry.get("library_duration_display")),
            "mb_duration": _as_text(entry.get("mb_duration_display")),
        })
    return checks


def _track_proposals(
    local_tracks: list[dict[str, Any]],
    comparison: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build per-track "current -> proposed" lists.

    Track matching comes from ``compare_musicbrainz_release`` (so the preview
    and the Compare button can never disagree); per-track enrichment comes from
    the release metadata, joined on the recording MBID.
    """
    by_id = {_as_text(row.get("id")): row for row in local_tracks}

    enrichment: dict[str, dict[str, Any]] = {}
    for mb_track in metadata.get("tracks") or []:
        rec = _as_text(mb_track.get("mb_recording_mbid") or mb_track.get("recording_mbid"))
        if rec:
            enrichment[rec] = mb_track

    proposals: list[dict[str, Any]] = []
    for entry in comparison:
        track_id = _as_text(entry.get("library_track_id"))
        if not track_id or not entry.get("matched"):
            continue
        local = by_id.get(track_id)
        if local is None:
            continue

        ignored = _ignored_fields(local)
        rec_mbid = _as_text(entry.get("mb_recording_mbid"))
        mb_track = enrichment.get(rec_mbid, {})

        changes: list[dict[str, Any]] = []

        # ``current`` for the MB-derived keys is the matched library value from
        # the comparison; the enrichment keys read the local row directly.
        current_map: dict[str, str] = {
            "title": _as_text(local.get("title")),
            "track_number": _as_text(local.get("track_number")),
            "disc_number": _as_text(local.get("disc_number") or "1"),
            "mbid": _as_text(local.get("mbid")),
            "writer": _as_text(local.get("writer")),
            "musicbrainz_genres": _as_text(local.get("musicbrainz_genres")),
        }
        proposed_map: dict[str, str] = {
            "title": _as_text(entry.get("mb_title")),
            "track_number": _as_text(entry.get("mb_track_number")),
            "disc_number": _as_text(entry.get("mb_disc_number")),
            "mbid": rec_mbid,
            "writer": _as_text(mb_track.get("writer")),
            "musicbrainz_genres": _as_text(mb_track.get("musicbrainz_genres")),
        }

        for field, label, _key in _TRACK_FIELD_SPECS:
            if field in ignored:
                continue
            proposed = proposed_map.get(field, "")
            if not proposed:
                continue
            current = current_map.get(field, "")
            if _norm(proposed) == _norm(current):
                continue
            # Track/disc numbers are compared numerically-ish: "01" and "1"
            # are the same position and must not raise a bar.
            if field in {"track_number", "disc_number"}:
                try:
                    if int(float(current or 0)) == int(float(proposed)):
                        continue
                except (TypeError, ValueError):
                    pass
            changes.append({
                "field": field,
                "label": label,
                "current": current,
                "proposed": proposed,
            })

        # ── Cover verdict (a structured change, not a string swap) ──────────
        if "is_cover" not in ignored and mb_track.get("is_cover"):
            original_artist = _as_text(mb_track.get("original_cover_artist"))
            if not int(local.get("is_cover") or 0):
                changes.append({
                    "field": "is_cover",
                    "label": "Cover",
                    "current": "not a cover",
                    "proposed": (
                        f"cover of {original_artist}" if original_artist else "cover"
                    ),
                    "value": 1,
                    "original_cover_artist": original_artist,
                })

        if changes:
            proposals.append({
                "track_id": track_id,
                "title": _as_text(local.get("title")),
                "track_number": _as_text(local.get("track_number")),
                "changes": changes,
            })

    return proposals


def propose_album_metadata(
    artist: str,
    album: str,
    release_mbid: str,
) -> dict[str, Any]:
    """Return the metadata a MusicBrainz import WOULD write for this album.

    Writes nothing.  ``release_mbid`` may be a concrete release id or a
    release-group id — ``compare_musicbrainz_release`` resolves the concrete
    release inside the group.

    Returns::

        {
          "success": True,
          "release_mbid": "...", "release_group_mbid": "...", "release_title": "...",
          "album":     [{"field", "label", "current", "proposed"}, ...],
          "tracks":    [{"track_id", "title", "track_number",
                         "changes": [{"field", "label", "current", "proposed"}, ...]},
                        ...],
          "duration_checks": [{"track_id", "title", "track_number",
                              "library_duration", "mb_duration"}, ...],
          "counts":    {"album_changes": n, "tracks_changed": m, "track_changes": k,
                        "duration_mismatches": d},
          "missing":   [{"mb_track_number", "mb_title", ...}, ...],
          "extra":     [{"library_track_id", "library_title", ...}, ...],
        }

    ``duration_checks`` is INFORMATIONAL: a length difference means the file is
    probably a different version of the recording, but a file's duration cannot
    be written by a metadata import, so those entries are reported rather than
    staged. See :func:`_duration_checks`.
    """
    artist = _as_text(artist)
    album = _as_text(album)
    release_mbid = _as_text(release_mbid)

    if not release_mbid:
        return {
            "success": False,
            "error": "release_mbid is required",
            "album_changes": [], "track_changes": [], "missing": [], "extra": [],
            "counts": {"album_changes": 0, "tracks_changed": 0, "track_changes": 0},
        }

    local_tracks = _load_local_tracks(artist, album)
    if not local_tracks:
        return {
            "success": False,
            "error": "No library tracks found for this album",
            "album_changes": [], "track_changes": [], "missing": [], "extra": [],
            "counts": {"album_changes": 0, "tracks_changed": 0, "track_changes": 0},
        }

    # Imported lazily: musicbrainz_service imports helpers that would create a
    # cycle if this module were imported from its top level.
    from services.enrichment.musicbrainz_service import (
        compare_musicbrainz_release,
        fetch_musicbrainz_release_metadata,
    )

    comparison_result = compare_musicbrainz_release(artist, album, release_mbid) or {}
    if not comparison_result.get("success"):
        return {
            "success": False,
            "error": _as_text(comparison_result.get("error")) or "MusicBrainz comparison failed",
            "album_changes": [], "track_changes": [], "missing": [], "extra": [],
            "counts": {"album_changes": 0, "tracks_changed": 0, "track_changes": 0},
        }

    comparison = comparison_result.get("comparison") or []
    resolved_release_id = _as_text(
        comparison_result.get("mb_release_mbid") or comparison_result.get("release_mbid")
    )

    metadata = fetch_musicbrainz_release_metadata(resolved_release_id) or {}

    album_changes = _album_level_proposals(local_tracks, metadata)
    track_changes = _track_proposals(local_tracks, comparison, metadata)
    # Duration is reported SEPARATELY and never staged: a file's length is
    # intrinsic to the audio, so there is nothing a metadata import could write
    # for it. It is surfaced because a mismatch is the clearest sign the file is
    # a different version of the recording (a radio edit, a live take), which is
    # exactly what a metadata review should draw attention to.
    duration_checks = _duration_checks(comparison)

    return {
        "success": True,
        "artist": artist,
        "album": album,
        "release_mbid": resolved_release_id or release_mbid,
        "release_group_mbid": _as_text(
            comparison_result.get("mb_release_group_mbid")
            or comparison_result.get("release_group_mbid")
        ),
        "release_title": _as_text(metadata.get("release_title")),
        "album_changes": album_changes,
        "track_changes": track_changes,
        "duration_checks": duration_checks,
        "missing": [
            {
                "mb_track_number": entry.get("mb_track_number"),
                "mb_disc_number": entry.get("mb_disc_number"),
                "mb_title": entry.get("mb_title"),
                "mb_recording_mbid": entry.get("mb_recording_mbid"),
            }
            for entry in comparison
            if not entry.get("matched")
        ],
        "extra": comparison_result.get("extra_tracks") or [],
        "counts": {
            "album_changes": len(album_changes),
            "tracks_changed": len(track_changes),
            "track_changes": sum(len(t["changes"]) for t in track_changes),
            "total_tracks": len(comparison),
            "duration_mismatches": len(duration_checks),
        },
    }
