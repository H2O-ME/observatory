#!/usr/bin/env bash
# 一键部署 / 首次安装。
#
#   sudo bash deploy/setup.sh
#
# 做四件事：
#   1. 检查 docker / compose，缺失则安装
#   2. 申请两个域名的证书（如果还没有）
#   3. 准备 server/.env（首次运行会生成随机密钥）
#   4. 构建镜像并启动，安装 nginx 站点
#
# 脚本可以重复运行：已有的证书、.env 与数据卷都不会被覆盖。

set -Eeuo pipefail
IFS=$'\n\t'

APP_DIR="${APP_DIR:-/opt/observatory}"
REPO_URL="${REPO_URL:-}"
CONSOLE_HOST="${CONSOLE_HOST:-control.tianhw.top}"
AGENT_HOST="${AGENT_HOST:-observatory.123423.xyz}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-}"
STACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$STACK_DIR/.." && pwd)"

log()  { printf '\033[36m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[setup]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[setup] %s\033[0m\n' "$*" >&2; exit 1; }

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "请用 root 运行：sudo bash deploy/setup.sh"
  fi
}

install_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    log "docker 已就绪：$(docker --version)"
    return
  fi
  log "安装 docker 与 compose 插件"
  apt-get update -y
  apt-get install -y --no-install-recommends ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "${VERSION_CODENAME}") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
}

install_nginx_certbot() {
  if ! command -v nginx >/dev/null 2>&1; then
    log "安装 nginx 与 certbot"
    apt-get update -y
    apt-get install -y --no-install-recommends nginx certbot python3-certbot-nginx
    systemctl enable --now nginx
  fi
  mkdir -p /var/www/certbot
}

ensure_repo() {
  if [[ -d "$APP_DIR/.git" ]]; then
    log "已有仓库，跳过克隆：$APP_DIR"
    return
  fi
  if [[ -z "$REPO_URL" ]]; then
    if [[ -d "$REPO_ROOT/.git" ]]; then
      log "使用当前目录作为部署源：$REPO_ROOT"
      if [[ "$REPO_ROOT" != "$APP_DIR" ]]; then
        mkdir -p "$(dirname "$APP_DIR")"
        cp -a "$REPO_ROOT" "$APP_DIR"
      fi
      return
    fi
    die "请设置 REPO_URL=git@github.com:you/observatory.git 后重试"
  fi
  log "克隆仓库到 $APP_DIR"
  mkdir -p "$(dirname "$APP_DIR")"
  git clone "$REPO_URL" "$APP_DIR"
}

ensure_certs() {
  local cert_root="/etc/letsencrypt/live"
  if [[ -f "$cert_root/$CONSOLE_HOST/fullchain.pem" && -f "$cert_root/$AGENT_HOST/fullchain.pem" ]]; then
    log "证书已存在，跳过申请"
  else
    if [[ -z "$CERTBOT_EMAIL" ]]; then
      die "首次部署需要 CERTBOT_EMAIL=you@example.com 用于申请证书"
    fi
    log "申请证书：$CONSOLE_HOST, $AGENT_HOST"
    systemctl stop nginx || true
    certbot certonly --standalone --non-interactive --agree-tos \
      -m "$CERTBOT_EMAIL" -d "$CONSOLE_HOST" -d "$AGENT_HOST"
    systemctl start nginx
  fi
  mkdir -p "$APP_DIR/certs"
  ln -sfn "$cert_root/$CONSOLE_HOST/fullchain.pem" "$APP_DIR/certs/server.crt"
  ln -sfn "$cert_root/$CONSOLE_HOST/privkey.pem" "$APP_DIR/certs/server.key"
  chmod 755 "$APP_DIR/certs"
}

ensure_env() {
  local env_file="$APP_DIR/server/.env"
  if [[ -f "$env_file" ]]; then
    log "server/.env 已存在，保留现有配置"
    return
  fi
  log "生成 server/.env（随机密钥）"
  mkdir -p "$APP_DIR/server"
  local secret enroll
  secret="$(head -c 48 /dev/urandom | base64 | tr -d '\n=+/' | cut -c1-64)"
  enroll="$(head -c 18 /dev/urandom | base64 | tr -d '\n=+/' | cut -c1-24)"
  cat > "$env_file" <<EOF
# 由 deploy/setup.sh 生成。请勿提交到仓库。
OBS_SECRET_KEY=$secret
OBS_ADMIN_PASSWORD=$(head -c 12 /dev/urandom | base64 | tr -d '\n=+/' | cut -c1-16)
OBS_ENROLL_CODE=$enroll
OBS_DATA_DIR=/data
OBS_CONSOLE_HOST=$CONSOLE_HOST
OBS_AGENT_HOSTS=$AGENT_HOST,$CONSOLE_HOST
OBS_PUBLIC_BASE_URLS=https://$CONSOLE_HOST,https://$AGENT_HOST
EOF
  chmod 600 "$env_file"
  warn "已生成随机管理员密码，记录在这里： $env_file"
  grep -E 'OBS_ADMIN_PASSWORD|OBS_ENROLL_CODE' "$env_file" || true
}

install_nginx_sites() {
  log "安装 nginx 站点配置"
  install -m 644 "$APP_DIR/deploy/nginx/$CONSOLE_HOST.conf" "/etc/nginx/sites-available/$CONSOLE_HOST.conf"
  install -m 644 "$APP_DIR/deploy/nginx/$AGENT_HOST.conf" "/etc/nginx/sites-available/$AGENT_HOST.conf"
  ln -sfn "/etc/nginx/sites-available/$CONSOLE_HOST.conf" "/etc/nginx/sites-enabled/$CONSOLE_HOST.conf"
  ln -sfn "/etc/nginx/sites-available/$AGENT_HOST.conf" "/etc/nginx/sites-enabled/$AGENT_HOST.conf"
  rm -f /etc/nginx/sites-enabled/default
  nginx -t
  systemctl reload nginx
}

start_stack() {
  log "构建并启动容器"
  cd "$APP_DIR"
  docker compose --env-file server/.env up -d --build
  log "等待健康检查"
  for _ in $(seq 1 60); do
    if curl -fsSk "https://127.0.0.1:8443/api/v1/health" >/dev/null 2>&1; then
      log "服务已就绪"
      break
    fi
    sleep 2
  done
  local version
  version="$(curl -fsSk https://127.0.0.1:8443/api/v1/version | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')"
  log "当前版本：${version:-未知}"
}

main() {
  require_root
  install_docker
  install_nginx_certbot
  ensure_repo
  ensure_env
  ensure_certs
  install_nginx_sites
  start_stack
  cat <<EOF

部署完成。

控制台    : https://$CONSOLE_HOST
受控端接入: https://$CONSOLE_HOST  与  https://$AGENT_HOST （互为备份）
安装命令  : 在控制台「设置」页复制
版本      : $(cat "$APP_DIR/VERSION")

下一步：
  1. 上传受控端安装包（控制台 -> 更新 -> 上传），或运行 deploy/build-agent.sh
  2. 在目标机器运行安装命令（只会弹一次 UAC）
EOF
}

main "$@"