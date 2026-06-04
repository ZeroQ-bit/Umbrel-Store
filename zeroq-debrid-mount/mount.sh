#!/bin/sh
set -eu

CONFIG_DIR="${DEBRID_CONFIG_DIR:-/config}"
STATUS_DIR="${DEBRID_STATUS_DIR:-/status}"
MOUNTPOINT="${DEBRID_MOUNTPOINT:-/mnt/debrid}"
HOST_MOUNT_PATH="${DEBRID_HOST_MOUNT_PATH:-/home/Downloads/.vortexo-source}"
CONFIG_FILE="${CONFIG_DIR}/debrid.env"
RCLONE_CONFIG="${CONFIG_DIR}/rclone.conf"
ZURG_CONFIG="${CONFIG_DIR}/zurg.yml"

mkdir -p "${CONFIG_DIR}" "${STATUS_DIR}" "${CONFIG_DIR}/rclone-cache"

write_status() {
  title="$1"
  detail="$2"
  marker="${3:-}"

  rm -f "${STATUS_DIR}/config-needed" "${STATUS_DIR}/ready" "${STATUS_DIR}/mounting"
  if [ -n "${marker}" ]; then
    touch "${STATUS_DIR}/${marker}"
  fi

  cat > "${STATUS_DIR}/index.html" <<EOF
<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Debrid Mount</title>
    <style>
      body { margin: 0; min-height: 100vh; font: 16px/1.5 ui-sans-serif, system-ui, sans-serif; color: #eef2ff; background: radial-gradient(circle at top left, #164e63, #020617 52%, #111827); }
      main { max-width: 760px; margin: 0 auto; padding: 56px 22px; }
      section { border: 1px solid rgba(255,255,255,.16); border-radius: 24px; padding: 28px; background: rgba(15,23,42,.72); box-shadow: 0 24px 80px rgba(0,0,0,.32); }
      h1 { margin: 0 0 12px; font-size: clamp(32px, 7vw, 58px); line-height: .95; letter-spacing: -.05em; }
      p { color: #cbd5e1; }
      code { color: #bae6fd; background: rgba(14,165,233,.14); padding: 2px 6px; border-radius: 8px; }
      .status { display: inline-block; margin-bottom: 18px; padding: 6px 10px; border-radius: 999px; color: #d9f99d; background: rgba(132,204,22,.16); }
    </style>
  </head>
  <body>
    <main>
      <section>
        <div class="status">${title}</div>
        <h1>Stable debrid mount for Plex and Vortexo</h1>
        <p>${detail}</p>
        <p>Config file: <code>${CONFIG_FILE}</code></p>
        <p>Host mount: <code>${HOST_MOUNT_PATH}</code></p>
        <p>Vortexo and Plex path: <code>/downloads/.vortexo-source</code></p>
      </section>
    </main>
  </body>
</html>
EOF
}

write_sample_config() {
  if [ -f "${CONFIG_FILE}" ]; then
    return
  fi

  cat > "${CONFIG_FILE}" <<'EOF'
# Debrid Mount config
#
# Default mode is direct WebDAV, which works for TorBox:
DEBRID_MODE='webdav'
DEBRID_WEBDAV_URL='https://webdav.torbox.app'
DEBRID_WEBDAV_VENDOR='other'
DEBRID_WEBDAV_USER=''
DEBRID_WEBDAV_PASS=''
#
# Real-Debrid via zurg is also supported:
# DEBRID_MODE='zurg'
# DEBRID_ZURG_TOKEN=''
# DEBRID_ZURG_PORT='9999'
#
# Rclone tuning:
DEBRID_RCLONE_VFS_CACHE_MODE='full'
DEBRID_RCLONE_VFS_CACHE_MAX_SIZE='20G'
DEBRID_RCLONE_VFS_CACHE_MAX_AGE='6h'
DEBRID_RCLONE_DIR_CACHE_TIME='10s'
DEBRID_RCLONE_LOG_LEVEL='INFO'
EOF
  chmod 600 "${CONFIG_FILE}"
}

start_status_server() {
  rclone serve http "${STATUS_DIR}" --addr ":8080" --read-only --log-level ERROR > "${STATUS_DIR}/status-server.log" 2>&1 &
}

sleep_until_configured() {
  write_status "Needs config" "Edit the generated config file, add your TorBox WebDAV username/password or Real-Debrid zurg token, then restart this app." "config-needed"
  tail -f /dev/null
}

unmount_existing() {
  if grep -q " ${MOUNTPOINT} " /proc/self/mountinfo || ! ls -ld "${MOUNTPOINT}" >/dev/null 2>&1; then
    fusermount3 -uz "${MOUNTPOINT}" 2>/dev/null || fusermount -uz "${MOUNTPOINT}" 2>/dev/null || umount -l "${MOUNTPOINT}" 2>/dev/null || true
  fi
}

prepare_mountpoint() {
  unmount_existing
  if mkdir -p "${MOUNTPOINT}" 2>/tmp/debrid-mount-mkdir.err; then
    rm -f /tmp/debrid-mount-mkdir.err
    return 0
  fi

  if grep -qi 'socket not connected\|transport endpoint' /tmp/debrid-mount-mkdir.err 2>/dev/null; then
    fusermount3 -uz "${MOUNTPOINT}" 2>/dev/null || fusermount -uz "${MOUNTPOINT}" 2>/dev/null || umount -l "${MOUNTPOINT}" 2>/dev/null || true
    rm -rf "${MOUNTPOINT}" 2>/dev/null || true
    mkdir -p "${MOUNTPOINT}"
    rm -f /tmp/debrid-mount-mkdir.err
    return 0
  fi

  cat /tmp/debrid-mount-mkdir.err >&2
  rm -f /tmp/debrid-mount-mkdir.err
  return 1
}

enable_fuse_allow_other() {
  if [ -w /etc/fuse.conf ] && ! grep -q '^[[:space:]]*user_allow_other' /etc/fuse.conf; then
    echo user_allow_other >> /etc/fuse.conf
  fi
}

write_webdav_rclone_config() {
  if [ -z "${DEBRID_WEBDAV_USER:-}" ] || [ -z "${DEBRID_WEBDAV_PASS:-}" ]; then
    sleep_until_configured
  fi

  obscured_pass="$(rclone obscure "${DEBRID_WEBDAV_PASS}")"
  cat > "${RCLONE_CONFIG}" <<EOF
[debrid]
type = webdav
url = ${DEBRID_WEBDAV_URL:-https://webdav.torbox.app}
vendor = ${DEBRID_WEBDAV_VENDOR:-other}
user = ${DEBRID_WEBDAV_USER}
pass = ${obscured_pass}
EOF
  chmod 600 "${RCLONE_CONFIG}"
}

write_default_zurg_config() {
  if [ -f "${ZURG_CONFIG}" ]; then
    return
  fi
  if [ -z "${DEBRID_ZURG_TOKEN:-}" ]; then
    sleep_until_configured
  fi

  cat > "${ZURG_CONFIG}" <<EOF
zurg: v1
token: ${DEBRID_ZURG_TOKEN}
host: "0.0.0.0"
port: ${DEBRID_ZURG_PORT:-9999}
check_for_changes_every_secs: 10
enable_repair: true
auto_delete_rar_torrents: true
directories:
  shows:
    group_order: 20
    group: media
    filters:
      - has_episodes: true
  movies:
    group_order: 30
    group: media
    only_show_the_biggest_file: true
    filters:
      - regex: /.*/
EOF
  chmod 600 "${ZURG_CONFIG}"
}

wait_for_zurg() {
  port="${DEBRID_ZURG_PORT:-9999}"
  for _ in $(seq 1 60); do
    if wget -q -O /dev/null "http://127.0.0.1:${port}/dav/version.txt"; then
      return 0
    fi
    sleep 1
  done
  echo "zurg did not become ready on port ${port}" >&2
  return 1
}

write_zurg_rclone_config() {
  port="${DEBRID_ZURG_PORT:-9999}"
  cat > "${RCLONE_CONFIG}" <<EOF
[debrid]
type = webdav
url = http://127.0.0.1:${port}/dav
vendor = other
EOF
  chmod 600 "${RCLONE_CONFIG}"
}

write_sample_config
start_status_server

set -a
# shellcheck disable=SC1090
. "${CONFIG_FILE}"
set +a

DEBRID_MODE="$(printf '%s' "${DEBRID_MODE:-webdav}" | tr '[:upper:]' '[:lower:]')"
enable_fuse_allow_other
prepare_mountpoint
write_status "Mounting" "Starting ${DEBRID_MODE} debrid mount. Plex can keep using mounted media even when Vortexo Server is stopped." "mounting"

case "${DEBRID_MODE}" in
  webdav)
    write_webdav_rclone_config
    ;;
  zurg)
    write_default_zurg_config
    zurg -c "${ZURG_CONFIG}" > "${STATUS_DIR}/zurg.log" 2>&1 &
    wait_for_zurg
    write_zurg_rclone_config
    ;;
  *)
    echo "Unsupported DEBRID_MODE=${DEBRID_MODE}. Use webdav or zurg." >&2
    sleep_until_configured
    ;;
esac

write_status "Ready" "The debrid source is mounted. Point Vortexo at /downloads/.vortexo-source and keep Vortexo's own WebDAV mount toggle off." "ready"

exec rclone mount debrid: "${MOUNTPOINT}" \
  --config "${RCLONE_CONFIG}" \
  --allow-other \
  --allow-non-empty \
  --read-only \
  --dir-cache-time "${DEBRID_RCLONE_DIR_CACHE_TIME:-10s}" \
  --vfs-cache-mode "${DEBRID_RCLONE_VFS_CACHE_MODE:-full}" \
  --vfs-cache-max-size "${DEBRID_RCLONE_VFS_CACHE_MAX_SIZE:-20G}" \
  --vfs-cache-max-age "${DEBRID_RCLONE_VFS_CACHE_MAX_AGE:-6h}" \
  --cache-dir "${CONFIG_DIR}/rclone-cache" \
  --poll-interval "0" \
  --umask "002" \
  --uid "1000" \
  --gid "1000" \
  --log-level "${DEBRID_RCLONE_LOG_LEVEL:-INFO}"
