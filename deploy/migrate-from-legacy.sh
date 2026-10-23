#!/usr/bin/env bash
# 在部署服务器上把旧版 shenmi 栈换成 Observatory 0.3.0。
#
# 这是“一次性迁移”脚本，之后就只用 deploy/update.sh。
# 旧栈的容器会被停止，但 /opt/shenmi/data 原样保留（不删除、不迁移），
# 需要回滚时重新 `docker compose -p shenmi -f /opt/shenmi/docker-compose.yml up -d` 即可。
#
# 0.3.0 修订：
#   * 证书实体拷贝并 chown 到容器 UID（非 root 容器需要读私钥）；
#   * server/.env 补齐 compose 层变量（绑定回环、转发头信任、安装令牌 TTL）；
#   * 启动时导出 OBS_IMAGE_TAG=<VERSION>，并维护 :latest；
#   * 健康检查失败退出非 0（旧版只打印一行）；
#   * nginx 站点不在这里安装：迁移后运行一次 sudo bash deploy/setup.sh
#     （它会渲染 0.3.0 站点模板、申请/复用证书并做双域名断言）。

set -Eeuo pipefail
IFS=$'\n\t'

APP="${APP:-/opt/observatory}"
OLD="${OLD:-/opt/shenmi}"
CONSOLE_HOST="${CONSOLE_HOST:-control.tianhw.top}"
AGENT_HOST="${AGENT_HOST:-observatory.123423.xyz}"
OBS_UID="${OBS_UID:-10001}"

log()  { printf '\033[36m[migrate]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[migrate]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[migrate] %s\033[0m\n' "$*" >&2; exit 1; }

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

log "准备证书（实体拷贝，供非 root 容器读取）"
install -d -m 0755 "$APP/certs"
if [[ -f "$OLD/certs/server.crt" && -f "$OLD/certs/server.key" ]]; then
  install -m 0644 "$OLD/certs/server.crt" "$APP/certs/server.crt"
  install -m 0600 "$OLD/certs/server.key" "$APP/certs/server.key"
  chown "$OBS_UID:$OBS_UID" "$APP/certs/server.crt" "$APP/certs/server.key"
else
  warn "旧证书不存在：迁移完成后运行 deploy/setup.sh 重新申请/实体化证书"
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
# 由 deploy/migrate-from-legacy.sh 生成（0.3.0）。请勿提交。
OBS_SECRET_KEY=$SECRET
OBS_ADMIN_PASSWORD=$ADMIN
# 0.2.x 兼容用；0.3.0 新机使用一次性安装令牌。
OBS_ENROLL_CODE=$ENROLL
# 安装令牌 TTL 由控制台设置 install_token_ttl_minutes 管理，不写入 .env。
OBS_DATA_DIR=/data
OBS_CONSOLE_HOST=$CONSOLE_HOST
OBS_AGENT_HOSTS=$AGENT_HOST,$CONSOLE_HOST
OBS_PUBLIC_BASE_URLS=https://$CONSOLE_HOST,https://$AGENT_HOST
OBS_BIND_ADDR=127.0.0.1
OBS_PORT=8443
OBS_TRUST_FORWARDED_FOR=true
OBS_UID=$OBS_UID
OBS_MEM_LIMIT=1g
OBS_CPUS=2.0
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
VERSION="$(tr -d '[:space:]' < VERSION)"
export OBS_IMAGE_TAG="$VERSION"
docker compose -f deploy/docker-compose.yml --env-file server/.env up -d --build \
  || die "容器启动失败，请查看 docker compose logs"

log "等待健康检查（失败即退出非 0）"
ready=0
for _ in $(seq 1 90); do
  if curl -fsSk https://127.0.0.1:8443/api/v1/health >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "$ready" != "1" ]]; then
  docker compose -f deploy/docker-compose.yml --env-file server/.env logs --tail=80 || true
  die "迁移后服务未就绪：检查证书（/certs）、数据卷属主（应为 $OBS_UID）与端口"
fi
docker tag "observatory/api:$VERSION" "observatory/api:latest" >/dev/null 2>&1 \
  || warn "无法追加 latest tag（非致命）"

echo
log "版本：$(curl -fsSk https://127.0.0.1:8443/api/v1/version || echo 未知)"
log "容器："
docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}' | grep -E 'observatory|shenmi' || true
echo
log "剩余步骤："
log "  1. sudo bash deploy/setup.sh   # 渲染 0.3.0 nginx 模板 + 双域名证书断言"
log "  2. cron: 0 4 * * * root bash $APP/deploy/renew-certs.sh"
log "旧数据仍在 $OLD/data（未迁移），回滚命令："
log "  docker compose -p shenmi -f $OLD/docker-compose.yml up -d"