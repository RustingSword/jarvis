#!/usr/bin/env bash
set -euo pipefail

APP_USER="${APP_USER:-jarvis}"
APP_GROUP="${APP_GROUP:-${APP_USER}}"
INSTALL_DIR="${INSTALL_DIR:-/opt/jarvis}"
CONFIG_DIR="${CONFIG_DIR:-/etc/jarvis}"
DATA_DIR="${DATA_DIR:-/var/lib/jarvis}"
LOG_DIR="${LOG_DIR:-/var/log/jarvis}"
SERVICE_PATH="${SERVICE_PATH:-/etc/systemd/system/jarvis.service}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root (e.g., sudo $0)" >&2
  exit 1
fi

if ! id "${APP_USER}" >/dev/null 2>&1; then
  useradd -r -s /usr/sbin/nologin "${APP_USER}"
fi

if ! getent group "${APP_GROUP}" >/dev/null 2>&1; then
  groupadd -r "${APP_GROUP}"
fi

mkdir -p "${INSTALL_DIR}" "${CONFIG_DIR}" "${DATA_DIR}" "${LOG_DIR}"
chown -R "${APP_USER}:${APP_GROUP}" "${INSTALL_DIR}" "${DATA_DIR}" "${LOG_DIR}"

# Copy code into install dir (skip if already in place)
if [ "${ROOT_DIR}" != "${INSTALL_DIR}" ]; then
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete "${ROOT_DIR}/" "${INSTALL_DIR}/"
  else
    rm -rf "${INSTALL_DIR:?}"/*
    cp -a "${ROOT_DIR}/." "${INSTALL_DIR}/"
  fi
fi

# Create virtualenv and install dependencies
if [ ! -d "${INSTALL_DIR}/.venv" ]; then
  python3 -m venv "${INSTALL_DIR}/.venv"
fi
"${INSTALL_DIR}/.venv/bin/python" -m pip install -U pip
"${INSTALL_DIR}/.venv/bin/pip" install -e "${INSTALL_DIR}"

# Install config/env if missing
if [ ! -f "${CONFIG_DIR}/config.yaml" ]; then
  cp "${INSTALL_DIR}/config.sample.yaml" "${CONFIG_DIR}/config.yaml"
  chown "${APP_USER}:${APP_GROUP}" "${CONFIG_DIR}/config.yaml"
fi

if [ ! -f "${CONFIG_DIR}/jarvis.env" ]; then
  cp "${INSTALL_DIR}/.env.example" "${CONFIG_DIR}/jarvis.env"
  chown "${APP_USER}:${APP_GROUP}" "${CONFIG_DIR}/jarvis.env"
fi

# Render systemd service with current paths
cat > "${SERVICE_PATH}" <<SERVICE
[Unit]
Description=Jarvis Telegram Assistant
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_GROUP}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=-${CONFIG_DIR}/jarvis.env
ExecStart=${INSTALL_DIR}/.venv/bin/python -m jarvis --config ${CONFIG_DIR}/config.yaml
Restart=on-failure
RestartSec=5

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=${LOG_DIR} ${DATA_DIR} ${CONFIG_DIR}

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable jarvis
systemctl restart jarvis

echo "Jarvis installed and started. Edit ${CONFIG_DIR}/config.yaml and ${CONFIG_DIR}/jarvis.env as needed."
