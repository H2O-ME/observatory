#!/usr/bin/env bash
# 服务器端部署器（0.3.7 起替代 update.sh，CI 直推模式）。
#
#   deploy.sh release <tag> [--full-backup]   # CI/人工入口：异步启动部署 worker
#   deploy.sh status                          # 查看状态文件（CI 轮询这个）
#   deploy.sh logs [N]                        # 尾随最近一次部署日志
#   deploy.sh rollback                        # 人工回滚到最近一次部署前的状态
#   deploy.sh reset-breaker                   # 清除熔断计数
#
# 设计（2026-10-26 八小时宕机复盘）：
#   1. supervisor/worker 分离——触发方（CI 的 SSH、人工终端）只负责点火；
#      worker 由 setsid 脱离会话独立运行，全程写状态文件。SSH 断线、systemd
#      超时、终端关闭都不可能把部署杀死在换容器中途（03:08 事故：回滚脚本被
#      SIGKILL 打断，新旧容器两头落空）。
#   2. 健康验证多点化——容器内回环、宿主模拟 nginx 路径（真实 Host 头，走过
#      HostSplit）、docker healthcheck、版本端点、WebUI 数据卷版本。任一不过
#      立即回滚。历史上 update.sh 只从宿主 curl Host:127.0.0.1，与 HostSplit
#      的反代豁免逻辑互相误判，健康检查永远失败。
#   3. 快备份——默认只备 DB 在线快照 + 证书 + .env（秒级）。数据卷全量 tar
#      （数百 MB、数分钟）只在 --full-backup 时执行；帧数据另有 GitHub 增量归档。
#   4. 幂等——目标 commit、运行版本、WebUI 版本都已就位时直接 no-op，
#      同版本重复触发不再重建容器。
#   5. 熔断——同一目标 commit 连续 3 次失败后拒绝自动部署（reset-breaker 解除），
#      防止坏 tag 被反复重试。
#
# 自升级：worker 先 fetch/checkout 目标 tag，再 re-exec 新版 deploy.sh --phase2，
# 因此脚本本身的修复随下一个 tag 生效，服务器上的旧副本只承担「点火 + 检出」。

set -Eeuo pipefail
IFS=$'\n\t'
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:$PATH}"

APP_DIR="${APP_DIR:-/opt/observatory}"
LOCK_FILE="${LOCK_FILE:-/var/lock/observatory-deploy.lock}"
STATUS_FILE="${STATUS_FILE:-$APP_DIR/deploy.status}"
LOG_DIR="$APP_DIR/backups/deploy"
STATE_DIR="${STATE_DIR:-/var/lib/observatory-deploy}"
BREAKER_MAX="${BREAKER_MAX:-3}"
STAMP="$(date +%Y%m%d-%H%M%S)"

LOG_FILE="${OBS_DEPLOY_LOG:-$LOG_DIR/deploy-$STAMP.log}"

log()  { printf '\033[36m[deploy]\033[0m %s\n' "$*" | tee -a "$LOG_FILE" 2>/dev/null || true; }
warn() { printf '\033[33m[deploy]\033[0m %s\n' "$*" | tee -a "$LOG_FILE" >&2 2>/dev/null || true; }
die()  { printf '\033[31m[deploy] %s\033[0m\n' "$*" | tee -a "$LOG_FILE" >&2 2>/dev/null || true; exit 1; }

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "请用 root 运行：sudo bash deploy/deploy.sh $*"
  fi
}

# ---------------------------------------------------------------------------
# 状态文件（CI 唯一事实来源；原子写避免读到半行）
# ---------------------------------------------------------------------------
status_write() {
  # status_write key=value ...  （phase/tag/commit/version/result/started/finished/reason）
  local out="" kv
  mkdir -p "$(dirname "$STATUS_FILE")"
  for kv in "$@"; do out+="$kv"$'\n'; done
  printf '%s' "$out" > "$STATUS_FILE.tmp"
  mv -f "$STATUS_FILE.tmp" "$STATUS_FILE"
}

status_field() {
  sed -n "s/^$1=//p" "$STATUS_FILE" 2>/dev/null | head -1
}

