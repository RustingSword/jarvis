#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_PATH="${CONFIG_PATH:-${ROOT_DIR}/config.yaml}"
RUN_SH="${ROOT_DIR}/scripts/run.sh"
PID_FILE="${ROOT_DIR}/.jarvis.pid"

DELAY_SECONDS=0
FORCE=0
DRY_RUN=0

usage() {
  cat <<'USAGE'
Usage: restart_clean.sh [--delay <seconds>] [--force] [--dry-run]

Options:
  --delay <seconds>  Delay before executing stop/cleanup/start
  --force            Force kill residual Jarvis processes (TERM -> KILL)
  --dry-run          Print actions without executing
  -h, --help         Show help
USAGE
}

log() {
  echo "[restart_clean] $*"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --delay)
      DELAY_SECONDS="${2:-}"
      if [[ -z "${DELAY_SECONDS}" ]]; then
        log "--delay requires a value"
        exit 2
      fi
      shift 2
      ;;
    --force)
      FORCE=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      log "Unknown option: $1"
      usage
      exit 2
      ;;
  esac
 done

if [[ ! -x "${RUN_SH}" ]]; then
  log "run.sh not found or not executable: ${RUN_SH}"
  exit 1
fi

if [[ ${DELAY_SECONDS} -gt 0 ]]; then
  log "Delaying ${DELAY_SECONDS}s before restart"
  if [[ ${DRY_RUN} -eq 0 ]]; then
    sleep "${DELAY_SECONDS}"
  fi
fi

log "Stopping via run.sh (PID file: ${PID_FILE})"
if [[ ${DRY_RUN} -eq 0 ]]; then
  if ! "${RUN_SH}" stop; then
    log "run.sh stop failed or Jarvis not running; continuing"
  fi
else
  log "[dry-run] ${RUN_SH} stop"
fi

pattern="python -m jarvis --config ${CONFIG_PATH}"
mapfile -t pids < <(pgrep -f "${pattern}" || true)

if [[ ${#pids[@]} -gt 0 ]]; then
  log "Residual Jarvis processes detected: ${pids[*]}"
  if [[ ${FORCE} -eq 0 ]]; then
    log "Refusing to kill residual processes without --force"
    exit 2
  fi

  for pid in "${pids[@]}"; do
    if [[ ${DRY_RUN} -eq 1 ]]; then
      log "[dry-run] kill -TERM ${pid}"
    else
      log "Sending TERM to ${pid}"
      kill -TERM "${pid}" || true
    fi
  done

  if [[ ${DRY_RUN} -eq 0 ]]; then
    timeout=10
    while [[ ${timeout} -gt 0 ]]; do
      alive=0
      for pid in "${pids[@]}"; do
        if kill -0 "${pid}" 2>/dev/null; then
          alive=1
          break
        fi
      done
      if [[ ${alive} -eq 0 ]]; then
        break
      fi
      sleep 1
      timeout=$((timeout - 1))
    done

    for pid in "${pids[@]}"; do
      if kill -0 "${pid}" 2>/dev/null; then
        log "Sending KILL to ${pid}"
        kill -KILL "${pid}" || true
      fi
    done
  fi
fi

log "Starting via run.sh"
if [[ ${DRY_RUN} -eq 0 ]]; then
  "${RUN_SH}" start
else
  log "[dry-run] ${RUN_SH} start"
fi

if [[ ${DRY_RUN} -eq 0 ]]; then
  if [[ -f "${PID_FILE}" ]]; then
    new_pid="$(cat "${PID_FILE}")"
    if [[ -n "${new_pid}" ]] && kill -0 "${new_pid}" 2>/dev/null; then
      log "Jarvis started successfully (PID: ${new_pid})"
      log "Log file: ~/.jarvis/jarvis.log"
    else
      log "Jarvis failed to start; check log: ~/.jarvis/jarvis.log"
      exit 4
    fi
  else
    log "PID file not found; check log: ~/.jarvis/jarvis.log"
    exit 4
  fi
fi
