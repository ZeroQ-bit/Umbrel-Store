export APP_ZEROQ_RIVEN_TS_URL="http://zeroq-riven-ts_server_1:8080"
export APP_ZEROQ_RIVEN_TS_DATA_DIR="${EXPORTS_APP_DIR}/data/riven"
export APP_ZEROQ_RIVEN_TS_MOUNT_DIR="${UMBREL_ROOT}/data/storage/downloads/riven-ts"
export APP_ZEROQ_RIVEN_TS_PORT="8097"

primary_local_ipv4="$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ { for (i = 1; i <= NF; i++) if ($i == "src") { print $(i+1); exit } }')"

if [[ -z "${primary_local_ipv4}" ]]; then
  local_ipv4s="$(hostname --all-ip-addresses 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+$' || true)"
  primary_local_ipv4="$(printf '%s\n' "${local_ipv4s}" | grep '^192\\.168\\.' | head -n 1)"
fi

if [[ -z "${primary_local_ipv4}" && -n "${local_ipv4s:-}" ]]; then
  primary_local_ipv4="$(printf '%s\n' "${local_ipv4s}" | grep '^10\\.' | head -n 1)"
fi

if [[ -z "${primary_local_ipv4}" && -n "${local_ipv4s:-}" ]]; then
  primary_local_ipv4="$(printf '%s\n' "${local_ipv4s}" | head -n 1)"
fi

if [[ -n "${primary_local_ipv4}" ]]; then
  export APP_ZEROQ_RIVEN_TS_LOCAL_URL="http://${primary_local_ipv4}:${APP_ZEROQ_RIVEN_TS_PORT}"
else
  export APP_ZEROQ_RIVEN_TS_LOCAL_URL="http://${DEVICE_DOMAIN_NAME}:${APP_ZEROQ_RIVEN_TS_PORT}"
fi
