#!/usr/bin/env bash
# 从 GitHub 的 `agent-dist` 分支取受控端安装包并登记到控制台。
#
#   sudo bash deploy/fetch-agent-package.sh
#
# 为什么是分支而不是 Release 资产：仓库是私有的，Release 资产需要 PAT 才能下载；
# 而部署服务器本来就有该仓库的 git 访问权限，因此 CI 构建完把 zip 覆盖式推到一个
# 单提交分支，服务器 `git fetch` 即可。全程零新增凭据，也不需要在客户端上传 19MB
# 的二进制。
#
# 分支由 .github/workflows/build.yml 的 `agent` job 维护，内容固定为：
#   observatory-agent-<VERSION>.zip
#   VERSION
#   SHA256SUMS
set -Eeuo pipefail
IFS=$'\n\t'

APP_DIR="${APP_DIR:-/opt/observatory}"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-agent-dist}"
TRACKING="refs/remotes/${REMOTE}/${BRANCH}"

log()  { printf '\033[36m[agent-fetch]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[agent-fetch]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[agent-fetch] %s\033[0m\n' "$*" >&2; exit 1; }

cd "$APP_DIR" || die "找不到 $APP_DIR"
[[ -d .git ]] || die "$APP_DIR 不是 git 仓库"

VERSION="$(cat VERSION)"
NAME="observatory-agent-${VERSION}.zip"
OUT_DIR="$APP_DIR/agent/dist"
mkdir -p "$OUT_DIR"

log "当前仓库版本：$VERSION"
log "拉取 ${REMOTE}/${BRANCH}"
git fetch --force --prune "$REMOTE" "+${BRANCH}:${TRACKING}" \
  || die "拉取 ${REMOTE}/${BRANCH} 失败（CI 可能还没跑完，或该分支尚未创建）"

REMOTE_VERSION="$(git show "${TRACKING}:VERSION" 2>/dev/null | tr -d '[:space:]' || true)"
log "分支里的版本：${REMOTE_VERSION:-未知}"
[[ "$REMOTE_VERSION" == "$VERSION" ]] \
  || die "分支里是 ${REMOTE_VERSION:-未知}，本地 VERSION 是 $VERSION。先部署代码（deploy/update.sh），等 CI 产出同版本包后再取"

git show "${TRACKING}:${NAME}" > "${OUT_DIR}/${NAME}" \
  || die "分支里没有 ${NAME}"

# GNU sha256sum 在文本模式输出 `<hash>  <file>`，但在 Git Bash（Windows runner）
# 上会退化成二进制模式 `<hash> *<file>`。两种都接受，否则校验会被静默跳过。
EXPECTED="$(git show "${TRACKING}:SHA256SUMS" 2>/dev/null \
  | awk -v n="$NAME" '{ f=$2; sub(/^\*/,"",f); if (f==n) { print $1; exit } }' || true)"
ACTUAL="$(sha256sum "${OUT_DIR}/${NAME}" | cut -d' ' -f1)"
if [[ -n "$EXPECTED" ]]; then
  [[ "$EXPECTED" == "$ACTUAL" ]] || die "sha256 不匹配：分支=$EXPECTED 实际=$ACTUAL"
  log "sha256 校验通过：$ACTUAL"
else
  warn "分支里没有 SHA256SUMS，跳过校验（实际 sha256=$ACTUAL）"
fi

log "大小：$(du -h "${OUT_DIR}/${NAME}" | cut -f1)"

log "登记到控制台"
docker cp "${OUT_DIR}/${NAME}" observatory-api:/data/packages/ \
  || die "docker cp 失败（容器 observatory-api 是否在运行？）"
if docker exec -w /srv observatory-api python -m observatory.cli package-add "/data/packages/${NAME}" >/dev/null; then
  log "已登记 ${NAME}"
else
  warn "登记 ${NAME} 失败（可能已存在同版本）"
fi

log "完成。受控端 <= ${VERSION} 且 >= 0.2.5 的在线设备会在下次心跳自动更新。"
log "查看：控制台 -> 更新"