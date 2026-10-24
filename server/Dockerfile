# Observatory 控制服务器（0.3.0）
#
# 多阶段：控制台由同一 commit 在镜像里构建，容器不会 serve 过期的 WebUI。
# 版本来自仓库根 VERSION，并写入镜像 label（构建期通过 BUILD_VERSION 注入）。
#
# 0.3.0 容器卫生（A24）：
#   * 非 root 用户运行（OBS_UID，默认 10001）；
#   * 根文件系统只读（由 compose 的 read_only 保证），只有 /data 与 /tmp 可写；
#   * /certs 只读挂载，证书文件属主为容器 UID（setup.sh 负责 chown）。

FROM node:20-alpine AS webui

WORKDIR /build
COPY webui/package.json webui/package-lock.json* ./
RUN npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund
COPY webui/ ./
# F44: dist 版本戳——启动时数据卷副本与镜像内副本比对，不一致则以镜像为准覆盖
RUN npm run build && printf '%s' "$BUILD_VERSION" > dist/.version


FROM python:3.12-slim

ARG BUILD_VERSION=dev
ARG OBS_UID=10001

LABEL org.opencontainers.image.title="Observatory" \
      org.opencontainers.image.description="Observatory 控制服务器" \
      org.opencontainers.image.version="${BUILD_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OBS_DATA_DIR=/data \
    HOME=/tmp

# tzdata so log timestamps match the host; the application itself stores epoch
# milliseconds and is timezone-agnostic.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# 非 root 运行用户；/data 的所有权会被复制到命名卷初始内容里。
RUN groupadd --gid "${OBS_UID}" observatory \
    && useradd --uid "${OBS_UID}" --gid observatory --create-home --shell /usr/sbin/nologin observatory \
    && mkdir -p /srv /data \
    && chown -R observatory:observatory /srv /data

WORKDIR /srv

COPY --chown=observatory:observatory server/requirements.txt ./
RUN pip install -r requirements.txt

COPY --chown=observatory:observatory VERSION ./VERSION
COPY --chown=observatory:observatory server/observatory ./observatory

# The console is copied into the data volume at start-up by main.py, but a
# self-contained copy is baked in as the default so a fresh volume still works.
COPY --chown=observatory:observatory --from=webui /build/dist ./webui-dist

VOLUME ["/data"]

EXPOSE 8443

# TLS terminates here rather than in a separate reverse proxy: one container means
# one thing to reason about on a shared host.
USER observatory

# A11：只信任本机 nginx / Docker 网桥的转发头。若改为 CDN 直连 8443，
# 需要把 OBS_FORWARDED_ALLOW_IPS 换成 CDN 回源网段，并同时显式打开
# OBS_TRUST_FORWARDED_FOR。
# 0.3.1（F45/F16）：转发信任网段可经 .env 覆盖（默认收窄场景写 172.18.0.1）；
# --timeout-graceful-shutdown 15 与 compose 的 stop_grace_period: 30s 配合，
# 让 20s 长轮询在途连接先收到正常断连而不是 SIGKILL。
CMD ["sh", "-c", "uvicorn observatory.main:app --host 0.0.0.0 --port 8443 --ssl-keyfile /certs/server.key --ssl-certfile /certs/server.crt --proxy-headers --forwarded-allow-ips \"${OBS_FORWARDED_ALLOW_IPS:-127.0.0.1,172.16.0.0/12}\" --timeout-graceful-shutdown 15 --no-server-header"]