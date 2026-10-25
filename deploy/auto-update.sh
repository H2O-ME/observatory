#!/usr/bin/env bash
# 服务器端自动更新（轮询机）——把「本地 push → GitHub → 服务器」串成一条链路。
#
#   sudo bash deploy/auto-update.sh            # 单次检查（cron/systemd 也能直接跑）
#   sudo bash deploy/install-auto-update.sh    # 安装并启用 2 分钟一次的 timer
#
# 行为：
#   1. fetch GitHub 的 main 与 agent-dist（服务器用只读 deploy key，不存 PAT）；
#   2. main 有新提交时先校验 GitHub Actions 结论（check-runs 全绿才部署）——
#      CI 红灯的提交绝不自动上线；私库需要在环境里给 OBS_GITHUB_TOKEN
#      （只读 contents:read 即可，公共仓库匿名可查）；
#   3. 同一提交连续 3 轮失败（CI 红或部署失败）即熔断跳过，直到 origin 前进；
#   4. 本地已有 v<VERSION> tag 时等待 CI 产出同版本 agent-dist；
#      分支 VERSION 对上后调用 deploy/fetch-agent-package.sh 登记安装包；
#   5. 只用 flock 防止自己重叠；update.sh 自身还有升级锁，不会并发重建容器。
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
# 私有仓库查 check-runs 需要只读 token；公共仓库可省。
GITHUB_TOKEN="${OBS_GITHUB_TOKEN:-}"
CIRCUIT_MAX="${CIRCUIT_MAX:-3}"

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

# ---------------------------------------------------------------------------
# CI 结论校验 + 失败熔断
# ---------------------------------------------------------------------------
REPO_SLUG="$(git remote get-url origin 2>/dev/null | sed -nE 's#.*[:/]([^/:]+/[^/]+?)(\.git)?/?$#\1#p')"
[[ -n "${REPO_SLUG}" ]] || REPO_SLUG="H2O-ME/observatory"
FAILED_SHA_FILE="${STATE_DIR}/failed.sha"
FAILED_COUNT_FILE="${STATE_DIR}/failed.count"

# 记一次失败；达到 CIRCUIT_MAX 后熔断（本轮起跳过该 SHA，直到 origin 前进）。
# 返回 0=本轮应跳过（已熔断），1=仍允许继续重试。
circuit_break() {
  local sha="$1" count
  count="$(( $(cat "$FAILED_COUNT_FILE" 2>/dev/null || echo 0) + 1 ))"
  printf '%s\n' "$sha" > "$FAILED_SHA_FILE"
  printf '%s\n' "$count" > "$FAILED_COUNT_FILE"
  if (( count >= CIRCUIT_MAX )); then
    warn "同一提交 ${sha:0:12} 已连续失败 ${count} 轮，熔断：跳过部署直到 origin 有新提交（修好后 push 即恢复）"
    return 0
  fi
  warn "失败计数 ${count}/${CIRCUIT_MAX}（${sha:0:12}）"
  return 1
}

circuit_clear() { rm -f "$FAILED_SHA_FILE" "$FAILED_COUNT_FILE"; }

# 输出: 0=全绿可部署; 1=进行中/暂不可判（下轮再看）; 2=结论为失败。
# 依赖 python3 解析 JSON（服务器镜像栈本就有 python）。
ci_verdict() {
  local sha="$1" auth_header=() http_code body
  [[ -n "$GITHUB_TOKEN" ]] && auth_header=(-H "Authorization: Bearer ${GITHUB_TOKEN}")
  body="$(curl -sS --max-time 20 -w $'\n%{http_code}' \
    -H "Accept: application/vnd.github+json" "${auth_header[@]}" \
    "https://api.github.com/repos/${REPO_SLUG}/commits/${sha}/check-runs" || true)"
  http_code="${body##*$'\n'}"
  if [[ "$http_code" != "200" ]]; then
    warn "无法读取 ${REPO_SLUG}@${sha:0:12} 的 CI 结论（HTTP ${http_code}）。私有仓库请在服务器的 OBS_GITHUB_TOKEN 提供只读 token。本轮不部署。"
    return 1
  fi
  printf '%s\n' "${body%$'\n'*}" | python3 -c '
import json, sys
data = json.load(sys.stdin)
runs = data.get("check_runs") or []
if not runs:
    print("NO_RUNS"); raise SystemExit(0)
states = {r.get("status") for r in runs}
if any(s != "completed" for s in states):
    print("PENDING"); raise SystemExit(0)
bad = [r.get("name") for r in runs if r.get("conclusion") not in ("success", "skipped", "neutral")]
if bad:
    print("FAILED " + ",".join(str(b) for b in bad)); raise SystemExit(0)
print("GREEN")
'
}

