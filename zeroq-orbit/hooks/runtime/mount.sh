#!/bin/sh
# Debrid Mount entrypoint.
# The web UI (web_ui.py) owns :8080 and manages the rclone mount as a child.
# All config generation, mounting, and validation logic lives in web_ui.py.
set -eu

CONFIG_DIR="${DEBRID_CONFIG_DIR:-/config}"
STATUS_DIR="${DEBRID_STATUS_DIR:-/status}"
MOUNTPOINT="${DEBRID_MOUNTPOINT:-/mnt/debrid}"

mount_error="$(LC_ALL=C stat "${MOUNTPOINT}" 2>&1 >/dev/null || true)"
case "${mount_error}" in
  *"Transport endpoint is not connected"*|*"Socket not connected"*)
    echo "[entrypoint] detaching stale FUSE socket at ${MOUNTPOINT}"
    fusermount3 -uz "${MOUNTPOINT}" 2>/dev/null \
      || fusermount -uz "${MOUNTPOINT}" 2>/dev/null \
      || umount -l "${MOUNTPOINT}" 2>/dev/null \
      || true
    ;;
esac

mkdir -p "${CONFIG_DIR}" "${STATUS_DIR}" "${MOUNTPOINT}"

echo "[entrypoint] starting Debrid Mount web UI on :8080"
exec python3 /usr/local/bin/web_ui.py
