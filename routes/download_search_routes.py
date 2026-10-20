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
from helpers.config_helpers import get_config, get_search_quality_config
from helpers.logging_config import log_queue, log_search
from services.downloads.slskd_service import SlskdService, rank_search_results

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


def _file_meets_quality(
    file: Any,
    min_bitrate: int,
    min_sample_rate: int,
    min_length: int,
    allowed_formats: set[str] | None = None,
) -> bool:
    try:
        meets = bool(file.matches_quality(min_bitrate, min_sample_rate, allowed_formats=allowed_formats))
    except Exception as exc:
        logger.debug("Quality check unavailable for search file; keeping it", error=str(exc))
        return True

    if meets and min_length:
        try:
            length = int(getattr(file, "length", 0) or 0)
        except (TypeError, ValueError):
            length = 0
        if length and length < min_length:
            return False

    return meets


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
            connected = await asyncio.to_thread(slskd_service.is_connected)
        except Exception:
            connected = True
        if not connected:
            return jsonify({
                "error": "slskd is not connected to the Soulseek network",
                "slskd_disconnected": True,
            }), 503

        try:
            await asyncio.to_thread(slskd_service.maybe_clear_stale_searches, budget_seconds=6)
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


@slskd_bp.route("/search/<search_id>/stop", methods=["POST"])
async def slskd_stop_search(search_id: str) -> Any:
    """Explicitly stop a running slskd search to free up worker threads."""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd not enabled"}), 400

    try:
        client = SlskdHttpClient(slskd_config["web_url"], slskd_config.get("api_key", ""))
        success = await asyncio.to_thread(client.stop_search, search_id)
        return jsonify({"success": success, "searchId": search_id})
    except Exception as exc:
        logger.error("Failed to stop search", search_id=search_id, error=str(exc))
        return jsonify({"error": str(exc)}), 500


@slskd_bp.route("/transfers/cleanup", methods=["POST"])
async def slskd_cleanup_transfers() -> Any:
    """Clear out stalled, errored, or timed-out downloads from slskd."""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd not enabled"}), 400

    try:
        client = SlskdHttpClient(slskd_config["web_url"], slskd_config.get("api_key", ""))
        cancelled_count = await asyncio.to_thread(client.cancel_all_stalled_downloads)
        await asyncio.to_thread(client.remove_completed_downloads)
        return jsonify({"success": True, "cancelledCount": cancelled_count})
    except Exception as exc:
        logger.error("Failed to clean up transfers", error=str(exc))
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

        with _manual_search_lock:
            _query = str((_manual_search_state.get(search_id) or {}).get("query") or "")

        _quality_cfg = get_search_quality_config()
        _min_bitrate = int(_quality_cfg.get("min_bitrate") or 0)
        _min_sample_rate = int(_quality_cfg.get("min_sample_rate") or 0)
        _min_length = int(_quality_cfg.get("min_length_seconds") or 0)
        _allowed_formats = set(_quality_cfg.get("allowed_formats") or set())
        _format_filtered = 0

        results = []
        for resp in responses or []:
            username = getattr(resp, "username", "") or ""
            free_slot = bool(getattr(resp, "has_free_upload_slot", True))
            for file in getattr(resp, "files", []) or []:
                if _allowed_formats and (getattr(file, "extension", "") or "") not in _allowed_formats:
                    _format_filtered += 1
                    continue
                meets = _file_meets_quality(
                    file, _min_bitrate, _min_sample_rate, _min_length,
                    allowed_formats=_allowed_formats,
                )
                results.append({
                    "username": username,
                    "filename": getattr(file, "filename", "") or "",
                    "size": getattr(file, "size", 0) or 0,
                    "size_mb": f"{getattr(file, 'size_mb', 0):.2f}",
                    "bitrate": getattr(file, "bitrate", 0) or 0,
                    "sample_rate": getattr(file, "sample_rate", 0) or 0,
                    "length": getattr(file, "length", 0) or 0,
                    "duration": getattr(file, "duration_formatted", "0:00") or "0:00",
                    "freeUploadSlots": 1 if free_slot else 0,
                    "has_free_upload_slot": free_slot,
                    "upload_speed": getattr(resp, "upload_speed", None),
                    "queue_length": getattr(resp, "queue_length", None),
                    "meets_quality": bool(meets),
                })

        ranked = rank_search_results(results, _query)
        qualifying = [r for r in ranked if r.get("meets_quality")]
        below_quality = [r for r in ranked if not r.get("meets_quality")]
        ordered = qualifying + below_quality

        count = len(ordered or [])
        response_count = len(responses or [])

        with _manual_search_lock:
            state_entry = _manual_search_state.get(search_id)
            if state_entry is not None:
                if count != state_entry.get("last_result_count", -1):
                    state_entry["last_result_count"] = count
                    _log_manual_search_event(
                        search_type="manual",
                        query=state_entry.get("query") or search_id,
                        result_count=count,
                        notes="results_returned",
                    )
                if is_complete:
                    _term_count = state_entry.get("_terminal_seen", 0) + 1
                    state_entry["_terminal_seen"] = _term_count
                    if _term_count >= 2 or count > 0:
                        _manual_search_state.pop(search_id, None)

        return jsonify({
            "results": ordered,
            "state": state or "InProgress",
            "responseCount": response_count,
            "fileCount": count,
            "isComplete": bool(is_complete),
            "quality": {
                "min_bitrate": _min_bitrate,
                "min_sample_rate": _min_sample_rate,
                "min_length_seconds": _min_length,
                "qualifying": len(qualifying),
                "total": count,
            },
            "qualityFallback": bool(count) and not qualifying,
        })
    except Exception as exc:
        logger.error("Failed to poll search results", search_id=search_id, error=str(exc))
        return jsonify({"error": str(exc)}), 500


@slskd_bp.route("/download", methods=["POST"])
async def slskd_download() -> Any:
    """Proxy endpoint to download from slskd."""
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
        result = await asyncio.to_thread(client.cancel_download, username, transfer_id)
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
    """Retry a failed Soulseek download."""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd not enabled"}), 400

    payload = (await request.get_json()) or {}
    username = payload.get("username", "")
    transfer_id = payload.get("transfer_id") or ""

    if not username or not transfer_id:
        return jsonify({
            "error": "username and transfer_id required",
            "detail": "slskd identifies a transfer by username + transfer id.",
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

        if not enqueued:
            logger.error("slskd rejected queue download", queue_id=queue_id, username=username, filename=_stored_filename)
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
    return jsonify({"words": list(_slsk_banned_words.keys())})


@slsk_bp.route("/banned-words", methods=["POST"])
async def api_add_banned_word() -> Any:
    data = (await request.get_json()) or {}
    word = str(data.get("word") or "").strip().lower()

    if not word:
        return jsonify({"error": "word required"}), 400

    with _slsk_banned_words_lock:
        _slsk_banned_words[word] = True

    return jsonify({"success": True})


@slskd_bp.route("/banned-words/<path:word>", methods=["DELETE"])
def api_delete_banned_word(word: str) -> Any:
    with _slsk_banned_words_lock:
        _slsk_banned_words.pop(word.strip().lower(), None)

    return jsonify({"success": True})


@slsk_bp.route("/banned-words/dismiss-all", methods=["POST"])
def api_dismiss_all_suggested_words() -> Any:
    return jsonify({"success": True})