compose() {
  OBS_IMAGE_TAG="${OBS_IMAGE_TAG:-$TARGET_VERSION}" \
    docker compose -f "$APP_DIR/deploy/docker-compose.yml" --env-file "$APP_DIR/server/.env" "$@"
}

container_uid() {
  local uid="${OBS_UID:-}"
  if [[ -z "$uid" ]]; then
    uid="$(grep -E '^OBS_UID=' "$APP_DIR/server/.env" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
  fi
  printf '%s' "${uid:-10001}"
}

console_host() {
  local host=""
  host="$(grep -E '^OBS_CONSOLE_HOST=' "$APP_DIR/server/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
  printf '%s' "${host:-control.tianhw.top}"
}

# ---------------------------------------------------------------------------
# 熔断：同一目标 commit 连续失败 BREAKER_MAX 次后拒绝部署
# ---------------------------------------------------------------------------
breaker_check() {
  local sha="$1" count sha_recorded
  sha_recorded="$(cat "$STATE_DIR/failed.sha" 2>/dev/null || true)"
  count="$(( $(cat "$STATE_DIR/failed.count" 2>/dev/null || echo 0) ))"
  if [[ "$sha_recorded" == "$sha" && "$count" -ge "$BREAKER_MAX" ]]; then
    die "目标 ${sha:0:12} 已连续失败 ${count} 次并熔断；修复后执行 deploy.sh reset-breaker（或用 OBS_DEPLOY_FORCE=1 强制一次）"
  fi
  if [[ "${OBS_DEPLOY_FORCE:-0}" == "1" ]]; then
    warn "OBS_DEPLOY_FORCE=1：忽略熔断强制部署一次"
  fi
}

breaker_record_failure() {
  local sha="$1" count prev_sha
  prev_sha="$(cat "$STATE_DIR/failed.sha" 2>/dev/null || true)"
  if [[ "$prev_sha" == "$sha" ]]; then
    count="$(( $(cat "$STATE_DIR/failed.count" 2>/dev/null || echo 0) + 1 ))"
  else
    count=1
  fi
  mkdir -p "$STATE_DIR"
  printf '%s\n' "$sha" > "$STATE_DIR/failed.sha"
  printf '%s\n' "$count" > "$STATE_DIR/failed.count"
  warn "失败计数 ${count}/${BREAKER_MAX}（${sha:0:12}）"
}

breaker_clear() { rm -f "$STATE_DIR/failed.sha" "$STATE_DIR/failed.count"; }

# ---------------------------------------------------------------------------
# 备份：默认快速路径（DB 在线快照 + 证书 + .env）
# ---------------------------------------------------------------------------
fast_backup() {
  local dest="$ROLLBACK_DIR"
  mkdir -p "$dest"
  chmod 700 "$APP_DIR/backups" "$dest" 2>/dev/null || true

  log "备份数据库（SQLite 在线快照，容器内）"
  if docker exec observatory-api python - <<'PY' 2>>"$LOG_FILE"
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
print("wrote online snapshot")
PY
  then
    docker cp observatory-api:/data/.observatory-online.db "$dest/observatory-db-$STAMP.db" >/dev/null 2>&1 \
      && chmod 600 "$dest/observatory-db-$STAMP.db" \
      && log "数据库快照：$dest/observatory-db-$STAMP.db" \
      || warn "容器内没有可备份的数据库（首次部署）；继续"
  else
    die "数据库在线备份失败，已中止（未改动代码与容器）"
  fi
  docker exec observatory-api rm -f /data/.observatory-online.db >/dev/null 2>&1 || true

  if [[ -f "$APP_DIR/certs/server.crt" && -f "$APP_DIR/certs/server.key" ]]; then
    install -m 600 "$APP_DIR/certs/server.key" "$dest/server.key" 2>/dev/null || true
    install -m 644 "$APP_DIR/certs/server.crt" "$dest/server.crt" 2>/dev/null || true
  fi
  [[ -f "$APP_DIR/server/.env" ]] && cp "$APP_DIR/server/.env" "$dest/env"
  log "快速备份完成（$dest）；数据卷全量 tar 用 --full-backup 另行执行"
}

