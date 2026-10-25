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
#: Prevent two operators (or a retry while the first run is still building)
#: from recreating the same container concurrently. That is how the
#: 2026-10-24 outage turned a 6-minute upgrade into 20 minutes of 521s.
LOCK_FILE="${LOCK_FILE:-/var/lock/observatory-update.lock}"
#: Persistent upgrade log: the 2026-10-24 failed attempts left only
#: upgrade.env behind, so the reason (non-root container vs root-owned volume)
#: had to be reconstructed from dockerd's journal.
LOG_FILE="${LOG_FILE:-$APP_DIR/backups/update.log}"

OLD_COMMIT=""
OLD_VERSION=""
OLD_IMAGE_ID=""
ROLLBACK_DIR=""
NEW_VERSION=""
ROLLBACK_NEEDED=0
SUCCESS=0

log()  { printf '\033[36m[update]\033[0m %s\n' "$*" | tee -a "$LOG_FILE" 2>/dev/null || true; }
warn() { printf '\033[33m[update]\033[0m %s\n' "$*" | tee -a "$LOG_FILE" >&2 2>/dev/null || true; }
die()  { printf '\033[31m[update] %s\033[0m\n' "$*" | tee -a "$LOG_FILE" >&2 2>/dev/null || true; exit 1; }

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

container_uid() {
  # 容器以非 root 运行时（0.3.0 起），数据卷与 /certs 必须属于这个 UID；
  # 否则 uvicorn 在启动阶段就无法写库/读私钥，容器会 exit 1 并无限重启
  # （2026-10-24 的 521：旧镜像 user=root，新镜像 user=observatory，卷仍是 root）。
  local uid="${OBS_UID:-}"
  if [[ -z "$uid" ]]; then
    uid="$(grep -E '^OBS_UID=' "$APP_DIR/server/.env" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
  fi
  printf '%s' "${uid:-10001}"
}

ensure_volume_owner() {
  # 幂等：每次升级都校正，但失败必须让调用方回滚——只 warn 会让用户看到
  # "升级成功" 的日志，实际容器在 crash loop，CDN 返回 521（#1 的教训）。
  # 0.3.1（F47）：先 stat 卷内 DB 属主，已对就跳过——无条件 chown -R 会在
  # 数万帧文件的卷上制造每次升级数分钟的 IO 风暴。
  local uid
  uid="$(container_uid)"
  if docker volume inspect observatory-data >/dev/null 2>&1; then
    local current_owner
    current_owner="$(docker run --rm -v observatory-data:/data alpine:3 \
      sh -c 'stat -c %u /data/observatory.db 2>/dev/null || echo unknown')" || current_owner="unknown"
    if [[ "$current_owner" == "$uid" ]]; then
      log "数据卷属主已是 $uid，跳过 chown"
      return 0
    fi
    log "校正数据卷属主为容器用户 $uid（当前 $current_owner）"
    if ! docker run --rm -v observatory-data:/data alpine:3 chown -R "$uid:$uid" /data; then
      warn "数据卷 chown 失败（容器将以非 root 运行，无法写 /data）"
      return 1
    fi
  fi
  return 0
}

ensure_cert_owner() {
  # 镜像在 0.3.0 从 root 切到 observatory 用户；certbot/BT 面板续期后属主
  # 常变回 root，容器读不到 /certs/server.key 就起不来。升级前必须校正。
  local uid crt="$APP_DIR/certs/server.crt" key="$APP_DIR/certs/server.key"
  uid="$(container_uid)"
  if [[ ! -f "$crt" || ! -f "$key" ]]; then
    warn "证书缺失：$crt / $key（容器无法在 8443 提供 TLS）"
    return 1
  fi
  if ! chown "$uid:$uid" "$crt" "$key"; then
    warn "证书属主校正失败（应为 $uid:$uid）"
    return 1
  fi
  chmod 0644 "$crt" 2>/dev/null || true
  chmod 0600 "$key" 2>/dev/null || true
  log "证书属主已校正为 $uid"
  return 0
}

wait_started() {
  # 比 wait_health 更早发现 crash loop：容器处于 restarting/exited 时立即
  # 回滚，避免用户继续吃 521。只有 running 才继续等 /api/v1/health。
  local tries="${1:-20}" i status
  for ((i = 1; i <= tries; i++)); do
    status="$(docker inspect --format '{{.State.Status}}' observatory-api 2>/dev/null || true)"
    case "$status" in
      running) return 0 ;;
      restarting | exited | dead) return 1 ;;
    esac
    sleep 1
  done
  return 1
}

acquire_lock() {
  # re-exec 会继承锁；不要在新进程里重复加锁（否则会把自己挡住）。
  if [[ -n "${OBS_UPDATE_LOCK_HELD:-}" ]]; then
    return 0
  fi
  mkdir -p "$(dirname "$LOCK_FILE")" "$(dirname "$LOG_FILE")" 2>/dev/null || true
  touch "$LOG_FILE" 2>/dev/null || true
  command -v flock >/dev/null 2>&1 || die "缺少 flock（util-linux）；无法保证升级互斥"
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    die "另一个 deploy/update.sh 正在运行（锁 $LOCK_FILE）；并发升级会导致容器被反复重建"
  fi
  export OBS_UPDATE_LOCK_HELD=1
  log "已获取升级锁：$LOCK_FILE（日志：$LOG_FILE）"
}

