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
# 分支由 .github/workflows/build.yml 的 `agent-windows` job 维护，内容固定为：
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

# tr 是防御性的：VERSION 一旦带 CRLF，$(cat VERSION) 会得到 "0.2.8\r"，
# 与分支里的 "0.2.8" 永远不相等（见 .gitattributes 的说明）。
VERSION="$(tr -d '[:space:]' < VERSION)"
NAME="observatory-agent-${VERSION}.zip"
OUT_DIR="$APP_DIR/agent/dist"
mkdir -p "$OUT_DIR"

log "当前仓库版本：$VERSION"

# 拉取策略刻意保持保守，因为 agent-dist 是 CI 每次构建都会强推的分支：
#   * 用完整的 refs/heads/ 路径，避免短名歧义；
#   * 不加 --prune —— 它会在拉取与强推撞车的瞬间把本地跟踪引用删掉，报出
#     「(none) -> origin/agent-dist」，看起来像分支不存在，实际只是竞态；
#   * 失败重试几次，CI 刚跑完/正在跑时网络抖动很常见。
FETCH_OK=0
for attempt in 1 2 3 4 5; do
  log "拉取 ${REMOTE}/${BRANCH}（第 ${attempt} 次）"
  if git fetch --force --no-tags "$REMOTE" "+refs/heads/${BRANCH}:${TRACKING}" 2>&1; then
    if git rev-parse --verify --quiet "${TRACKING}" >/dev/null; then
      FETCH_OK=1
      break
    fi
  fi
  sleep 6
done
[[ "$FETCH_OK" == "1" ]] \
  || die "拉取 ${REMOTE}/${BRANCH} 失败（CI 可能还没跑完，或该分支尚未创建）"
log "分支提交：$(git rev-parse --short "${TRACKING}")"

REMOTE_VERSION="$(git show "${TRACKING}:VERSION" 2>/dev/null | tr -d '[:space:]' || true)"
log "分支里的版本：${REMOTE_VERSION:-未知}"
[[ "$REMOTE_VERSION" == "$VERSION" ]] \
  || die "分支里是 ${REMOTE_VERSION:-未知}，本地 VERSION 是 $VERSION。先部署代码（deploy/update.sh），等 CI 产出同版本包后再取"

# 正常情况下文件名就是 observatory-agent-<VERSION>.zip；如果 CI 那侧因为任何
# 原因写成了别的名字，退回到"分支里唯一那个 zip"，而不是直接失败。
if ! git cat-file -e "${TRACKING}:${NAME}" 2>/dev/null; then
  ALT="$(git ls-tree --name-only "${TRACKING}" | grep -E '^observatory-agent-.*\.zip$' | head -1 || true)"
  [[ -n "$ALT" ]] || die "分支 ${TRACKING} 里没有任何 observatory-agent-*.zip"
  warn "分支里没有 ${NAME}，改用 ${ALT}"
  NAME="$ALT"
fi

git show "${TRACKING}:${NAME}" > "${OUT_DIR}/${NAME}" \
  || die "无法从分支导出 ${NAME}"

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

log "完成。低于 ${VERSION} 的在线设备会在下次心跳拿到更新（受控端按版本比较自行更新）。"
log "查看：控制台 -> 更新"