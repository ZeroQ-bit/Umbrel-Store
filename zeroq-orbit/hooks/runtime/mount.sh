#!/bin/sh
# Debrid Mount entrypoint.
# The web UI (web_ui.py) owns :8080 and manages the rclone mount as a child.
# All config generation, mounting, and validation logic lives in web_ui.py.
set -eu

CONFIG_DIR="${DEBRID_CONFIG_DIR:-/config}"
STATUS_DIR="${DEBRID_STATUS_DIR:-/status}"
MOUNTPOINT="${DEBRID_MOUNTPOINT:-/mnt/debrid}"
HOST_MOUNT_PATH="${DEBRID_HOST_MOUNT_PATH:-}"
SAFETY_MARKER="${STATUS_DIR}/host-storage-safe"

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

mount_parent="$(dirname "${MOUNTPOINT}")"
host_mount_parent="$(dirname "${HOST_MOUNT_PATH}")"
if [ "${mount_parent}" != "/downloads" ] || [ -z "${HOST_MOUNT_PATH}" ]; then
  echo "[entrypoint] refusing unexpected mount path ${MOUNTPOINT}" >&2
  exit 1
fi
propagation="$(findmnt -T "${mount_parent}" -o PROPAGATION -n 2>/dev/null || true)"
case "${propagation}" in
  shared|rshared) ;;
  *)
    echo "[entrypoint] refusing non-shared media root ${mount_parent}" >&2
    exit 1
    ;;
esac
mount_fs="$(findmnt -n -o FSTYPE -M "${MOUNTPOINT}" 2>/dev/null || true)"
if [ -z "${mount_fs}" ] \
    && find "${MOUNTPOINT}" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
  echo "[entrypoint] refusing to hide local files in ${MOUNTPOINT}" >&2
  exit 1
fi
umask 077
printf '%s\n' "${host_mount_parent}" > "${SAFETY_MARKER}.tmp"
mv "${SAFETY_MARKER}.tmp" "${SAFETY_MARKER}"
echo "[entrypoint] verified shared host storage safety"

echo "[entrypoint] starting Debrid Mount web UI on :8080"
exec python3 /usr/local/bin/web_ui.py
