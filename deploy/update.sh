#!/usr/bin/env bash
# 服务器端更新（Observatory 0.3.0）—— 可回滚。
#
#   sudo bash deploy/update.sh
#
# 0.3.0 起这是“有状态”的升级：
#   1. 记录升级前状态：git commit、VERSION、运行中镜像 ID、.env 指纹；
#   2. 自动备份数据库 + 数据卷（默认调用 deploy/backup.sh，SKIP_BACKUP=1 可跳过）；
#   3. stash → git pull --ff-only → 脚本自身被更新时 re-exec 一次；
#   4. 构建并启动**带版本 tag** 的镜像（observatory/api:<VERSION>），同时维护 :latest；
#   5. 健康检查失败（或运行版本 ≠ VERSION）自动回退旧 commit + 旧镜像，并退出非 0；
#   6. 成功后只登记与 VERSION 一致的受控端安装包。
#
# 回退不会自动恢复数据库：迁移是加列/加表型的，旧镜像通常可直接运行新库；
# 如需恢复数据，用记录中给出的备份执行 deploy/restore.sh（人工确认，避免丢写入）。

set -Eeuo pipefail
IFS=$'\n\t'

APP_DIR="${APP_DIR:-/opt/observatory}"
BRANCH="${BRANCH:-main}"
STAMP="$(date +%Y%m%d-%H%M%S)"

OLD_COMMIT=""
OLD_VERSION=""
OLD_IMAGE_ID=""
ROLLBACK_DIR=""
NEW_VERSION=""
ROLLBACK_NEEDED=0
SUCCESS=0

log()  { printf '\033[36m[update]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[update]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[update] %s\033[0m\n' "$*" >&2; exit 1; }

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

running_version() {
  curl -fsSk "https://127.0.0.1:8443/api/v1/version" 2>/dev/null \
    | sed -n 's/.*"version":"\([^"]*\)".*/\1/p' || true
}

record_upgrade_state() {
  # re-exec 时保留第一次进入脚本时的旧状态（否则会把新 commit 当成“旧版本”）。
  if [[ -n "${OBS_UPDATE_ROLLBACK_COMMIT:-}" ]]; then
    OLD_COMMIT="$OBS_UPDATE_ROLLBACK_COMMIT"
    OLD_VERSION="$OBS_UPDATE_ROLLBACK_VERSION"
    OLD_IMAGE_ID="$OBS_UPDATE_ROLLBACK_IMAGE"
    ROLLBACK_DIR="$OBS_UPDATE_ROLLBACK_DIR"
    log "沿用本次升级已记录的旧状态：commit=$OLD_COMMIT version=$OLD_VERSION"
    return
  fi

  OLD_COMMIT="$(git -C "$APP_DIR" rev-parse HEAD)"
  OLD_VERSION="$(tr -d '[:space:]' < "$APP_DIR/VERSION")"
  OLD_IMAGE_ID="$(docker inspect --format '{{.Image}}' observatory-api 2>/dev/null || true)"
  [[ -n "$OLD_IMAGE_ID" ]] || die "容器 observatory-api 未运行；请先执行 sudo bash deploy/setup.sh"
  ROLLBACK_DIR="$APP_DIR/backups/upgrade-$STAMP"
  mkdir -p "$ROLLBACK_DIR"
  chmod 700 "$APP_DIR/backups" "$ROLLBACK_DIR" 2>/dev/null || true
  {
    echo "old_commit=$OLD_COMMIT"
    echo "old_version=$OLD_VERSION"
    echo "old_image_id=$OLD_IMAGE_ID"
    echo "started_at=$(date -Is)"
    echo "host=$(hostname)"
  } > "$ROLLBACK_DIR/upgrade.env"
  if [[ -f "$APP_DIR/server/.env" ]]; then
    sha256sum "$APP_DIR/server/.env" > "$ROLLBACK_DIR/env.sha256"
  fi
  export OBS_UPDATE_ROLLBACK_COMMIT="$OLD_COMMIT"
  export OBS_UPDATE_ROLLBACK_VERSION="$OLD_VERSION"
  export OBS_UPDATE_ROLLBACK_IMAGE="$OLD_IMAGE_ID"
  export OBS_UPDATE_ROLLBACK_DIR="$ROLLBACK_DIR"
  log "旧状态：commit=$OLD_COMMIT version=$OLD_VERSION image=${OLD_IMAGE_ID:0:12} 记录=$ROLLBACK_DIR"
}

