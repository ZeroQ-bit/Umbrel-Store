#!/bin/sh
# Debrid Mount entrypoint.
# The web UI (web_ui.py) owns :8080 and manages the rclone mount as a child.
# All config generation, mounting, and validation logic lives in web_ui.py.
set -eu

CONFIG_DIR="${DEBRID_CONFIG_DIR:-/config}"
STATUS_DIR="${DEBRID_STATUS_DIR:-/status}"
MOUNTPOINT="${DEBRID_MOUNTPOINT:-/mnt/debrid}"

mkdir -p "${CONFIG_DIR}" "${STATUS_DIR}" "${MOUNTPOINT}" "${CONFIG_DIR}/rclone-cache"

echo "[entrypoint] starting Debrid Mount web UI on :8080"
exec python3 /usr/local/bin/web_ui.py
