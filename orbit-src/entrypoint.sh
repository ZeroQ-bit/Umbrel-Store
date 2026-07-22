#!/bin/sh
set -eu

mkdir -p "${ORBIT_DATA_DIR:-/data}" "${PD_CONFIG_DIR:-/config}" "${PD_LOG_DIR:-/logs}"

if [ "${ORBIT_ROLE:-server}" = "automation" ]; then
  exec python3 "${PD_ROOT:-/app/plex_debrid}/main.py" \
    --config-dir "${PD_CONFIG_DIR:-/config}" -service
fi

exec python3 -m orbit
