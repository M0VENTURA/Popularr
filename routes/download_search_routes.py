"""slskd download search/proxy routes — migrated from old app.py."""

from __future__ import annotations

import asyncio
import re
import threading
import time
from typing import Any

import structlog
from quart import Blueprint, jsonify, request

from api_clients.slskd_http import SlskdHttpClient
from db.repositories.queue import update_queue_item
from db.repositories.search_logs import log_slskd_search
from helpers.config_helpers import get_config
from helpers.logging_config import log_queue, log_search
from services.downloads.slskd_service import SlskdService

logger = structlog.get_logger(__name__)

slskd_bp = Blueprint("slskd_api", __name__, url_prefix="/api/slskd")
slsk_bp = Blueprint("slsk_api", __name__, url_prefix="/api/slsk")


# =============================================================================
# MANUAL SEARCH LOGGING
# =============================================================================

_manual_search_state: dict[str, dict[str, Any]] = {}
_manual_search_lock = threading.Lock()


def _log_manual_search_event(
    *,
    search_type: str,
    query: str,
    result_count: int = 0,
    duration_seconds: float | None = None,
    notes: str | None = None,
    selected_result: dict[str, Any] | None = None,
) -> None:
    """Record a manual Soulseek search to ``search.log`` and the search DB."""
    try:
        suffix = f" ({notes})" if notes else ""
        msg = f"[{search_type.upper()}] {query} → {result_count} results"
        if duration_seconds:
            msg += f" in {round(duration_seconds, 1)}s"
        msg += suffix

        log_search(msg)
    except Exception as exc:
        logger.debug("Failed to write to search log", error=str(exc))

    try:
        log_slskd_search(
            search_type=search_type,
            query=query,
            result_count=result_count,
            duration_seconds=duration_seconds,
            notes=notes,
            selected_result=selected_result,
        )
    except Exception as exc:
        logger.debug("Failed to write to search DB", error=str(exc))


def _normalize_slskd_query(value: str) -> str:
    text_val = str(value or "")
    text_val = text_val.replace("\\u0026", " ").replace("&amp;", " ").replace("&", " ")
    text_val = re.sub(r"\s+", " ", text_val)
    return text_val.strip()


def _coerce_optional_int(value: Any, allow_prefix: bool = False) -> int | None:
    if value is None:
        return None
    text_val = str(value).strip()
    if not text_val:
        return None
    candidate = text_val
    if allow_prefix and "/" in text_val:
        candidate = text_val.split("/", 1)[0].strip()
    if not candidate:
        return None
    signless = candidate[1:] if candidate.startswith("-") else candidate
    if not signless.isdigit():
        return None
    try:
        return int(candidate)
    except (TypeError, ValueError):
        return None


# ===========================================================================
# SLSKD ROUTES
# ===========================================================================

@slskd_bp.route("/search", methods=["POST"])
async def slskd_search() -> Any:
    """Proxy endpoint for slskd search API."""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd not enabled"}), 400

    query = _normalize_slskd_query(((await request.get_json()) or {}).get("query", ""))
    if not query:
        return jsonify({"error": "query required"}), 400

    web_url = slskd_config.get("web_url", "http://localhost:5030")
    api_key = slskd_config.get("api_key", "")

    try:
        client = SlskdHttpClient(web_url, api_key)
        slskd_service = SlskdService(http_client=client)

        try:
            await asyncio.to_thread(slskd_service.clear_stale_searches, budget_seconds=6)
        except Exception:
            pass

        try:
            active = await asyncio.to_thread(slskd_service.list_searches, 8)
            active_states = {"None", "Queued", "Requested", "InProgress", "Initializing", "In Progress"}
            busy = [s for s in (active or []) if (s.get("state") or s.get("State") or "") in active_states]

            if busy:
                a = busy[0]
                return jsonify({
                    "slotBusy": True,
                    "activeSearchId": a.get("id") or a.get("searchId") or "",
                    "activeSearchQuery": a.get("searchText") or a.get("query") or "",
                    "activeSearchState": a.get("state") or a.get("State") or "",
                }), 202
        except Exception:
            pass

        started = time.time()
        search_id = await asyncio.to_thread(slskd_service.start_search, query, 20)

        if search_id:
            with _manual_search_lock:
                _manual_search_state[search_id] = {
                    "query": query,
                    "last_result_count": -1,
                }
            _log_manual_search_event(
                search_type="manual",
                query=query,
                result_count=0,
                duration_seconds=round(time.time() - started, 1),
                notes="search_started",
            )
            return jsonify({"searchId": search_id, "status": "searching"})

        return jsonify({"error": "Failed to start search — slskd search slot may be busy. Try again in a moment."}), 500
    except Exception as exc:
        logger.error("Slskd search failed", error=str(exc))
        return jsonify({"error": str(exc)}), 500


