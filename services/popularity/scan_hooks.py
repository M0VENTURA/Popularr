"""Popularity scan hook helpers.

This module connects the scan pipeline to the shared classification and
normalisation services.

It keeps the main scanner clean by centralising:
- lookup title normalization
- Last.fm lookup normalization
- cover-title cleanup
- remaster-only detection
- album classification
- track stat-exclusion flags
"""

from __future__ import annotations

from typing import Any

from services.catalog.album_classification_service import (
    classify_compilation_category,
    detect_alternate_takes,
    detect_greatest_hits_album,
    detect_live_album_type,
    is_live_or_alternate_album,
    normalize_primary_release_type,
    should_exclude_track_from_stats,
)

from helpers.normalization_service import (
    detect_cover_and_normalize_title,
    is_remastered_only_variant,
    normalise_scan_track_identity,
    normalize_title_for_lastfm,
    normalize_title_for_lookup,
    strip_remaster_suffix,
    strip_single_release_suffix,
)


def _duration_seconds(value: Any) -> float | None:
    """Best-effort track duration in seconds (None when unknown/zero)."""
    try:
        v = float(value or 0)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def prepare_album_context(
    *,
    artist: str,
    album: str,
    tracks: list[dict[str, Any]],
    album_artist: str | None = None,
    spotify_album_type: str | None = None,
    musicbrainz_album_type: str | None = None,
) -> dict[str, Any]:

    primary_release_type = normalize_primary_release_type(
        spotify_album_type or musicbrainz_album_type or ""
    )

    album_type_from_field = spotify_album_type or musicbrainz_album_type or ""

    live_album_type = detect_live_album_type(
        album,
        album_type_from_field=album_type_from_field,
    )

    # The album-type field (MusicBrainz/Spotify match) is authoritative for
    # the live verdict; the title-based check only fills in for albums with
    # NO matched album type.
    is_live_album = bool(live_album_type) or (
        not album_type_from_field and is_live_or_alternate_album(album)
    )

    # Compilation classification is split into VA (per-track artist context
    # required) vs single-artist (Greatest Hits — treated like a studio
    # album).  ``is_compilation`` keeps the boolean "any compilation" view
    # for legacy consumers; downstream stages use the subtype flags.
    comp_category = classify_compilation_category(
        artist=artist,
        album=album,
        tracks=tracks,
        album_artist=album_artist,
        spotify_album_type=spotify_album_type,
        musicbrainz_album_type=musicbrainz_album_type,
    )
    is_compilation = comp_category != ""
    is_va_compilation = comp_category == "va"
    is_single_artist_compilation = comp_category == "single_artist"

    is_greatest_hits = detect_greatest_hits_album(album=album, artist=artist)

    alternate_takes = detect_alternate_takes(tracks)

    return {
        "artist": artist,
        "album": album,
        "album_artist": album_artist,
        "primary_release_type": primary_release_type,
        "spotify_album_type": spotify_album_type,
        "musicbrainz_album_type": musicbrainz_album_type,
        "live_album_type": live_album_type,
        "is_live_album": is_live_album,
        "is_compilation": is_compilation,
        "is_va_compilation": is_va_compilation,
        "is_single_artist_compilation": is_single_artist_compilation,
        "is_greatest_hits": is_greatest_hits,
        "alternate_takes": alternate_takes,
    }


