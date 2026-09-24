"""SQLAlchemy ORM models for Popularr.

Auto-generated and hand-tuned from the db/schema.py definitions.
Upgraded to modern SQLAlchemy 2.0 type-hinted Mapped columns.

Conventions:
    - Mapped[Type] declarations are used over legacy Column() definitions.
    - Optional types (e.g., Mapped[str | None]) translate automatically to nullable=True.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Double,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    Sequence,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.engine import Base


# =============================================================================
# artists
# =============================================================================

class Artist(Base):
    __tablename__ = "artists"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    country: Mapped[str | None] = mapped_column(String)
    bio: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(String)
    similar_artists_lastfm: Mapped[str | None] = mapped_column(Text)
    similar_artists_listenbrainz: Mapped[str | None] = mapped_column(Text)
    similar_artists_last_updated: Mapped[str | None] = mapped_column(String)

    def __repr__(self) -> str:
        return f"<Artist(id={self.id!r}, name={self.name!r})>"


# =============================================================================
# artist_stats
# =============================================================================

class ArtistStat(Base):
    __tablename__ = "artist_stats"

    artist_id: Mapped[str] = mapped_column(String, primary_key=True)
    artist_name: Mapped[str] = mapped_column(String)
    album_count: Mapped[int | None] = mapped_column(Integer)
    track_count: Mapped[int | None] = mapped_column(Integer)
    last_updated: Mapped[str | None] = mapped_column(String)

    def __repr__(self) -> str:
        return f"<ArtistStat(artist_id={self.artist_id!r})>"


# =============================================================================
# tracks — the largest table in the system
# =============================================================================

class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[str] = mapped_column(String, primary_key=True)

    # Base Identifiers & Metadata
    artist_id: Mapped[str | None] = mapped_column(String)
    artist: Mapped[str | None] = mapped_column(String)
    album_artist: Mapped[str | None] = mapped_column(String)
    album: Mapped[str | None] = mapped_column(String)
    title: Mapped[str | None] = mapped_column(String)
    duration: Mapped[float | None] = mapped_column(Double)
    file_path: Mapped[str | None] = mapped_column(String)
    track_number: Mapped[str | None] = mapped_column(String)
    disc_number: Mapped[str | None] = mapped_column(String)
    year: Mapped[str | None] = mapped_column(String)
    release_year: Mapped[int | None] = mapped_column(Integer)
    releasecountry: Mapped[str | None] = mapped_column(String)

    # Flags & Modifiers
    is_single: Mapped[bool | None] = mapped_column(server_default=text("FALSE"))
    is_cover: Mapped[int | None] = mapped_column(BigInteger, server_default=text("0"))
    is_cover_reason: Mapped[str | None] = mapped_column(String)
    original_cover_artist: Mapped[str | None] = mapped_column(String)
    cover_manual_override: Mapped[bool | None] = mapped_column(server_default=text("FALSE"))
    cover_last_checked: Mapped[datetime | None] = mapped_column(DateTime)
    is_live: Mapped[int | None] = mapped_column(BigInteger, server_default=text("0"))
    is_acoustic: Mapped[int | None] = mapped_column(BigInteger, server_default=text("0"))
    is_remix: Mapped[int | None] = mapped_column(BigInteger, server_default=text("0"))
    album_context_live: Mapped[int | None] = mapped_column(BigInteger, server_default=text("0"))
    alternate_take: Mapped[int | None] = mapped_column(BigInteger, server_default=text("0"))
    is_compilation: Mapped[int | None] = mapped_column(BigInteger, server_default=text("0"))
    base_track_id: Mapped[str | None] = mapped_column(String)

    # Single Detection
    single_confidence: Mapped[str | None] = mapped_column(Text)
    single_confidence_score: Mapped[float | None] = mapped_column(Double)
    single_status: Mapped[str | None] = mapped_column(Text)
    single_sources: Mapped[str | None] = mapped_column(Text)
    single_sources_used: Mapped[str | None] = mapped_column(Text)
    single_detection_last_updated: Mapped[datetime | None] = mapped_column(DateTime)
    single_manual_override: Mapped[bool | None] = mapped_column(server_default=text("FALSE"))

    # Genres & Classifications
    genres: Mapped[str | None] = mapped_column(Text)
    genre: Mapped[str | None] = mapped_column(String)
    manual_genres: Mapped[str | None] = mapped_column(Text)
    navidrome_genres: Mapped[str | None] = mapped_column(Text)
    spotify_genres: Mapped[str | None] = mapped_column(Text)
    lastfm_tags: Mapped[str | None] = mapped_column(Text)
    listenbrainz_genres: Mapped[str | None] = mapped_column(Text)
    discogs_genres: Mapped[str | None] = mapped_column(Text)
    musicbrainz_genres: Mapped[str | None] = mapped_column(Text)
    essentia_genres: Mapped[str | None] = mapped_column(Text)
    tags_last_updated: Mapped[str | None] = mapped_column(String)

    # Mood & Audio Features
    mood: Mapped[str | None] = mapped_column(String)
    mood_confidence: Mapped[float | None] = mapped_column(Double)
    mood_source: Mapped[str | None] = mapped_column(String)
    mood_last_updated: Mapped[datetime | None] = mapped_column(DateTime)
    danceability: Mapped[float | None] = mapped_column(Double)
    bpm: Mapped[float | None] = mapped_column(Double)
    essentia_last_updated: Mapped[datetime | None] = mapped_column(DateTime)
    essentia_model_version: Mapped[str | None] = mapped_column(String)
    essentia_scan_version: Mapped[str | None] = mapped_column(String)

    # Scoring, Popularity & Playcounts
    stars: Mapped[int | None] = mapped_column(Integer)
    star_rating: Mapped[int | None] = mapped_column(Integer)
    popularity: Mapped[float | None] = mapped_column(Double)
    final_score: Mapped[float | None] = mapped_column(Double)
    age_score: Mapped[float | None] = mapped_column(Double)
    spotify_score: Mapped[float | None] = mapped_column(Double)
    lastfm_score: Mapped[float | None] = mapped_column(Double)
    listenbrainz_score: Mapped[float | None] = mapped_column(Double)
    popularity_marked: Mapped[bool | None] = mapped_column(server_default=text("FALSE"))
    popularity_frozen: Mapped[bool | None] = mapped_column(server_default=text("FALSE"))
    popularity_frozen_at: Mapped[datetime | None] = mapped_column(DateTime)

    # External Provider IDs
    mbid: Mapped[str | None] = mapped_column(String)
    suggested_mbid: Mapped[str | None] = mapped_column(String)
    recording_mbid: Mapped[str | None] = mapped_column(String)
    musicbrainz_id: Mapped[str | None] = mapped_column(String)
    musicbrainz_trackid: Mapped[str | None] = mapped_column(String)
    musicbrainz_albumid: Mapped[str | None] = mapped_column(String)
    musicbrainz_album_mbid: Mapped[str | None] = mapped_column(String)
    musicbrainz_artistid: Mapped[str | None] = mapped_column(String)
    musicbrainz_albumartistid: Mapped[str | None] = mapped_column(String)
    musicbrainz_releasegroupid: Mapped[str | None] = mapped_column(String)
    musicbrainz_releasetrackid: Mapped[str | None] = mapped_column(String)
    musicbrainz_workid: Mapped[str | None] = mapped_column(String)
    musicbrainz_albumstatus: Mapped[str | None] = mapped_column(String)
    musicbrainz_albumtype: Mapped[str | None] = mapped_column(String)
    discogs_artist_id: Mapped[str | None] = mapped_column(String)

    # System & Sync
    writer: Mapped[str | None] = mapped_column(String)
    isrc: Mapped[str | None] = mapped_column(String)
    work: Mapped[str | None] = mapped_column(String)
    pending_mb_updates: Mapped[str | None] = mapped_column(Text)
    mb_ignored_fields: Mapped[str | None] = mapped_column(Text)
    last_scanned: Mapped[str | None] = mapped_column(String)
    verification_status: Mapped[str | None] = mapped_column(String)
    verification_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    verification_error: Mapped[str | None] = mapped_column(String)

    def __repr__(self) -> str:
        return f"<Track(id={self.id!r}, title={self.title!r}, artist={self.artist!r})>"


# =============================================================================
# scan_history
# =============================================================================

class ScanHistory(Base):
    __tablename__ = "scan_history"

    id: Mapped[int] = mapped_column(BigInteger, Sequence("scan_history_id_seq"), primary_key=True)
    scan_type: Mapped[str | None] = mapped_column(String)
    artist: Mapped[str | None] = mapped_column(String)
    album: Mapped[str | None] = mapped_column(String)
    status: Mapped[str | None] = mapped_column(String)
    message: Mapped[str | None] = mapped_column(String)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_seconds: Mapped[float | None] = mapped_column(Double)
    changed_albums: Mapped[int | None] = mapped_column(Integer)
    tracks_added: Mapped[int | None] = mapped_column(Integer)

    def __repr__(self) -> str:
        return f"<ScanHistory(id={self.id}, artist={self.artist!r}, status={self.status!r})>"


# =============================================================================
# scan_states 
# =============================================================================

class ScanState(Base):
    __tablename__ = "scan_states"

    scan_type: Mapped[str] = mapped_column(String, primary_key=True)
    is_running: Mapped[bool | None] = mapped_column(server_default=text("FALSE"))
    status: Mapped[str | None] = mapped_column(String, server_default=text("'idle'"))
    stop_requested: Mapped[bool | None] = mapped_column(server_default=text("FALSE"))
    current_artist: Mapped[str | None] = mapped_column(String)
    last_scanned_artist: Mapped[str | None] = mapped_column(String)
    extra_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), 
        server_default=text("CURRENT_TIMESTAMP"), 
        onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<ScanState(scan_type={self.scan_type!r}, status={self.status!r})>"


# =============================================================================
# download_queue
# =============================================================================

class DownloadQueue(Base):
    __tablename__ = "download_queue"

    id: Mapped[int] = mapped_column(BigInteger, Sequence("download_queue_id_seq"), primary_key=True)
    artist: Mapped[str | None] = mapped_column(String)
    title: Mapped[str | None] = mapped_column(String)
    album: Mapped[str | None] = mapped_column(String)
    status: Mapped[str | None] = mapped_column(String, server_default=text("'queued'"))
    source: Mapped[str | None] = mapped_column(String, server_default=text("'soulseek'"))
    priority: Mapped[int | None] = mapped_column(Integer, server_default=text("5"))
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))

    # Extended columns
    album_artist: Mapped[str | None] = mapped_column(String)
    track_number: Mapped[str | None] = mapped_column(String)
    disc_number: Mapped[str | None] = mapped_column(String)
    year: Mapped[str | None] = mapped_column(String)
    release_year: Mapped[int | None] = mapped_column(Integer)
    release_id: Mapped[str | None] = mapped_column(String)
    release_source: Mapped[str | None] = mapped_column(String)
    release_mbid: Mapped[str | None] = mapped_column(String)
    recording_mbid: Mapped[str | None] = mapped_column(String)
    cover_art_url: Mapped[str | None] = mapped_column(String)
    duration: Mapped[float | None] = mapped_column(Double)
    found_filename: Mapped[str | None] = mapped_column(String)
    file_path: Mapped[str | None] = mapped_column(String)
    matched_file_path: Mapped[str | None] = mapped_column(String)
    music_file_path: Mapped[str | None] = mapped_column(String)
    failure_reason: Mapped[str | None] = mapped_column(String)
    retry_count: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    # NOTE: ``max_retries`` was removed in migration 015 — it was never enforced
    # (mark_failed ignores any ceiling), so retries are deliberately unbounded.
    retry_delay_minutes: Mapped[int | None] = mapped_column(Integer, server_default=text("30"))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_failure_time: Mapped[datetime | None] = mapped_column(DateTime)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime)
    copied_individually: Mapped[bool | None] = mapped_column(server_default=text("FALSE"))
    copied_individually_at: Mapped[datetime | None] = mapped_column(DateTime)
    match_confidence: Mapped[float | None] = mapped_column(Double)
    match_method: Mapped[str | None] = mapped_column(String)
    metadata_data: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"))
    metadata_id: Mapped[int | None] = mapped_column(BigInteger)
    release_metadata_id: Mapped[int | None] = mapped_column(BigInteger)
    collection_track_id: Mapped[str | None] = mapped_column(String)
    collection_matched_at: Mapped[str | None] = mapped_column(String)
    in_collection: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    auto_delete_at: Mapped[datetime | None] = mapped_column(DateTime)
    queue_folder: Mapped[str | None] = mapped_column(String)
    is_manual_download: Mapped[bool | None] = mapped_column(server_default=text("FALSE"))
    slskd_username: Mapped[str | None] = mapped_column(String)
    slskd_transfer_id: Mapped[str | None] = mapped_column(String)
    slskd_state: Mapped[str | None] = mapped_column(String)
    slskd_queue_position: Mapped[int | None] = mapped_column(Integer)
    slskd_last_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    search_query: Mapped[str | None] = mapped_column(String)
    source_id: Mapped[str | None] = mapped_column(String)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))

    def __repr__(self) -> str:
        return f"<DownloadQueue(id={self.id}, title={self.title!r}, status={self.status!r})>"


# =============================================================================
# bookmarks
# =============================================================================

class Bookmark(Base):
    __tablename__ = "bookmarks"

    id: Mapped[int] = mapped_column(BigInteger, Sequence("bookmarks_id_seq"), primary_key=True)
    bookmark_type: Mapped[str | None] = mapped_column(String)
    type: Mapped[str | None] = mapped_column(String)
    entity_id: Mapped[str | None] = mapped_column(String)
    name: Mapped[str | None] = mapped_column(String)
    artist_name: Mapped[str | None] = mapped_column(String)
    album_name: Mapped[str | None] = mapped_column(String)
    title: Mapped[str | None] = mapped_column(String)
    notes: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))

    def __repr__(self) -> str:
        return f"<Bookmark(id={self.id}, name={self.name!r})>"


# =============================================================================
# genre_updates
# =============================================================================

class GenreUpdate(Base):
    __tablename__ = "genre_updates"

    id: Mapped[int] = mapped_column(BigInteger, Sequence("genre_updates_id_seq"), primary_key=True)
    artist_name: Mapped[str | None] = mapped_column(String)
    album_name: Mapped[str | None] = mapped_column(String)
    track_id: Mapped[str | None] = mapped_column(String)
    genres_before: Mapped[str | None] = mapped_column(String)
    genres_after: Mapped[str | None] = mapped_column(String)
    action_type: Mapped[str | None] = mapped_column(String)
    affected_track_count: Mapped[int | None] = mapped_column(Integer)
    change_summary: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))

    def __repr__(self) -> str:
        return f"<GenreUpdate(id={self.id}, action={self.action_type!r})>"


# =============================================================================
# slskd_search_logs
# =============================================================================

class SlskdSearchLog(Base):
    __tablename__ = "slskd_search_logs"

    id: Mapped[int] = mapped_column(BigInteger, Sequence("slskd_search_logs_id_seq"), primary_key=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"))
    search_type: Mapped[str | None] = mapped_column(String)
    query: Mapped[str | None] = mapped_column(String)
    queue_id: Mapped[int | None] = mapped_column(Integer)
    artist: Mapped[str | None] = mapped_column(String)
    title: Mapped[str | None] = mapped_column(String)
    album: Mapped[str | None] = mapped_column(String)
    result_count: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(String)
    selected_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    results: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    def __repr__(self) -> str:
        return f"<SlskdSearchLog(id={self.id}, query={self.query!r})>"


# =============================================================================
# musicbrainz_releases
# =============================================================================

class MusicbrainzRelease(Base):
    __tablename__ = "musicbrainz_releases"

    id: Mapped[int] = mapped_column(BigInteger, Sequence("musicbrainz_releases_id_seq"), primary_key=True)
    release_id: Mapped[str] = mapped_column(String, unique=True)
    release_title: Mapped[str] = mapped_column(String)
    artist: Mapped[str] = mapped_column(String)
    release_year: Mapped[int | None] = mapped_column(Integer)
    total_tracks: Mapped[int | None] = mapped_column(Integer)
    monitoring_folder_path: Mapped[str | None] = mapped_column(String)
    final_folder_path: Mapped[str | None] = mapped_column(String)
    status: Mapped[str | None] = mapped_column(String, server_default=text("'active'"))
    method: Mapped[str | None] = mapped_column(String)
    discovered_count: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    organized_count: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    finalized_count: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    album_artist: Mapped[str | None] = mapped_column(String)
    genres: Mapped[str | None] = mapped_column(String)
    cover_art_url: Mapped[str | None] = mapped_column(String)
    release_source: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime)

    tracks: Mapped[list["MusicbrainzReleaseTrack"]] = relationship(
        back_populates="release",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<MusicbrainzRelease(id={self.id}, release_id={self.release_id!r})>"


# =============================================================================
# musicbrainz_release_tracks
# =============================================================================

class MusicbrainzReleaseTrack(Base):
    __tablename__ = "musicbrainz_release_tracks"

    id: Mapped[int] = mapped_column(BigInteger, Sequence("musicbrainz_release_tracks_id_seq"), primary_key=True)
    release_id: Mapped[str] = mapped_column(String, ForeignKey("musicbrainz_releases.release_id"))
    queue_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("download_queue.id"))
    disc_number: Mapped[int | None] = mapped_column(Integer)
    track_number: Mapped[int | None] = mapped_column(Integer)
    track_title: Mapped[str | None] = mapped_column(String)
    track_artist: Mapped[str | None] = mapped_column(String)
    duration: Mapped[int | None] = mapped_column(Integer)
    isrc: Mapped[str | None] = mapped_column(String)
    recording_title: Mapped[str | None] = mapped_column(String)
    recording_mbid: Mapped[str | None] = mapped_column(String)
    composer: Mapped[str | None] = mapped_column(String)
    album_artist: Mapped[str | None] = mapped_column(String)
    year: Mapped[str | None] = mapped_column(String)
    status: Mapped[str | None] = mapped_column(String, server_default=text("'queued'"))
    found_filename: Mapped[str | None] = mapped_column(String)
    file_path: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))

    release: Mapped["MusicbrainzRelease"] = relationship(back_populates="tracks")

    def __repr__(self) -> str:
        return f"<MusicbrainzReleaseTrack(id={self.id}, title={self.track_title!r})>"


# =============================================================================
# missing_releases
# =============================================================================

class MissingRelease(Base):
    __tablename__ = "missing_releases"

    id: Mapped[int] = mapped_column(BigInteger, Sequence("missing_releases_id_seq"), primary_key=True)
    artist: Mapped[str] = mapped_column(String)
    release_id: Mapped[str] = mapped_column(String)
    title: Mapped[str | None] = mapped_column(String)
    primary_type: Mapped[str | None] = mapped_column(String)
    first_release_date: Mapped[str | None] = mapped_column(String)
    cover_art_url: Mapped[str | None] = mapped_column(String)
    category: Mapped[str | None] = mapped_column(String)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))
    tracklist: Mapped[str | None] = mapped_column(String)

    def __repr__(self) -> str:
        return f"<MissingRelease(id={self.id}, release_id={self.release_id!r})>"


# =============================================================================
# album_art
# =============================================================================

class AlbumArt(Base):
    __tablename__ = "album_art"

    id: Mapped[int] = mapped_column(BigInteger, Sequence("album_art_id_seq"), primary_key=True)
    artist_name: Mapped[str] = mapped_column(String)
    album_name: Mapped[str] = mapped_column(String)
    image_data: Mapped[bytes | None] = mapped_column(LargeBinary)
    image_mime_type: Mapped[str | None] = mapped_column(String)
    source: Mapped[str | None] = mapped_column(String)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))

    def __repr__(self) -> str:
        return f"<AlbumArt(id={self.id}, artist={self.artist_name!r}, album={self.album_name!r})>"

# =============================================================================
# missing_album_tracks
# =============================================================================

from sqlalchemy import Boolean, UniqueConstraint

class MissingAlbumTrack(Base):
    __tablename__ = "missing_album_tracks"

    id: Mapped[int] = mapped_column(BigInteger, Sequence("missing_album_tracks_id_seq"), primary_key=True)
    artist_name: Mapped[str] = mapped_column(String, nullable=False)
    album_name: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    
    track_number: Mapped[str | None] = mapped_column(String)
    track_artist: Mapped[str | None] = mapped_column(String)
    year: Mapped[str | None] = mapped_column(String)
    release_id: Mapped[str | None] = mapped_column(String)
    recording_mbid: Mapped[str | None] = mapped_column(String)
    
    duration: Mapped[int | None] = mapped_column(Integer)
    disc_number: Mapped[int | None] = mapped_column(Integer, server_default=text("1"))
    ignored: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"), nullable=False)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))

    __table_args__ = (
        UniqueConstraint(
            "artist_name", 
            "album_name", 
            "title", 
            "disc_number", 
            name="uq_missing_album_tracks"
        ),
    )

    def __repr__(self) -> str:
        return f"<MissingAlbumTrack(id={self.id}, artist={self.artist_name!r}, title={self.title!r})>"
