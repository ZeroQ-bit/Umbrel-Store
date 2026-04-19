export APP_ZEROQ_RIVEN_URL="http://zeroq-riven_server_1:8080"
export APP_ZEROQ_RIVEN_DATA_DIR="${EXPORTS_APP_DIR}/data/riven"
export APP_ZEROQ_RIVEN_MOUNT_DIR="${UMBREL_ROOT}/data/storage/downloads/riven"
export APP_ZEROQ_RIVEN_PORT="8080"

local_ipv4s="$(hostname --all-ip-addresses 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+$' || true)"
primary_local_ipv4="$(printf '%s\n' "${local_ipv4s}" | head -n 1)"

if [[ -n "${primary_local_ipv4}" ]]; then
  export APP_ZEROQ_RIVEN_LOCAL_URL="http://${primary_local_ipv4}:${APP_ZEROQ_RIVEN_PORT}"
else
  export APP_ZEROQ_RIVEN_LOCAL_URL="http://${DEVICE_DOMAIN_NAME}:${APP_ZEROQ_RIVEN_PORT}"
fi