full_backup() {
  log "全量备份数据卷（backup.sh，慢）"
  if ! bash "$APP_DIR/deploy/backup.sh" "$ROLLBACK_DIR/backup"; then
    die "全量备份失败，已中止（未改动代码与容器）"
  fi
}

# ---------------------------------------------------------------------------
# 属主校正（沿袭 update.sh 的实战逻辑）
# ---------------------------------------------------------------------------
ensure_cert_owner() {
  local uid crt="$APP_DIR/certs/server.crt" key="$APP_DIR/certs/server.key"
  uid="$(container_uid)"
  if [[ ! -f "$crt" || ! -f "$key" ]]; then
    warn "证书缺失：$crt / $key（容器无法在 8443 提供 TLS）"
    return 1
  fi
  chown "$uid:$uid" "$crt" "$key" || { warn "证书属主校正失败（应为 $uid:$uid）"; return 1; }
  chmod 0644 "$crt" 2>/dev/null || true
  chmod 0600 "$key" 2>/dev/null || true
  log "证书属主已确认为 $uid"
}

ensure_volume_owner() {
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
    log "校正数据卷属主为 $uid（当前 $current_owner）"
    docker run --rm -v observatory-data:/data alpine:3 chown -R "$uid:$uid" /data \
      || { warn "数据卷 chown 失败"; return 1; }
  fi
}

# ---------------------------------------------------------------------------
# 健康验证（多层，任一失败即回滚）
# ---------------------------------------------------------------------------
wait_started() {
  local tries="${1:-30}" i status
  for ((i = 1; i <= tries; i++)); do
    status="$(docker inspect --format '{{.State.Status}}' observatory-api 2>/dev/null || true)"
    case "$status" in
      running) return 0 ;;
      restarting | exited | dead) warn "容器状态 $status（第 $i 次探测）"; return 1 ;;
    esac
    sleep 1
  done
  warn "容器 30 秒内未进入 running"
  return 1
}

in_container_health() {
  # 容器内回环直连：绕过一切反代/HostSplit 网络位形，验证应用本体。
  docker exec observatory-api python - <<'PY' >/dev/null 2>&1
import json
import ssl
import urllib.request

ctx = ssl._create_unverified_context()
with urllib.request.urlopen("https://127.0.0.1:8443/api/v1/health", context=ctx, timeout=8) as resp:
    body = json.load(resp)
if not body.get("ok"):
    raise SystemExit(f"health ok=false: {body}")
print(body.get("version", ""))
PY
}

host_path_health() {
  # 宿主模拟 nginx 路径：真实 Host 头走 HostSplit，验证「反代进来的流量可用」
  # （2026-10-26 事故：只测回环、不测真实 Host，策略层回归全部漏网）。
  curl -fsSk --max-time 8 -H "Host: $(console_host)" \
    "https://127.0.0.1:${OBS_PORT:-8443}/api/v1/health" 2>/dev/null
}

docker_health_green() {
  [[ "$(docker inspect --format '{{.State.Health.Status}}' observatory-api 2>/dev/null || true)" == "healthy" ]]
}

wait_healthy() {
  local tries="${1:-80}" i
  for ((i = 1; i <= tries; i++)); do
    if docker_health_green; then return 0; fi
    sleep 3
  done
  warn "docker healthcheck 4 分钟内未达 healthy"
  return 1
}

verify_deployment() {
  local want_version="$1" reported i

  wait_started 30 || return 1

  # 快速失败通道：容器内直连，不等 healthcheck 的 30s 周期。
  for i in 1 2 3 4 5 6 7 8 9 10; do
    reported="$(in_container_health || true)"
    [[ -n "$reported" ]] && break
    sleep 3
  done
  [[ -n "$reported" ]] || { warn "容器内健康检查 30 秒无响应"; return 1; }
  log "容器内健康 ok（version=$reported）"

  [[ "$reported" == "$want_version" ]] \
    || { warn "运行版本 $reported ≠ 目标 $want_version"; return 1; }

  wait_healthy 80 || return 1
  log "docker healthcheck：healthy"

  local host_body
  host_body="$(host_path_health || true)"
  [[ -n "$host_body" ]] || { warn "宿主→容器（真实 Host 头）路径健康检查失败（HostSplit/反代层回归？）"; return 1; }
  grep -q '"ok":true' <<<"$host_body" || { warn "宿主路径 health 响应异常：$host_body"; return 1; }
  log "宿主模拟反代路径（Host: $(console_host)）ok"

  local webui_ver=""
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    webui_ver="$(docker exec observatory-api cat /data/webui/.version 2>/dev/null | tr -d '[:space:]' || true)"
    [[ "$webui_ver" == "$want_version" ]] && break
    sleep 3
  done
  [[ "$webui_ver" == "$want_version" ]] \
    || { warn "WebUI 数据卷版本 $webui_ver ≠ 目标 $want_version（前端会是旧 SPA）"; return 1; }
  log "WebUI 数据卷版本 ok（$webui_ver）"

  return 0
}

