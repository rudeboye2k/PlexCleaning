# PlexCleaner — multi-arch image for x86_64 and ARM64 NAS hardware.
FROM python:3.11-slim

LABEL org.opencontainers.image.title="PlexCleaner" \
      org.opencontainers.image.description="Cross-reference Plex, Tautulli, Sonarr, Radarr and Seerr to remove stale media and inactive users" \
      org.opencontainers.image.source="https://github.com/rudeboye2k/PlexCleaning" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLEXCLEANER_DATA_DIR=/data \
    PUID=1000 \
    PGID=1000 \
    UMASK=022

# gosu drops privileges in the entrypoint; the rest are for usermod/groupmod
# and the health check.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu passwd \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY pyproject.toml README.md ./
COPY plexcleaner ./plexcleaner
RUN pip install --no-deps .

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && useradd --create-home --uid 1000 --shell /bin/sh plexcleaner \
    && mkdir -p /data /config \
    && chown -R plexcleaner:plexcleaner /data /config

VOLUME ["/data"]
EXPOSE 8585

# /healthz is exempt from the network guard so this works from inside.
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8585/healthz', timeout=8).status==200 else 1)"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8585"]
