#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_PATH="${CONFIG_PATH:-${ROOT_DIR}/config.yaml}"
PID_FILE="${ROOT_DIR}/jarvis.pid"

if [ -f "${ROOT_DIR}/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  source "${ROOT_DIR}/.env"
  set +a
fi

VENV_PATH="${VENV_PATH:-${ROOT_DIR}/.venv}"
PYTHON_BIN="${PYTHON_BIN:-${VENV_PATH}/bin/python}"

if [ ! -x "${PYTHON_BIN}" ]; then
  echo "Python venv not found at ${PYTHON_BIN}. Create it with: python -m venv ${VENV_PATH}" >&2
  exit 1
fi

get_pid() {
  if [ -f "${PID_FILE}" ]; then
    cat "${PID_FILE}"
  fi
}

is_running() {
  local pid=$(get_pid)
  if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
    return 0
  fi
  return 1
}

start() {
  if is_running; then
    echo "Jarvis is already running (PID: $(get_pid))"
    exit 1
  fi

  echo "Starting Jarvis..."
  nohup "${PYTHON_BIN}" -m jarvis --config "${CONFIG_PATH}" > /dev/null 2>&1 &
  local pid=$!
  echo ${pid} > "${PID_FILE}"

  sleep 1
  if is_running; then
    echo "Jarvis started successfully (PID: ${pid})"
    echo "Log file: ~/.jarvis/jarvis.log"
  else
    echo "Failed to start Jarvis"
    rm -f "${PID_FILE}"
    exit 1
  fi
}

stop() {
  if ! is_running; then
    echo "Jarvis is not running"
    rm -f "${PID_FILE}"
    exit 1
  fi

  local pid=$(get_pid)
  echo "Stopping Jarvis (PID: ${pid})..."
  kill "${pid}"

  local timeout=10
  while [ ${timeout} -gt 0 ] && is_running; do
    sleep 1
    timeout=$((timeout - 1))
  done

  if is_running; then
    echo "Force killing Jarvis..."
    kill -9 "${pid}"
    sleep 1
  fi

  rm -f "${PID_FILE}"
  echo "Jarvis stopped"
}

restart() {
  echo "Restarting Jarvis..."
  if is_running; then
    stop
  fi
  start
}

status() {
  if is_running; then
    echo "Jarvis is running (PID: $(get_pid))"
    exit 0
  else
    echo "Jarvis is not running"
    exit 1
  fi
}

case "${1:-}" in
  start)
    start
    ;;
  stop)
    stop
    ;;
  restart)
    restart
    ;;
  status)
    status
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status}"
    exit 1
    ;;
esac
