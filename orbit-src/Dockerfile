FROM ghcr.io/zeroq-bit/plex_debrid:main@sha256:2f30c4b3842fa49dedf729820e9ee4960871af4ba3b9a6eef202b0152ab1173c

COPY orbit /app/orbit
COPY entrypoint.sh /app/orbit-entrypoint.sh

ENV PYTHONPATH=/app \
    ORBIT_DATA_DIR=/data \
    ORBIT_PORT=8080 \
    PD_ROOT=/app/plex_debrid \
    PD_CONFIG_DIR=/config \
    PD_LOG_DIR=/logs \
    ORBIT_ACQUIRE_COMMAND="python3 -m orbit.acquire_legacy"

EXPOSE 8080
ENTRYPOINT ["/bin/sh", "/app/orbit-entrypoint.sh"]
