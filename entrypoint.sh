#!/bin/bash
# Popularr container entrypoint.
# Orchestration only; database logic lives in db/ and migrations/.

set -Eeuo pipefail

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║              Popularr — Music Popularity Scanner         ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

QUEUE_PID=""

log()  { echo "$*"; }
warn() { echo "⚠ $*"; }
ok()   { echo "✓ $*"; }
ok2()  { echo "  ✓ $*"; }

cleanup() {
    log "Stopping Popularr..."
    if [ -n "${QUEUE_PID:-}" ] && kill -0 "$QUEUE_PID" 2>/dev/null; then
        log "Stopping queue processor (PID: $QUEUE_PID)..."
        kill "$QUEUE_PID" 2>/dev/null || true
        wait "$QUEUE_PID" 2>/dev/null || true
    fi
}

# ---------------------------------------------------------------------------
# Wait for PostgreSQL to accept connections (Docker Compose support)
# ---------------------------------------------------------------------------
verify_scan_unwrap_fix() {
    # Startup self-check for the "Album failed: <Future ...>" fix.
    #
    # THE BUG: per-track work runs in a thread pool, and the collector used to
    # let a raw concurrent.futures.Future escape into the album-failure report,
    # so the log read  "Album failed: <Future at 0x...>"  instead of the real
    # exception. The fix unwraps every future -- ``future.result()`` inside a
    # guarded loop in the collector -- so a worker failure surfaces as itself.
    #
    # ⚠ THIS CHECK USED TO GREP A LOG STRING. It looked for the literal
    # "Album future completed", a message the original fix happened to log.
    # A later refactor of the track executor (5193741e "Cleaned up some issues
    # causing timeouts during scanning") removed that message, so the check
    # printed  source_marker_hits=0 loaded_module=MISSING  on EVERY boot and
    # could never pass again. That also destroyed the only thing it exists for:
    # telling an operator whether a stale .pyc / old image is running -- a
    # warning that always fires cannot distinguish "old image" from "expected".
    #
    # It now probes the STRUCTURAL invariant instead, which survives rewording:
    # the collector must exist and must call .result() on each completed future.
    # Log strings are free to change; that call IS the fix.
    local file="/app/services/popularity/scan_stage_runner.py"
    local src_state="0"
    if [ ! -f "$file" ]; then
        # Distinguish "wrong image / wrong path" from "marker absent" — both
        # used to collapse to the same 0, which hid a missing file entirely.
        src_state="FILE-MISSING"
    else
        src_state=$(grep -c "as_completed" "$file" 2>/dev/null || true)
    fi

    local loaded
    loaded=$(python3 -c "
import inspect
try:
    from services.popularity.scan_stage_runner import _execute_track_jobs_safely as fn
    src = inspect.getsource(fn)
except Exception as exc:
    print('ERROR: %s' % exc)
else:
    print('PRESENT' if ('as_completed' in src and 'future.result()' in src) else 'MISSING')
" 2>&1)

    log "Scan unwrap fix check: collector_source=${src_state} loaded_function=${loaded}"
    if [ "$src_state" = "FILE-MISSING" ]; then
        warn "Scan unwrap fix NOT VERIFIED — ${file} is not present in this image."
        return 1
    fi
    if [ "${src_state:-0}" -ge 1 ] && echo "$loaded" | grep -q "PRESENT"; then
        ok2 "Scan unwrap fix VERIFIED (per-future unwrap present in source + fresh import)"
        return 0
    fi
    warn "Scan unwrap fix NOT VERIFIED — collector_source=${src_state}, loaded_function=${loaded}"
    warn "If this is the current image, check for stale __pycache__/old image."
    return 1
}

wait_for_db() {
    local host="${PG_HOST:-}"
    local port="${PG_PORT:-5432}"
    local user="${PG_USER:-popularr}"
    local retries=15
    local delay=2

    # Skip the wait when no remote PG host is configured (DATABASE_URL-driven
    # setups) — the app engine requires PostgreSQL either way.
    if [ -z "$host" ] || [ "$host" = "localhost" ]; then
        return 0
    fi

    log "Waiting for PostgreSQL at $host:$port..."
    for i in $(seq 1 $retries); do
        if command -v pg_isready >/dev/null 2>&1; then
            if pg_isready -h "$host" -p "$port" -U "$user" >/dev/null 2>&1; then
                ok2 "PostgreSQL connected (attempt $i)"
                return 0
            fi
        else
            # Fallback: try a TCP connection using python
            if python3 -c "import socket; s=socket.create_connection(('$host',$port), timeout=3); s.close()" 2>/dev/null; then
                ok2 "PostgreSQL connected (attempt $i)"
                return 0
            fi
        fi
        if [ "$i" -lt "$retries" ]; then
            log "  PostgreSQL not ready yet, retrying in ${delay}s... ($i/$retries)"
            sleep "$delay"
        fi
    done
    warn "PostgreSQL did not become ready after $retries attempts — continuing anyway"
}

run_queue_startup_schema() {
    # Silent on success — the queue schema is ensured before the processor
    # starts; only failures surface (bootstrap also covers these columns).
    if python3 migrations/ensure_queue_startup_schema.py >/dev/null 2>&1; then
        return 0
    else
        warn "Minimal queue startup schema bootstrap failed (non-fatal)"
        return 1
    fi
}

run_alembic_migrations() {
    if [ ! -f alembic.ini ] || [ ! -d migrations/versions ]; then
        return 0
    fi

    # Apply migrations with FULL output visible.  Every revision is written
    # idempotently (existence-guarded DDL), so "upgrade head" converges on
    # ANY starting state: fresh DB (full chain runs), bootstrap-built DB
    # (guards skip already-created objects), or partially-migrated DB.
    #
    # The old behaviour piped stderr to /dev/null and fell back to a BLIND
    # "alembic stamp head" on failure — which marked never-applied DDL as
    # applied and produced recurring "relation ... already exists" noise on
    # every boot.  A real failure now surfaces loudly and is retried by the
    # deferred bootstrap path instead of being masked.
    if python3 -m alembic upgrade head; then
        ok2 "Alembic migrations applied"
    else
        warn "Alembic 'upgrade head' FAILED — see error above."
        warn "The runtime schema bootstrap will retry migrations after PostgreSQL is fully available."
    fi
}

run_schema_bootstrap() {
    # Silent on success — bootstrap prints its own banner/table-group lines;
    # we surface a single compact confirmation instead.
    if python3 -m db.bootstrap >/dev/null 2>&1; then
        ok2 "All 9 table groups verified"
        return 0
    else
        warn "Database schema bootstrap failed — retrying with output"
        python3 -m db.bootstrap || true
        return 1
    fi
}

check_ffmpeg() {
    log "Checking ffmpeg availability..."
    if command -v ffmpeg >/dev/null 2>&1; then
        # `ffmpeg -version` line 1 is "ffmpeg version X.Y.Z …" — $2 is the
        # literal word "version", $3 is the actual version number.
        ok2 "ffmpeg found: version $(ffmpeg -version | head -n 1 | awk '{print $3}')"
        return 0
    fi
    warn "ffmpeg not found in PATH. FLAC→MP3 conversion will be unavailable."
    if [ "${SPTNR_AUTO_INSTALL_FFMPEG:-0}" = "1" ] && command -v apt-get >/dev/null 2>&1 && [ "$(id -u)" = "0" ]; then
        log "Attempting runtime ffmpeg install..."
        apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
    fi
}

start_queue_processor() {
    local interval="${SPTNR_QUEUE_PROCESSOR_INTERVAL:-30}"
    local log_file="${SPTNR_QUEUE_PROCESSOR_LOG:-/config/queue_processor.log}"
    # Ensure log directory exists before redirect
    local log_dir
    log_dir="$(dirname "$log_file")"
    mkdir -p "$log_dir" 2>/dev/null || true
    log "Starting download queue processor (interval: ${interval}s)..."
    export SPTNR_QUEUE_PROCESSOR_MANAGED_EXTERNALLY=1
    if python3 -m services.queue.queue_worker "$interval" > "$log_file" 2>&1 &
    then
        QUEUE_PID=$!
        ok2 "Queue processor started (PID: $QUEUE_PID)"
    else
        warn "Queue processor failed to start (non-fatal, continuing)"
    fi
}

preflight_python() {
    log "Running Python syntax checks..."
    local errors=0
    local failed_files=""
    local files=(
        app.py
        services/queue/queue_worker.py
        db/utils.py
        db/context.py
        db/schema.py
        db/schema_helpers.py
        db/bootstrap.py
        db/cleanup.py
        db/database.py
        db/repositories/artists.py
        db/repositories/bookmarks.py
        db/repositories/genres.py
        db/repositories/library.py
        db/repositories/navidrome.py
        db/repositories/tracks.py
        migrations/ensure_queue_startup_schema.py
    )
    for f in "${files[@]}"; do
        if ! python3 -m py_compile "$f" 2>&1; then
            errors=$((errors + 1))
            failed_files="${failed_files}  - ${f}\n"
            warn "Syntax check FAILED: $f"
        fi
    done
    if [ "$errors" -gt 0 ]; then
        warn "Python syntax checks completed with ${errors} error(s):"
        printf "%b" "$failed_files"
        warn "Continuing despite syntax errors (non-fatal)"
    else
        ok2 "Python syntax checks passed"
    fi
    # py_compile writes .pyc files — purge them so the app never reuses
    # bytecode compiled from a stale/partial state during this boot.
    find /app -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
}

start_web_app() {
    local _error_log="${SPTNR_ERROR_LOG:-/config/error.log}"
    log "Starting web server (hypercorn, ${SPTNR_GUNICORN_WORKERS:-4} workers)..."
    echo ""
    log "Log files (viewable at /logs or via docker exec):"
    log "  Access:  ${SPTNR_ACCESS_LOG:-/config/access.log}"
    log "  Errors:  ${_error_log}"
    log "  Debug:   /config/debug.log"
    log "  Info:    /config/info.log"
    log "  Scan:    /config/unified_scan.log"

    # Hypercorn writes access/error logs directly (no built-in rotation), so a
    # long-running instance can grow access.log without bound.  Run a small
    # background rotator that copytruncates oversized files and keeps a few
    # numbered backups.  copytruncate is safe here because the writers open these
    # files in APPEND mode (hypercorn's file handler, and client.log which is
    # reopened on every write), so truncating simply moves the next write back to
    # offset 0.
    #
    # The cap MATCHES helpers/logging_config._LOG_MAX_BYTES (5 MB), which every
    # log the app owns already rotates at.  It used to default to 50 MB — TEN
    # times the app's own limit — which made access.log the one file that looked
    # like it grew without bound (reported at 26.8 MB and still climbing).
    #
    # Deliberately NOT rotated: queue_processor.log.  It is a SHELL REDIRECT
    # (``> file``), so its writer keeps its byte offset across a truncation and
    # the file would go sparse, padded with NUL bytes.  It stays near-empty
    # anyway: the queue worker calls setup_logging() and writes through the
    # rotating queue.log, so only bare prints reach the redirect.
    _rotate_log_if_large() {
        local file="$1" max_bytes="$2" keep="$3"
        [ -f "$file" ] || return 0
        local size
        size=$(stat -c %s "$file" 2>/dev/null || echo 0)
        if [ "${size:-0}" -gt "$max_bytes" ]; then
            local i
            for i in $(seq $((keep - 1)) -1 1); do
                if [ -f "$file.$i" ]; then
                    mv -f "$file.$i" "$file.$((i + 1))" 2>/dev/null || true
                fi
            done
            cp -f "$file" "$file.1" 2>/dev/null || true
            : > "$file" 2>/dev/null || true
            log "Rotated oversized log: $file (${size} bytes)"
        fi
    }
    start_log_rotation() {
        local max_bytes="${SPTNR_ACCESS_LOG_MAX_SIZE:-5242880}"
        local keep="${SPTNR_ACCESS_LOG_BACKUPS:-3}"
        local _log_dir
        _log_dir="$(dirname "${SPTNR_ACCESS_LOG:-/config/access.log}")"

        _rotate_all_logs() {
            _rotate_log_if_large "${SPTNR_ACCESS_LOG:-/config/access.log}" "$max_bytes" "$keep"
            _rotate_log_if_large "${SPTNR_ERROR_LOG:-/config/error.log}" "$max_bytes" "$keep"
            _rotate_log_if_large "${_log_dir}/client.log" "$max_bytes" "$keep"
        }

        (
            # Housekeeping FIRST, then every 5 minutes: a file left oversized by a
            # previous run is trimmed at boot instead of after the first sleep.
            _rotate_all_logs
            while true; do
                sleep 300
                _rotate_all_logs
            done
        ) &
    }
    start_log_rotation

    # Default log level is WARNING so error.log carries only real server
    # problems (bind failures, worker crashes), not the INFO "Running on ..."
    # banner that Hypercorn writes to its error stream on every start/restart.
    # Set SPTNR_LOG_LEVEL=info to restore full hypercorn output (incl. access).
    exec hypercorn             --bind "${SPTNR_GUNICORN_BIND:-0.0.0.0:5000}"             --workers "${SPTNR_GUNICORN_WORKERS:-4}"             --worker-class asyncio             --keep-alive "${SPTNR_GUNICORN_KEEP_ALIVE:-5}"             --access-logfile "${SPTNR_ACCESS_LOG:-/config/access.log}"             --error-logfile "${_error_log}"             --log-level "${SPTNR_LOG_LEVEL:-warning}"             "app:app"
}

main() {
    trap cleanup SIGTERM SIGINT EXIT
    trap 'warn "Fatal error on line $LINENO — see above for details"' ERR

    log "── Pre-flight Checks ───────────────────────────────────────"

    # Drop stale bytecode: a ``__pycache__`` compiled by a previous run can
    # silently shadow updated ``.py`` sources when source mtimes collide
    # (git checkouts / COPY preserve uniform timestamps), so a restarted
    # process keeps executing the OLD code.  Always purge before launch so
    # the app loads fresh from the current files on every start.
    find /app -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

    # Confirm the running build actually has the album-Future unwrap fix.
    verify_scan_unwrap_fix || true

    preflight_python || true

    # Test Quart app import BEFORE hypercorn
    if python3 -c "import sys; sys.path.insert(0, '.'); from app import app" 2>&1; then
        ok2 "App import OK"
    else
        warn "App import FAILED — see error above"
    fi

    check_ffmpeg || true

    echo ""
    log "── Database ────────────────────────────────────────────────"

    wait_for_db

    run_queue_startup_schema || true
    run_alembic_migrations || true
    run_schema_bootstrap || true

    echo ""
    log "── Background Services ──────────────────────────────────────"

    start_queue_processor || true

    echo ""
    log "── Launch ──────────────────────────────────────────────────"

    start_web_app

    # If we reach here, hypercorn failed to start
    warn "Web server did not start — check the error above"
    sleep 60
}

main "$@"