# main 与 tag：失败立即退出（更新链路不可用要能被 timer 发现）。
log "拉取 origin/$BRANCH"
git fetch --prune origin "+refs/heads/${BRANCH}:refs/remotes/origin/${BRANCH}" --tags
# agent-dist 可能尚未由 CI 创建；缺失只告警，不影响代码更新。
git fetch --force --no-tags origin \
  "+refs/heads/${AGENT_BRANCH}:refs/remotes/origin/${AGENT_BRANCH}" \
  || warn "agent-dist 暂不可用（CI 尚未产出或分支不存在）"

LOCAL_SHA="$(git rev-parse HEAD)"
REMOTE_SHA="$(git rev-parse "refs/remotes/origin/${BRANCH}")"

# 熔断中的 SHA：只要 origin 还停在那里就什么都不做。
if [[ -f "$FAILED_SHA_FILE" && "$(cat "$FAILED_SHA_FILE")" == "$REMOTE_SHA" ]]; then
  count="$(cat "$FAILED_COUNT_FILE" 2>/dev/null || echo 0)"
  if (( count >= CIRCUIT_MAX )); then
    log "origin 仍停在已熔断的 ${REMOTE_SHA:0:12}，跳过本轮"
    exit 0
  fi
fi

if [[ "$LOCAL_SHA" == "$REMOTE_SHA" ]]; then
  log "代码已是最新（$(git rev-parse --short HEAD)）"
else
  if ! git diff --quiet || ! git diff --cached --quiet; then
    warn "工作区有未提交改动，跳过自动更新（先手工处理或清理）"
    exit 0
  fi

  # 部署前先看 CI：红灯/无结论的提交绝不上生产（P0：此前只比对 SHA）。
  verdict="$(ci_verdict "$REMOTE_SHA")"
  case "$verdict" in
    GREEN)
      circuit_clear
      log "CI 全绿（${REMOTE_SHA:0:12}），允许部署"
      ;;
    PENDING|NO_RUNS|"")
      log "CI 尚未完成（${verdict:-无响应}），等待下一轮"
      exit 1
      ;;
    FAILED*)
      warn "CI 红灯，拒绝部署：${verdict#FAILED }"
      circuit_break "$REMOTE_SHA" || true
      exit 1
      ;;
    *)
      warn "CI 状态无法判定（${verdict}），本轮不部署"
      exit 1
      ;;
  esac

  CHANGED="$(git diff --name-only "$LOCAL_SHA" "$REMOTE_SHA")"
  NEED_BUILD=0
  while IFS= read -r file; do
    [[ -z "$file" ]] && continue
    case "$file" in
      deploy/docker-compose.yml)
        NEED_BUILD=1 ;;
      docs/*|README.md|.github/*|tests/*|tools/*|agent/*|\
      deploy/*|webui/DESIGN_SYSTEM.md|.gitignore|.dockerignore)
        ;;
      *)
        NEED_BUILD=1 ;;
    esac
  done <<< "$CHANGED"

  if [[ "$NEED_BUILD" -eq 0 ]]; then
    log "新提交只涉及文档/CI/部署脚本/受控端源码，快进代码（不重建镜像）"
    git merge --ff-only "$REMOTE_SHA"
    LOCAL_SHA="$(git rev-parse HEAD)"
    log "已快进到 $(git rev-parse --short HEAD)"
  else
    log "发现运行时新提交 ${LOCAL_SHA:0:12} → ${REMOTE_SHA:0:12}，开始部署"
    # 自动链路不保留 200MB+ 的升级前数据卷备份（用户明确不需要）；
    # 手工执行 deploy/update.sh 时仍默认备份。
    if ! SKIP_BACKUP=1 bash deploy/update.sh; then
      warn "update.sh 失败（已由其内部回滚）"
      circuit_break "$REMOTE_SHA" || true
      exit 1
    fi
    circuit_clear
    LOCAL_SHA="$(git rev-parse HEAD)"
    log "部署完成，当前 commit $(git rev-parse --short HEAD)"
  fi
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