@slskd_bp.route("/search-slot", methods=["GET"])
def slskd_search_slot() -> Any:
    """Return whether the slskd search slot is free."""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    if not slskd_config.get("enabled"):
        return jsonify({"slotFree": False, "error": "slskd not enabled"}), 400

    try:
        client = SlskdHttpClient(slskd_config["web_url"], slskd_config.get("api_key", ""))
        searches = client.list_searches(timeout=8)
        active_states = {"None", "Queued", "Requested", "InProgress", "Initializing", "In Progress"}
        active = [s for s in (searches or []) if (s.get("state") or s.get("State") or "") in active_states]

        if active:
            a = active[0]
            return jsonify({
                "slotFree": False,
                "activeSearchId": a.get("id") or a.get("searchId") or "",
                "activeSearchQuery": a.get("searchText") or a.get("query") or "",
                "activeSearchState": a.get("state") or a.get("State") or "",
            })

        return jsonify({"slotFree": True})
    except Exception as exc:
        logger.error("Failed to check search slot", error=str(exc))
        return jsonify({"slotFree": True, "error": str(exc)}), 200


@slskd_bp.route("/search/<search_id>", methods=["GET"])
def slskd_search_results(search_id: str) -> Any:
    """Poll for Soulseek search results."""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd not enabled"}), 400

    try:
        client = SlskdHttpClient(slskd_config["web_url"], slskd_config.get("api_key", ""))
        slskd_service = SlskdService(http_client=client)
        responses, state, is_complete = slskd_service.get_search_results(search_id, timeout=10)

        results = []
        for resp in responses or []:
            username = getattr(resp, "username", "") or ""
            for file in getattr(resp, "files", []) or []:
                results.append({
                    "username": username,
                    "filename": getattr(file, "filename", "") or "",
                    "size": getattr(file, "size", 0) or 0,
                    "size_mb": f"{getattr(file, 'size_mb', 0):.2f}",
                    "bitrate": getattr(file, "bitrate", 0) or 0,
                    "sample_rate": getattr(file, "sample_rate", 0) or 0,
                    "length": getattr(file, "length", 0) or 0,
                    "duration": getattr(file, "duration_formatted", "0:00") or "0:00",
                })

        count = len(results or [])
        response_count = len(responses or [])

        with _manual_search_lock:
            state_entry = _manual_search_state.get(search_id)
            if state_entry is not None:
                # Log whenever the result count CHANGES (including the first
                # non-zero count after a "Completed, TimedOut" terminal flag —
                # slskd keeps streaming responses past the timeout, and the
                # frontend grace-polls so this route is hit again).
                if count != state_entry.get("last_result_count", -1):
                    state_entry["last_result_count"] = count
                    _log_manual_search_event(
                        search_type="manual",
                        query=state_entry.get("query") or search_id,
                        result_count=count,
                        notes="results_returned",
                    )
                # Keep the state alive through the frontend's grace window so
                # late-arriving results get logged.  Pop only when the search
                # is complete AND we either have results or have already
                # observed the terminal state once (a second terminal poll
                # means the frontend stopped grace-polling).
                if is_complete:
                    _term_count = state_entry.get("_terminal_seen", 0) + 1
                    state_entry["_terminal_seen"] = _term_count
                    if _term_count >= 2 or count > 0:
                        _manual_search_state.pop(search_id, None)

        return jsonify({
            "results": results,
            "state": state or "InProgress",
            "responseCount": response_count,
            "fileCount": count,
            "isComplete": bool(is_complete),
        })
    except Exception as exc:
        logger.error("Failed to poll search results", search_id=search_id, error=str(exc))
        return jsonify({"error": str(exc)}), 500


