#!/usr/bin/env bash
# 从备份恢复数据卷（Observatory 0.3.0）。
#
#   sudo bash deploy/restore.sh <observatory-data-YYYYmmdd-HHMMSS.tar.gz> \
#        [observatory-db-YYYYmmdd-HHMMSS.db] [--yes]
#
# 默认行为：
#   1. 先做一次“恢复前”备份（容器还在运行时），避免恢复错了无法回头；
#   2. 把归档解到临时目录并校验（至少存在 observatory.db；有 python3 时做
#      PRAGMA integrity_check）；
#   3. 停容器 → 清空 observatory-data 卷 → 写入归档（含隐藏文件）→
#      chown 到容器用户（OBS_UID，默认 10001，非 root 容器必需）；
#   4. 启动容器并等待健康检查，失败退出非 0。
#
# 证书不在此脚本内恢复：证书归档请手工展开到 /etc/letsencrypt，
# 然后运行 deploy/setup.sh（会重新实体拷贝）或 deploy/renew-certs.sh。

set -Eeuo pipefail
umask 077

APP_DIR="${APP_DIR:-/opt/observatory}"
OBS_UID="${OBS_UID:-10001}"
ARCHIVE=""
DB_SNAPSHOT=""
ASSUME_YES=0

log()  { printf '\033[36m[restore]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[restore]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[restore] %s\033[0m\n' "$*" >&2; exit 1; }

compose() {
  docker compose -f "$APP_DIR/deploy/docker-compose.yml" --env-file "$APP_DIR/server/.env" "$@"
}

wait_health() {
  local tries="${1:-60}" i
  for ((i = 1; i <= tries; i++)); do
    if curl -fsSk "https://127.0.0.1:8443/api/v1/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

usage() {
  die "用法：deploy/restore.sh <数据卷备份.tar.gz> [db 快照.db] [--yes]"
}

for arg in "$@"; do
  case "$arg" in
    --yes) ASSUME_YES=1 ;;
    --*) usage ;;
    *)
      if [[ -z "$ARCHIVE" ]]; then
        ARCHIVE="$arg"
      elif [[ -z "$DB_SNAPSHOT" ]]; then
        DB_SNAPSHOT="$arg"
      else
        usage
      fi
      ;;
  esac
done

[[ -n "$ARCHIVE" ]] || usage
[[ -f "$ARCHIVE" ]] || die "找不到备份文件：$ARCHIVE"
ARCHIVE="$(readlink -f "$ARCHIVE")"
if [[ -n "$DB_SNAPSHOT" ]]; then
  [[ -f "$DB_SNAPSHOT" ]] || die "找不到数据库快照：$DB_SNAPSHOT"
  DB_SNAPSHOT="$(readlink -f "$DB_SNAPSHOT")"
fi
[[ -d "$APP_DIR/deploy" ]] || die "APP_DIR 不正确：$APP_DIR"

if [[ "$ASSUME_YES" -ne 1 ]]; then
  warn "这会覆盖 observatory-data 卷（当前数据会被替换）。"
  read -r -p "输入 RESTORE 继续： " confirm
  [[ "$confirm" == "RESTORE" ]] || die "已取消"
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
RESTORE_DIR="$APP_DIR/backups/restore-$STAMP"
mkdir -p "$RESTORE_DIR"
chmod 700 "$APP_DIR/backups" "$RESTORE_DIR" 2>/dev/null || true

# 1) 恢复前备份（best effort；失败只告警，不阻止人工恢复）
if docker inspect --format '{{.State.Running}}' observatory-api 2>/dev/null | grep -qx true; then
  log "恢复前自动备份当前数据（失败不阻止恢复，但请留意）"
  if ! bash "$APP_DIR/deploy/backup.sh" "$APP_DIR/backups/pre-restore-$STAMP"; then
    warn "恢复前备份失败：继续恢复将无法回到当前状态"
  fi
else
  warn "容器未运行，跳过恢复前备份"
fi

# 2) 解包到临时目录并校验
log "解包并校验备份：$ARCHIVE"
tar xzf "$ARCHIVE" -C "$RESTORE_DIR" || die "归档解包失败（文件可能损坏）：$ARCHIVE"
[[ -f "$RESTORE_DIR/observatory.db" ]] || die "归档里没有 observatory.db，拒绝恢复"
if [[ -n "$DB_SNAPSHOT" ]]; then
  log "使用一致性数据库快照覆盖归档中的 observatory.db：$DB_SNAPSHOT"
  install -m 0644 "$DB_SNAPSHOT" "$RESTORE_DIR/observatory.db"
fi
if command -v python3 >/dev/null 2>&1; then
  python3 - "$RESTORE_DIR/observatory.db" <<'PY' || die "数据库完整性校验失败，拒绝覆盖当前数据"
import sqlite3
import sys

path = sys.argv[1]
with sqlite3.connect(path) as conn:
    result = conn.execute("PRAGMA integrity_check").fetchone()
print("integrity_check:", result[0] if result else "unknown")
raise SystemExit(0 if result and result[0] == "ok" else 1)
PY
else
  warn "本机没有 python3，跳过 sqlite integrity_check"
fi

# 3) 停容器 → 清空卷 → 写入归档 → 修正属主
log "停止容器"
compose down || die "容器停止失败"

log "恢复数据卷（含隐藏文件；属主改为容器用户 $OBS_UID）"
docker run --rm \
  -e RESTORE_UID="$OBS_UID" \
  -v observatory-data:/data \
  -v "$RESTORE_DIR":/restore:ro \
  alpine:3 \
  sh -c 'find /data -mindepth 1 -maxdepth 1 -exec rm -rf {} + && cp -a /restore/. /data/ && chown -R "$RESTORE_UID:$RESTORE_UID" /data' \
  || die "写入数据卷失败；数据卷可能处于部分写入状态，请用恢复前备份重试"

# 4) 启动并等待健康
log "启动容器"
compose up -d --no-build || die "容器启动失败"
if ! wait_health 60; then
  compose logs --tail=80 || true
  die "恢复后 120 秒内未通过健康检查；恢复前备份在 $APP_DIR/backups/pre-restore-$STAMP"
fi
log "恢复完成，服务健康"

rm -rf "$RESTORE_DIR"
log "如需恢复证书：tar xzf <certs.tar.gz> -C /etc && sudo bash deploy/setup.sh"
exit 0