"""Album enrichment/statistics stage.
​Orchestrates album-level enrichment during a popularity scan while delegating
to existing services.enrichment.* and services.metadata.* modules.
"""
from future import annotations
​import itertools
import json
import os
import re
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
​import httpx
import structlog
from sqlalchemy import bindparam, text
​from db.engine import db_session
from db.utils import row_get
from services.catalog.album_classification_service import (
classify_compilation_category,
detect_live_album_type,
is_alternate_or_bonus_title,
is_live_or_alternate_album,
is_live_or_unplugged_track_title,
normalize_primary_release_type,
detect_greatest_hits_album,
)
from services.enrichment.album_art_service import (
fetch_album_art_from_musicbrainz,
save_album_art_to_db,
)
from services.enrichment.artist_bio_service import get_artist_biography
from services.enrichment.musicbrainz_service import (
get_shared_mb_client,
get_shared_mb_service,
)
from helpers.normalization_service import (
append_annotation_once,
strip_live_acoustic_suffix,
)
Logger = structlog.get_logger(name)
​_SLOW_CALL_HEARTBEAT_SECONDS = max(
5.0,
float(os.getenv("ENRICHMENT_HEARTBEAT_SECONDS", "30")),
)
_MONITOR_TICK_SECONDS = 2.0
_HTTP_TIMEOUT = httpx.Timeout(
connect=float(os.getenv("ENRICHMENT_HTTP_CONNECT_TIMEOUT", "5")),
read=float(os.getenv("ENRICHMENT_HTTP_READ_TIMEOUT", "15")),
write=float(os.getenv("ENRICHMENT_HTTP_WRITE_TIMEOUT", "15")),
pool=float(os.getenv("ENRICHMENT_HTTP_POOL_TIMEOUT", "5")),
)
​_MAX_ARTWORK_BYTES = int(os.getenv("ENRICHMENT_MAX_ARTWORK_BYTES", str(20 * 1024 * 1024)))
_ARTIST_MEMBERS_TTL_DAYS = int(os.getenv("ARTIST_MEMBERS_TTL_DAYS", "7"))
​def _lookup_artist_members(artist: str, mb_client: Any) -> list[dict[str, Any]]:
results = mb_client.search_artists(artist, limit=5)
if not results:
return []
preferred = next(
(a for a in results if (a.get("type") or "").lower() in {"group", "orchestra", "choir"}),
results[0],
)
artist_mbid = preferred.get("id")
if not artist_mbid:
return []
return mb_client.get_artist_members(artist_mbid) or []
​def _safe_error(exc: BaseException) -> str:
return f"{type(exc).name}: {exc}"
​@contextmanager
def _log_section(section: str, context: Any) -> Iterator[None]:
start = time.monotonic()
Logger.debug("[ENRICH] section started", section=section, context)
try:
yield
except Exception as exc:
Logger.exception(
"[ENRICH] section failed",
section=section,
elapsed_s=round(time.monotonic() - start, 3),
error=_safe_error(exc),
context,
)
raise
else:
Logger.debug(
"[ENRICH] section completed",
section=section,
elapsed_s=round(time.monotonic() - start, 3),
context,
)
​_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: "OrderedDict[int, dict[str, Any]]" = OrderedDict()
_INFLIGHT_IDS = itertools.count()
_MONITOR_THREAD: threading.Thread | None = None
_MONITOR_INIT_LOCK = threading.Lock()
​def _monitor_loop() -> None:
while True:
time.sleep(_MONITOR_TICK_SECONDS)
now = time.monotonic()
due: list[dict[str, Any]] = []
with _INFLIGHT_LOCK:
for entry in _INFLIGHT.values():
if now >= entry["next_warn"]:
entry["next_warn"] = now + _SLOW_CALL_HEARTBEAT_SECONDS
due.append(
{
"section": entry["section"],
"elapsed_s": round(now - entry["started"], 1),
"context": entry["context"],
}
)
for item in due:
Logger.warning(
"[ENRICH] section still running",
section=item["section"],
elapsed_s=item["elapsed_s"],
item["context"],
)
​def _ensure_monitor() -> None:
global _MONITOR_THREAD
if _MONITOR_THREAD is not None and _MONITOR_THREAD.is_alive():
return
with _MONITOR_INIT_LOCK:
if _MONITOR_THREAD is None or not _MONITOR_THREAD.is_alive():
_MONITOR_THREAD = threading.Thread(
target=_monitor_loop,
name="enrichment-heartbeat-monitor",
daemon=True,
)
_MONITOR_THREAD.start()
​def _call_with_heartbeat(
section: str,
func: Callable[..., Any],
*args: Any,
log_context: dict[str, Any] | None = None,
kwargs: Any,
) -> Any:
context = dict(log_context or {})
start = time.monotonic()
_ensure_monitor()
call_id = next(_INFLIGHT_IDS)
with _INFLIGHT_LOCK:
_INFLIGHT[call_id] = {
"section": section,
"started": start,
"context": context,
"next_warn": start + _SLOW_CALL_HEARTBEAT_SECONDS,
}
​Logger.debug("[ENRICH] call started", section=section, context)
try:
result = func(*args, kwargs)
except Exception as exc:
Logger.exception(
"[ENRICH] call failed",
section=section,
elapsed_s=round(time.monotonic() - start, 3),
error=_safe_error(exc),
context,
)
raise
else:
Logger.debug(
"[ENRICH] call completed",
section=section,
elapsed_s=round(time.monotonic() - start, 3),
context,
)
return result
finally:
with _INFLIGHT_LOCK:
_INFLIGHT.pop(call_id, None)
​@contextmanager
def _row_savepoint(session: Any) -> Iterator[None]:
nested = session.begin_nested()
try:
yield
except Exception:
try:
nested.rollback()
except Exception:
pass
raise
else:
if nested.is_active:
nested.commit()
​_ACTIVE_ALBUMS_LOCK = threading.Lock()
_ACTIVE_ALBUMS: dict[tuple[str, str], float] = {}
​@contextmanager
def _album_scan_guard(artist: str, album: str) -> Iterator[bool]:
key = (str(artist or "").casefold().strip(), str(album or "").casefold().strip())
with _ACTIVE_ALBUMS_LOCK:
started = _ACTIVE_ALBUMS.get(key)
if started is not None:
Logger.warning(
"[ENRICH] album scan already in progress",
artist=artist,
album=album,
running_for_s=round(time.monotonic() - started, 1),
)
acquired = False
else:
_ACTIVE_ALBUMS[key] = time.monotonic()
acquired = True
try:
yield acquired
finally:
if acquired:
with _ACTIVE_ALBUMS_LOCK:
_ACTIVE_ALBUMS.pop(key, None)
​def _sanitize_release_name(album_name: str) -> str:
if not album_name:
return ""
cleaned = re.sub(
r"\s*[([].?(edition\vert{}deluxe\vert{}remaster\vert{}version\vert{}bonus\vert{}expanded\vert{}explicit\vert{}clean).?[)]]",
"",
album_name,
flags=re.IGNORECASE,
).strip()
return cleaned or album_name
​_RICH_TYPE_MARKERS: tuple[tuple[str, str], ...] = (
("+compilation", "album+compilation"),
("+soundtrack", "album+soundtrack"),
("+live", "album+live"),
("+acoustic", "album+acoustic"),
("+remix", "album+remix"),
("+spokenword", "album+spokenword"),
("+demo", "album+demo"),
("+fieldrecording", "album+fieldrecording"),
("+dj-mix", "album+dj-mix"),
("+mixtape/street", "album+mixtape/street"),
("+interview", "album+interview"),
("+audiobook", "album+audiobook"),
("+audio drama", "album+audio drama"),
("+score", "album+score"),
("(soundtrack)", "album+soundtrack"),
("(live)", "album+live"),
("(compilation)", "album+compilation"),
("(remix)", "album+remix"),
)
​_COMPILATION_ARTISTS = frozenset(
{"various artists", "various artists –", "various", "compilation", "soundtrack"}
)
_HETEROGENEOUS_MARKERS = (
"+compilation", "(compilation)", "+soundtrack", "(soundtrack)",
"+live", "(live)", "+remix", "(remix)", "+spokenword", "(spokenword)",
)
​_LIVE_ALBUM_PATTERNS = (
r"\blive\s+at\b", r"\blive\s+in\b", r"\blive\s+from\b",
r"\blive\s+session\b", r"[([]live[)]]\s*",
r"-\s*live\s*", r",\slive\s", r"\+\s*live\s*",
r"live\s+recording\b", r"live\s+tour\b", r"\bin\s+concert\b",
r"\bunplugged\b", r"\bacoustic\b",
)
​_DESTRUCTIVE_SECONDARY_TYPES = ("+live", "+acoustic", "+remix", "+compilation")
_LIVE_TRACK_CORROBORATION_RATIO = 0.5
​def _rich_stored_album_type(raw: str | None) -> str | None:
text = str(raw or "").strip().lower()
if not text:
return None
​if "+" in text:
primary = text.partition("+")[0].strip()
if not primary:
return None
return text
​for marker, resolved in _RICH_TYPE_MARKERS:
if marker in text:
return resolved
​if text in {"ep", "single"}:
return text
​try:
from services.catalog.release_categories import _resolve_mb_secondary
​secondary = _resolve_mb_secondary(text)
except Exception:
secondary = None
if secondary:
return f"album+{secondary}"
​return None
​def _stored_album_type(tracks: list[dict[str, Any]] | None) -> str:
values = {
str(t.get("musicbrainz_albumtype") or "").strip().lower()
for t in tracks or []
if str(t.get("musicbrainz_albumtype") or "").strip()
}
return next(iter(values)) if len(values) == 1 else ""
​def _stored_release_group_mbid(tracks: list[dict[str, Any]] | None) -> str:
values = {
str(t.get("musicbrainz_releasegroupid") or "").strip()
for t in tracks or []
if str(t.get("musicbrainz_releasegroupid") or "").strip()
}
return next(iter(values)) if len(values) == 1 else ""
​def _detect_album_type(
artist: str,
album: str,
album_artist: str | None,
spotify_type: str | None,
tracks: list[dict[str, Any]] | None = None,
) -> str:
artist_lower = (artist or "").casefold().strip()
album_lower = (album or "").casefold().strip()
album_artist_lower = (album_artist or "").casefold().strip()
​stored = _rich_stored_album_type(spotify_type or _stored_album_type(tracks))
if stored:
return stored
​if artist_lower in _COMPILATION_ARTISTS or album_artist_lower in _COMPILATION_ARTISTS:
return "album+compilation"
​if detect_greatest_hits_album(album, artist):
return "album+compilation"
​if "soundtrack" in album_lower:
return "album+soundtrack"
if any(re.search(pattern, album_lower) for pattern in _LIVE_ALBUM_PATTERNS):
return "album+live"
if "+remix" in album_lower or "(remix)" in album_lower:
return "album+remix"
​return "album"
​def _mb_type_is_corroborated(
mb_type: str,
album: str,
artist: str,
tracks: list[dict[str, Any]],
context: dict[str, Any],
) -> bool:
lower = (mb_type or "").casefold()
marker = next(
(value for value in _DESTRUCTIVE_SECONDARY_TYPES if value in lower),
"",
)
if not marker:
return True
​if marker == "+compilation":
if (
"compilation" in (album or "").casefold()
or detect_greatest_hits_album(album, artist)
or _is_various_artists_compilation(artist, None, album, tracks)
):
return True
Logger.warning(
"[ENRICH] MusicBrainz compilation secondary type rejected",
reason="album title and tracklist do not corroborate compilation status",
musicbrainz_type=mb_type,
context,
)
return False
​if marker == "+remix":
if "remix" in (album or "").casefold():
return True
Logger.warning(
"[ENRICH] MusicBrainz secondary type rejected",
reason="album title carries no remix marker",
musicbrainz_type=mb_type,
context,
)
return False
​if is_live_or_alternate_album(album):
return True
​live_tracks, total_tracks = _live_track_ratio(tracks)
if total_tracks and (live_tracks / total_tracks) >= _LIVE_TRACK_CORROBORATION_RATIO:
return True
​Logger.warning(
"[ENRICH] MusicBrainz secondary type rejected",
reason="neither album nor track titles corroborate live/acoustic release",
musicbrainz_type=mb_type,
context,
)
return False
​def _live_track_ratio(tracks: list[dict[str, Any]]) -> tuple[int, int]:
titled = [str(track.get("title") or "") for track in tracks or []]
titled = [title for title in titled if title.strip()]
if not titled:
return 0, 0
live = 0
for title in titled:
try:
if is_live_or_unplugged_track_title(title):
live += 1
except Exception:
continue
return live, len(titled)
​def _is_various_artists_compilation(
artist: str,
album_artist: str | None,
album: str = "",
tracks: list[dict[str, Any]] | None = None,
) -> bool:
names = {
(artist or "").casefold().strip(),
(album_artist or "").casefold().strip(),
}
if names & frozenset({"various artists", "various artist", "various", "va", "v/a", "compilation", "soundtrack"}):
return True
​if not tracks:
return False
​try:
return classify_compilation_category(
artist or "", album or "", tracks, album_artist=album_artist
) == "va"
except Exception:
return False
​def _tracklist_corroborates_release_group(
artist: str,
album: str,
album_artist: str | None,
tracks: list[dict[str, Any]],
release_group_mbid: str,
context: dict[str, Any],
) -> bool:
if not tracks:
return True
​from services.enrichment.musicbrainz_service import release_group_tracklist_passes
​try:
passed, _ = release_group_tracklist_passes(
release_group_mbid, tracks, 0.6, artist, album
)
return passed
except Exception:
return False
​def _lookup_musicbrainz_album_type(
artist: str,
album: str,
album_artist: str | None = None,
tracks: list[dict[str, Any]] | None = None,
) -> tuple[str | None, str | None]:
clean_album = _sanitize_release_name(album)
context = {"artist": artist, "album": album, "query_album": clean_album}
​try:
stored_type = _rich_stored_album_type(_stored_album_type(tracks))
stored_rg = _stored_release_group_mbid(tracks)
if stored_type and stored_rg:
return stored_type, stored_rg
except Exception:
pass
​try:
service = get_shared_mb_service()
matches = service.search_releasegroup_matches(artist, clean_album, limit=5) or []
if not matches:
return None, None
​best = matches[0] if isinstance(matches[0], dict) else {}
score = float(best.get("match_score") or 0)
​stored_rg = _stored_release_group_mbid(tracks)
if stored_rg:
for candidate in matches:
if isinstance(candidate, dict) and str(candidate.get("id") or "").strip() == stored_rg:
best = candidate
score = float(candidate.get("match_score") or 1.0)
break
​if score < 0.6:
return None, None
​candidate_mbid = str(best.get("id") or "").strip()
if candidate_mbid and _is_various_artists_compilation(artist, album_artist, album, tracks):
if not _tracklist_corroborates_release_group(artist, album, album_artist, tracks or [], candidate_mbid, context):
return None, None
​primary = str(best.get("primary_type") or "").casefold()
release_group_mbid = candidate_mbid or None
secondary = {
str(value).casefold()
for value in (best.get("secondary_types") or [])
if value
}
​_SECONDARY_ORDER = (
("compilation", "compilation"),
("soundtrack", "soundtrack"),
("live", "live"),
("acoustic", "acoustic"),
("unplugged", "acoustic"),
("remix", "remix"),
("spokenword", "spokenword"),
("demo", "demo"),
("score", "score"),
)
​matched_secondary = next(
(label for mb_type, label in _SECONDARY_ORDER if mb_type in secondary),
"",
)
​if primary in {"single", "ep"}:
resolved = f"{primary}+{matched_secondary}" if matched_secondary else primary
elif matched_secondary:
resolved = f"album+{matched_secondary}"
else:
resolved = primary if primary in {"single", "ep"} else "album"
​return resolved, release_group_mbid
except Exception:
return None, None
​def _resolve_album_type(
artist: str,
album: str,
album_artist: str | None,
spotify_type: str | None,
tracks: list[dict[str, Any]],
) -> tuple[str, str | None, str | None, str | None]:
effective_stored = spotify_type or _stored_album_type(tracks)
detected = _detect_album_type(artist, album, album_artist, effective_stored, tracks)
​try:
from services.metadata.album_lookup_skip_service import album_lookup_skip
if album_lookup_skip(artist, album):
return detected, None, None, None
except Exception:
pass
​mb_type, release_group_mbid = _lookup_musicbrainz_album_type(
artist, album, album_artist, tracks
)
original_mb_type = mb_type
​if mb_type:
if not _mb_type_is_corroborated(mb_type, album, artist, tracks or [], {"artist": artist, "album": album}):
mb_type = "album" if mb_type.startswith("album") else mb_type
​if detected == "album":
detected = mb_type
elif mb_type in {"single", "ep"} and "+" not in detected:
detected = mb_type
​return detected, mb_type, release_group_mbid, original_mb_type
​def _needs_release_mbid(artist: str, album: str) -> bool:
try:
with db_session() as session:
row = session.execute(
text(
"SELECT 1 FROM tracks "
"WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) "
"AND LOWER(album) = LOWER(:album) "
"AND (musicbrainz_album_mbid IS NULL OR TRIM(musicbrainz_album_mbid) = '') "
"LIMIT 1"
),
{"artist": artist, "album": album},
).first()
return bool(row)
except Exception:
return True
​def _persist_album_type_to_tracks(
artist: str,
album: str,
tracks: list[dict[str, Any]],
album_type: str,
release_group_mbid: str | None,
) -> None:
if not album_type:
return
​primary = normalize_primary_release_type(album_type)
pending = [
str(track.get("id"))
for track in tracks or []
if track.get("id") and not str(track.get("musicbrainz_albumtype") or "").strip()
]
if pending:
try:
with db_session() as session:
session.execute(
text("""
UPDATE tracks
SET spotify_album_type = :album_type,
releasetype = :primary,
musicbrainz_albumtype = :album_type
WHERE CAST(id AS TEXT) IN :track_ids
AND COALESCE(NULLIF(TRIM(musicbrainz_albumtype), ''), '') = ''
""").bindparam("track_ids", expanding=True),
{
"album_type": album_type,
"primary": primary,
"track_ids": pending,
},
)
for track in tracks or []:
if str(track.get("id")) in pending:
track["spotify_album_type"] = album_type
track["releasetype"] = primary
track["musicbrainz_albumtype"] = album_type
except Exception:
pass
​if release_group_mbid:
try:
with db_session() as session:
session.execute(
text("""
UPDATE tracks
SET musicbrainz_releasegroupid = :release_group_mbid
WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
AND LOWER(album) = LOWER(:album)
AND (musicbrainz_releasegroupid IS NULL OR TRIM(musicbrainz_releasegroupid) = '')
"""),
{"release_group_mbid": release_group_mbid, "artist": artist, "album": album},
)
for track in tracks or []:
if not track.get("musicbrainz_releasegroupid"):
track["musicbrainz_releasegroupid"] = release_group_mbid
except Exception:
pass
​def enrich_album(
*,
album_row: dict[str, Any],
album_context: dict[str, Any],
stat_eligible_tracks: list[dict[str, Any]],
options: dict[str, Any],
) -> dict[str, Any]:
artist = str(album_row.get("artist") or "").strip()
album = str(album_row.get("album") or "").strip()
album_artist = str(album_row.get("album_artist") or "").strip()
spotify_type = str(
album_row.get("musicbrainz_album_type")
or album_row.get("spotify_album_type")
or ""
).strip()
album_tracks = album_row.get("tracks") or []
​popularity_pass = bool(options.get("popularity_only"))
finalise_pass = bool(options.get("finalise_only"))
detected_type = "album"
is_heterogeneous = False
mb_type_raw: str | None = None
​def _result(detected: str, heterogeneous: bool) -> dict[str, Any]:
return {
"album_row": album_row,
"album_context": album_context,
"stat_eligible_tracks": stat_eligible_tracks,
"detected_album_type": detected,
"musicbrainz_secondary_type_raw": mb_type_raw,
"is_heterogeneous": heterogeneous,
}
​with _album_scan_guard(artist, album) as acquired:
if not acquired:
return _result(detected_type, is_heterogeneous)
​try:
if popularity_pass or finalise_pass:
detected_type = _detect_album_type(artist, album, album_artist or None, spotify_type or None, album_tracks)
is_heterogeneous = any(marker in detected_type.casefold() for marker in _HETEROGENEOUS_MARKERS)
else:
detected_type, mb_type, release_group_mbid, mb_type_raw = _resolve_album_type(
artist, album, album_artist or None, spotify_type or None, album_tracks
)
is_heterogeneous = any(marker in detected_type.casefold() for marker in _HETEROGENEOUS_MARKERS)
_persist_album_type_to_tracks(artist, album, album_tracks, detected_type, release_group_mbid)
except Exception:
pass
​return _result(detected_type, is_heterogeneous)