def prepare_track_context(
    track: dict[str, Any],
    album_context: dict[str, Any] | None = None,
) -> dict[str, Any]:

    album_context = album_context or {}

    title = track.get("title") or track.get("name") or ""
    artist = (
        track.get("artist")
        or track.get("album_artist")
        or album_context.get("artist")
        or ""
    )
    album = track.get("album") or album_context.get("album") or ""

    # ---------------------------------------------------------------------
    # Canonicalise WHO this track is by, BEFORE anything reads the identity.
    #
    # A featured credit found in the TITLE is moved onto the artist
    # ("Bring Me To Life feat 12 stones" -> "Evanescence feat. 12 Stones" /
    # "Bring Me To Life"), a false "(X Cover)" attribution and a remaster
    # marker are stripped from the title, and the album artist is pinned to the
    # PRIMARY artist. See
    # ``helpers.normalization_service.normalise_scan_track_identity``.
    #
    # The result is written back onto the RAW track dict — the same object
    # ``track_dicts`` / ``album_tracks`` reference in the scan runner — so the
    # persisted row and every downstream lookup agree. This is the same reason
    # ``exclude_from_stats`` is written back below: without it the popularity
    # lookups would keep querying Last.fm/ListenBrainz with the featured credit
    # glued to the artist or the marker glued to the title.
    #
    # Scan-only by design: a Navidrome import stores what the file says, and a
    # manual edit keeps what the user typed; the scan is what decides the
    # canonical record.
    # ---------------------------------------------------------------------
    _identity = normalise_scan_track_identity(
        artist=artist,
        title=title,
        album_artist=str(track.get("album_artist") or album_context.get("album_artist") or ""),
        is_va_compilation=bool(album_context.get("is_va_compilation")),
    )
    title = _identity["title"] or title
    artist = _identity["artist"] or artist
    # ``album_artist`` is filled ONLY when the row had none AND the incoming
    # artist carried no featured credit of its own. Both conditions exist to keep
    # the album key STABLE: that key is
    # ``COALESCE(NULLIF(album_artist, ''), artist)``, so
    #
    #   * filling an empty album artist with the PRIMARY artist leaves the key
    #     untouched whenever the artist was feat.-free ("Evanescence" before the
    #     move from the title, "Evanescence" after) — which is the case this
    #     whole pass exists for; and
    #   * for an artist that ALREADY carried a credit, filling (or stripping) it
    #     WOULD move the album to a new key mid-scan, and every album-scoped
    #     lookup issued with the old spelling (file-tag sync, release MBID,
    #     extended metadata) would silently find nothing. Nothing is written, so
    #     the album behaves exactly as it did before this pass — and shows the
    #     same credit-laden artist name it always did.
    #
    # A populated album artist is the user's (or Navidrome's) and outranks
    # anything derivable here — ``track_stage`` even protects the column from a
    # stale overwrite.
    _album_artist_was_empty = not str(track.get("album_artist") or "").strip()
    _write_album_artist = bool(
        _album_artist_was_empty and not _identity["artist_already_had_credit"]
    )
    _scan_album_artist = _identity["album_artist"] if _write_album_artist else ""

    # The TRACK's OWN artist — as opposed to ``artist`` above, which falls back to
    # the album artist so that LOOKUPS always have something to query with.
    #
    # That fallback must never be written back onto the row. On a Various Artists
    # compilation it stamped "Various Artists" (the ALBUM artist) onto every track
    # that had no artist of its own, and because the metadata pass only fills an
    # artist that is EMPTY, it also stopped MusicBrainz from supplying the song's
    # real track artist — the reported "track artist is being incorrectly
    # overwritten on Various Artists compilations as Various Artist".
    _own_artist = str(track.get("artist") or "").strip()

    for _field, _value in (
        ("title", title),
        # Only the track's own artist (see ``_own_artist``): an empty value is
        # skipped below so the column is left for MusicBrainz to fill.
        ("artist", artist if _own_artist else ""),
        # ``album_artist`` is protected from stale overwrites in track_stage, so
        # the flag is what makes this single, deliberate write persist.
        ("album_artist", _scan_album_artist),
        ("album_artist_from_scan_identity", bool(_scan_album_artist)),
        ("featured_artist", _identity["featured_artist"]),
        # Flags consumed by ``track_stage`` (cover verdict + logging). Not
        # ``tracks`` columns — the DB writer drops unknown keys.
        ("title_had_cover_wording", _identity["title_had_cover_wording"]),
        ("title_had_featured_credit", _identity["title_had_featured_credit"]),
        ("title_had_remaster_wording", _identity["title_had_remaster_wording"]),
    ):
        if _field in ("artist", "album_artist") and not _value:
            # Never blank — or fabricate — either artist column. An empty value
            # here means "leave it as it is", so a track with no artist of its
            # own stays empty for the metadata pass to fill from MusicBrainz,
            # and an existing album artist is never cleared.
            continue
        try:
            track[_field] = _value
        except Exception:
            pass

    is_cover, cover_normalized_title = detect_cover_and_normalize_title(title)

    lookup_title = normalize_title_for_lookup(title)
    lastfm_title = normalize_title_for_lastfm(title)

    remaster_stripped_title = strip_remaster_suffix(title)
    single_suffix_stripped_title = strip_single_release_suffix(title)

    remastered_only = is_remastered_only_variant(title)

    is_live = int(track.get("is_live") or 0)
    album_context_live = 1 if album_context.get("is_live_album") else 0

    exclude_from_stats = should_exclude_track_from_stats(
        title=title,
        album=album,
        is_live=is_live,
        album_context_live=album_context_live,
        album_type=album_context.get("musicbrainz_album_type")
        or album_context.get("spotify_album_type")
        or "",
        duration=_duration_seconds(track.get("duration")),
    )

    # Write the exclusion verdict back onto the RAW track dict (the same
    # object ``track_dicts`` / ``album_tracks`` reference in the scan runner).
    # The fresh in-memory album listener distributions
    # (``_build_album_listener_distributions``) filter on this flag; without
    # it the raw DB rows — which have no ``exclude_from_stats`` column —
    # would include live / intro / short tracks in singles detection's
    # album-z baseline even though the star-rating and DB-stored stats paths
    # exclude them.
    try:
        track["exclude_from_stats"] = exclude_from_stats
    except Exception:
        pass

    return {
        "track": track,
        "title": title,
        "artist": artist,
        "album": album,

        "lookup_title": lookup_title,
        "lastfm_title": lastfm_title,
        "cover_normalized_title": cover_normalized_title,
        "remaster_stripped_title": remaster_stripped_title,
        "single_suffix_stripped_title": single_suffix_stripped_title,

        "is_cover": is_cover,
        "is_remastered_only_variant": remastered_only,
        "title_had_cover_wording": bool(_identity["title_had_cover_wording"]),
        "title_had_featured_credit": bool(_identity["title_had_featured_credit"]),
        "title_had_remaster_wording": bool(_identity["title_had_remaster_wording"]),
        "featured_artist": _identity["featured_artist"],
        "exclude_from_stats": exclude_from_stats,
        "album_context_live": album_context_live,

        "album_context": album_context,
    }