# ---------------------------------------------------------------------------
# 受控端包登记（沿袭 update.sh：经 /data/tmp 中转避免 root 属主覆盖失败）
# ---------------------------------------------------------------------------
register_agent_packages() {
  local version="$1"
  local zip="$APP_DIR/agent/dist/observatory-agent-$version.zip"
  if [[ ! -f "$zip" ]]; then
    if [[ "${SKIP_FETCH_AGENT:-0}" != "1" && -f "$APP_DIR/deploy/fetch-agent-package.sh" ]]; then
      log "本地没有 $version 的受控端包，尝试从 agent-dist 分支获取"
      if bash "$APP_DIR/deploy/fetch-agent-package.sh"; then
        log "已从 agent-dist 取回 $version 受控端包"
      else
        warn "取受控端包失败（CI 可能尚未产出）。设备保持当前版本；稍后可单独执行 deploy/fetch-agent-package.sh"
      fi
    fi
  fi

  shopt -s nullglob
  local candidate name remote_tmp
  for candidate in "$APP_DIR"/agent/dist/observatory-agent-*.zip; do
    name="$(basename "$candidate")"
    if [[ "$name" != "observatory-agent-$version.zip" ]]; then
      warn "跳过非当前版本的安装包：$name"
      continue
    fi
    log "登记受控端安装包：$name"
    remote_tmp="/data/tmp/$name"
    if ! docker cp "$candidate" "observatory-api:$remote_tmp"; then
      warn "docker cp 失败，跳过登记 $name"
      continue
    fi
    docker exec observatory-api rm -f "/data/packages/$name" >/dev/null 2>&1 || true
    if docker exec -w /srv observatory-api python -m observatory.cli package-add "$remote_tmp" >/dev/null; then
      log "已登记 $name"
    else
      warn "登记失败：$name"
    fi
    docker exec observatory-api rm -f "$remote_tmp" >/dev/null 2>&1 || true
  done
}

prune_old_runs() {
  local keep="${OBS_DEPLOY_KEEP:-5}" old
  [[ -d "$APP_DIR/backups" ]] || return 0
  old="$(ls -1dt "$APP_DIR"/backups/upgrade-* 2>/dev/null | tail -n +$((keep + 1)) || true)"
  if [[ -n "$old" ]]; then
    while IFS= read -r dir; do rm -rf -- "$dir"; done <<<"$old"
  fi
  old="$(ls -1t "$LOG_DIR"/deploy-*.log 2>/dev/null | tail -n +$((keep + 1)) || true)"
  if [[ -n "$old" ]]; then
    while IFS= read -r f; do rm -f -- "$f"; done <<<"$old"
  fi
}

acquire_lock() {
  if [[ -n "${OBS_DEPLOY_LOCK_HELD:-}" ]]; then
    return 0
  fi
  mkdir -p "$(dirname "$LOCK_FILE")" "$LOG_DIR" "$STATE_DIR" 2>/dev/null || true
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    die "另一个部署正在进行（锁 $LOCK_FILE）"
  fi
  export OBS_DEPLOY_LOCK_HELD=1
}

