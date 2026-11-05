#!/usr/bin/env bash
# 极简发布：bump 版本 → 打 tag → 推送。云端 CI 自动构建镜像并推送香港上线。
#   tools/release.sh            # patch +1
#   tools/release.sh minor      # minor +1
#   tools/release.sh 0.4.0      # 显式版本
# 本地想跑测试：python -m pytest tests/unit tests/integration tests/contract tests/acceptance -m "not perf"
set -Eeuo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)"

git diff --quiet || { echo "工作树有未提交改动，先 commit/stash" >&2; exit 1; }
[[ "$(git rev-parse --abbrev-ref HEAD)" == "main" ]] || { echo "只允许从 main 发布" >&2; exit 1; }
git fetch --quiet origin 2>/dev/null || true
[[ "$(git rev-list --count HEAD..origin/main)" == "0" ]] || { echo "本地落后 origin/main，先 pull --rebase" >&2; exit 1; }

BUMP="${1:-patch}"
case "$BUMP" in
  patch|minor|major)
    cur="$(tr -d '[:space:]' < VERSION)"
    IFS=. read -r ma mi pa <<<"$cur"
    case "$BUMP" in
      patch) v="$ma.$mi.$((pa + 1))" ;;
      minor) v="$ma.$((mi + 1)).0" ;;
      major) v="$((ma + 1)).0.0" ;;
    esac
    ;;
  [0-9]*.[0-9]*.[0-9]*) v="$BUMP" ;;
  *) echo "用法: tools/release.sh [patch|minor|major|X.Y.Z]" >&2; exit 1 ;;
esac

python tools/set_version.py "$v" >/dev/null
python tools/set_version.py --check >/dev/null
VER="$(tr -d '[:space:]' < VERSION)"
TAG="v$VER"

git add -u
git commit -q -m "chore(release): $TAG"
git tag -a "$TAG" -m "$TAG"

TOKEN="${SHENMI_GITHUB_TOKEN:-}"
[[ -z "$TOKEN" && -f .localdata/github-token ]] && TOKEN="$(tr -d '[:space:]' < .localdata/github-token)"
[[ -n "$TOKEN" ]] || { echo "缺发布令牌：SHENMI_GITHUB_TOKEN 或 .localdata/github-token" >&2; exit 1; }

git push "https://H2O-ME:${TOKEN}@github.com/H2O-ME/observatory.git" main "$TAG"
echo "已推送 $TAG → 云端构建镜像并自动上线（进度看仓库 Actions 页，约 3-5 分钟）"