prune_upgrade_backups() {
  # F47：升级备份只增不清会把小盘 VPS 填满（backup.sh 的 7 天清理只作用于
  # 其自己的 DEST）。保留最近 3 份，成功路径末尾调用。
  local keep="${OBS_UPGRADE_BACKUPS_KEEP:-3}"
  [[ -d "$APP_DIR/backups" ]] || return 0
  local old
  old="$(ls -1dt "$APP_DIR"/backups/upgrade-* 2>/dev/null | tail -n +$((keep + 1)) || true)"
  if [[ -n "$old" ]]; then
    log "清理旧升级备份（保留 $keep 份）"
    while IFS= read -r dir; do
      rm -rf -- "$dir"
    done <<<"$old"
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
      # 旧镜像可能以 root 运行，但先校正属主可保证回滚后再次切非 root 时不再踩坑。
      ensure_volume_owner || warn "回滚前数据卷属主校正失败（旧镜像若为 root 仍可启动）"
      ensure_cert_owner || warn "回滚前证书属主校正失败（旧镜像若为 root 仍可启动）"
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
    name="$(basename "$candidate")"
    log "登记受控端安装包：${name}"
    # 容器 rootfs 只读，/data/packages 由 observatory 用户拥有；docker cp 到
    # /data/packages 会留下 root 属主文件，CLI 随后无法覆盖写入。统一经
    # /data/tmp 中转，并先 unlink 旧目标（目录 owner 可以 unlink）。
    remote_tmp="/data/tmp/${name}"
    if ! docker cp "$candidate" "observatory-api:${remote_tmp}"; then
      warn "docker cp 失败，跳过登记 ${name}"
      continue
    fi
    docker exec observatory-api rm -f "/data/packages/${name}" >/dev/null 2>&1 || true
    if docker exec -w /srv observatory-api python -m observatory.cli package-add "${remote_tmp}" >/dev/null; then
      log "已登记 ${name}"
    else
      warn "登记失败：${name}"
    fi
    docker exec observatory-api rm -f "${remote_tmp}" >/dev/null 2>&1 || true
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

  # 只读/幂等预检：不构建、不换镜像，可在生产上随时跑。
  if [[ "${1:-}" == "--preflight" ]]; then
    acquire_lock
    log "预检模式：只检查属主、容器、健康与 origin 通道，不构建/不替换镜像"
    ensure_volume_owner || die "数据卷属主预检失败"
    ensure_cert_owner || die "证书属主预检失败"
    compose ps || true
    # origin 凭据/网络坏了的话，auto-update 会每 2 分钟空转——在这里提前暴露。
    if ! git fetch --dry-run origin >/dev/null 2>&1; then
      die "git fetch origin 失败（凭据/网络）；自动更新链路当前不可用，请检查 origin remote 与 SSH alias"
    fi
    log "origin 通道正常"
    if curl -fsSk --max-time 10 https://127.0.0.1:8443/api/v1/health >/dev/null 2>&1; then
      log "本机健康检查通过：https://127.0.0.1:8443/api/v1/health"
    else
      die "本机健康检查失败；不要继续升级，先看 compose logs"
    fi
    log "预检通过。可以执行 sudo bash deploy/update.sh"
    exit 0
  fi

  acquire_lock

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

  # 0.3.0 镜像以非 root 运行；旧卷/证书常是 root 属主。先把这件事做成
  # “失败即回滚”的前置条件，再启动容器——2026-10-24 的 20 分钟 521 就是
  # 新镜像 crash loop、脚本却继续等健康检查造成的。
  local old_user new_user
  old_user="$(docker image inspect "$OLD_IMAGE_ID" --format '{{.Config.User}}' 2>/dev/null || true)"
  new_user="$(docker image inspect "observatory/api:$NEW_VERSION" --format '{{.Config.User}}' 2>/dev/null || true)"
  log "镜像用户：旧='${old_user:-root}' 新='${new_user:-root}'（非空表示非 root）"

  log "启动前预检：数据卷与证书属主"
  if ! ensure_volume_owner; then
    rollback "数据卷属主校正失败（新容器以非 root 运行，启动必然失败）"
  fi
  if ! ensure_cert_owner; then
    rollback "证书属主/文件校正失败（新容器无法读取 /certs/server.key）"
  fi

  log "启动新容器"
  if ! compose up -d; then
    rollback "容器启动失败"
  fi
  if ! wait_started 20; then
    compose logs --tail=80 || true
    rollback "新容器启动后进入 restarting/exited（常见原因：数据卷或证书属主、端口占用、数据库）"
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

  prune_upgrade_backups

  log "升级完成：$OLD_VERSION → $NEW_VERSION"
  log "旧状态与数据库备份：$ROLLBACK_DIR"
  log "当前运行版本：$(running_version)"
  log "受控端会在下一次 welcome/心跳收到 update_offer 并自行更新（0.3.1 起 v3 链路闭环）。"
  exit 0
}

main "$@"