@slskd_bp.route("/download", methods=["POST"])
async def slskd_download() -> Any:
    """Proxy endpoint to download from slskd.

    Accepts TWO payload shapes:

    * **Batch** — ``{"files": [{"username", "filename", "size"}, ...]}``.
      This is what the search page sends (``downloads_page.js``
      ``downloadSlskdBatch`` and ``services/slskd.js`` ``download``).  It was
      silently dropped when the routes were split out of ``app.py``, so every
      multi-file / "Download selected" request was rejected with
      "username and filename required".
    * **Single** — ``{"username", "filename", "size"}``.

    A single download is just a batch of one, so both funnel into
    ``SlskdService.download_files``.
    """
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd not enabled"}), 400

    payload = (await request.get_json()) or {}
    files_payload = payload.get("files")

    if files_payload is not None:
        if not isinstance(files_payload, list):
            return jsonify({"error": "files must be a list"}), 400
        if not files_payload:
            return jsonify({"error": "files must not be empty"}), 400
        for entry in files_payload:
            if not isinstance(entry, dict) or not entry.get("username") or not entry.get("filename"):
                return jsonify({"error": "Each file requires username and filename"}), 400
        # Retained for the log line below and for callers that mix both shapes.
        files = files_payload
    else:
        username = payload.get("username", "")
        filename = payload.get("filename", "")
        if not username or not filename:
            return jsonify({"error": "username and filename required"}), 400
        files = [{"username": username, "filename": filename, "size": payload.get("size")}]

    try:
        client = SlskdHttpClient(slskd_config["web_url"], slskd_config.get("api_key", ""))
        slskd = SlskdService(http_client=client)

        results = await asyncio.to_thread(slskd.download_files, files)

        requested = sum(int(item.get("requested") or 0) for item in results)
        successful_users = sum(1 for item in results if item.get("success"))
        # ``requested`` is a count of files slskd ACCEPTED, not files that will
        # finish.  Reporting success with requested == 0 is the silent no-op
        # that made a rejected download look enqueued.
        overall_success = requested > 0 and successful_users > 0

        label = files[0]["filename"] if len(files) == 1 else f"{len(files)} files"
        _log_manual_search_event(
            search_type="manual",
            query=files[0]["username"] if len(files) == 1 else f"{len(files)} files",
            result_count=len(files),
            notes="selected_for_download" if overall_success else "download_enqueue_failed",
            selected_result={"files": files, "requested": requested},
        )

        if not overall_success:
            logger.error("Failed to enqueue download", file_count=len(files), requested=requested)
            return jsonify({
                "success": False,
                "error": "Failed to enqueue download",
                "requested": requested,
                "userBatches": results,
            }), 500

        logger.info("Enqueued download(s)", file_count=len(files), requested=requested, label=label)
        return jsonify({
            "success": True,
            "requested": requested,
            "userBatches": results,
        })
    except Exception as exc:
        logger.error("Failed to enqueue download", file_count=len(files), error=str(exc))
        return jsonify({"error": str(exc)}), 500


@slskd_bp.route("/cancel", methods=["POST"])
async def slskd_cancel() -> Any:
    """Cancel a Soulseek download."""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd not enabled"}), 400

    payload = (await request.get_json()) or {}
    username = payload.get("username", "")
    filename = payload.get("filename", "")
    transfer_id = payload.get("transfer_id")

    if not username or not (filename or transfer_id):
        return jsonify({"error": "username and filename/transfer_id required"}), 400

    try:
        client = SlskdHttpClient(slskd_config["web_url"], slskd_config.get("api_key", ""))
        result = await asyncio.to_thread(client.cancel_download, username, filename, transfer_id)
        return jsonify({"success": True, "result": result})
    except Exception as exc:
        logger.error("Failed to cancel download", username=username, error=str(exc))
        return jsonify({"error": str(exc)}), 500


@slskd_bp.route("/status", methods=["GET"])
def slskd_status() -> Any:
    """Get slskd download status."""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd not enabled"}), 400

    try:
        client = SlskdHttpClient(slskd_config["web_url"], slskd_config.get("api_key", ""))
        downloads = client.get_active_downloads(timeout=10)
        return jsonify({"success": True, "downloads": downloads or []})
    except Exception as exc:
        logger.error("Failed to fetch download status", error=str(exc))
        return jsonify({"error": str(exc)}), 500


@slskd_bp.route("/retry", methods=["POST"])
async def slskd_retry() -> Any:
    """Retry a failed Soulseek download.

    slskd scopes a transfer by BOTH the peer username and the server-assigned
    transfer id — ``POST /transfers/downloads/{username}/{transfer_id}/retry``
    — so both are required.  This route used to accept a ``filename`` and pass
    it as the transfer id, which could never match a real transfer.
    """
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd not enabled"}), 400

    payload = (await request.get_json()) or {}
    username = payload.get("username", "")
    # Accept the legacy ``filename`` key but never use it as a transfer id: a
    # filename is not a transfer id, and passing one silently retried nothing.
    transfer_id = payload.get("transfer_id") or ""

    if not username or not transfer_id:
        return jsonify({
            "error": "username and transfer_id required",
            "detail": "slskd identifies a transfer by username + transfer id, not by filename.",
        }), 400

    try:
        client = SlskdHttpClient(slskd_config["web_url"], slskd_config.get("api_key", ""))
        result = await asyncio.to_thread(client.retry_download, username, transfer_id)
        if not result:
            return jsonify({
                "success": False,
                "error": "slskd rejected the retry request",
                "result": result,
            }), 502
        return jsonify({"success": True, "result": result})
    except Exception as exc:
        logger.error("Failed to retry download", username=username, transfer_id=transfer_id, error=str(exc))
        return jsonify({"error": str(exc)}), 500


