#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_PATH="${CONFIG_PATH:-${ROOT_DIR}/config.yaml}"
VENV_PATH="${VENV_PATH:-${ROOT_DIR}/.venv}"
PYTHON_BIN="${PYTHON_BIN:-${VENV_PATH}/bin/python}"

if [ -f "${ROOT_DIR}/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  source "${ROOT_DIR}/.env"
  set +a
fi

if [ ! -f "${CONFIG_PATH}" ]; then
  echo "Config not found: ${CONFIG_PATH}" >&2
  exit 1
fi

if [ ! -x "${PYTHON_BIN}" ]; then
  echo "Python venv not found at ${PYTHON_BIN}. Create it with: python -m venv ${VENV_PATH}" >&2
  exit 1
fi

"${PYTHON_BIN}" - <<PY
from pathlib import Path
import shutil
from jarvis.config import load_config
from jarvis.storage import Storage

config = load_config("${CONFIG_PATH}")
print("Config loaded.")

if not config.telegram.token or config.telegram.token == "YOUR_BOT_TOKEN":
    print("WARNING: Telegram token looks unset.")

if not shutil.which(config.codex.exec_path):
    print(f"WARNING: codex CLI not found in PATH: {config.codex.exec_path}")

storage = Storage(config.storage)
import asyncio

async def main():
    await storage.connect()
    await storage.close()
    print("Storage initialized.")

asyncio.run(main())
PY

echo "Smoke test completed."
