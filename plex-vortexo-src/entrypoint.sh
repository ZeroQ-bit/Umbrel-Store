#!/bin/sh
set -eu

role="${VORTEXO_ROLE:-gateway}"

case "${role}" in
  gateway)
    mkdir -p /data/vortexo /tmp/nginx/client /tmp/nginx/proxy
    chown -R vortexo:vortexo /data/vortexo /tmp/nginx
    su-exec vortexo python3 -m vortexo.service &
    api_pid="$!"
    trap 'kill "${api_pid}" 2>/dev/null || true; wait "${api_pid}" 2>/dev/null || true' EXIT INT TERM
    su-exec vortexo nginx -g "daemon off;"
    ;;
  mount)
    exec python3 -m vortexo.mount
    ;;
  *)
    echo "Unsupported VORTEXO_ROLE: ${role}" >&2
    exit 64
    ;;
esac
