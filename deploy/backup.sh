#!/usr/bin/env bash
# 备份数据卷（数据库、画面、安装包）与证书。
#
#   sudo bash deploy/backup.sh [目标目录]
#
# 备份是冷备份：先在容器内做 SQLite 的在线备份，再打包整个卷。

set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/observatory}"
DEST="${1:-$APP_DIR/backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"

log() { printf '\033[36m[backup]\033[0m %s\n' "$*"; }

mkdir -p "$DEST"

log "数据库在线备份"
docker exec observatory-api python - <<'PY'
import sqlite3, pathlib, shutil, time
src = pathlib.Path("/data/observatory.db")
dst = pathlib.Path(f"/data/observatory-backup-{int(time.time())}.db")
if src.exists():
    with sqlite3.connect(src) as source, sqlite3.connect(dst) as target:
        source.backup(target)
        print(f"wrote {dst}")
else:
    print("no database yet")
PY

log "打包数据卷"
docker run --rm \
  -v observatory-data:/data:ro \
  -v "$DEST":/backup \
  alpine:3 \
  tar czf "/backup/observatory-data-$STAMP.tar.gz" -C /data .

log "打包证书"
if [[ -d /etc/letsencrypt ]]; then
  tar czf "$DEST/observatory-certs-$STAMP.tar.gz" -C /etc letsencrypt
fi

log "清理 7 天前的备份"
find "$DEST" -maxdepth 1 -name 'observatory-*.tar.gz' -mtime +7 -delete || true

ls -lh "$DEST" | tail -n 5
log "完成：$DEST"