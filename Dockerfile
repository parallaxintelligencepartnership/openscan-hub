# OpenScanHub container image (linux/amd64 and linux/arm64).
# Build: docker build -t openscan-hub .
FROM python:3.13-slim

ARG VERSION=0.3.0

LABEL org.opencontainers.image.title="OpenScanHub" \
      org.opencontainers.image.description="Network scanner bridge: scans land in a folder, a NAS share or Paperless-NGX" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.vendor="Parallax Intelligence Partnership, LLC" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/parallaxintelligencepartnership/openscan-hub" \
      org.opencontainers.image.documentation="https://github.com/parallaxintelligencepartnership/openscan-hub#readme"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OPENSCAN_DOCKER=1 \
    OPENSCAN_CONFIG_DIR=/config \
    OPENSCAN_BIND=0.0.0.0 \
    OPENSCAN_PORT=8020

WORKDIR /app

# Pinned, hash-checked dependencies (pure wheels exist for amd64 and arm64).
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock

COPY LICENSE THIRD_PARTY_NOTICES.md ./
COPY openscan/ openscan/

# The `openscan` command (for example `docker exec openscan-hub openscan
# --show-token`). A wrapper rather than `pip install .`, which would fetch an
# unpinned setuptools at build time. `python -m openscan` keeps working too.
RUN printf '#!/bin/sh\nPYTHONPATH="/app${PYTHONPATH:+:$PYTHONPATH}" exec python -m openscan "$@"\n' \
        > /usr/local/bin/openscan \
    && chmod 755 /usr/local/bin/openscan

# Run as an ordinary user. UID 1000 matches the first user on most Linux
# hosts and NAS boxes, so files in ./scans belong to you, not root. A
# bind-mounted /config must be writable by 1000:1000 (chown -R 1000:1000 it).
RUN useradd --system --uid 1000 --user-group --home-dir /app --shell /usr/sbin/nologin openscan \
    && mkdir -p /config /scans \
    && chown openscan:openscan /config /scans

USER openscan

VOLUME ["/config", "/scans"]

# Web UI. The FTP receiver (2121 plus passive ports 60000-60010) is off by
# default; publish those ports only if you turn it on.
EXPOSE 8020

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('OPENSCAN_PORT', '8020'), timeout=4)" || exit 1

CMD ["python", "-m", "openscan", "--no-browser"]
