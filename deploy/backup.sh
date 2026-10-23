#!/usr/bin/env bash
# 备份数据卷（数据库、画面、安装包）、证书与数据库一致性快照。
#
#   sudo bash deploy/backup.sh [目标目录]
#
# 默认目标：$APP_DIR/backups
#
# 0.3.0 修订：
#   * umask 077：备份目录与归档（尤其证书私钥）不再对本机其他用户可读；
#   * 在线 SQLite 备份改为 `docker exec -i`（旧版少了 -i，python 读到空 stdin，
#     所谓“在线备份”实际什么都没做）；
#   * 数据库快照输出到宿主机 $DEST/observatory-db-<stamp>.db，与数据卷 tar 分离，
#     restore.sh 可据此恢复一致性数据库；
#   * 生成 manifest（大小 + sha256），便于校验与异地保存。

set -Eeuo pipefail
umask 077

APP_DIR="${APP_DIR:-/opt/observatory}"
DEST="${1:-$APP_DIR/backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DB_FILE=""

log()  { printf '\033[36m[backup]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[backup]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[backup] %s\033[0m\n' "$*" >&2; exit 1; }

if [[ "${EUID}" -ne 0 ]]; then
  die "请用 root 运行：sudo bash deploy/backup.sh"
fi

mkdir -p "$DEST"
chmod 700 "$DEST" 2>/dev/null || true

running="$(docker inspect --format '{{.State.Running}}' observatory-api 2>/dev/null || echo false)"

if [[ "$running" == "true" ]]; then
  log "数据库在线备份（SQLite backup API，容器内）"
  if docker exec -i observatory-api python - <<'PY'
import pathlib
import sqlite3

src = pathlib.Path("/data/observatory.db")
dst = pathlib.Path("/data/.observatory-online.db")
if not src.exists():
    print("no database yet")
    raise SystemExit(0)
dst.unlink(missing_ok=True)
with sqlite3.connect(src) as source, sqlite3.connect(dst) as target:
    source.backup(target)
print(f"wrote {dst}")
PY
  then
    if docker cp observatory-api:/data/.observatory-online.db "$DEST/observatory-db-$STAMP.db" >/dev/null 2>&1; then
      DB_FILE="$DEST/observatory-db-$STAMP.db"
      chmod 600 "$DB_FILE"
      log "数据库快照：$DB_FILE"
    else
      warn "容器内没有数据库快照（首次部署可能尚无数据库）"
    fi
    docker exec observatory-api rm -f /data/.observatory-online.db >/dev/null 2>&1 || true
  else
    die "数据库在线备份失败（容器内 python/sqlite3 异常）"
  fi
else
  warn "容器 observatory-api 未运行：跳过在线 DB 备份（停止状态下数据卷 tar 是一致的）"
fi

log "打包数据卷 → $DEST/observatory-data-$STAMP.tar.gz"
docker run --rm \
  -v observatory-data:/data:ro \
  -v "$DEST":/backup \
  alpine:3 \
  tar czf "/backup/observatory-data-$STAMP.tar.gz" -C /data .
chmod 600 "$DEST/observatory-data-$STAMP.tar.gz"

log "打包证书（含私钥，归档权限 600）"
if [[ -d /etc/letsencrypt ]]; then
  tar czf "$DEST/observatory-certs-$STAMP.tar.gz" -C /etc letsencrypt
  chmod 600 "$DEST/observatory-certs-$STAMP.tar.gz"
else
  warn "/etc/letsencrypt 不存在，跳过证书备份"
fi

log "生成 manifest"
{
  echo "# Observatory backup $STAMP"
  echo "created_at=$(date -Is)"
  echo "host=$(hostname)"
  echo "db_snapshot=$DB_FILE"
} > "$DEST/manifest-$STAMP.txt"
for artifact in "$DEST/observatory-data-$STAMP.tar.gz" "$DEST/observatory-certs-$STAMP.tar.gz" "$DB_FILE"; do
  [[ -n "$artifact" && -f "$artifact" ]] || continue
  sha256sum "$artifact" >> "$DEST/manifest-$STAMP.txt"
done

log "清理 7 天前的备份"
find "$DEST" -maxdepth 1 \
  \( -name 'observatory-*.tar.gz' -o -name 'observatory-db-*.db' -o -name 'manifest-*.txt' \) \
  -mtime +7 -delete 2>/dev/null || true

echo
ls -lh "$DEST" | tail -n 8
log "完成：$DEST"
[[ -n "$DB_FILE" ]] && log "一致性数据库快照：$DB_FILE"
exit 0