backup_before_upgrade() {
  if [[ -n "${OBS_UPDATE_BACKUP_DONE:-}" ]]; then
    log "升级前备份已完成（沿用 $ROLLBACK_DIR/backup）"
    return
  fi
  if [[ "${SKIP_BACKUP:-0}" == "1" ]]; then
    warn "SKIP_BACKUP=1：跳过升级前备份（不推荐；回滚将没有数据库备份）"
  else
    log "升级前备份数据库 + 数据卷（可回滚的数据基线）"
    if ! bash "$APP_DIR/deploy/backup.sh" "$ROLLBACK_DIR/backup"; then
      die "升级前备份失败，已中止（未改动代码与容器）"
    fi
    log "备份完成：$ROLLBACK_DIR/backup"
  fi
  export OBS_UPDATE_BACKUP_DONE=1
}

ensure_volume_owner() {
  # 从 0.2.x（root 卷）升到 0.3.0（非 root 容器）时校正属主；已有 0.3.0 卷是幂等的。
  local volume_uid="${OBS_UID:-}"
  if [[ -z "$volume_uid" ]]; then
    volume_uid="$(grep -E '^OBS_UID=' "$APP_DIR/server/.env" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
  fi
  volume_uid="${volume_uid:-10001}"
  if docker volume inspect observatory-data >/dev/null 2>&1; then
    log "校正数据卷属主为容器用户 $volume_uid"
    docker run --rm -v observatory-data:/data alpine:3 chown -R "$volume_uid:$volume_uid" /data \
      || warn "数据卷 chown 失败；新容器可能无法写 /data"
  fi
}

rollback() {
  local reason="${1:-未知错误}"
  trap - EXIT INT TERM
  ROLLBACK_NEEDED=0
  warn "升级失败（$reason），开始自动回退"
  warn "  旧 commit : $OLD_COMMIT"
  warn "  旧版本    : $OLD_VERSION"
  warn "  旧镜像    : ${OLD_IMAGE_ID:-未记录}"
  warn "  备份目录  : $ROLLBACK_DIR"

  if git -C "$APP_DIR" reset --hard "$OLD_COMMIT" >/dev/null 2>&1; then
    log "代码已回退到 $OLD_COMMIT"
  else
    warn "git reset --hard $OLD_COMMIT 失败；仍尝试旧镜像回滚"
  fi

  if [[ -n "$OLD_IMAGE_ID" ]]; then
    local rollback_tag="rollback-$STAMP"
    if docker tag "$OLD_IMAGE_ID" "observatory/api:$rollback_tag" >/dev/null 2>&1; then
      export OBS_IMAGE_TAG="$rollback_tag"
      if compose up -d --no-build; then
        docker tag "observatory/api:$rollback_tag" "observatory/api:latest" >/dev/null 2>&1 || true
        if wait_health 60; then
          log "已用旧镜像启动并恢复健康（observatory/api:$rollback_tag）"
        else
          warn "旧镜像已启动但 120 秒内未恢复健康，请人工检查 docker compose logs"
        fi
      else
        warn "旧镜像启动失败，请人工检查 docker compose logs"
      fi
    else
      warn "无法给旧镜像打回滚 tag（$OLD_IMAGE_ID）"
    fi
  else
    warn "没有记录到旧镜像 ID，无法自动回滚镜像"
  fi

  if [[ -d "$ROLLBACK_DIR/backup" ]]; then
    warn "如需恢复数据库：sudo bash deploy/restore.sh <$ROLLBACK_DIR/backup/observatory-data-*.tar.gz> [db 备份]"
  fi
  warn "升级失败，退出码非 0。"
  exit 1
}

on_exit() {
  local rc=$?
  if [[ "$SUCCESS" -ne 1 && "$ROLLBACK_NEEDED" -eq 1 ]]; then
    rollback "退出码 $rc"
  fi
  exit "$rc"
}

trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "请用 root 运行：sudo bash deploy/update.sh"
  fi
}

