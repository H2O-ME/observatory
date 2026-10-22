#!/usr/bin/env bash
# 服务器端更新：拉取提交 -> 重新构建 -> 重启 -> 登记新的受控端安装包。
#
#   sudo bash deploy/update.sh
#
# 这是日常唯一需要执行的命令。受控端不需要重新安装：新版本上传后，
# 旧版本设备会在下一次心跳时自动下载并替换。

set -Eeuo pipefail
IFS=$'\n\t'

APP_DIR="${APP_DIR:-/opt/observatory}"
BRANCH="${BRANCH:-main}"

log()  { printf '\033[36m[update]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[update]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[update] %s\033[0m\n' "$*" >&2; exit 1; }

cd "$APP_DIR" || die "找不到 $APP_DIR"

[[ -d .git ]] || die "$APP_DIR 不是 git 仓库"

log "当前版本：$(cat VERSION 2>/dev/null || echo 未知)"

if [[ -n "$(git status --porcelain)" ]]; then
  warn "工作区有未提交改动，先 stash 以保证 pull 干净"
  git stash push -u -m "update.sh $(date -Is)" >/dev/null
fi

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_HASH_BEFORE="$(sha256sum "$SELF" | cut -d' ' -f1)"

log "拉取 origin/$BRANCH"
git fetch --prune origin
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

# A pull can replace this very file. Bash keeps executing the old text from
# memory, so the freshly pulled fixes would silently not apply — re-exec once.
SCRIPT_HASH_AFTER="$(sha256sum "$SELF" | cut -d' ' -f1)"
if [[ "$SCRIPT_HASH_BEFORE" != "$SCRIPT_HASH_AFTER" && -z "${OBS_UPDATE_REEXEC:-}" ]]; then
  log "部署脚本自身已更新，重新执行一次"
  OBS_UPDATE_REEXEC=1 exec bash "$SELF" "$@"
fi

VERSION="$(cat VERSION)"
log "目标版本：$VERSION"

COMPOSE=(docker compose -f deploy/docker-compose.yml --env-file server/.env)

log "重新构建镜像（会同时重建控制台前端）"
"${COMPOSE[@]}" build --pull
"${COMPOSE[@]}" up -d

log "等待服务就绪"
ready=0
for _ in $(seq 1 60); do
  if curl -fsSk https://127.0.0.1:8443/api/v1/health >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
[[ "$ready" == "1" ]] || die "服务在 120 秒内没有就绪，请查看 docker compose logs"

# 受控端由 GitHub Actions 在 Windows runner 上构建（PyInstaller 不能交叉编译），
# 产物由 CI 推到 `agent-dist` 分支。本地没有该版本的包时，从那个分支取回来并登记。
if [[ ! -f "agent/dist/observatory-agent-${VERSION}.zip" ]]; then
  if [[ -x deploy/fetch-agent-package.sh || -f deploy/fetch-agent-package.sh ]]; then
    log "本地没有 ${VERSION} 的受控端包，尝试从 CI 的 agent-dist 分支获取"
    if ! bash deploy/fetch-agent-package.sh; then
      warn "获取受控端包失败（CI 可能还没跑完）。受控端将保持当前版本；稍后可单独执行："
      warn "  sudo bash deploy/fetch-agent-package.sh"
    fi
  fi
fi

# 如果本次提交带来了新的受控端安装包，自动登记（可被设备自动更新拉取）。
shopt -s nullglob
for zip in agent/dist/observatory-agent-*.zip; do
  name="$(basename "$zip")"
  log "登记受控端安装包：$name"
  docker cp "$zip" observatory-api:/data/packages/
  if docker exec -w /srv observatory-api python -m observatory.cli package-add "/data/packages/$name" >/dev/null; then
    log "已登记 $name"
  else
    warn "登记 $name 失败（可能已经存在）"
  fi
done
shopt -u nullglob

log "当前运行版本：$(curl -fsSk https://127.0.0.1:8443/api/v1/version || echo 未知)"
log "完成。受控端会在下一次心跳时看到新版本并自行更新。"