# ---------------------------------------------------------------------------
# 回滚
# ---------------------------------------------------------------------------
rollback() {
  local reason="${1:-未知错误}"
  trap - EXIT INT TERM
  warn "部署失败（$reason），开始自动回退"
  warn "  旧 commit : ${OLD_COMMIT:-未记录}"
  warn "  旧版本    : ${OLD_VERSION:-未记录}"
  warn "  旧镜像    : ${OLD_IMAGE_ID:-未记录}"

  if [[ -n "${OLD_COMMIT:-}" ]]; then
    if git -C "$APP_DIR" reset --hard "$OLD_COMMIT" >/dev/null 2>&1; then
      log "代码已回退到 ${OLD_COMMIT:0:12}"
      # 老提交里可能还没有部署脚本本身（CI 直推管线的首个版本）——
      # 从目标 tag 恢复，避免回滚把 deploy.sh/ci-gate.sh 一起删掉、网关失联。
      if [[ ! -f "$APP_DIR/deploy/deploy.sh" && -n "${TARGET_TAG:-}" ]]; then
        git -C "$APP_DIR" checkout "$TARGET_TAG" -- deploy/deploy.sh deploy/ci-gate.sh 2>/dev/null || true
      fi
    else
      warn "git reset --hard $OLD_COMMIT 失败；仍尝试旧镜像回滚"
    fi
  fi

  if [[ -n "${OLD_IMAGE_ID:-}" ]]; then
    local rollback_tag="rollback-$STAMP"
    if docker tag "$OLD_IMAGE_ID" "observatory/api:$rollback_tag" >/dev/null 2>&1; then
      ensure_volume_owner || warn "回滚前数据卷属主校正失败"
      ensure_cert_owner || warn "回滚前证书属主校正失败"
      if OBS_IMAGE_TAG="$rollback_tag" compose up -d --no-build; then
        docker tag "observatory/api:$rollback_tag" "observatory/api:latest" >/dev/null 2>&1 || true
        if wait_started 30 && verify_deployment "${OLD_VERSION:-}"; then
          log "已回滚并恢复健康（observatory/api:$rollback_tag）"
        else
          warn "旧镜像已启动但未完全恢复健康，请人工检查 docker compose logs"
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

  status_write \
    "phase=done" "tag=${TARGET_TAG:-}" "commit=${TARGET_COMMIT:-}" "version=${TARGET_VERSION:-}" \
    "result=failed" "reason=$reason" \
    "started=${DEPLOY_STARTED:-}" "finished=$(date -Is)" \
    "previous_commit=${OLD_COMMIT:-}" "previous_version=${OLD_VERSION:-}"
  warn "部署失败结束。如需恢复数据库：deploy/restore.sh <$ROLLBACK_DIR/observatory-db-*.db>"
  exit 1
}

on_exit() {
  local rc=$?
  if [[ "$rc" -ne 0 && "${DEPLOY_ARMED:-0}" == "1" ]]; then
    rollback "退出码 $rc"
  fi
  exit "$rc"
}

