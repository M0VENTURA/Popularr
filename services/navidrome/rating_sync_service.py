"""Navidrome rating sync helpers.

Handles synchronisation of ratings between Popularr and Navidrome.
Provides:
    - navidrome_scan_running(): Check if Navidrome is currently scanning.
    - load_navidrome_users_from_config(): Load Navidrome user credentials.

Uses a progress file to detect active Navidrome scans, preventing
concurrent operations that could cause conflicts.
"""
from __future__ import annotations
import json
import os
from typing import Any

from helpers.config_helpers import get_navidrome_progress_file

NAVIDROME_PROGRESS_FILE = get_navidrome_progress_file()


def navidrome_scan_running() -> bool:
    try:
        if os.path.exists(NAVIDROME_PROGRESS_FILE):
            with open(NAVIDROME_PROGRESS_FILE, "r", encoding="utf-8") as handle:
                return bool(json.load(handle).get("is_running"))
    except Exception:
        return False
    return False


def load_navidrome_users_from_config() -> list[dict]:
    try:
        from helpers.config_helpers import get_navidrome_users_normalized
        return get_navidrome_users_normalized()
    except Exception:
        return []


def is_sync_ratings_to_all_users_enabled() -> bool:
    try:
        from helpers.config_helpers import get_config
        return bool((get_config() or {}).get("features", {}).get("sync_ratings_to_all_users", False))
    except Exception:
        return False


def sync_track_rating_to_navidrome(track_id: str, stars: int) -> bool:
    try:
        clients = get_rating_sync_clients()
        return sync_track_rating_with_clients(clients, track_id, stars)
    except Exception:
        return False


def get_rating_sync_clients() -> list[Any]:
    """Build the rating-sync Navidrome clients ONCE per album.

    The old per-track path reconstructed a ``NavidromeClient`` — and reloaded
    the user list plus the ``sync_ratings_to_all_users`` config flag — for
    EVERY track on the album.  Building the clients once per album removes
    that per-track config load / client churn; each ``set_rating`` still makes
    its own Subsonic HTTP call.  Returns one client per configured user
    (primary user only unless ``sync_ratings_to_all_users`` is on), or an
    empty list when Navidrome is not configured.
    """
    clients: list[Any] = []
    try:
        from api_clients.navidrome import NavidromeClient

        # ⚠️ Skip UNUSABLE entries BEFORE picking the primary user.
        #
        # This used to take ``users[:1]`` unconditionally. A config that keeps
        # a setup-wizard stub (or any row with an empty user/password) first
        # therefore built a client with no credentials for the rating sync,
        # while the playlist paths iterate and SKIP such rows — which is why
        # playlist writes could succeed against a real account ("moventura")
        # in the same minute every ``setRating`` failed. A row without
        # base_url/user/pass is not an account; it must not become the target.
        users = [
            user
            for user in load_navidrome_users_from_config()
            if str(user.get("base_url") or "").strip()
            and str(user.get("user", user.get("username", "")) or "").strip()
            and str(user.get("pass", user.get("password", "")) or "").strip()
        ]
        if not is_sync_ratings_to_all_users_enabled() and users:
            users = users[:1]
        for user in users:
            clients.append(
                NavidromeClient(
                    base_url=user.get("base_url", ""),
                    username=user.get("user", user.get("username", "")),
                    password=user.get("pass", user.get("password", "")),
                )
            )
    except Exception:
        clients = []
    return clients


def _client_rating_status(client: Any, track_id: str, stars: int) -> str:
    """One client's outcome: ``ok`` / ``not_found`` / ``deferred`` / ``auth``
    / ``failed``."""
    fn = getattr(client, "set_rating_status", None)
    if callable(fn):
        try:
            return str(fn(track_id, stars) or "failed")
        except Exception:
            return "failed"
    try:
        return "ok" if client.set_rating(track_id, stars) else "failed"
    except Exception:
        return "failed"


def rating_status_for_track(
    clients: list[Any], track_id: str, stars: int
) -> str:
    """Aggregate every user's outcome for one track.

    * any ``ok``                     → ``ok``
    * otherwise any real failure/auth → ``failed``
    * ALL ``not_found``              → ``not_found`` (a stale song id)
    * ALL ``deferred``               → ``deferred`` (an import was running)

    ``not_found`` must NOT count as an outage: it is per-track, and the scan
    keeps syncing the album's remaining tracks.

    ``deferred`` likewise must not: the IMPORT GATE skipped the request (see
    ``NavidromeClient.set_rating_status``), so nothing was rejected and nothing
    is wrong with the server. Reporting it as a failure aborted the artist with
    "Navidrome unreachable?" after three tracks whenever a Navidrome import
    overlapped the scan. It is checked BEFORE the failure branch only when every
    client deferred — a mix of deferred and genuinely failed is still a failure.
    """
    if not clients:
        return "failed"
    statuses = [_client_rating_status(client, track_id, stars) for client in clients]
    if not statuses:
        return "failed"
    if "ok" in statuses:
        return "ok"
    if any(status in {"failed", "auth"} for status in statuses):
        return "failed"
    if all(status == "deferred" for status in statuses):
        return "deferred"
    if all(status == "not_found" for status in statuses):
        return "not_found"
    return "failed"


def sync_track_rating_with_clients(clients: list[Any], track_id: str, stars: int) -> bool:
    """Push one track rating through pre-built clients (one per user).

    Reuses the per-album client set from ``get_rating_sync_clients`` so a
    track's rating is set on every configured user without reconstructing a
    client (or reloading config) per track.
    """
    return rating_status_for_track(clients, track_id, stars) == "ok"