@slskd_bp.route("/queue-download", methods=["POST"])
async def slskd_queue_download() -> Any:
    """Initiate a Soulseek download linked to a specific queue item."""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd not enabled"}), 400
        
    payload = (await request.get_json()) or {}
    queue_id = payload.get("queue_id")
    username = payload.get("username", "")
    filename = payload.get("filename", "")
    
    # NEW: Extract size from payload
    size = payload.get("size")
    
    if not queue_id or not username or not filename:
        return jsonify({"error": "queue_id, username, and filename required"}), 400
        
    try:
        client = SlskdHttpClient(slskd_config["web_url"], slskd_config.get("api_key", ""))
        slskd = SlskdService(http_client=client)

        results = await asyncio.to_thread(
            slskd.download_files,
            [{"username": username, "filename": filename, "size": size}],
        )
        requested = sum(int(item.get("requested") or 0) for item in results)
        enqueued = requested > 0

        _stored_filename = str(filename).replace("\\", "/").strip()

        # Only link the queue row once slskd has actually accepted the file.
        # Marking it "downloading" unconditionally told the completion matcher
        # to wait for a transfer that was never requested — the row looked
        # active forever while nothing was downloading.
        if not enqueued:
            logger.error(
                "slskd rejected queue download — leaving queue item untouched",
                queue_id=queue_id,
                username=username,
                filename=_stored_filename,
            )
            return jsonify({
                "success": False,
                "error": "slskd did not accept the download request",
                "requested": requested,
                "userBatches": results,
            }), 502
        try:
            update_queue_item(
                queue_id,
                found_filename=_stored_filename,
                slskd_username=username,
                status="downloading",
                is_manual_download=True,
            )
        except Exception as db_err:
            logger.warning("Could not link queue to download", queue_id=queue_id, error=str(db_err))

        try:
            log_queue(f"[MANUAL] {username} → queue {queue_id}: {_stored_filename} (downloading)")
        except Exception as exc:
            logger.debug("Failed to write to queue log", error=str(exc))

        return jsonify({"success": True, "requested": requested, "userBatches": results})
    except Exception as exc:
        logger.error("Failed to queue download", queue_id=queue_id, error=str(exc))
        return jsonify({"error": str(exc)}), 500

@slskd_bp.route("/events", methods=["GET"])
def slskd_events() -> Any:
    """Get recent slskd events."""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd not enabled"}), 400

    try:
        client = SlskdHttpClient(slskd_config["web_url"], slskd_config.get("api_key", ""))
        events = client.get_events(timeout=10)
        return jsonify({"success": True, "events": events or []})
    except Exception as exc:
        logger.error("Failed to fetch events", error=str(exc))
        return jsonify({"error": str(exc)}), 500


# ===========================================================================
# /api/slsk/ routes
# ===========================================================================

_slsk_banned_words: dict[str, bool] = {}
_slsk_banned_words_lock = threading.Lock()


@slsk_bp.route("/banned-words", methods=["GET"])
def api_get_banned_words() -> Any:
    """Get banned words and suggested words."""
    return jsonify({"words": list(_slsk_banned_words.keys())})


@slsk_bp.route("/banned-words", methods=["POST"])
async def api_add_banned_word() -> Any:
    """Add or update a banned word."""
    data = (await request.get_json()) or {}
    word = str(data.get("word") or "").strip().lower()

    if not word:
        return jsonify({"error": "word required"}), 400

    with _slsk_banned_words_lock:
        _slsk_banned_words[word] = True

    return jsonify({"success": True})


@slsk_bp.route("/banned-words/<path:word>", methods=["DELETE"])
def api_delete_banned_word(word: str) -> Any:
    """Remove a word from the banned words list."""
    with _slsk_banned_words_lock:
        _slsk_banned_words.pop(word.strip().lower(), None)

    return jsonify({"success": True})


@slsk_bp.route("/banned-words/dismiss-all", methods=["POST"])
def api_dismiss_all_suggested_words() -> Any:
    """Move all suggested words into dismissed."""
    return jsonify({"success": True})