# ---------------------------------------------------------------------------
# release：supervisor——点火后立即返回，CI 轮询状态文件
# ---------------------------------------------------------------------------
cmd_release() {
  require_root release
  local tag="${1:-}"; shift || true
  [[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "用法：deploy.sh release <vX.Y.Z> [--full-backup]（得到：$tag）"

  [[ -d "$APP_DIR/.git" ]] || die "$APP_DIR 不是 git 仓库"
  [[ -f "$APP_DIR/server/.env" ]] || die "缺少 $APP_DIR/server/.env"

  # 只探测、不持有：supervisor 即刻退出，锁由 worker 全程持有（worker 里
  # acquire_lock）。这里若真拿锁并 export HELD，worker 会继承标志位跳过加锁，
  # 部署期间锁就形同虚设，第二个 release 可以进来双写容器。
  if ! (exec 8>"$LOCK_FILE" && flock -n 8); then
    die "已有部署在进行中（锁 $LOCK_FILE）；先 deploy.sh status 查看"
  fi

  local full=0
  if [[ "${1:-}" == "--full-backup" ]]; then
    full=1
  fi

  mkdir -p "$LOG_DIR"
  : > "$LOG_FILE"
  status_write \
    "phase=triggered" "result=pending" "tag=$tag" \
    "started=$(date -Is)" "log=$LOG_FILE"

  # setsid：脱离 SSH 会话与控制终端——触发方掉线绝不影响 worker。
  if [[ "$full" == "1" ]]; then
    OBS_DEPLOY_LOG="$LOG_FILE" setsid bash "$APP_DIR/deploy/deploy.sh" --worker "$tag" --full-backup >>"$LOG_FILE" 2>&1 &
  else
    OBS_DEPLOY_LOG="$LOG_FILE" setsid bash "$APP_DIR/deploy/deploy.sh" --worker "$tag" >>"$LOG_FILE" 2>&1 &
  fi
  local pid=$!
  sleep 1
  if ! kill -0 "$pid" 2>/dev/null; then
    status_write "phase=done" "result=failed" "tag=$tag" "reason=worker 启动即退出" "log=$LOG_FILE"
    die "worker 启动失败，详见 $LOG_FILE"
  fi
  log "部署 worker 已启动（pid=$pid, tag=$tag）；状态：deploy.sh status"
}

# ---------------------------------------------------------------------------
# worker：脱离会话的真正部署流程
# ---------------------------------------------------------------------------
cmd_worker() {
  require_root worker
  local tag="${1:-}"; shift || true
  local full=0
  if [[ "${1:-}" == "--full-backup" ]]; then
    full=1
  fi

  acquire_lock
  cd "$APP_DIR"

  status_write "phase=fetch" "result=pending" "tag=$tag" "started=$(date -Is)" "log=$LOG_FILE"
  log "worker 开始：tag=$tag pid=$$"

  # git fetch 用服务器既有的 origin 凭据（ssh 别名 github-observatory / deploy key）。
  if ! git fetch --force origin "refs/tags/$tag:refs/tags/$tag"; then
    status_write "phase=done" "result=failed" "tag=$tag" "reason=git fetch $tag 失败" \
      "started=$(date -Is)" "finished=$(date -Is)" "log=$LOG_FILE"
    die "git fetch $tag 失败（CI 是否已把 tag 推上来？服务器 origin 凭据是否有效？）"
  fi
  git fetch --quiet origin main >/dev/null 2>&1 || true

  TARGET_TAG="$tag"
  TARGET_COMMIT="$(git rev-parse "$tag^{commit}")"
  TARGET_VERSION="${tag#v}"
  export TARGET_TAG TARGET_COMMIT TARGET_VERSION

  local tree_version
  tree_version="$(git show "$tag":VERSION 2>/dev/null | tr -d '[:space:]')"
  [[ "$tree_version" == "$TARGET_VERSION" ]] \
    || { status_write "phase=done" "result=failed" "tag=$tag" "commit=$TARGET_COMMIT" \
          "reason=tag 内 VERSION($tree_version) 与 tag 名不一致" "started=$(date -Is)" "log=$LOG_FILE"; \
         die "tag $tag 内 VERSION=$tree_version 与 tag 名不一致"; }

  breaker_check "$TARGET_COMMIT"

  # 幂等：目标已完整就位 → no-op。
  local run_ver="" webui_ver="" head_commit=""
  run_ver="$(curl -fsSk --max-time 5 "https://127.0.0.1:${OBS_PORT:-8443}/api/v1/health" 2>/dev/null | sed -n 's/.*"version":"\([^"]*\)".*/\1/p' || true)"
  webui_ver="$(docker exec observatory-api cat /data/webui/.version 2>/dev/null | tr -d '[:space:]' || true)"
  head_commit="$(git rev-parse HEAD 2>/dev/null || true)"
  if [[ "$run_ver" == "$TARGET_VERSION" && "$webui_ver" == "$TARGET_VERSION" && "$head_commit" == "$TARGET_COMMIT" ]] \
    && docker image inspect "observatory/api:$TARGET_VERSION" >/dev/null 2>&1; then
    log "目标版本已在运行（commit=${TARGET_COMMIT:0:12} version=$TARGET_VERSION webui=$webui_ver）——no-op"
    register_agent_packages "$TARGET_VERSION" || true
    breaker_clear
    status_write "phase=done" "result=success" "tag=$tag" "commit=$TARGET_COMMIT" "version=$TARGET_VERSION" \
      "noop=1" "started=$(date -Is)" "finished=$(date -Is)" "log=$LOG_FILE"
    return 0
  fi

  # 记录旧状态（回滚与人工 rollback 都靠它）
  OLD_COMMIT="$head_commit"
  OLD_VERSION="$run_ver"
  OLD_IMAGE_ID="$(docker inspect --format '{{.Image}}' observatory-api 2>/dev/null || true)"
  ROLLBACK_DIR="$APP_DIR/backups/upgrade-$STAMP"
  DEPLOY_STARTED="$(date -Is)"
  export OLD_COMMIT OLD_VERSION OLD_IMAGE_ID ROLLBACK_DIR DEPLOY_STARTED
  mkdir -p "$ROLLBACK_DIR"
  {
    echo "old_commit=$OLD_COMMIT"
    echo "old_version=$OLD_VERSION"
    echo "old_image_id=$OLD_IMAGE_ID"
    echo "target_tag=$TARGET_TAG"
    echo "target_commit=$TARGET_COMMIT"
    echo "started_at=$DEPLOY_STARTED"
  } > "$ROLLBACK_DIR/upgrade.env"
  chmod 700 "$ROLLBACK_DIR" 2>/dev/null || true
  log "旧状态：commit=${OLD_COMMIT:0:12} version=${OLD_VERSION:-unknown} image=${OLD_IMAGE_ID:0:12}"

  status_write "phase=backup" "result=pending" "tag=$tag" "commit=$TARGET_COMMIT" \
    "started=$DEPLOY_STARTED" "log=$LOG_FILE"
  fast_backup
  [[ "$full" == "1" ]] && full_backup

  # 检出目标 tag 后 re-exec 该版本自身的 --phase2（脚本自升级）。
  status_write "phase=checkout" "result=pending" "tag=$tag" "commit=$TARGET_COMMIT" \
    "started=$DEPLOY_STARTED" "log=$LOG_FILE"
  git checkout -q -f "$tag"
  log "已检出 $tag（${TARGET_COMMIT:0:12}），re-exec 新版部署逻辑"
  exec bash "$APP_DIR/deploy/deploy.sh" --phase2
}

# ---------------------------------------------------------------------------
# phase2：git 树已是目标 tag，执行 构建→换容器→验证→登记
# ---------------------------------------------------------------------------
cmd_phase2() {
  require_root phase2
  # worker 传下来的状态变量必须在
  : "${TARGET_VERSION:?}" "${TARGET_COMMIT:?}" "${TARGET_TAG:?}" "${OLD_COMMIT:?}" "${ROLLBACK_DIR:?}"
  acquire_lock
  cd "$APP_DIR"

  DEPLOY_ARMED=1
  trap on_exit EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  status_write "phase=preflight" "result=pending" "tag=$TARGET_TAG" "commit=$TARGET_COMMIT" \
    "started=$DEPLOY_STARTED" "log=$LOG_FILE"

  # 磁盘余量：df 读不到（data-root 路径不存在等）只 warn 不拦——
  # 把"探测失败"当"0MB"会误杀部署（2026-10-26 v0.3.7 首发教训）。
  avail_mb="$(df -PM /var/lib/docker 2>/dev/null | awk 'NR==2{print $4}' || true)"
  if [[ -z "$avail_mb" ]]; then
    avail_mb="$(df -PM /var/lib 2>/dev/null | awk 'NR==2{print $4}' || true)"
  fi
  if [[ -n "$avail_mb" ]] && (( avail_mb < 2048 )); then
    rollback "磁盘剩余 ${avail_mb}MB 不足 2GB，拒绝部署"
  fi
  [[ -n "$avail_mb" ]] && log "磁盘剩余 ${avail_mb}MB" || warn "无法探测磁盘余量（继续部署）"

  ensure_cert_owner || rollback "证书属主/存在性校正失败"
  ensure_volume_owner || rollback "数据卷属主校正失败"

  status_write "phase=build" "result=pending" "tag=$TARGET_TAG" "commit=$TARGET_COMMIT" \
    "started=$DEPLOY_STARTED" "log=$LOG_FILE"
  log "构建镜像 observatory/api:$TARGET_VERSION（旧容器不受影响）"
  if ! compose build --pull; then
    rollback "镜像构建失败"
  fi

  status_write "phase=swap" "result=pending" "tag=$TARGET_TAG" "commit=$TARGET_COMMIT" \
    "started=$DEPLOY_STARTED" "log=$LOG_FILE"
  log "切换容器到新镜像"
  if ! compose up -d --no-build; then
    rollback "容器启动失败"
  fi

  status_write "phase=verify" "result=pending" "tag=$TARGET_TAG" "commit=$TARGET_COMMIT" \
    "started=$DEPLOY_STARTED" "log=$LOG_FILE"
  if ! verify_deployment "$TARGET_VERSION"; then
    rollback "健康验证未通过"
  fi

  docker tag "observatory/api:$TARGET_VERSION" "observatory/api:latest" >/dev/null 2>&1 || true
  register_agent_packages "$TARGET_VERSION" || warn "受控端包登记未完成（不影响服务端）"

  breaker_clear
  prune_old_runs
  status_write \
    "phase=done" "result=success" "tag=$TARGET_TAG" "commit=$TARGET_COMMIT" "version=$TARGET_VERSION" \
    "started=$DEPLOY_STARTED" "finished=$(date -Is)" \
    "previous_commit=$OLD_COMMIT" "previous_version=$OLD_VERSION" "log=$LOG_FILE"
  log "部署成功：$TARGET_TAG（${TARGET_COMMIT:0:12}）"
  DEPLOY_ARMED=0
}

# ---------------------------------------------------------------------------
# 人工 rollback：用状态文件里记录的 previous_* 回到上一个版本
# ---------------------------------------------------------------------------
cmd_rollback() {
  require_root rollback
  local prev_commit prev_version
  prev_commit="$(status_field previous_commit)"
  prev_version="$(status_field previous_version)"
  [[ -n "$prev_commit" ]] || die "状态文件没有 previous_commit 记录；请用 backups/upgrade-*/upgrade.env 人工回滚"

  acquire_lock
  cd "$APP_DIR"
  local prev_image
  prev_image="$(docker images --format '{{.ID}}' "observatory/api:$prev_version" 2>/dev/null | head -1 || true)"
  [[ -n "$prev_image" ]] || die "找不到镜像 observatory/api:$prev_version（可能已被清理）；请改用 backups/ 里的材料人工恢复"

  local rollback_tag="manual-rollback-$STAMP"
  docker tag "$prev_image" "observatory/api:$rollback_tag"
  git reset --hard "$prev_commit"
  ensure_cert_owner || true
  ensure_volume_owner || true
  OBS_IMAGE_TAG="$rollback_tag" compose up -d --no-build
  if verify_deployment "$prev_version"; then
    log "人工回滚完成：$prev_version（$rollback_tag）"
  else
    warn "回滚已执行但健康验证未完全通过，请检查 docker compose logs"
  fi
  status_write "phase=done" "result=success" "action=manual-rollback" "version=$prev_version" \
    "commit=$prev_commit" "started=$(date -Is)" "finished=$(date -Is)" "log=$LOG_FILE"
}

cmd_status() {
  if [[ -f "$STATUS_FILE" ]]; then
    cat "$STATUS_FILE"
  else
    echo "phase=unknown result=unknown（尚无部署记录：$STATUS_FILE）"
  fi
}

cmd_logs() {
  local n="${1:-80}"
  local latest
  latest="$(ls -1t "$LOG_DIR"/deploy-*.log 2>/dev/null | head -1 || true)"
  if [[ -n "$latest" && -f "${OBS_DEPLOY_LOG:-}" ]]; then
    latest="$OBS_DEPLOY_LOG"
  fi
  if [[ -n "$latest" && -f "$latest" ]]; then
    tail -n "$n" "$latest"
  else
    echo "没有部署日志（$LOG_DIR）"
  fi
}

cmd_reset_breaker() {
  require_root reset-breaker
  breaker_clear
  echo "熔断计数已清除"
}

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

case "${1:-}" in
  release)        shift; cmd_release "$@" ;;
  --worker)       shift; cmd_worker "$@" ;;
  --phase2)       shift; cmd_phase2 "$@" ;;
  status)         cmd_status ;;
  logs)           shift; cmd_logs "$@" ;;
  rollback)       cmd_rollback ;;
  reset-breaker)  cmd_reset_breaker ;;
  -h|--help|help) usage 0 ;;
  *)              usage 1 ;;
esac
