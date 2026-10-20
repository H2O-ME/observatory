#!/usr/bin/env bash
# 在部署服务器上把旧版 shenmi 栈换成 Observatory 0.2.0。
#
# 这是"一次性迁移"脚本，之后就只用 deploy/update.sh。
# 旧栈的容器会被停止，但 /opt/shenmi/data 原样保留（不删除、不迁移），
# 需要回滚时重新 `docker compose -p shenmi -f /opt/shenmi/docker-compose.yml up -d` 即可。

set -Eeuo pipefail
IFS=$'\n\t'

APP="${APP:-/opt/observatory}"
OLD="${OLD:-/opt/shenmi}"
CONSOLE_HOST="${CONSOLE_HOST:-control.tianhw.top}"
AGENT_HOST="${AGENT_HOST:-observatory.123423.xyz}"

log()  { printf '\033[36m[migrate]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[migrate]\033[0m %s\n' "$*" >&2; }

log "配置 GitHub 部署密钥"
mkdir -p /root/.ssh && chmod 700 /root/.ssh
if ! grep -q '^Host github-observatory$' /root/.ssh/config 2>/dev/null; then
  cat >> /root/.ssh/config <<'EOF'

Host github-observatory
    HostName github.com
    User git
    IdentityFile /root/.ssh/id_ed25519_observatory
    IdentitiesOnly yes
EOF
  chmod 600 /root/.ssh/config
fi

log "获取代码（$APP）"
if [[ -d "$APP/.git" ]]; then
  git -C "$APP" fetch --prune origin main
  git -C "$APP" reset --hard origin/main
else
  git clone ssh://github-observatory/H2O-ME/observatory.git "$APP"
fi

log "准备证书"
mkdir -p "$APP/certs"
if [[ -f "$OLD/certs/server.crt" && -f "$OLD/certs/server.key" ]]; then
  cp -f "$OLD/certs/server.crt" "$OLD/certs/server.key" "$APP/certs/"
  chmod 600 "$APP/certs/server.key"
else
  warn "旧证书不存在，容器可能无法启动 TLS"
fi

log "准备 server/.env"
if [[ ! -f "$APP/server/.env" ]]; then
  get() { grep -E "^$1=" "$OLD/.env" 2>/dev/null | head -1 | cut -d= -f2- || true; }
  SECRET="$(get SHENMI_SECRET_KEY)"
  ADMIN="$(get SHENMI_ADMIN_PASSWORD)"
  [[ -n "$SECRET" ]] || SECRET="$(head -c 48 /dev/urandom | base64 | tr -d '\n=+/' | cut -c1-64)"
  [[ -n "$ADMIN" ]] || ADMIN="$(head -c 12 /dev/urandom | base64 | tr -d '\n=+/' | cut -c1-16)"
  ENROLL="$(head -c 18 /dev/urandom | base64 | tr -d '\n=+/' | cut -c1-24)"
  cat > "$APP/server/.env" <<EOF
# 由 deploy/migrate-from-legacy.sh 生成。请勿提交。
OBS_SECRET_KEY=$SECRET
OBS_ADMIN_PASSWORD=$ADMIN
OBS_ENROLL_CODE=$ENROLL
OBS_DATA_DIR=/data
OBS_CONSOLE_HOST=$CONSOLE_HOST
OBS_AGENT_HOSTS=$AGENT_HOST,$CONSOLE_HOST
OBS_PUBLIC_BASE_URLS=https://$CONSOLE_HOST,https://$AGENT_HOST
TZ=Asia/Shanghai
EOF
  chmod 600 "$APP/server/.env"
  log "管理员密码与注册码："
  grep -E 'OBS_ADMIN_PASSWORD|OBS_ENROLL_CODE' "$APP/server/.env"
fi

log "停止旧版容器（保留 $OLD/data 数据）"
if docker ps --format '{{.Names}}' | grep -qx shenmi-api; then
  docker compose -p shenmi -f "$OLD/docker-compose.yml" down || warn "旧栈停止失败，继续"
else
  log "旧版容器未运行"
fi

log "构建并启动 Observatory"
cd "$APP"
docker compose -f deploy/docker-compose.yml --env-file server/.env up -d --build

log "等待健康检查"
for _ in $(seq 1 90); do
  if curl -fsSk https://127.0.0.1:8443/api/v1/health >/dev/null 2>&1; then
    log "服务已就绪"
    break
  fi
  sleep 2
done

echo
log "版本：$(curl -fsSk https://127.0.0.1:8443/api/v1/version || echo 未知)"
log "容器："
docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}' | grep -E 'observatory|shenmi' || true
echo
log "旧数据仍在 $OLD/data（未迁移），回滚命令："
log "  docker compose -p shenmi -f $OLD/docker-compose.yml up -d"