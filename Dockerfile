# syntax=docker/dockerfile:1
FROM python:3.12-alpine AS build
WORKDIR /w
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt \
 && find /install -name '__pycache__' -type d -prune -exec rm -rf {} + \
 && find /install -name 'tests' -type d -prune -exec rm -rf {} +

FROM python:3.12-alpine
ARG BRIDGE_VERSION=dev
LABEL org.opencontainers.image.source="https://github.com/Rilnaista/huawei2mqtt" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.title="huawei2mqtt" \
      org.opencontainers.image.description="Huawei SUN2000 (Modbus TCP) to MQTT bridge with web portal" \
      org.opencontainers.image.version="${BRIDGE_VERSION}"
ENV DATA_DIR=/data WEB_DIR=/app/web BRIDGE_VERSION=${BRIDGE_VERSION} \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN addgroup -g 1000 app && adduser -D -u 1000 -G app -H app \
 && mkdir /data && chown 1000:1000 /data
COPY --from=build /install /usr/local
WORKDIR /app
COPY bridge/ /app/bridge/
COPY web/index.html /app/web/index.html
COPY web/static/ /app/web/static/
COPY web/i18n/ /app/web/i18n/
COPY web/manifest.webmanifest web/sw.js /app/web/
USER 1000:1000
VOLUME /data
EXPOSE 8080
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORTAL_LISTEN_PORT','8080'), timeout=4).status==200 else 1)"]
CMD ["python", "-m", "bridge"]
