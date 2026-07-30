#!/usr/bin/env bash
# Railway APP-service entrypoint — API-FIRST + supervised workers.
#
# Boot order (deliberate, after the 2026-07-30 incident where boot-blocking work
# hung new containers so healthchecks flunked, cutovers never completed, and the
# unsupervised pipeline/driver died silently for ~40 min):
#
#   1. serve_api starts FIRST (supervised) and binds $PORT immediately, so the
#      Railway healthcheck (GET /health) passes in seconds and cutovers complete
#      reliably — no more drained-old-container limbo. On an existing volume the
#      schema is already present; on a fresh one the API self-heals once bootstrap
#      runs init_db (serve_api relaunches under its supervisor meanwhile).
#   2. BOOTSTRAP runs AFTER the API is up: wait-for-DB-dir-writable (covers a volume
#      that attaches late), then init_db, hydrate_seed(+self-heal), assign policies,
#      model fetch, entities, universe — each with a HARD TIMEOUT, a loud [boot]
#      line, and isolated failure (log + continue). Nothing here can hang boot.
#   3. WORKERS (pipeline, and the driver if TRADER_DRIVER_ENABLED) launch AFTER
#      bootstrap, SUPERVISED: if a worker exits it is relaunched with capped
#      backoff and a loud [boot] line — a worker death is never silent again.
#
# Every stage prints an unmistakable [boot] line so a runtime log instantly shows
# where boot is. Order placement stays ONLY in the driver's internal clock loop.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

# Make the `pipeline` package (src/ layout) importable without an editable install.
export PYTHONPATH="$PWD/src:$PWD:${PYTHONPATH:-}"
export HOST=0.0.0.0
export SENTIMENT_MODE="${SENTIMENT_MODE:-lexicon}"
INTERVAL="${PIPELINE_INTERVAL:-300}"
PORT="${PORT:-8001}"

log() { echo "[boot] $*"; }

# Run a bootstrap step with a hard timeout, loud logging, isolated failure. A step
# that hangs is killed at the timeout and boot continues — it can never wedge boot.
run_step() {
  local name="$1" secs="$2"; shift 2
  log "START $name (timeout ${secs}s)"
  if timeout "${secs}" "$@"; then
    log "OK    $name"
  else
    log "FAIL  $name (rc=$? — continuing)"
  fi
}

# Supervise a long-running worker: (re)launch forever, relaunch on ANY exit with
# capped exponential backoff and a loud line. Runs as a detached subshell.
supervise() {
  local name="$1"; shift
  (
    local delay=5
    while true; do
      log "WORKER start: ${name}"
      "$@"
      local rc=$?
      log "WORKER ${name} EXITED rc=${rc} — relaunching in ${delay}s"
      sleep "${delay}"
      delay=$(( delay * 2 )); [ "${delay}" -gt 60 ] && delay=60
    done
  ) &
}

# --- 1. API FIRST -----------------------------------------------------------
log "=== API-FIRST BOOT (port ${PORT}) ==="
supervise "api" python scripts/serve_api.py
log "api launching + supervised (healthcheck target GET /health)"

# --- 2. BOOTSTRAP (after the API is up) -------------------------------------
bootstrap() {
  log "=== BOOTSTRAP START ==="

  # Wait for the DB directory to be writable (a volume can attach a beat late).
  # Bounded to ~60s; on timeout we continue rather than hang the whole boot.
  local dbdir
  dbdir="$(python -c "from pipeline.common.db import database_url; from pipeline.common.volume import sqlite_dir; print(sqlite_dir(database_url()) or '')" 2>/dev/null || echo "")"
  if [ -n "${dbdir}" ]; then
    mkdir -p "${dbdir}" 2>/dev/null || true
    local i=0
    while ! { touch "${dbdir}/.boot_probe" 2>/dev/null && rm -f "${dbdir}/.boot_probe" 2>/dev/null; }; do
      i=$(( i + 1 ))
      [ "${i}" -eq 1 ] && log "waiting for DB dir ${dbdir} to be writable (volume attach)…"
      if [ "${i}" -ge 60 ]; then
        log "WARN DB dir ${dbdir} not writable after 60s — continuing"
        break
      fi
      sleep 1
    done
    [ "${i}" -lt 60 ] && log "DB dir ${dbdir} writable"
  fi

  run_step "volume-banner"     20  python -c "from pipeline.common.volume import volume_status as v; s=v(); print('[boot] VOLUME', 'OK' if s['confirmed'] else ('EPHEMERAL' if s['on_railway'] else 'local'), '-', s['reason'])"
  run_step "init_db"           60  python scripts/init_db.py
  run_step "hydrate_seed"      300 python scripts/hydrate_seed.py
  run_step "assign_policies"   60  python scripts/assign_exit_policies.py
  run_step "fetch_model"       600 python scripts/fetch_model.py
  run_step "seed_entities"     300 python scripts/seed_entities.py
  run_step "snapshot_universe" 300 python scripts/snapshot_universe.py

  log "=== BOOTSTRAP COMPLETE ==="
}
bootstrap

# --- 3. WORKERS (supervised) ------------------------------------------------
supervise "pipeline" python scripts/run_pipeline.py --interval "${INTERVAL}"
log "pipeline supervised (interval=${INTERVAL}s, SENTIMENT_MODE=${SENTIMENT_MODE})"

case "${TRADER_DRIVER_ENABLED:-}" in
  1 | true | TRUE | True | yes | on)
    # Supervised: if the driver refuses (guard) or dies, it relaunches with
    # backoff and retries — so a late-attaching volume auto-recovers into trading,
    # and TRADER_VOLUME_GUARD=off / the guard/kill-switch semantics still apply.
    supervise "driver" python scripts/run_trader.py
    log "TRADER driver ENABLED — supervised"
    ;;
  *)
    log "TRADER driver disabled (TRADER_DRIVER_ENABLED unset/false)"
    ;;
esac

# --- 4. keep the container alive on the supervisors (they never exit) --------
log "=== BOOT DONE — supervising api + pipeline${TRADER_DRIVER_ENABLED:+ + driver} ==="
wait
