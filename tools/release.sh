#!/usr/bin/env bash
# 一键发布（本机入口，0.3.7 起 CI 已删除、全本地）：版本 bump → 本地测试 →
# 提交 → 打 tag → 推送 → SSH 触发服务器部署 → 盯到结束。
#
#   tools/release.sh                # patch +1（0.3.7 → 0.3.8）
#   tools/release.sh minor          # minor +1
#   tools/release.sh 0.4.0          # 显式版本
#   tools/release.sh patch -m "修复xxx"
#   tools/release.sh patch --no-watch   # 推完就返回，不等 CI/部署
#
# 凭据：推送用 GitHub PAT（workflow 权限），来自环境变量 SHENMI_GITHUB_TOKEN
# 或 .localdata/github-token 文件（已 gitignore，不落仓库）。
# 部署盯守用 SSH（OBS_SSH_KEY/HOST/PORT/USER 可覆盖，默认指向生产机）。
set -Eeuo pipefail
IFS=$'\n\t'

cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)"
REPO_SLUG="H2O-ME/observatory"
SSH_KEY="${OBS_SSH_KEY:-D:/SSH密钥/REDACTED}"
SSH_HOST="${OBS_SSH_HOST:-<server-ip>}"
SSH_PORT="${OBS_SSH_PORT:-2009}"
SSH_USER="${OBS_SSH_USER:-root}"
CI_TIMEOUT_S="${CI_TIMEOUT_S:-4500}"

log()  { printf '\033[36m[release]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[release]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[release] %s\033[0m\n' "$*" >&2; exit 1; }

usage() { sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

BUMP="patch"
MSG=""
WATCH=1
RUN_TESTS=1
BUILD_AGENT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    patch|minor|major) BUMP="$1"; shift ;;
    [0-9]*.[0-9]*.[0-9]*) BUMP="$1"; shift ;;
    -m|--message) MSG="${2:-}"; shift 2 ;;
    --no-watch) WATCH=0; shift ;;
    --no-test) RUN_TESTS=0; shift ;;
    --agent) BUILD_AGENT=1; shift ;;
    -h|--help) usage ;;
    *) die "未知参数：$1（--help 看用法）" ;;
  esac
done

# ---- 预检 ----------------------------------------------------------------
command -v git >/dev/null || die "没有 git"
git diff --quiet || die "工作树有未提交改动，先 commit/stash"
git diff --cached --quiet || die "暂存区非空，先 commit"
branch="$(git rev-parse --abbrev-ref HEAD)"
[[ "$branch" == "main" ]] || die "当前在 $branch 分支；发布只允许从 main"
log "同步远端…"
fetch_ok=0
for i in 1 2 3 4 5; do
  if git fetch --quiet origin 2>/dev/null; then fetch_ok=1; break; fi
  warn "fetch 失败（第 $i/5 次，代理抖动？）5 秒后重试"
  sleep 5
done
[[ "$fetch_ok" == "1" ]] || die "git fetch 连续失败；检查网络/代理后重跑"
local_head="$(git rev-parse HEAD)"
remote_head="$(git rev-parse origin/main)"
behind="$(git rev-list --count HEAD..origin/main)"
[[ "$behind" == "0" ]] || die "本地落后 origin/main $behind 个提交；先 pull --rebase（领先没关系，本脚本会连 tag 一起推）"

# ---- 本地测试（CI 不再跑重型套件——0.3.7 起测试在本机执行）-----------------
if [[ "$RUN_TESTS" == "1" ]]; then
  log "本地测试：pytest 全套（--no-test 跳过）…"
  python -m pytest tests/unit tests/integration tests/contract tests/acceptance \
    -q -m "not perf" || die "本地测试未过；未推送任何内容"
  log "本地测试：WebUI 类型检查 + 生产构建…"
  (cd webui && npm run typecheck && npm run build >/dev/null) \
    || die "WebUI 本地检查未过；未推送任何内容"
  log "本地测试全绿"
fi

# ---- 版本 bump ------------------------------------------------------------
log "bump 版本（$BUMP）…"
if [[ "$BUMP" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  python tools/set_version.py "$BUMP"
else
  # set_version.py 只认 --bump 和显式版本号；minor/major 在这里算好再传
  cur="$(tr -d '[:space:]' < VERSION)"
  IFS=. read -r ma mi pa <<<"$cur"
  case "$BUMP" in
    patch) python tools/set_version.py "$ma.$mi.$((pa + 1))" ;;
    minor) python tools/set_version.py "$ma.$((mi + 1)).0" ;;
    major) python tools/set_version.py "$((ma + 1)).0.0" ;;
  esac
fi
python tools/set_version.py --check || die "版本副本不一致（set_version --check 失败）"
VER="$(tr -d '[:space:]' < VERSION)"
TAG="v$VER"

# tag 不得已存在于远端
if git ls-remote --exit-code --tags origin "refs/tags/$TAG" >/dev/null 2>&1; then
  die "$TAG 已存在于远端；检查 VERSION 是否漏 bump"
fi

