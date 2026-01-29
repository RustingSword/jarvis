#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_PATH="${CONFIG_PATH:-${ROOT_DIR}/config.yaml}"

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

exec "${PYTHON_BIN}" -m jarvis --config "${CONFIG_PATH}"
