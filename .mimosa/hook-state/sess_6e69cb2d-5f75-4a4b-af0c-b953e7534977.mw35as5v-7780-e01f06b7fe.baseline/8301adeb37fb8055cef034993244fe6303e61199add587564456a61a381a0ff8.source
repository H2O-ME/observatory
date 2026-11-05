#!/usr/bin/env bash
# 在服务器上产出受控端 exe 包，并登记到控制台（自动更新就从这里取包）。
#
# 【推荐路径】不要用本脚本。受控端由 .github/workflows/ci-cd.yml 在 GitHub 的
# Windows runner 上原生构建（仅 agent/** 或 VERSION 变更时），产物挂固定
# Release `latest-agent`；上线时 deploy/pull-and-restart.sh 自行下载登记。
#
# 背景：PyInstaller 不做交叉编译，Linux 上的 Python 只能产出 Linux 二进制，
# 所以"在部署服务器上构建 Windows 包"必须靠 Wine 跑 Windows 版 Python。实测
# Debian 12 + wine 8.0 下 Python 官方 MSI 安装器无法静默安装（kernel32 加载
# 失败 / 安装器静默退出），要绕到 embeddable zip + 手动铺 wheel 才勉强可行，
# 代价远大于收益。本脚本因此只作为离线兜底保留：
#
#   sudo bash deploy/build-agent.sh                        # 在服务器上用 Wine 构建（兜底）
#   sudo bash deploy/build-agent.sh --from-release v0.2.8  # 取 CI 已发布的 Release 资产
#
# 注意：`--from-release` 在私有仓库下需要凭据（gh 已登录，或提供令牌）；
# 正常链路里服务器侧由 deploy/pull-and-restart.sh 用 .gh_read_token 自行下载登记。
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
VERSION="$(tr -d '[:space:]' < VERSION)"
OUT_DIR="$APP_DIR/agent/dist"
mkdir -p "$OUT_DIR"

register_package() {
  local zip="$1"
  local name expected actual
  name="$(basename "$zip")"
  [[ "$name" == observatory-agent-*.zip ]] || die "包名必须形如 observatory-agent-<版本>.zip"

  # 完整性：这个 zip 会被推给所有在线受控端自动执行，登记前必须校验。
  # --from-release 模式下与 Release 的 SHA256SUMS 比对；本地 Wine 构建没有
  # 外部参照，退化为本地重算 + 明示指纹（人工核对），绝不静默跳过。
  expected=""
  if [[ -f "$OUT_DIR/SHA256SUMS" ]]; then
    expected="$(awk -v n="$name" '{ f=$2; sub(/^\*/,"",f); if (f==n) { print $1; exit } }' "$OUT_DIR/SHA256SUMS")"
  fi
  actual="$(sha256sum "$zip" | cut -d' ' -f1)"
  if [[ -n "$expected" ]]; then
    [[ "$expected" == "$actual" ]] || die "sha256 与 Release SHA256SUMS 不匹配：清单=$expected 本地=$actual"
    log "sha256 与 Release SHA256SUMS 一致：$actual"
  else
    warn "无 SHA256SUMS 可比对。本地 sha256=$actual（请人工核对后再信任）"
  fi

  log "登记 $name"
  # 不能直接 docker cp 到 /data/packages：文件会归 root，而容器以 10001 运行，
  # package-add 复制时会 PermissionError（容器以 10001 运行，见 compose）。
  # 走 /data/tmp 中转 + 预清理同名的旧 root 属主文件。
  local remote_tmp="/data/tmp/${name}"
  docker cp "$zip" "observatory-api:${remote_tmp}" \
    || die "docker cp 失败（容器 observatory-api 是否在运行？）"
  docker exec observatory-api rm -f "/data/packages/${name}" >/dev/null 2>&1 || true
  if docker exec -w /srv observatory-api python -m observatory.cli package-add "${remote_tmp}" >/dev/null; then
    docker exec observatory-api rm -f "${remote_tmp}" >/dev/null 2>&1 || true
    log "已登记 ${name}；受控端将自动更新到 $(cat VERSION)"
  else
    docker exec observatory-api rm -f "${remote_tmp}" >/dev/null 2>&1 || true
    die "登记 ${name} 失败（查看 docker exec observatory-api python -m observatory.cli package-add 的输出）"
  fi
}

build_with_release() {
  [[ -n "$RELEASE_TAG" ]] || die "--from-release 需要一个 tag，例如 --from-release v$VERSION"
  if command -v gh >/dev/null 2>&1; then
    log "用 gh 下载 release 资产 $RELEASE_TAG"
    gh release download "$RELEASE_TAG" \
      --pattern 'observatory-agent-*.zip' --pattern 'SHA256SUMS' \
      --dir "$OUT_DIR" --clobber
  else
    local slug
    slug="$(git config --get remote.origin.url | sed -nE 's#.*[:/]([^/]+/[^/.]+)(\.git)?/?$#\1#p')"
    local base="https://github.com/${slug}/releases/download/$RELEASE_TAG"
    log "下载 $base/observatory-agent-$VERSION.zip（含 SHA256SUMS）"
    curl -fL --retry 3 -o "$OUT_DIR/observatory-agent-$VERSION.zip" "$base/observatory-agent-$VERSION.zip"
    curl -fL --retry 3 -o "$OUT_DIR/SHA256SUMS" "$base/SHA256SUMS" \
      || die "Release 缺少 SHA256SUMS 资产；拒绝登记未校验的二进制"
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