register_agent_packages() {
  local zip="$APP_DIR/agent/dist/observatory-agent-$NEW_VERSION.zip"
  if [[ ! -f "$zip" ]]; then
    if [[ "${SKIP_FETCH_AGENT:-0}" != "1" && -f "$APP_DIR/deploy/fetch-agent-package.sh" ]]; then
      log "本地没有 $NEW_VERSION 的受控端包，尝试从 CI 的 agent-dist 分支获取"
      if bash "$APP_DIR/deploy/fetch-agent-package.sh"; then
        log "已从 CI 取回 $NEW_VERSION 受控端包"
      else
        warn "取受控端包失败（CI 可能尚未产出）。设备保持当前版本；稍后可单独执行："
        warn "  sudo bash $APP_DIR/deploy/fetch-agent-package.sh"
      fi
    fi
  fi

  shopt -s nullglob
  local found=0
  for candidate in "$APP_DIR"/agent/dist/observatory-agent-*.zip; do
    if [[ "$(basename "$candidate")" != "observatory-agent-$NEW_VERSION.zip" ]]; then
      warn "跳过非当前版本的安装包：$(basename "$candidate")"
      continue
    fi
    found=1
    log "登记受控端安装包：$(basename "$candidate")"
    docker cp "$candidate" observatory-api:/data/packages/
    if docker exec -w /srv observatory-api python -m observatory.cli package-add "/data/packages/$(basename "$candidate")" >/dev/null; then
      log "已登记 $(basename "$candidate")"
    else
      warn "登记失败（可能已存在同版本）"
    fi
  done
  shopt -u nullglob
  if [[ "$found" -eq 0 ]]; then
    warn "没有找到 observatory-agent-$NEW_VERSION.zip；受控端不会被自动升级"
  fi
}

main() {
  require_root
  cd "$APP_DIR" || die "找不到 $APP_DIR"
  [[ -d .git ]] || die "$APP_DIR 不是 git 仓库"

  log "当前版本：$(tr -d '[:space:]' < VERSION 2>/dev/null || echo 未知)"
  record_upgrade_state

  # A7：先做数据备份，再改动代码/容器。备份失败直接中止，什么都不动。
  backup_before_upgrade

  if [[ -z "${OBS_UPDATE_REEXEC:-}" ]] && [[ -n "$(git status --porcelain)" ]]; then
    warn "工作区有未提交改动，先 stash 以保证 pull 干净（可用 git stash list 找回）"
    git stash push -u -m "update.sh $STAMP" >/dev/null
  fi

  local self hash_before hash_after
  self="$(readlink -f "${BASH_SOURCE[0]}")"
  hash_before="$(sha256sum "$self" | cut -d' ' -f1)"

  log "拉取 origin/$BRANCH"
  git fetch --prune origin
  git checkout "$BRANCH"
  git pull --ff-only origin "$BRANCH"

  # pull 可能替换本脚本自身。bash 继续执行内存里的旧文本，因此重新执行一次。
  hash_after="$(sha256sum "$self" | cut -d' ' -f1)"
  if [[ "$hash_before" != "$hash_after" && -z "${OBS_UPDATE_REEXEC:-}" ]]; then
    log "部署脚本自身已更新，重新执行一次（旧状态与备份记录会沿用）"
    OBS_UPDATE_REEXEC=1 exec bash "$self" "$@"
  fi

  # 防御性 tr：VERSION 若带 CRLF，所有字符串比较都会静默失败（见 .gitattributes）。
  NEW_VERSION="$(tr -d '[:space:]' < VERSION)"
  log "目标版本：$NEW_VERSION"
  export OBS_IMAGE_TAG="$NEW_VERSION"
  ROLLBACK_NEEDED=1

  log "重新构建镜像（会同时重建控制台前端），tag=observatory/api:$NEW_VERSION"
  if ! compose build --pull; then
    rollback "镜像构建失败"
  fi
  log "启动新容器"
  ensure_volume_owner
  if ! compose up -d; then
    rollback "容器启动失败"
  fi
  if ! wait_health 60; then
    compose logs --tail=80 || true
    rollback "健康检查 120 秒未通过"
  fi
  local served
  served="$(running_version)"
  if [[ "$served" != "$NEW_VERSION" ]]; then
    rollback "运行版本 '$served' 与 VERSION '$NEW_VERSION' 不一致"
  fi

  docker tag "observatory/api:$NEW_VERSION" "observatory/api:latest" >/dev/null 2>&1 \
    || warn "无法更新 :latest tag（非致命）"

  SUCCESS=1
  trap - EXIT INT TERM
  {
    echo "result=success"
    echo "old_commit=$OLD_COMMIT"
    echo "old_version=$OLD_VERSION"
    echo "new_version=$NEW_VERSION"
    echo "old_image_id=$OLD_IMAGE_ID"
    echo "new_image_id=$(docker inspect --format '{{.Image}}' observatory-api 2>/dev/null || true)"
    echo "finished_at=$(date -Is)"
  } > "$ROLLBACK_DIR/result.env" 2>/dev/null || true

  # 包登记失败不回滚代码（服务端已健康），只告警。
  register_agent_packages || warn "受控端安装包登记异常（服务端不回滚）"

  log "升级完成：$OLD_VERSION → $NEW_VERSION"
  log "旧状态与数据库备份：$ROLLBACK_DIR"
  log "当前运行版本：$(running_version)"
  log "受控端会在下一次心跳看到新版本并自行更新。"
  exit 0
}

main "$@"