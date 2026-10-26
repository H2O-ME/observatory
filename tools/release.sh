#!/usr/bin/env bash
# 一键发布（本机入口）：版本 bump → 提交 → 打 tag → 推送 → 盯 CI → 盯生产部署。
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
while [[ $# -gt 0 ]]; do
  case "$1" in
    patch|minor|major) BUMP="$1"; shift ;;
    [0-9]*.[0-9]*.[0-9]*) BUMP="$1"; shift ;;
    -m|--message) MSG="${2:-}"; shift 2 ;;
    --no-watch) WATCH=0; shift ;;
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

# ---- 版本 bump ------------------------------------------------------------
log "bump 版本（$BUMP）…"
if [[ "$BUMP" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  python tools/set_version.py "$BUMP"
else
  python tools/set_version.py "--$BUMP"
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
log "已推送 main + $TAG → GitHub；CI 开始跑全门禁"

[[ "$WATCH" == "1" ]] || { log "--no-watch：到此为止。部署结果看 Actions 的 deploy job。"; exit 0; }

# ---- 盯 CI（含 deploy job，一次 run 的 conclusion 覆盖全部 job）------------
log "盯 CI run（head=$local_head，超时 $((CI_TIMEOUT_S / 60)) 分钟）…"
deadline=$(( $(date +%s) + CI_TIMEOUT_S ))
run_id=""
while (( $(date +%s) < deadline )); do
  sleep 20
  resp="$(curl -fsS --max-time 20 \
    -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/${REPO_SLUG}/actions/runs?head_sha=${local_head}" 2>/dev/null || true)"
  run_id="$(python -c "
import json,sys
try:
    d=json.loads(sys.stdin.read() or '{}')
    runs=[r for r in d.get('workflow_runs',[]) if r.get('name')=='build' or 'build' in (r.get('path') or '')]
    print(runs[0]['id'] if runs else '')
except Exception:
    print('')
" <<<"$resp")"
  [[ -n "$run_id" ]] && break
done
[[ -n "$run_id" ]] || die "找不到 CI run（tag 是否推上去了？Actions 是否被禁用？）"

conclusion=""
while (( $(date +%s) < deadline )); do
  line="$(curl -fsS --max-time 20 \
    -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/${REPO_SLUG}/actions/runs/${run_id}" 2>/dev/null \
    | python -c "import json,sys; d=json.load(sys.stdin); print(d.get('status',''), d.get('conclusion',''))" 2>/dev/null || echo '')"
  status="${line%% *}"
  echo "  [$(date +%H:%M:%S)] CI $line"
  if [[ "$status" == "completed" ]]; then
    conclusion="${line#* }"
    break
  fi
  sleep 30
done
if [[ "$conclusion" != "success" ]]; then
  die "CI 结论：${conclusion:-unknown}（run $run_id）——生产未动，去 Actions 看红灯 job"
fi
log "CI 全绿（含 deploy job 的服务器部署）"

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