git add -u
git commit -q -m "chore(release): $TAG${MSG:+ $MSG}"
git tag -a "$TAG" -m "$TAG${MSG:+: $MSG}"
log "已提交并打 tag：$TAG"
# 盯 CI 用的 head_sha 必须是发布提交本身（bump commit），不是进脚本时的 HEAD
local_head="$(git rev-parse HEAD)"

# ---- 推送 -----------------------------------------------------------------
TOKEN="${SHENMI_GITHUB_TOKEN:-}"
if [[ -z "$TOKEN" && -f .localdata/github-token ]]; then
  TOKEN="$(tr -d '[:space:]' < .localdata/github-token)"
fi
[[ -n "$TOKEN" ]] || die "没有发布令牌：设 SHENMI_GITHUB_TOKEN 或写 .localdata/github-token"

push_url="https://H2O-ME:${TOKEN}@github.com/${REPO_SLUG}.git"
push_ok=0
for i in 1 2 3; do
  if git push "$push_url" "main" "$TAG" >/tmp/release-push.log 2>&1; then
    push_ok=1
    grep -v "^remote:" /tmp/release-push.log || true
    break
  fi
  grep -v "^remote:" /tmp/release-push.log >&2 || true
  warn "推送失败（第 $i/3 次）10 秒后重试"
  sleep 10
done
[[ "$push_ok" == "1" ]] || die "推送连续失败（令牌过期/无 workflow 权限/网络）"
unset push_url
log "已推送 main + $TAG → GitHub"

# ---- 受控端 exe：本地构建（--agent 时），scp 到服务器待登记 -----------------
if [[ "$BUILD_AGENT" == "1" ]]; then
  log "本地构建受控端 exe（PyInstaller）…"
  pwsh -NoProfile -File agent/build/build.ps1 || die "受控端本地构建失败"
  zip="agent/dist/observatory-agent-$VER.zip"
  [[ -f "$zip" ]] || die "构建产物缺失：$zip"
  log "上传受控端包到服务器（$(du -h "$zip" | cut -f1)）…"
  for i in 1 2 3; do
    if scp -q -o IdentitiesOnly=yes -o ConnectTimeout=15 -i "$SSH_KEY" -P "$SSH_PORT" \
      "$zip" "$SSH_USER@$SSH_HOST:/opt/observatory/agent/dist/"; then break; fi
    warn "scp 失败（第 $i/3 次）"; sleep 8
  done
fi

[[ "$WATCH" == "1" ]] || { log "--no-watch：到此为止。手动部署：ssh obs -- bash /opt/observatory/deploy/deploy.sh release $TAG"; exit 0; }

# ---- 触发服务器部署（GitHub Actions 已删除；服务器直接拉 tag 部署）---------
log "SSH 触发生产部署：deploy.sh release $TAG"
for i in 1 2 3; do
  if ssh -o IdentitiesOnly=yes -o ConnectTimeout=15 -i "$SSH_KEY" -p "$SSH_PORT" \
    "$SSH_USER@$SSH_HOST" "bash /opt/observatory/deploy/deploy.sh release $TAG" 2>&1; then
    break
  fi
  warn "SSH 点火失败（第 $i/3 次，代理抖动？）8 秒后重试"
  sleep 8
done

# 盯服务器状态文件直到 done（含 docker build，给 35 分钟）
deadline=$(( $(date +%s) + CI_TIMEOUT_S ))
while (( $(date +%s) < deadline )); do
  sleep 25
  st="$(ssh -o IdentitiesOnly=yes -o ConnectTimeout=15 -i "$SSH_KEY" -p "$SSH_PORT" \
    "$SSH_USER@$SSH_HOST" "cat /opt/observatory/deploy.status" 2>/dev/null | tr '\n' ' ')"
  echo "  [$(date +%H:%M:%S)] $st"
  if grep -q "result=success" <<<"$st"; then
    log "服务器部署成功"
    break
  fi
  if grep -q "result=failed" <<<"$st"; then
    ssh -o IdentitiesOnly=yes -o ConnectTimeout=15 -i "$SSH_KEY" -p "$SSH_PORT" \
      "$SSH_USER@$SSH_HOST" "bash /opt/observatory/deploy/deploy.sh logs 80" >&2 || true
    die "服务器部署失败（已自动回滚；日志见上）"
  fi
done

# ---- 终验 -----------------------------------------------------------------
log "生产终验…"
health="$(curl -fsSk --max-time 10 --resolve control.tianhw.top:443:<server-ip> \
  "https://control.tianhw.top/api/v1/health" 2>/dev/null || true)"
if [[ -n "$health" ]]; then
  log "生产 health：$health"
  grep -q "\"version\":\"$VER\"" <<<"$health" \
    && log "✅ 发布完成：生产已运行 $VER"
  warn "生产版本与 $VER 不一致？ssh 查 deploy.sh status"
else
  warn "公网 health 探测失败（CDN 抖动？）；用 ssh $SSH_USER@$SSH_HOST bash deploy/deploy.sh status 确认"
fi
