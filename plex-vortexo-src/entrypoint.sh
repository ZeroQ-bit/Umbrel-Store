#!/bin/sh
set -eu

role="${VORTEXO_ROLE:-gateway}"

case "${role}" in
  gateway)
    mkdir -p \
      /data/vortexo \
      /tmp/nginx/client \
      /tmp/nginx/fastcgi \
      /tmp/nginx/proxy \
      /tmp/nginx/scgi \
      /tmp/nginx/uwsgi
    touch /tmp/nginx/error.log
    chown -R vortexo:vortexo /data/vortexo /tmp/nginx
    su-exec vortexo tail -n 0 -f /tmp/nginx/error.log >&2 &
    nginx_log_pid="$!"
    su-exec vortexo python3 -m vortexo.service &
    api_pid="$!"
    cleanup() {
      kill "${api_pid}" "${nginx_log_pid}" 2>/dev/null || true
      wait "${api_pid}" "${nginx_log_pid}" 2>/dev/null || true
    }
    trap cleanup EXIT INT TERM
    su-exec vortexo nginx -e /tmp/nginx/error.log -g "daemon off;"
    ;;
  mount)
    exec python3 -m vortexo.mount
    ;;
  *)
    echo "Unsupported VORTEXO_ROLE: ${role}" >&2
    exit 64
    ;;
esac
