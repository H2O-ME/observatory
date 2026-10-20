#!/usr/bin/env bash
# 在服务器上产出受控端 exe 包，并登记到控制台（自动更新就从这里取包）。
#
#   sudo bash deploy/build-agent.sh                # 在服务器上用 Wine 构建
#   sudo bash deploy/build-agent.sh --from-release v0.2.1   # 直接取 CI 产物
#
# 两种方式都合法：
#
#   * `--from-release` 从 GitHub Release 下载由 Windows runner 构建好的 zip。
#     这是推荐路径：PyInstaller 的 Windows 引导器在原生 Windows 上最稳。
#   * 默认方式在本机用 Wine 跑 Windows 版 Python + PyInstaller。可行，但对
#     Wine 版本敏感，失败时会明确打印诊断而不是静默产出坏包。
#
# 无论哪种方式，产物都会：校验版本号 -> 放进数据卷 -> 调用 CLI 登记。
# 登记之后，旧版本受控端会在下一次心跳自动更新，不需要重新安装。

set -Eeuo pipefail
IFS=$'\n\t'

APP_DIR="${APP_DIR:-/opt/observatory}"
MODE="wine"
RELEASE_TAG=""
WINE_PREFIX="${WINE_PREFIX:-/opt/observatory/.wine-agent}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from-release) MODE="release"; RELEASE_TAG="${2:-}"; shift 2 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

log()  { printf '\033[36m[agent-build]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[agent-build]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[agent-build] %s\033[0m\n' "$*" >&2; exit 1; }

cd "$APP_DIR"
VERSION="$(cat VERSION)"
OUT_DIR="$APP_DIR/agent/dist"
mkdir -p "$OUT_DIR"

register_package() {
  local zip="$1"
  local name
  name="$(basename "$zip")"
  [[ "$name" == observatory-agent-*.zip ]] || die "包名必须形如 observatory-agent-<版本>.zip"
  log "登记 $name"
  docker cp "$zip" observatory-api:/data/packages/
  docker exec -w /srv observatory-api python -m observatory.cli package-add "/data/packages/$name"
  log "受控端将自动更新到 $(cat VERSION)"
}

build_with_release() {
  [[ -n "$RELEASE_TAG" ]] || die "--from-release 需要一个 tag，例如 --from-release v$VERSION"
  local url=""
  if command -v gh >/dev/null 2>&1; then
    log "用 gh 下载 release 资产 $RELEASE_TAG"
    gh release download "$RELEASE_TAG" --pattern 'observatory-agent-*.zip' --dir "$OUT_DIR" --clobber
  else
    url="https://github.com/$(git config --get remote.origin.url | sed -E 's#.*[:/]([^/]+/[^/.]+)(\.git)?$#\1#')/releases/download/$RELEASE_TAG/observatory-agent-$VERSION.zip"
    log "下载 $url"
    curl -fL --retry 3 -o "$OUT_DIR/observatory-agent-$VERSION.zip" "$url"
  fi
  local zip="$OUT_DIR/observatory-agent-$VERSION.zip"
  [[ -f "$zip" ]] || die "没有拿到 $zip"
  register_package "$zip"
}

build_with_wine() {
  command -v wine >/dev/null 2>&1 || die "没装 wine。可以改用 --from-release，或 apt-get install wine64"
  local python_exe="$WINE_PREFIX/drive_c/Python312/python.exe"
  if [[ ! -f "$python_exe" ]]; then
    log "初始化 Wine 前缀并安装 Windows 版 Python 3.12"
    export WINEPREFIX="$WINE_PREFIX"
    export WINEDEBUG=-all
    wineboot --init >/dev/null 2>&1 || true
    local installer="$WINE_PREFIX/python-3.12.7-amd64.exe"
    curl -fL --retry 3 -o "$installer" https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe
    wine "$installer" /quiet InstallAllUsers=0 TargetDir='C:\Python312' Include_pip=1 || true
    [[ -f "$python_exe" ]] || die "Windows 版 Python 安装失败。建议改用 --from-release。"
  fi

  export WINEPREFIX="$WINE_PREFIX"
  export WINEDEBUG=-all
  log "安装构建依赖（Wine 内）"
  wine "$python_exe" -m pip install --quiet --upgrade pip
  wine "$python_exe" -m pip install --quiet -r agent/requirements.txt pyinstaller
  wine "$python_exe" -m PyInstaller --version >/dev/null || die "PyInstaller 在 Wine 下不可用"

  log "运行 PyInstaller（版本 $VERSION）"
  wine "$python_exe" -m PyInstaller \
    --noconfirm --clean \
    --distpath "$(winepath -w "$APP_DIR/agent/build/dist")" \
    --workpath "$(winepath -w "$APP_DIR/agent/build/build")" \
    "$(winepath -w "$APP_DIR/agent/build/observatory-agent.spec")"

  local dist="$APP_DIR/agent/build/dist/observatory-agent"
  [[ -f "$dist/observatory-agent.exe" ]] || die "Wine 构建没有产出 exe"
  echo "$VERSION" > "$dist/VERSION"

  local zip="$OUT_DIR/observatory-agent-$VERSION.zip"
  rm -f "$zip"
  (cd "$dist" && zip -qr "$zip" .)
  log "生成 $zip ($(du -h "$zip" | cut -f1))"
  register_package "$zip"
}

case "$MODE" in
  release) build_with_release ;;
  *) build_with_wine ;;
esac

log "完成。当前服务器版本：$VERSION"
log "查看效果：控制台 -> 更新"