def prepare_tracks_for_album(
    *,
    artist: str,
    album: str,
    tracks: list[dict[str, Any]],
    album_artist: str | None = None,
    spotify_album_type: str | None = None,
    musicbrainz_album_type: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:

    album_context = prepare_album_context(
        artist=artist,
        album=album,
        tracks=tracks,
        album_artist=album_artist,
        spotify_album_type=spotify_album_type,
        musicbrainz_album_type=musicbrainz_album_type,
    )

    track_contexts = [
        prepare_track_context(track, album_context)
        for track in tracks or []
    ]

    return album_context, track_contexts


def get_stat_eligible_tracks(track_contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        ctx["track"]
        for ctx in track_contexts or []
        if not ctx.get("exclude_from_stats")
    ]


def apply_context_fields_to_track(track_context: dict[str, Any]) -> dict[str, Any]:
    track = dict(track_context.get("track") or {})

    track.update({
        "lookup_title": track_context.get("lookup_title"),
        "lastfm_title": track_context.get("lastfm_title"),
        "cover_normalized_title": track_context.get("cover_normalized_title"),
        "is_cover": track_context.get("is_cover"),
        "is_remastered_only_variant": track_context.get("is_remastered_only_variant"),
        "exclude_from_stats": track_context.get("exclude_from_stats"),
        "album_context_live": track_context.get("album_context_live"),
    })

    return track


__all__ = [
    "normalize_title_for_lastfm",
    "normalize_title_for_lookup",
    "detect_cover_and_normalize_title",
    "is_remastered_only_variant",
    "strip_remaster_suffix",
    "strip_single_release_suffix",
    "prepare_album_context",
    "prepare_track_context",
    "prepare_tracks_for_album",
]
