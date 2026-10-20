#!/usr/bin/env bash
# 从备份恢复数据卷。
#
#   sudo bash deploy/restore.sh /opt/observatory/backups/observatory-data-YYYYmmdd-HHMMSS.tar.gz
#
# 会先停容器，恢复卷，再启动。恢复前请确认文件名——这个操作会覆盖当前数据。

set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/observatory}"
ARCHIVE="${1:-}"

log()  { printf '\033[36m[restore]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[restore] %s\033[0m\n' "$*" >&2; exit 1; }

[[ -n "$ARCHIVE" ]] || die "用法：deploy/restore.sh <数据卷备份.tar.gz>"
[[ -f "$ARCHIVE" ]] || die "找不到备份文件：$ARCHIVE"

read -r -p "这会覆盖 observatory-data 卷，输入 RESTORE 继续： " confirm
[[ "$confirm" == "RESTORE" ]] || die "已取消"

cd "$APP_DIR"
log "停止容器"
docker compose --env-file server/.env down

log "恢复数据卷"
docker run --rm \
  -v observatory-data:/data \
  -v "$(cd "$(dirname "$ARCHIVE")" && pwd)":/backup:ro \
  alpine:3 \
  sh -c "rm -rf /data/* && tar xzf /backup/$(basename "$ARCHIVE") -C /data"

log "启动容器"
docker compose --env-file server/.env up -d

log "等待就绪"
for _ in $(seq 1 60); do
  if curl -fsSk https://127.0.0.1:8443/api/v1/health >/dev/null 2>&1; then
    log "恢复完成"
    exit 0
  fi
  sleep 2
done
die "服务未能在 120 秒内就绪"