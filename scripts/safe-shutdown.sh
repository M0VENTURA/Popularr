#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Safe shutdown for PostgreSQL maintenance.
#
#   ./scripts/safe-shutdown.sh status      # what is running, and is it safe?
#   ./scripts/safe-shutdown.sh prepare     # quiesce + drain + release the pool
#   ./scripts/safe-shutdown.sh resume      # undo it
#   ./scripts/safe-shutdown.sh stop-db     # prepare, then stop PostgreSQL CLEANLY
#   ./scripts/safe-shutdown.sh start-db    # bring PostgreSQL back
#   ./scripts/safe-shutdown.sh upgrade     # stop-db, pull, start-db, wait healthy
#
# WHY THIS EXISTS
# ---------------
# `docker compose stop db` on its own is NOT a clean stop.
#
# `docker stop` sends SIGTERM, and PostgreSQL treats SIGTERM as a **smart
# shutdown**: it stops accepting work, then WAITS FOR EVERY CLIENT TO DISCONNECT.
# The application's connection pool holds idle connections open, and an idle
# pooled connection is still a client — so the postmaster keeps waiting until
# Docker's stop timeout expires and the container is **SIGKILLed** (exit 137).
# The next start then does crash recovery.
#
# It also stops the database underneath whatever the app is doing. A scan or a
# queue cycle that is mid-write against a server that is going away is the
# "engine disposed" cascade this codebase already suffers from.
#
# So this script does both halves in the right order:
#   1. quiesce  — pause the scheduler, stop scans, idle the queue worker
#   2. drain    — wait for in-flight work (reported, never forced)
#   3. release  — close the pool, which is what lets PostgreSQL shut down at once
#   4. stop     — and then VERIFY the exit code, because a force-kill is silent
#
# The verification at step 4 is the point. Without it you cannot tell a clean
# stop from a crash recovery waiting to happen.
#
# The app is driven through `docker compose exec`, NOT the HTTP API, so no
# session or credentials are involved and it still works when the web server is
# unhealthy.
# ─────────────────────────────────────────────────────────────────────────────

set -Eeuo pipefail

COMPOSE="${POPULARR_COMPOSE:-docker compose}"
APP_SERVICE="${POPULARR_APP_SERVICE:-app}"
DB_SERVICE="${POPULARR_DB_SERVICE:-db}"
DB_CONTAINER="${POPULARR_DB_CONTAINER:-popularr-db}"
DB_USER="${PG_USER:-popularr}"
DB_NAME="${PG_DATABASE:-popularr}"

# How long to wait for PostgreSQL to finish shutting down before we give up and
# report. A smart shutdown that has no clients left takes well under a second;
# anything approaching this means something is still connected.
DB_STOP_TIMEOUT="${POPULARR_DB_STOP_TIMEOUT:-30}"

