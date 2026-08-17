FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLEXCLEANER_CONFIG=/config/config.yaml

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY plexcleaner ./plexcleaner
RUN pip install --no-cache-dir --no-deps -e .

# Run as a non-root user; /config and /data are bind-mounted at runtime.
RUN useradd --create-home --uid 1000 plexcleaner \
    && mkdir -p /config /data \
    && chown -R plexcleaner:plexcleaner /app /config /data
USER plexcleaner

EXPOSE 8585

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8585/healthz', timeout=4).status==200 else 1)"

ENTRYPOINT ["plexcleaner"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8585"]
