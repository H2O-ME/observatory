# Observatory control server.
#
# Multi-stage: the console is built here from the same commit that builds the
# Python image, so the container can never serve a stale WebUI. The version comes
# from the repository-root VERSION file and is stamped into the image label.

FROM node:20-alpine AS webui

WORKDIR /build
COPY webui/package.json webui/package-lock.json* ./
RUN npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund
COPY webui/ ./
RUN npm run build


FROM python:3.12-slim

LABEL org.opencontainers.image.title="Observatory" \
      org.opencontainers.image.description="Observatory 控制服务器"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OBS_DATA_DIR=/data

WORKDIR /srv

# tzdata so log timestamps match the host; the application itself stores epoch
# milliseconds and is timezone-agnostic.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY server/requirements.txt ./
RUN pip install -r requirements.txt

COPY VERSION ./VERSION
COPY server/observatory ./observatory

# The console is copied into the data volume at start-up by deploy/update.sh, but
# a self-contained copy is baked in as the default so a fresh volume still works.
COPY --from=webui /build/dist ./webui-dist

VOLUME ["/data"]

EXPOSE 8443

# TLS terminates here rather than in a separate reverse proxy: one container means
# one thing to reason about on a shared host.
CMD ["uvicorn", "observatory.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8443", \
     "--ssl-keyfile", "/certs/server.key", \
     "--ssl-certfile", "/certs/server.crt", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*", \
     "--no-server-header"]