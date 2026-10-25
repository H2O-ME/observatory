#!/usr/bin/env bash
# 服务器端自动更新（轮询机）——把「本地 push → GitHub → 服务器」串成一条链路。
#
#   sudo bash deploy/auto-update.sh            # 单次检查（cron/systemd 也能直接跑）
#   sudo bash deploy/install-auto-update.sh    # 安装并启用 2 分钟一次的 timer
#
# 行为：
#   1. fetch GitHub 的 main 与 agent-dist（服务器用只读 deploy key，不存 PAT）；
#   2. main 有新提交时调用 deploy/update.sh（自动更新默认跳过升级前备份，
#      SKIP_BACKUP=1；需要备份时手动执行 update.sh）；
#   3. 本地已有 v<VERSION> tag 时等待 CI 产出同版本 agent-dist；
#      分支 VERSION 对上后调用 deploy/fetch-agent-package.sh 登记安装包；
#   4. 只用 flock 防止自己重叠；update.sh 自身还有升级锁，不会并发重建容器。
#
# 退出码：0=已处理/无需处理；非 0=本轮失败，timer 下一轮会重试。
set -Eeuo pipefail
IFS=$'\n\t'

APP_DIR="${APP_DIR:-/opt/observatory}"
BRANCH="${BRANCH:-main}"
AGENT_BRANCH="${AGENT_BRANCH:-agent-dist}"
LOCK_FILE="${LOCK_FILE:-/var/lock/observatory-auto-update.lock}"
STATE_DIR="${STATE_DIR:-/var/lib/observatory-auto-update}"
LOG_FILE="${LOG_FILE:-/var/log/observatory-auto-update.log}"

log()  { printf '[auto-update] %s\n' "$*" | tee -a "$LOG_FILE" 2>/dev/null || true; }
warn() { printf '[auto-update] %s\n' "$*" | tee -a "$LOG_FILE" >&2 2>/dev/null || true; }

mkdir -p "$STATE_DIR" "$(dirname "$LOG_FILE")"
: >> "$LOG_FILE"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "另一个 auto-update 仍在运行，跳过本轮"
  exit 0
fi

cd "$APP_DIR" || { warn "找不到 $APP_DIR"; exit 1; }
[[ -d .git ]] || { warn "$APP_DIR 不是 git 仓库"; exit 1; }

# main 与 tag：失败立即退出（更新链路不可用要能被 timer 发现）。
log "拉取 origin/$BRANCH"
git fetch --prune origin "+refs/heads/${BRANCH}:refs/remotes/origin/${BRANCH}" --tags
# agent-dist 可能尚未由 CI 创建；缺失只告警，不影响代码更新。
git fetch --force --no-tags origin \
  "+refs/heads/${AGENT_BRANCH}:refs/remotes/origin/${AGENT_BRANCH}" \
  || warn "agent-dist 暂不可用（CI 尚未产出或分支不存在）"

LOCAL_SHA="$(git rev-parse HEAD)"
REMOTE_SHA="$(git rev-parse "refs/remotes/origin/${BRANCH}")"

if [[ "$LOCAL_SHA" == "$REMOTE_SHA" ]]; then
  log "代码已是最新（$(git rev-parse --short HEAD)）"
else
  if ! git diff --quiet || ! git diff --cached --quiet; then
    warn "工作区有未提交改动，跳过自动更新（先手工处理或清理）"
    exit 0
  fi
  log "发现新提交 ${LOCAL_SHA:0:12} → ${REMOTE_SHA:0:12}，开始部署"
  # 自动链路不保留 200MB+ 的升级前数据卷备份（用户明确不需要）；
  # 手工执行 deploy/update.sh 时仍默认备份。
  SKIP_BACKUP=1 bash deploy/update.sh
  LOCAL_SHA="$(git rev-parse HEAD)"
  log "部署完成，当前 commit $(git rev-parse --short HEAD)"
fi

VERSION="$(tr -d '[:space:]' < VERSION)"
TAG="v${VERSION}"
if ! git rev-parse --verify --quiet "refs/tags/${TAG}" >/dev/null; then
  log "没有 tag ${TAG}：不等待受控端安装包（CI 只在 tag 上构建 agent-dist）"
  exit 0
fi

AGENT_SHA="$(git rev-parse --verify --quiet "refs/remotes/origin/${AGENT_BRANCH}" || true)"
if [[ -z "$AGENT_SHA" ]]; then
  log "agent-dist 分支不存在，等待 CI 构建 ${TAG}"
  exit 0
fi

REMOTE_AGENT_VERSION="$(git show "refs/remotes/origin/${AGENT_BRANCH}:VERSION" 2>/dev/null | tr -d '[:space:]' || true)"
if [[ "$REMOTE_AGENT_VERSION" != "$VERSION" ]]; then
  log "agent-dist 仍是 ${REMOTE_AGENT_VERSION:-未知}，等待 CI 产出 ${VERSION}"
  exit 0
fi

STATE_FILE="${STATE_DIR}/agent-dist.sha"
if [[ "$(cat "$STATE_FILE" 2>/dev/null || true)" == "$AGENT_SHA" ]]; then
  log "agent-dist ${AGENT_SHA:0:12} 已登记过，跳过"
  exit 0
fi

log "登记 ${VERSION} 受控端安装包（agent-dist ${AGENT_SHA:0:12}）"
if bash deploy/fetch-agent-package.sh; then
  printf '%s\n' "$AGENT_SHA" > "$STATE_FILE"
  log "受控端 ${VERSION} 已上线"
else
  warn "登记受控端失败；下一轮 timer 会重试"
  exit 1
fi
