#!/usr/bin/env bash
# GHCR 拉取式部署（0.3.7 末重构）：镜像由 GitHub Actions 构建推 ghcr.io，
# 服务器只负责 拉镜像 → 切容器 → 健康验证 → 失败切回旧镜像。
# 受控端 exe 有更新时顺路从 GitHub Release 拉取并登记。
set -Eeuo pipefail
IFS=$'\n\t'
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:$PATH}"

cd /opt/observatory
SHA="${1:?usage: pull-and-restart.sh <sha> [agent_changed]}"
AGENT_CHANGED="${2:-false}"
compose() { docker compose -f deploy/docker-compose.yml --env-file server/.env "$@"; }

# 1) 受控端有更新：从固定 Release（latest-agent）拉 zip 并登记（服务器→GitHub 满速）
if [[ "$AGENT_CHANGED" == "true" ]]; then
  tok="$(cat /opt/observatory/.gh_read_token)"
  rel="$(curl -fsSL --max-time 30 -H "Authorization: Bearer $tok" \
    "https://api.github.com/repos/H2O-ME/observatory/releases/tags/latest-agent")"
  asset="$(python3 -c "
import json,sys
d=json.loads(sys.stdin.read())
a=[x for x in d.get('assets',[]) if x['name'].endswith('.zip')][0]
print(a['name'], a['url'])
" <<<"$rel")"
  name="${asset%% *}"
  url="${asset##* }"
  echo "pulling agent package: $name"
  mkdir -p agent/dist
  curl -fsSL --max-time 300 -H "Authorization: Bearer $tok" -H "Accept: application/octet-stream" \
    "$url" -o "agent/dist/$name"
  # 登记进控制台（经 /data/tmp 中转避免 root 属主覆盖失败）
  docker cp "agent/dist/$name" "observatory-api:/data/tmp/$name"
  docker exec observatory-api rm -f "/data/packages/$name" >/dev/null 2>&1 || true
  docker exec -w /srv observatory-api python -m observatory.cli package-add "/data/tmp/$name"
  docker exec observatory-api rm -f "/data/tmp/$name" >/dev/null 2>&1 || true
fi

# 2) 记录旧镜像（秒级回滚锚点）+ 换库前 DB 快照（在线 SQLite backup，秒级）
OLD_IMAGE="$(docker inspect --format '{{.Config.Image}}' observatory-api 2>/dev/null || true)"
# `docker exec -i` 是必需的：没有 -i 时 stdin 不接管道，heredoc 根本进不去，
# 快照会静默失败（0.3.7 P4 部署时实际踩到，被下面的 || 吞掉）。
if ! docker exec -i observatory-api python - <<'PY'
import pathlib, sqlite3
src = pathlib.Path('/data/observatory.db'); dst = pathlib.Path('/data/.pre-deploy.db')
if src.exists():
    dst.unlink(missing_ok=True)
    with sqlite3.connect(src) as a, sqlite3.connect(dst) as b:
        a.backup(b)
PY
then
  echo "WARN: 换库前快照失败；应用自身的 pre-migrate 备份仍在 /data/backups/ 下" >&2
fi
if ! docker exec observatory-api test -s /data/.pre-deploy.db; then
  echo "WARN: /data/.pre-deploy.db 不存在或为空（回滚时请改用 /data/backups/pre-migrate-*.db）" >&2
fi

# 3) 拉镜像 + 切换（香港→ghcr 满速；构建不发生在本机）
export OBS_IMAGE_TAG="ghcr.io/h2o-me/observatory:${SHA}"
compose pull --quiet
compose up -d --no-build

# 4) 健康验证（容器内直连，绕过反代/HostSplit 网络位形；容忍一次性 DB 迁移，最长 6 分钟）
ok=""
for i in $(seq 1 72); do
  st="$(docker inspect --format '{{.State.Status}}' observatory-api 2>/dev/null || echo gone)"
  case "$st" in
    restarting|exited|dead|gone) break ;;
  esac
  if docker exec observatory-api python -c \
    "import ssl,urllib.request as u;ctx=ssl._create_unverified_context();u.urlopen('https://127.0.0.1:8443/api/v1/health',context=ctx,timeout=4).read()" \
    >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 5
done

# 5) 失败 → 切回旧镜像（DB 快照留在 /data/.pre-deploy.db 供 restore）
if [[ -z "$ok" ]]; then
  echo "health check FAILED; rolling back to ${OLD_IMAGE:-<none>}" >&2
  if [[ -n "$OLD_IMAGE" ]]; then
    OBS_IMAGE_TAG="$OLD_IMAGE" compose up -d --no-build
  fi
  exit 1
fi

echo "deployed ${SHA} successfully (image $(docker inspect --format '{{.Config.Image}}' observatory-api))"
