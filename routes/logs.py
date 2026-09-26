"""
Log viewing and download routes.
"""

from __future__ import annotations

import asyncio
import json
import os

from quart import (
    Blueprint,
    jsonify,
    make_response,
    request,
    Response,
    send_file,
)

from services.log_service import (
    get_unified_log,
    get_log_file_content,
    download_log,
    resolve_log_file_path,
    stream_append_log,
    export_log_lines_since,
)

logs_bp = Blueprint(
    "logs",
    __name__,
)


# =============================================================================
# LOG FILE CONTENT (any file in the log dir)
# =============================================================================

@logs_bp.route("/api/log-file", methods=["GET"])
def api_log_file():
    """Return the tail of an arbitrary log file from the log directory.

    Query params:
        name  - log filename (e.g. ``info.log``, ``debug.log``)
        lines - number of lines (1-2000, default 500), or ``all`` for the
                full file history (used by the /logs page)
    """
    name = request.args.get("name", "").strip()
    lines = request.args.get("lines", "500")
    result = get_log_file_content(name, lines)
    if isinstance(result, tuple) and len(result) == 2:
        payload, status = result
        return jsonify(payload), status
    return jsonify(result)


# =============================================================================
# UNIFIED LOG
# =============================================================================

@logs_bp.route(
    "/api/unified-log",
    methods=["GET"],
)
def api_unified_log():
    """
    Return unified log content.

    Query parameters:
        lines     - number of lines to return (1-2000)
        verbose   - include verbose/debug entries (0 or 1)
        last_hour - only return lines within the last hour (0 or 1)
    """

    try:
        lines = int(request.args.get("lines", 400))
        lines = max(1, min(lines, 2000))
    except (TypeError, ValueError):
        lines = 400

    verbose = request.args.get("verbose", "0") == "1"
    last_hour = request.args.get("last_hour", "0") == "1"

    # Now calling with just lines and verbose, as service resolves paths internally
    result = get_unified_log(
        lines=lines,
        verbose=verbose,
        last_hour=last_hour,
    )

    if isinstance(result, tuple) and len(result) == 2:
        payload, status = result
        return jsonify(payload), status

    return jsonify(result)


# =============================================================================
# DOWNLOAD LOG FILE
# =============================================================================

@logs_bp.route(
    "/api/download-log/<log_type>",
    methods=["GET"],
)
async def api_download_log(log_type: str):
    """
    Download a log file.
    """

    result = await download_log(log_type)

    # If result is a Flask Response (e.g. from _build_download_response), 
    # return it directly.
    if isinstance(result, Response):
        return result

    # Otherwise, handle the (payload, status) tuple
    if isinstance(result, tuple) and len(result) == 2:
        payload, status = result
        return jsonify(payload), status


# =============================================================================
# EXPORT LOGS (last N hours, per source)
# =============================================================================

@logs_bp.route("/api/logs/client", methods=["POST"])
async def api_client_log():
    """Append a client-side UI message to ``client.log``.

    Called by the global alert→toast shim (main.js) so every converted
    alert is also recorded in the log files for troubleshooting.
    """
    data = (await request.get_json(silent=True)) or {}
    message = str(data.get("message") or "").strip()
    if not message:
        return jsonify({"ok": False}), 400
    try:
        from services.log_service import append_client_log
        append_client_log(message)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@logs_bp.route("/api/logs/export", methods=["GET"])
def api_export_logs():
    """Export the last N hours of a log source as a downloadable .log file.

    Query params:
        source - scanner | soulseek | navidrome | system (default scanner)
        hours  - hours of history (1-24, default 1)
    """
    source = (request.args.get("source") or "scanner").strip().lower()
    try:
        hours = max(1, min(int(request.args.get("hours", 1)), 24))
    except (TypeError, ValueError):
        hours = 1

    content = export_log_lines_since(source, hours)
    return Response(
        content,
        mimetype="text/plain",
        headers={
            "Content-Disposition": f"attachment; filename={source}_log_last_{hours}h.log"
        },
    )


# =============================================================================
# DOWNLOAD BY FILENAME (any file in the log dir, security-constrained)
# =============================================================================

@logs_bp.route("/api/logs/download", methods=["GET"])
async def api_logs_download():
    """Download an arbitrary log file by its basename.

    Uses the same ``resolve_log_file_path`` basename constraint as the log
    viewer, so path traversal is impossible.
    """
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "Missing name parameter"}), 400
    log_path = resolve_log_file_path(name)
    if not log_path or not os.path.isfile(log_path):
        return jsonify({"error": f"Log not found: {name}"}), 404
    # No explicit download name: send_file derives the attachment filename
    # from the path basename, and resolve_log_file_path already constrains
    # the file to the logs directory.
    return await send_file(log_path, as_attachment=True)


# =============================================================================
# SSE LIVE TAIL STREAM
# =============================================================================

@logs_bp.route("/api/logs/stream")
async def api_logs_stream():
    """Server-Sent Events stream of appended log lines for a log file.

    Query param ``name`` selects the file (default ``unified_scan.log``).
    Frames are ``data: {"lines": [...]}\n\n`` emitted only when new complete
    lines appear (checked on a 1s tick).  The stream replaces the page's
    5s polling while active; the browser reconnects automatically.
    """
    name = request.args.get("name", "unified_scan.log").strip()
    log_path = resolve_log_file_path(name)
    if not log_path or not os.path.isfile(log_path):
        return jsonify({"error": f"Log not found: {name}"}), 404

    async def event_stream():
        offset = 0
        pending = ""
        while True:
            lines, offset, pending = stream_append_log(name, offset, pending)
            if lines:
                yield f"data: {json.dumps({'lines': lines})}\n\n"
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(1.0)

    response = await make_response(
        event_stream(),
        {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
    response.timeout = None
    return response