log()  { printf '%s\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
warn() { printf '  ⚠ %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

require_docker() {
    command -v docker >/dev/null 2>&1 || die "docker not found in PATH"
}

# Run a Python expression inside the app container and print its stdout.
# The app's own service module is used rather than the HTTP API: no auth, and it
# works even when hypercorn is not answering.
in_app() {
    $COMPOSE exec -T "$APP_SERVICE" python3 -c "$1"
}

cmd_status() {
    log "Safe shutdown status:"
    in_app "from services.maintenance.safe_shutdown_service import safe_shutdown_status as s; import json; print(json.dumps(s(), indent=2))"
}

cmd_prepare() {
    log "Quiescing the application (scheduler, scans, queue worker) and releasing connections..."
    in_app "
from services.maintenance.safe_shutdown_service import prepare_for_database_shutdown
import json
r = prepare_for_database_shutdown(reason='safe-shutdown.sh')
print(json.dumps(r, indent=2, default=str))
print()
print('SAFE TO STOP' if r.get('safe_to_stop') else 'STILL BUSY — see active_scans / queue_items above')
"
}

cmd_resume() {
    log "Resuming normal operation..."
    in_app "
from services.maintenance.safe_shutdown_service import cancel_safe_shutdown
import json
print(json.dumps(cancel_safe_shutdown(reason='safe-shutdown.sh'), indent=2, default=str))
"
}

# Ask the app whether it is safe, and fail if it is not.
assert_safe() {
    local verdict
    verdict="$(in_app "
from services.maintenance.safe_shutdown_service import safe_shutdown_status
s = safe_shutdown_status()
print('SAFE' if s.get('safe_to_stop') else 'BUSY')
print('scans=' + str(s.get('active_scans')) + ' queue=' + str(s.get('queue_items')))
" | tr -d '\r')"

    if ! printf '%s' "$verdict" | grep -q '^SAFE'; then
        warn "Not safe to stop the database yet:"
        printf '%s\n' "$verdict" | sed 's/^/    /' >&2
        die "refusing to stop PostgreSQL while work is in flight (re-run: $0 status)"
    fi
    ok "no scans running, queue idle"
}

cmd_stop_db() {
    require_docker
    cmd_prepare
    log ""
    log "Verifying quiescence before stopping PostgreSQL..."
    assert_safe

    log ""
    log "Stopping PostgreSQL (clean smart shutdown expected)..."
    $COMPOSE stop -t "$DB_STOP_TIMEOUT" "$DB_SERVICE" >/dev/null

    # ⚠️ THE VERIFICATION THAT MAKES THIS WORTH DOING.
    # A clean shutdown exits 0. A container that had to be SIGKILLed exits 137,
    # and the difference is invisible unless it is checked — the stop "succeeds"
    # either way.
    local exit_code
    exit_code="$(docker inspect -f '{{.State.ExitCode}}' "$DB_CONTAINER" 2>/dev/null || echo "unknown")"

    log ""
    if [ "$exit_code" = "0" ]; then
        ok "PostgreSQL stopped cleanly (exit 0). Safe to update."
    elif [ "$exit_code" = "137" ]; then
        warn "PostgreSQL was FORCE-KILLED (exit 137) — something still held a connection."
        warn "The next start will run crash recovery. Check: $COMPOSE logs $DB_SERVICE"
        return 1
    else
        warn "Could not confirm a clean stop (exit code: $exit_code). Check: $COMPOSE logs $DB_SERVICE"
        return 1
    fi
}

cmd_start_db() {
    require_docker
    log "Starting PostgreSQL..."
    $COMPOSE start "$DB_SERVICE" >/dev/null

    log "Waiting for it to accept connections..."
    local waited=0
    until docker exec "$DB_CONTAINER" pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; do
        if [ "$waited" -ge 60 ]; then
            warn "PostgreSQL did not become ready within 60s. Check: $COMPOSE logs $DB_SERVICE"
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    ok "PostgreSQL is accepting connections (${waited}s)"

    log ""
    log "Resuming the application..."
    cmd_resume
    log ""
    log "Done. The app reconnects on its own — the engine was kept, so the next"
    log "query opens a fresh connection. No container restart is required."
}

cmd_upgrade() {
    require_docker
    log "=== 1/4  Stop PostgreSQL cleanly ==="
    cmd_stop_db
    log ""
    log "=== 2/4  Pull the new image ==="
    $COMPOSE pull "$DB_SERVICE"
    log ""
    log "=== 3/4  Start PostgreSQL ==="
    cmd_start_db
    log ""
    log "=== 4/4  Check the logs for recovery ==="
    log "A clean stop means NO 'database system was interrupted' / crash recovery."
    log "Look for: 'database system is ready to accept connections'"
    log ""
    $COMPOSE logs --tail=30 "$DB_SERVICE" || true
    log ""
    warn "If the major version CHANGED (16 -> 17), the data directory is NOT"
    warn "compatible and PostgreSQL will refuse to start. That needs pg_upgrade"
    warn "or a dump/restore — this script only handles a clean stop and restart."
}

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

main() {
    case "${1:-}" in
        status)   cmd_status ;;
        prepare)  cmd_prepare ;;
        resume)   cmd_resume ;;
        stop-db)  cmd_stop_db ;;
        start-db) cmd_start_db ;;
        upgrade)  cmd_upgrade ;;
        ""|-h|--help|help) usage ;;
        *) die "unknown command: $1 (try: status, prepare, resume, stop-db, start-db, upgrade)" ;;
    esac
}

main "$@"
